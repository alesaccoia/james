from django.contrib import admin

from .models import (Competitor, MetaAdState, MetricPoint, MonthlyTraffic,
                     SovConfig, SovRun, TrafficUpload)


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ('name', 'domain', 'is_self', 'is_active')
    list_filter = ('is_self', 'is_active')


@admin.register(MonthlyTraffic)
class MonthlyTrafficAdmin(admin.ModelAdmin):
    list_display = ('month', 'domain', 'competitor', 'visits')
    list_filter = ('month',)
    search_fields = ('domain',)


@admin.register(TrafficUpload)
class TrafficUploadAdmin(admin.ModelAdmin):
    list_display = ('filename', 'uploaded_by', 'uploaded_at', 'domains', 'datapoints')


@admin.register(SovConfig)
class SovConfigAdmin(admin.ModelAdmin):
    list_display = ('geo', 'meta_reach_since', 'last_meta_run', 'meta_reach_period_days')


@admin.register(SovRun)
class SovRunAdmin(admin.ModelAdmin):
    list_display = ('run_date', 'trigger', 'status', 'started_at', 'finished_at')
    list_filter = ('trigger', 'status')


@admin.register(MetricPoint)
class MetricPointAdmin(admin.ModelAdmin):
    list_display = ('run_date', 'competitor', 'metric', 'value')
    list_filter = ('metric', 'competitor')
    date_hierarchy = 'run_date'


@admin.register(MetaAdState)
class MetaAdStateAdmin(admin.ModelAdmin):
    list_display = ('ad_archive_id', 'competitor', 'active', 'last_seen', 'reach')
    list_filter = ('competitor', 'active')
