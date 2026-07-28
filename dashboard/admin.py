from django import forms
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


# Maps a FunnelStageSource "kind" to the raw Airbyte stream that lists that
# kind of Meta entity (id + name), used to populate the admin's pick-lists.
META_ENTITY_STREAMS = {'campaign': 'fb_campaigns', 'ad_set': 'fb_ad_sets', 'ad': 'fb_ads'}


def _meta_entity_choices(stream):
    """[(id, name)], deduped and sorted by name, from a raw Meta entity stream."""
    seen = {}
    for data in AirbyteRecord.objects.filter(stream=stream).values_list('data', flat=True):
        eid, name = data.get('id'), data.get('name') or '(senza nome)'
        if eid:
            seen[str(eid)] = name
    return sorted(seen.items(), key=lambda kv: kv[1].lower())


class FunnelStageSourceForm(forms.ModelForm):
    """Lets you pick a real Meta campaign/ad set/ad from a dropdown (sourced
    live from the synced fb_campaigns/fb_ad_sets/fb_ads data) instead of
    typing a name — kind/name/external_id get filled in automatically so
    stats can be matched exactly by id. The plain fields stay editable too,
    for non-Meta sources (kind="Altro") or entities no longer in the feed."""
    pick_campaign = forms.ChoiceField(label='Scegli una campagna Meta', required=False)
    pick_ad_set = forms.ChoiceField(label='...oppure un ad set Meta', required=False)
    pick_ad = forms.ChoiceField(label='...oppure un ad Meta', required=False)

    class Meta:
        model = FunnelStageSource
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        blank = [('', '— nessuna —')]
        for field_name, stream in META_ENTITY_STREAMS.items():
            choices = [(eid, f'{name} ({eid[-6:]})') for eid, name in _meta_entity_choices(stream)]
            self.fields[f'pick_{field_name}'].choices = blank + choices
        self.fields['kind'].required = False
        self.fields['name'].required = False

    def clean(self):
        cleaned = super().clean()
        picked = next(((kind, cleaned.get(f'pick_{kind}')) for kind in META_ENTITY_STREAMS
                       if cleaned.get(f'pick_{kind}')), None)
        if picked:
            kind, eid = picked
            names = dict(_meta_entity_choices(META_ENTITY_STREAMS[kind]))
            cleaned['kind'] = kind
            cleaned['external_id'] = eid
            cleaned['name'] = names.get(eid, eid)
        elif not cleaned.get('kind') or not cleaned.get('name'):
            raise forms.ValidationError(
                'Scegli una campagna/ad set/ad qui sopra, oppure compila "Fase" e "Nome" a mano per una voce non Meta.')
        return cleaned


class FunnelStageSourceInline(admin.TabularInline):
    model = FunnelStageSource
    form = FunnelStageSourceForm
    extra = 0
    fields = ('pick_campaign', 'pick_ad_set', 'pick_ad', 'kind', 'name', 'external_id', 'notes')


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
    form = FunnelStageSourceForm
    list_display = ('stage', 'kind', 'name', 'external_id')
    list_filter = ('stage', 'kind')
    fields = ('stage', 'pick_campaign', 'pick_ad_set', 'pick_ad', 'kind', 'name', 'external_id', 'notes')
