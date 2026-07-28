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
    ab_id = models.CharField(max_length=300)
    emitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    data = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('stream', 'ab_id')]
        ordering = ['-emitted_at']

    def __str__(self):
        return f'{self.stream} · {self.ab_id[:12]}'


class FunnelStage(models.Model):
    """One step of the marketing/sales funnel, in order. Fully customizable —
    not tied to any single business's specific steps. Seeded with a sensible
    default (Lead -> Contatto -> Booking -> Cliente -> Rinnovo) but stages can
    be renamed, reordered, added or removed freely."""
    name = models.CharField(max_length=150)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.name


class FunnelKPI(models.Model):
    """A metric tracked for one funnel stage, e.g. 'Lead ricevuti', 'CAC per
    cliente pagante'. Defines what's tracked; the values themselves are a
    time series (FunnelKPIValue)."""
    UNIT_CHOICES = [
        ('count', 'Numero'),
        ('eur', 'Euro'),
        ('percent', 'Percentuale'),
        ('ratio', 'Rapporto'),
    ]
    stage = models.ForeignKey(FunnelStage, on_delete=models.CASCADE, related_name='kpis')
    name = models.CharField(max_length=150)
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='count')
    target_value = models.FloatField(
        null=True, blank=True,
        help_text='Obiettivo opzionale, mostrato come riferimento sul grafico.')
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['stage__order', 'order', 'id']

    def __str__(self):
        return f'{self.stage.name} · {self.name}'


class FunnelKPIValue(models.Model):
    """One time-series data point for a FunnelKPI.

    Entered manually for now: v1 deliberately does not compute these
    automatically from campaign/ad set data (see FunnelStageSource) — the
    goal here is just to make the evolution over time visible. Automatic
    calculation can be layered on later without changing this shape.
    """
    kpi = models.ForeignKey(FunnelKPI, on_delete=models.CASCADE, related_name='values')
    date = models.DateField(db_index=True)
    value = models.FloatField()
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = [('kpi', 'date')]

    def __str__(self):
        return f'{self.kpi} · {self.date} = {self.value}'


class FunnelStageSource(models.Model):
    """Associates a funnel stage with something that feeds it — a Meta
    campaign, an ad set, a GA4 channel, named freely with their real names.
    Not wired to automatic calculation yet (see FunnelKPIValue) — this just
    records the mapping so it's there when that gets built."""
    KIND_CHOICES = [
        ('campaign', 'Campagna'),
        ('ad_set', 'Ad set'),
        ('channel', 'Canale'),
        ('other', 'Altro'),
    ]
    stage = models.ForeignKey(FunnelStage, on_delete=models.CASCADE, related_name='sources')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='campaign')
    name = models.CharField(max_length=200)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['stage__order', 'kind', 'name']

    def __str__(self):
        return f'{self.stage.name} · {self.get_kind_display()}: {self.name}'


class MarketingEvent(models.Model):
    """A marked moment worth annotating on charts — a campaign tweak, a
    landing page change, a budget shift, anything experimental. Shown as a
    vertical line (hidden by default) on time-series charts across the app.
    """
    name = models.CharField(max_length=200)
    date = models.DateField(db_index=True)
    scope = models.CharField(
        max_length=200, blank=True,
        help_text='Free text reference only — e.g. "meta", "google, sito", "landing". Shown on every chart, does not filter.')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f'{self.date} — {self.name}'
