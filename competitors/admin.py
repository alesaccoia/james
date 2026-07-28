from django.contrib import admin

from .models import Competitor, MonthlyTraffic, TrafficUpload


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
