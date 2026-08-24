import os

from django.db import models


class SovConfig(models.Model):
    geo = models.CharField(max_length=8, default='IT')
    meta_reach_since = models.DateField(null=True, blank=True)
    meta_reach_period_days = models.PositiveIntegerField(default=14)
    serp_keywords = models.JSONField(default=list, blank=True)
    meta_access_token = models.CharField(max_length=600, blank=True)
    serpapi_key = models.CharField(max_length=200, blank=True)
    last_meta_run = models.DateField(null=True, blank=True)
    backfill_done_since = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'SOV configuration'
        verbose_name_plural = 'SOV configuration'

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()

    def meta_token(self):
        return self.meta_access_token.strip() or os.environ.get('META_ACCESS_TOKEN', '')

    def serp_key(self):
        return self.serpapi_key.strip() or os.environ.get('SERPAPI_KEY', '')


class Competitor(models.Model):
    """One tracked brand (including our own, flagged with is_self)."""
    name = models.CharField(max_length=120, unique=True)
    domain = models.CharField(max_length=200, blank=True)
    trustpilot_slug = models.CharField(max_length=200, blank=True)
    meta_page_id = models.BigIntegerField(null=True, blank=True)
    meta_search_terms = models.CharField(max_length=200, blank=True)
    is_self = models.BooleanField(default=False, help_text='This is our own brand.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-is_self', 'name']

    def __str__(self):
        return self.name


class SovRun(models.Model):
    STATUS_CHOICES = [('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')]
    TRIGGER_CHOICES = [('manual', 'Manual'), ('api', 'API'), ('cron', 'Cron'), ('import', 'Import')]
    run_date = models.DateField(db_index=True)
    trigger = models.CharField(max_length=10, choices=TRIGGER_CHOICES, default='manual')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='running')
    skip = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    log = models.TextField(blank=True)
    legacy_wundt_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ['-started_at']


class MetricPoint(models.Model):
    run = models.ForeignKey(SovRun, null=True, blank=True, on_delete=models.CASCADE, related_name='points')
    run_date = models.DateField(db_index=True)
    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name='metric_points')
    metric = models.CharField(max_length=60, db_index=True)
    value = models.FloatField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['run_date', 'competitor_id', 'metric']
        constraints = [models.UniqueConstraint(
            fields=['run_date', 'competitor', 'metric', 'note'], name='competitor_metric_point_unique')]


class MetaAdState(models.Model):
    ad_archive_id = models.CharField(max_length=40, unique=True)
    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name='meta_ads')
    first_seen = models.DateField()
    last_seen = models.DateField()
    active = models.BooleanField(default=False)
    start_time = models.CharField(max_length=40, blank=True)
    stop_time = models.CharField(max_length=40, blank=True)
    reach = models.BigIntegerField(default=0)
    spend_mid = models.FloatField(null=True, blank=True)
    impressions_mid = models.FloatField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)


class MonthlyTraffic(models.Model):
    """Monthly visits per domain (from SameAPI exports). Re-uploads overwrite:
    the latest upload wins."""
    month = models.DateField(help_text='First day of the month.')
    domain = models.CharField(max_length=200)
    competitor = models.ForeignKey(Competitor, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='monthly_traffic')
    visits = models.BigIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('month', 'domain')]
        ordering = ['month', '-visits']

    def __str__(self):
        return f'{self.month:%Y-%m} {self.domain}: {self.visits}'


class TrafficUpload(models.Model):
    """Audit log of SameAPI export uploads."""
    filename = models.CharField(max_length=300)
    uploaded_by = models.CharField(max_length=150, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    months = models.JSONField(default=list, blank=True)
    domains = models.PositiveIntegerField(default=0)
    datapoints = models.PositiveIntegerField(default=0)
    legacy_wundt_id = models.PositiveIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.filename} @ {self.uploaded_at:%Y-%m-%d %H:%M}'
