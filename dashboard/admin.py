from django.contrib import admin

from .models import (AirbyteRecord, FunnelKPI, FunnelKPIValue, FunnelStage,
                     FunnelStageSource, ImportLog, MarketingEvent)


@admin.register(ImportLog)
class ImportLogAdmin(admin.ModelAdmin):
    list_display = ('started_at', 'finished_at', 'ok', 'records_new', 'records_seen')


@admin.register(AirbyteRecord)
class AirbyteRecordAdmin(admin.ModelAdmin):
    list_display = ('stream', 'ab_id', 'emitted_at', 'imported_at')
    list_filter = ('stream',)
    search_fields = ('ab_id',)


@admin.register(MarketingEvent)
class MarketingEventAdmin(admin.ModelAdmin):
    list_display = ('date', 'name', 'scope')
    list_filter = ('scope',)
    search_fields = ('name', 'scope', 'notes')
    date_hierarchy = 'date'


# ------------------------------------------------------------------- funnel

class FunnelKPIInline(admin.TabularInline):
    model = FunnelKPI
    extra = 0
    fields = ('name', 'unit', 'target_value', 'order', 'is_active')


class FunnelStageSourceInline(admin.TabularInline):
    model = FunnelStageSource
    extra = 0
    fields = ('kind', 'name', 'notes')


@admin.register(FunnelStage)
class FunnelStageAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'slug', 'is_active')
    list_editable = ('order',)
    prepopulated_fields = {'slug': ('name',)}
    inlines = [FunnelKPIInline, FunnelStageSourceInline]


class FunnelKPIValueInline(admin.TabularInline):
    model = FunnelKPIValue
    extra = 3
    fields = ('date', 'value', 'note')


@admin.register(FunnelKPI)
class FunnelKPIAdmin(admin.ModelAdmin):
    list_display = ('stage', 'name', 'unit', 'target_value', 'is_active')
    list_filter = ('stage', 'unit')
    inlines = [FunnelKPIValueInline]


@admin.register(FunnelKPIValue)
class FunnelKPIValueAdmin(admin.ModelAdmin):
    list_display = ('kpi', 'date', 'value')
    list_filter = ('kpi__stage',)
    date_hierarchy = 'date'


@admin.register(FunnelStageSource)
class FunnelStageSourceAdmin(admin.ModelAdmin):
    list_display = ('stage', 'kind', 'name')
    list_filter = ('stage', 'kind')
