"""Import Airbyte's typed landing tables into James's own AirbyteRecord table.

Airbyte's modern Postgres destination (Destinations V2) writes one proper
typed table per stream — real columns, not a JSON blob — but keeps four
`_airbyte_*` metadata columns alongside the business columns. We reconstruct
a data dict from the non-meta columns of each row and mirror it here via the
'airbyte' Django DB alias, deduped by a per-stream natural key so re-syncing
an overlapping date range never piles up duplicates. For a handful of
streams (see REFRESHABLE_STREAMS below) the existing row is updated in
place instead of skipped, since the same natural key can carry different
data over time (e.g. a post's lifetime reach keeps growing).

Run manually, e.g.:
  .venv/bin/python manage.py import_airbyte
"""
import json
from datetime import datetime, date, timezone as dt_timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connections
from django.utils import timezone

from dashboard.models import AirbyteRecord, ImportLog

# Per-stream natural key, used instead of Airbyte's raw per-row id when
# available. Airbyte assigns a fresh raw id to every row on every sync, so
# streams re-synced with an overlapping window would otherwise pile up
# duplicate rows for the same business record. Streams not listed here fall
# back to Airbyte's raw id (still deduped, just less precisely).
NATURAL_KEYS = {
    'fb_ads_insights': lambda d: f"{d.get('ad_id')}|{d.get('date_start')}",
    'fb_campaigns': lambda d: d.get('id'),
    'fb_ad_sets': lambda d: d.get('id'),
    'fb_ads': lambda d: d.get('id'),
    'fb_ad_creatives': lambda d: d.get('id'),
    'fb_ad_account': lambda d: d.get('id') or d.get('account_id'),
    'fb_custom_conversions': lambda d: d.get('id'),
    'ga_website_overview': lambda d: d.get('date'),
    'ga_conversions_report': lambda d: f"{d.get('date')}|{d.get('eventName')}",
    'ga_pages_path_report': lambda d: f"{d.get('date')}|{d.get('pagePath')}",
    'ga_traffic_acquisition_session_campaign_report': lambda d: f"{d.get('date')}|{d.get('sessionCampaignName')}",
    'ga_traffic_acquisition_session_default_channel_grouping_report':
        lambda d: f"{d.get('date')}|{d.get('sessionDefaultChannelGrouping')}",
    'ga_user_acquisition_first_user_source_medium_report':
        lambda d: f"{d.get('date')}|{d.get('firstUserSource')}|{d.get('firstUserMedium')}",
    'ga_demographic_age_report': lambda d: f"{d.get('date')}|{d.get('userAgeBracket')}",
    'ga_demographic_gender_report': lambda d: f"{d.get('date')}|{d.get('userGender')}",
    'ga_demographic_country_report': lambda d: f"{d.get('date')}|{d.get('country')}",
    'ga_demographic_city_report': lambda d: f"{d.get('date')}|{d.get('city')}",
    'mentor_leads': lambda d: d.get('id'),
    'mentor_lead_status_events': lambda d: d.get('id'),
    'mentor_meta_pages_page': lambda d: d.get('id'),
    'mentor_meta_pages_post': lambda d: d.get('id'),
    'mentor_meta_pages_post_insights': lambda d: d.get('id'),
    'mentor_meta_pages_page_insights': lambda d: d.get('id'),
    'mentor_ig_page_ig_media': lambda d: d.get('id'),
    'mentor_ig_page_ig_media_insights': lambda d: d.get('id'),
}

# Streams whose natural key stays the same forever but whose *data* keeps
# changing after the row is first seen - Meta insights are lifetime running
# totals (reach, likes...) that grow for weeks after a post goes up, page
# profile fields (fan_count...) drift too, and fb_ads_insights rows for the
# last 1-2 days are NOT a closed bucket yet - Meta keeps attributing spend
# and conversions to "today"/"yesterday" for up to ~72h, so a row synced
# early in the day (or the day after) undercounts until it's resynced later.
# Confirmed live (30/07/2026): today's fb_ads_insights leads were frozen at
# 2 in James while the real, freshly-synced number was much higher - the
# first sync of the day had captured it before most of the day's spend/leads
# had posted, and every later sync that day was silently skipped as a
# duplicate. For these streams, re-syncing overwrites the stored data instead
# of being skipped, so numbers stay current. Everything else (entity lists
# like fb_campaigns, fb_ad_sets...) stays insert-once-skip-after, which is
# cheaper and correct once a period is genuinely closed.
#
# GA4 has the same problem, confirmed live (30/07/2026): its reporting API
# doesn't finalize a day's numbers immediately either - today showed 9
# sessions in James vs. 88 freshly synced, and even 2 days back was still
# stale (7 vs. 70). All GA4 report streams are keyed by date (+ a dimension),
# so they get the same treatment.
REFRESHABLE_STREAMS = {
    'mentor_meta_pages_page',
    'mentor_meta_pages_post',
    'mentor_meta_pages_post_insights',
    'mentor_meta_pages_page_insights',
    'mentor_ig_page_ig_media',
    'mentor_ig_page_ig_media_insights',
    'fb_ads_insights',
    'ga_website_overview',
    'ga_conversions_report',
    'ga_pages_path_report',
    'ga_traffic_acquisition_session_campaign_report',
    'ga_traffic_acquisition_session_default_channel_grouping_report',
    'ga_user_acquisition_first_user_source_medium_report',
    'ga_demographic_age_report',
    'ga_demographic_gender_report',
    'ga_demographic_country_report',
    'ga_demographic_city_report',
}

META_COLS = {'_airbyte_raw_id', '_airbyte_extracted_at', '_airbyte_meta', '_airbyte_generation_id'}


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str) and v[:1] in '{[':
        # jsonb columns (e.g. ads_insights.actions) come back from the raw
        # cursor as their JSON text representation, not a parsed object.
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return v
    return v


class Command(BaseCommand):
    help = "Import Airbyte's typed Postgres landing tables into AirbyteRecord."

    def handle(self, *args, **options):
        log = ImportLog.objects.create()
        try:
            with connections['airbyte'].cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT table_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND column_name = '_airbyte_raw_id'
                """)
                tables = [r[0] for r in cur.fetchall()]

                new_total, updated_total, seen_total = 0, 0, 0
                for stream in tables:
                    cur.execute(f'SELECT * FROM "{stream}"')
                    columns = [d[0] for d in cur.description]
                    data_cols = [c for c in columns if c not in META_COLS]
                    id_idx = columns.index('_airbyte_raw_id')
                    ts_idx = columns.index('_airbyte_extracted_at') if '_airbyte_extracted_at' in columns else None

                    refreshable = stream in REFRESHABLE_STREAMS
                    if refreshable:
                        existing_records = {r.ab_id: r for r in AirbyteRecord.objects.filter(stream=stream)}
                        existing = set(existing_records)
                    else:
                        existing_records = {}
                        existing = set(AirbyteRecord.objects.filter(stream=stream)
                                       .values_list('ab_id', flat=True))
                    key_fn = NATURAL_KEYS.get(stream)

                    batch, to_update, seen = [], [], 0
                    for row in cur.fetchall():
                        seen += 1
                        data = {c: _jsonable(row[columns.index(c)]) for c in data_cols}
                        natural = key_fn(data) if key_fn else None
                        ab_id = (str(natural) if natural else str(row[id_idx]))[:300]
                        ts = row[ts_idx] if ts_idx is not None else None
                        if ts and ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt_timezone.utc)
                        if ab_id in existing:
                            if refreshable:
                                rec = existing_records[ab_id]
                                if rec.data != data:
                                    rec.data = data
                                    rec.emitted_at = ts
                                    to_update.append(rec)
                            continue
                        batch.append(AirbyteRecord(stream=stream, ab_id=ab_id, emitted_at=ts, data=data))
                        existing.add(ab_id)

                    AirbyteRecord.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
                    if to_update:
                        AirbyteRecord.objects.bulk_update(to_update, ['data', 'emitted_at'], batch_size=500)
                    new_total += len(batch)
                    updated_total += len(to_update)
                    seen_total += seen
                    suffix = f', {len(to_update)} updated' if refreshable else ''
                    self.stdout.write(f'  {stream}: {seen} rows, {len(batch)} new{suffix}')

            log.tables_seen = tables
            log.records_new = new_total
            log.records_seen = seen_total
            self.stdout.write(self.style.SUCCESS(
                f'Import ok: {len(tables)} stream(s), {new_total} new record(s), {updated_total} updated.'))
        except Exception as exc:
            log.ok = False
            log.error = str(exc)[:2000]
            self.stdout.write(self.style.ERROR(f'Import failed: {exc}'))
            raise
        finally:
            log.finished_at = timezone.now()
            log.save()
