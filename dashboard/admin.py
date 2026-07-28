from django.contrib import admin

from .models import AirbyteRecord, ImportLog, MarketingEvent


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
