from django import forms
from django.contrib import admin

from .models import (AirbyteRecord, BudgetLine, BudgetPlan, ChannelCadence,
                     ContentPiece, FunnelKPI, FunnelKPIValue, FunnelStage,
                     FunnelStageSource, ImportLog, MarketingEvent, Tag,
                     TagDimension)


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
    list_display = ('stage', 'kind', 'name', 'external_id', 'tag_list')
    list_filter = ('stage', 'kind', 'tags__dimension', 'tags')
    filter_horizontal = ('tags',)
    fields = ('stage', 'pick_campaign', 'pick_ad_set', 'pick_ad', 'kind', 'name', 'external_id',
              'tags', 'notes')

    @admin.display(description='Tag')
    def tag_list(self, obj):
        return ', '.join(t.name for t in obj.tags.all()) or '—'


# -------------------------------------------------------------- tag taxonomy

class TagInline(admin.TabularInline):
    model = Tag
    extra = 0
    prepopulated_fields = {'slug': ('name',)}
    fields = ('name', 'slug', 'target_share', 'color', 'description', 'order', 'is_active')


@admin.register(TagDimension)
class TagDimensionAdmin(admin.ModelAdmin):
    list_display = ('name', 'order', 'allow_multiple', 'tag_count', 'share_total', 'is_active')
    list_editable = ('order',)
    prepopulated_fields = {'slug': ('name',)}
    inlines = [TagInline]

    @admin.display(description='N. tag')
    def tag_count(self, obj):
        return obj.tags.count()

    @admin.display(description='Somma quote')
    def share_total(self, obj):
        shares = [t.target_share for t in obj.tags.all() if t.target_share is not None]
        if not shares:
            return '—'
        total = sum(shares)
        return f'{total:g}%' + ('' if abs(total - 100) < 0.01 else ' ⚠')


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('dimension', 'name', 'target_share', 'order', 'is_active')
    list_filter = ('dimension', 'is_active')
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}


# ------------------------------------------------------------ budget planning

class BudgetLineInline(admin.TabularInline):
    model = BudgetLine
    extra = 0
    filter_horizontal = ('tags',)
    fields = ('order', 'label', 'stage', 'tags', 'source', 'percent', 'amount', 'is_media', 'notes')


@admin.register(BudgetPlan)
class BudgetPlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'period_start', 'period_end', 'total_budget', 'media_percent', 'is_active')
    inlines = [BudgetLineInline]

    @admin.display(description='% media allocata')
    def media_percent(self, obj):
        total = sum(l.resolved_percent or 0 for l in obj.lines.filter(is_media=True))
        return f'{total:g}%' + ('' if abs(total - 100) < 0.01 else ' ⚠')


@admin.register(BudgetLine)
class BudgetLineAdmin(admin.ModelAdmin):
    list_display = ('plan', 'label', 'stage', 'percent', 'amount', 'is_media')
    list_filter = ('plan', 'stage', 'is_media', 'tags')
    filter_horizontal = ('tags',)


# -------------------------------------------------------- editorial calendar

@admin.register(ChannelCadence)
class ChannelCadenceAdmin(admin.ModelAdmin):
    list_display = ('label', 'channel', 'target_min', 'target_max', 'period', 'role', 'order', 'is_active')
    list_editable = ('target_min', 'target_max', 'period', 'order')


@admin.register(ContentPiece)
class ContentPieceAdmin(admin.ModelAdmin):
    list_display = ('planned_date', 'title', 'channel', 'content_format', 'status', 'stage',
                    'owner', 'tag_list', 'is_linked')
    list_filter = ('status', 'channel', 'content_format', 'stage', 'tags__dimension', 'tags', 'owner')
    search_fields = ('title', 'brief', 'hook', 'notes', 'external_permalink')
    date_hierarchy = 'planned_date'
    filter_horizontal = ('tags',)
    fieldsets = (
        (None, {'fields': ('title', 'channel', 'content_format', 'status', 'owner')}),
        ('Pianificazione', {'fields': ('planned_date', 'published_date', 'stage', 'tags', 'campaign_source')}),
        ('Contenuto', {'fields': ('brief', 'hook', 'notes')}),
        ('Metriche reali', {'fields': ('external_permalink', 'external_post_id'),
                            'description': 'Incolla il permalink del post pubblicato per agganciare '
                                           'reach ed engagement reali a questa uscita.'}),
    )

    @admin.display(description='Tag')
    def tag_list(self, obj):
        return ', '.join(t.name for t in obj.tags.all()) or '—'

    @admin.display(description='Metriche', boolean=True)
    def is_linked(self, obj):
        return bool(obj.external_permalink or obj.external_post_id)
