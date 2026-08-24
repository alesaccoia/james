import json
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from competitors.models import (Competitor, MetaAdState, MetricPoint,
                                MonthlyTraffic, SovConfig, SovRun, TrafficUpload)


class Command(BaseCommand):
    help = 'Idempotently migrate the legacy WUNDT SOV tables into JAMES.'

    def add_arguments(self, parser):
        parser.add_argument('sqlite_path')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        def aware(value):
            parsed = parse_datetime(value) if value else None
            return timezone.make_aware(parsed) if parsed and timezone.is_naive(parsed) else parsed
        path = Path(options['sqlite_path']).expanduser().resolve()
        if not path.is_file():
            raise CommandError(f'WUNDT SQLite database not found: {path}')
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        counts = {}
        try:
            with transaction.atomic():
                config = connection.execute('select * from sov_sovconfig order by id limit 1').fetchone()
                if config:
                    SovConfig.objects.update_or_create(pk=1, defaults={
                        key: config[key] for key in ('geo', 'meta_reach_since', 'meta_reach_period_days',
                                                     'meta_access_token', 'serpapi_key', 'last_meta_run',
                                                     'backfill_done_since')
                        } | {'serp_keywords': json.loads(config['serp_keywords'] or '[]')})
                comp_map = {}
                for row in connection.execute('select * from sov_competitor'):
                    comp, _ = Competitor.objects.update_or_create(name=row['name'], defaults={
                        key: row[key] for key in ('domain', 'trustpilot_slug', 'meta_page_id',
                                                  'meta_search_terms', 'is_self', 'is_active')})
                    comp_map[row['id']] = comp
                counts['competitors'] = len(comp_map)
                run_map = {}
                for row in connection.execute('select * from sov_sovrun'):
                    run, _ = SovRun.objects.update_or_create(
                        legacy_wundt_id=row['id'], defaults={
                            'run_date': row['run_date'], 'trigger': row['trigger'],
                            'status': row['status'], 'skip': row['skip'],
                            'finished_at': aware(row['finished_at']), 'log': row['log']})
                    SovRun.objects.filter(pk=run.pk).update(started_at=aware(row['started_at']))
                    run_map[row['id']] = run
                counts['runs'] = len(run_map)
                for row in connection.execute('select * from sov_metricpoint'):
                    MetricPoint.objects.update_or_create(
                        run_date=row['run_date'], competitor=comp_map[row['competitor_id']],
                        metric=row['metric'], note=row['note'],
                        defaults={'run': run_map.get(row['run_id']), 'value': row['value']})
                counts['metric_points'] = MetricPoint.objects.count()
                for row in connection.execute('select * from sov_metaadstate'):
                    MetaAdState.objects.update_or_create(ad_archive_id=row['ad_archive_id'], defaults={
                        'competitor': comp_map[row['competitor_id']],
                        **{key: row[key] for key in ('first_seen', 'last_seen', 'active', 'start_time',
                                                     'stop_time', 'reach', 'spend_mid',
                                                     'impressions_mid', 'currency')}})
                counts['meta_ads'] = MetaAdState.objects.count()
                for row in connection.execute('select * from sov_monthlytraffic'):
                    MonthlyTraffic.objects.update_or_create(month=row['month'], domain=row['domain'], defaults={
                        'competitor': comp_map.get(row['competitor_id']), 'visits': row['visits']})
                counts['traffic'] = MonthlyTraffic.objects.count()
                for row in connection.execute('select * from sov_trafficupload'):
                    upload, _ = TrafficUpload.objects.update_or_create(
                        legacy_wundt_id=row['id'], defaults={
                            'filename': row['filename'], 'uploaded_by': row['uploaded_by'],
                            'months': json.loads(row['months'] or '[]'),
                            'domains': row['domains'], 'datapoints': row['datapoints']})
                    TrafficUpload.objects.filter(pk=upload.pk).update(uploaded_at=aware(row['uploaded_at']))
                counts['uploads'] = TrafficUpload.objects.count()
                if not options['apply']:
                    transaction.set_rollback(True)
        finally:
            connection.close()
        mode = 'applicata' if options['apply'] else 'DRY-RUN'
        self.stdout.write(f'Migrazione SOV {mode}: ' + ', '.join(f'{k}={v}' for k, v in counts.items()))
