"""Import Airbyte's typed landing tables into James's own AirbyteRecord table.

Airbyte's modern Postgres destination (Destinations V2) writes one proper
typed table per stream — real columns, not a JSON blob — but keeps four
`_airbyte_*` metadata columns alongside the business columns. We reconstruct
a data dict from the non-meta columns of each row and mirror it here via the
'airbyte' Django DB alias, deduped by a per-stream natural key so re-syncing
an overlapping date range never piles up duplicates.

Run manually, e.g.:
  .venv/bin/python manage.py import_airbyte
"""
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
}

META_COLS = {'_airbyte_raw_id', '_airbyte_extracted_at', '_airbyte_meta', '_airbyte_generation_id'}


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
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

                new_total, seen_total = 0, 0
                for stream in tables:
                    cur.execute(f'SELECT * FROM "{stream}"')
                    columns = [d[0] for d in cur.description]
                    data_cols = [c for c in columns if c not in META_COLS]
                    id_idx = columns.index('_airbyte_raw_id')
                    ts_idx = columns.index('_airbyte_extracted_at') if '_airbyte_extracted_at' in columns else None

                    existing = set(AirbyteRecord.objects.filter(stream=stream)
                                   .values_list('ab_id', flat=True))
                    key_fn = NATURAL_KEYS.get(stream)

                    batch, seen = [], 0
                    for row in cur.fetchall():
                        seen += 1
                        data = {c: _jsonable(row[columns.index(c)]) for c in data_cols}
                        natural = key_fn(data) if key_fn else None
                        ab_id = str(natural) if natural else str(row[id_idx])
                        if ab_id in existing:
                            continue
                        ts = row[ts_idx] if ts_idx is not None else None
                        if ts and ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt_timezone.utc)
                        batch.append(AirbyteRecord(stream=stream, ab_id=ab_id, emitted_at=ts, data=data))
                        existing.add(ab_id)

                    AirbyteRecord.objects.bulk_create(batch, batch_size=500, ignore_conflicts=True)
                    new_total += len(batch)
                    seen_total += seen
                    self.stdout.write(f'  {stream}: {seen} rows, {len(batch)} new')

            log.tables_seen = tables
            log.records_new = new_total
            log.records_seen = seen_total
            self.stdout.write(self.style.SUCCESS(
                f'Import ok: {len(tables)} stream(s), {new_total} new record(s).'))
        except Exception as exc:
            log.ok = False
            log.error = str(exc)[:2000]
            self.stdout.write(self.style.ERROR(f'Import failed: {exc}'))
            raise
        finally:
            log.finished_at = timezone.now()
            log.save()
