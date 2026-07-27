from django.db import models


class ImportLog(models.Model):
    """One execution of the import_airbyte command (audit log)."""
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    tables_seen = models.JSONField(default=list, blank=True)
    records_new = models.PositiveIntegerField(default=0)
    records_seen = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'Import {self.started_at:%Y-%m-%d %H:%M} (+{self.records_new})'


class AirbyteRecord(models.Model):
    """One raw record synced by Airbyte, mirrored 1:1 from the raw landing
    zone database (the 'airbyte' DB alias) so the rest of the app can query
    marketing data with the ORM without touching Airbyte's own tables.

    Keyed by a per-stream natural key when known (see
    dashboard.management.commands.import_airbyte.NATURAL_KEYS), falling back
    to Airbyte's own raw row id otherwise. Airbyte assigns a fresh raw id to
    every row on every sync, so a natural key is what keeps re-synced ranges
    from piling up as duplicates.
    """
    stream = models.CharField(max_length=200, db_index=True,
                              help_text='Prefixed stream name, e.g. "fb_ads_insights".')
    ab_id = models.CharField(max_length=64)
    emitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    data = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('stream', 'ab_id')]
        ordering = ['-emitted_at']

    def __str__(self):
        return f'{self.stream} · {self.ab_id[:12]}'
