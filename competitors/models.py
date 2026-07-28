from django.db import models


class Competitor(models.Model):
    """One tracked brand (including our own, flagged with is_self)."""
    name = models.CharField(max_length=120, unique=True)
    domain = models.CharField(max_length=200, blank=True)
    is_self = models.BooleanField(default=False, help_text='This is our own brand.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-is_self', 'name']

    def __str__(self):
        return self.name


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

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.filename} @ {self.uploaded_at:%Y-%m-%d %H:%M}'
