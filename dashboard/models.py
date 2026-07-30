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
    # Where the numbers come from. 'manual' keeps the original behaviour
    # (values typed in by hand as FunnelKPIValue); the others compute the
    # series from real synced data, restricted to the entities assigned to
    # this KPI's stage.
    SOURCE_CHOICES = [
        ('manual', 'Manuale'),
        ('paid', 'Meta paid (campagne/adset/ad)'),
        ('organic', 'Post organici'),
        ('wundt', 'CRM wundt'),
    ]
    # How the per-day values roll up into a week/month bucket. 'ratio' is the
    # important one: a cost-per-X or a rate must be recomputed as
    # sum(numerator)/sum(denominator) over the bucket, never averaged from the
    # daily figures - averaging ratios silently gives the wrong number as soon
    # as the daily volumes differ.
    AGGREGATION_CHOICES = [
        ('sum', 'Somma'),
        ('avg', 'Media'),
        ('ratio', 'Rapporto pesato (num. ÷ den.)'),
    ]

    stage = models.ForeignKey(FunnelStage, on_delete=models.CASCADE, related_name='kpis')
    name = models.CharField(max_length=150)
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='count')
    target_value = models.FloatField(
        null=True, blank=True,
        help_text='Obiettivo opzionale, mostrato come riferimento sul grafico.')
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default='manual',
        help_text='Da dove arrivano i numeri. "Manuale" usa i valori inseriti a mano.')
    metric = models.CharField(
        max_length=60, blank=True,
        help_text='Metrica di origine (es. impressions, reach, spend). Vedi METRIC_REGISTRY in views.py.')
    metric_denominator = models.CharField(
        max_length=60, blank=True,
        help_text='Solo per aggregazione "rapporto": la metrica al denominatore (es. spend ÷ leads = CPL).')
    aggregation = models.CharField(max_length=10, choices=AGGREGATION_CHOICES, default='sum')
    scale = models.FloatField(
        default=1.0,
        help_text='Moltiplicatore applicato al risultato, es. 100 per trasformare un rapporto in percentuale.')
    entity_level = models.CharField(
        max_length=20, blank=True,
        help_text='Solo per fonte "paid": limita il calcolo a un livello preciso '
                  '(campaign / ad_set / ad). Vuoto = qualunque livello assegnato alla fase.')
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['stage__order', 'order', 'id']

    def __str__(self):
        return f'{self.stage.name} · {self.name}'

    @property
    def is_computed(self):
        return self.source != 'manual' and bool(self.metric)


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
    campaign, ad set or ad (linked by real Meta id, so stats can be pulled
    straight from fb_ads_insights by campaign_id/adset_id/ad_id), a GA4
    channel, or anything else named freely. Powers the per-stage campaign
    breakdown and the computed KPIs in views.py (FUNNEL_COMPUTED_KPIS)."""
    KIND_CHOICES = [
        ('campaign', 'Campagna'),
        ('ad_set', 'Ad set'),
        ('ad', 'Ad'),
        ('channel', 'Canale'),
        ('other', 'Altro'),
    ]
    stage = models.ForeignKey(FunnelStage, on_delete=models.CASCADE, related_name='sources')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='campaign')
    name = models.CharField(max_length=200)
    external_id = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text='ID Meta reale (campaign_id / adset_id / ad_id) per collegare le statistiche esatte. '
                  'Vuoto per voci non Meta (es. "Altro").')
    # Same taxonomy used for budget and content: tagging the real Meta objects
    # is what lets actual spend be sliced by audience / message type / pillar,
    # instead of only by campaign name.
    tags = models.ManyToManyField('Tag', blank=True, related_name='sources')
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['stage__order', 'kind', 'name']

    def __str__(self):
        return f'{self.stage.name} · {self.get_kind_display()}: {self.name}'


# --------------------------------------------------------------- tagging
# A generic dimension + tag taxonomy. Deliberately not hardcoded to
# "audience" / "message type" / etc: the strategy deck's own cuts (genitori vs
# studenti, Think-Feel-Do, pilastro creativo, ordine scolastico, bisogno) are
# just seeded rows, and new axes can be added without a migration. Everything
# plannable and everything publishable hangs off these two models, which is
# what makes "budget per tag" and "resa per tag" possible at all.


class TagDimension(models.Model):
    """One axis of classification — e.g. "Audience", "Think-Feel-Do",
    "Pilastro creativo". Owns an ordered set of Tags."""
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    allow_multiple = models.BooleanField(
        default=False,
        help_text='Se attivo, una campagna/contenuto può portare più tag di questa dimensione '
                  '(es. "Bisogno"). Altrimenti se ne aspetta uno solo (es. "Audience").')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.name


class Tag(models.Model):
    """One value within a TagDimension. `target_share` is the *intended*
    weight within its dimension (genitori 70 / studenti 30, the creative mix
    percentages...) — the plan against which real budget and real output get
    compared. Left null when the dimension has no intended split."""
    dimension = models.ForeignKey(TagDimension, on_delete=models.CASCADE, related_name='tags')
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, blank=True, help_text='Colore hex, es. #f96a34.')
    target_share = models.FloatField(
        null=True, blank=True,
        help_text='Quota % attesa dentro la sua dimensione. Vuoto se non c\'è uno split previsto.')
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['dimension__order', 'order', 'id']
        unique_together = [('dimension', 'slug')]

    def __str__(self):
        return f'{self.dimension.name}: {self.name}'


# --------------------------------------------------------- budget planning


class BudgetPlan(models.Model):
    """A budget period to plan against — typically a month. `total_budget` is
    media spend only; operational effort (CRM, referral) is tracked as lines
    with no amount, matching how the deck separates the two."""
    name = models.CharField(max_length=150)
    period_start = models.DateField()
    period_end = models.DateField()
    total_budget = models.FloatField(default=0, help_text='Budget media totale del periodo, in euro.')
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']

    def __str__(self):
        return self.name


class BudgetLine(models.Model):
    """One planned slice of a BudgetPlan: a funnel stage and/or a combination
    of tags, with either a percentage of the plan or an absolute amount.

    Optionally points at a FunnelStageSource (a real Meta campaign/ad set),
    which is what lets planned spend be compared against actual spend pulled
    from fb_ads_insights. A line with no source is still useful as intent —
    it just can't be reconciled automatically.
    """
    plan = models.ForeignKey(BudgetPlan, on_delete=models.CASCADE, related_name='lines')
    label = models.CharField(max_length=200)
    stage = models.ForeignKey(FunnelStage, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='budget_lines')
    tags = models.ManyToManyField(Tag, blank=True, related_name='budget_lines')
    source = models.ForeignKey(FunnelStageSource, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='budget_lines',
                               help_text='Campagna/ad set Meta reale, per confrontare pianificato e speso.')
    percent = models.FloatField(null=True, blank=True, help_text='% del budget totale del piano.')
    amount = models.FloatField(null=True, blank=True, help_text='Importo fisso in euro (alternativo alla %).')
    is_media = models.BooleanField(
        default=True,
        help_text='Disattiva per voci che sono effort operativo e non budget media (es. CRM), '
                  'così non entrano nella somma del 100%.')
    order = models.PositiveIntegerField(default=1)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.plan.name} · {self.label}'

    @property
    def resolved_amount(self):
        """Euro value of this line: explicit amount wins, else percent of the
        plan total. None when neither is set (pure-intent line)."""
        if self.amount is not None:
            return self.amount
        if self.percent is not None:
            return self.plan.total_budget * self.percent / 100.0
        return None

    @property
    def resolved_percent(self):
        if self.percent is not None:
            return self.percent
        if self.amount is not None and self.plan.total_budget:
            return self.amount / self.plan.total_budget * 100.0
        return None


# ------------------------------------------------------- editorial calendar


class ChannelCadence(models.Model):
    """Expected publishing rhythm per channel, from the deck's editorial plan.
    Drives the "are we actually publishing enough" check on the calendar —
    the frequency commitment the social team and designer are held to."""
    PERIOD_CHOICES = [('week', 'Settimana'), ('month', 'Mese')]

    channel = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=100)
    target_min = models.FloatField(help_text='Minimo di uscite attese nel periodo.')
    target_max = models.FloatField(null=True, blank=True, help_text='Massimo, se è un intervallo.')
    period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='week')
    role = models.CharField(max_length=250, blank=True, help_text='Ruolo del canale nel funnel.')
    order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.label


class ContentPiece(models.Model):
    """One entry in the editorial calendar — planned, in production, or
    published. Tagged along the same axes as budget, so the same taxonomy
    answers both "where is the money going" and "what are we actually
    publishing, and how did it do".

    Once `external_permalink` (or `external_post_id`) is filled in, the piece
    is matched against the real synced Facebook/Instagram post and its actual
    reach/engagement flow back in — which is what turns the calendar from a
    plan into a measurement instrument.
    """
    STATUS_CHOICES = [
        ('idea', 'Idea'),
        ('brief', 'Brief'),
        ('produzione', 'In produzione'),
        ('pronto', 'Pronto'),
        ('pubblicato', 'Pubblicato'),
        ('archiviato', 'Archiviato'),
    ]
    FORMAT_CHOICES = [
        ('reel', 'Reel'),
        ('post', 'Post'),
        ('carosello', 'Carosello'),
        ('story', 'Story'),
        ('video', 'Video'),
        ('statico', 'Statico'),
        ('email', 'Email'),
        ('altro', 'Altro'),
    ]

    title = models.CharField(max_length=250)
    channel = models.CharField(max_length=40, help_text='Deve combaciare con ChannelCadence.channel.')
    content_format = models.CharField(max_length=20, choices=FORMAT_CHOICES, default='post')
    stage = models.ForeignKey(FunnelStage, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='content_pieces')
    tags = models.ManyToManyField(Tag, blank=True, related_name='content_pieces')
    campaign_source = models.ForeignKey(
        FunnelStageSource, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='content_pieces',
        help_text='Campagna/ad set Meta a cui questa creatività è associata, se è un contenuto paid.')
    planned_date = models.DateField(db_index=True)
    published_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='idea', db_index=True)
    owner = models.CharField(max_length=120, blank=True, help_text='Chi lo produce (social, designer, tutor...).')
    brief = models.TextField(blank=True)
    hook = models.CharField(
        max_length=300, blank=True,
        help_text='Come il brand è riconoscibile nei primi 2 secondi (regola creativa della strategia).')
    external_permalink = models.URLField(blank=True, help_text='Link al post pubblicato, per agganciare le metriche reali.')
    external_post_id = models.CharField(max_length=100, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-planned_date', '-id']

    def __str__(self):
        return f'{self.planned_date} — {self.title}'

    @property
    def effective_date(self):
        return self.published_date or self.planned_date


class ComparePreset(models.Model):
    """A saved configuration of the Confronto page — which metrics are on the
    chart, the period, the grouping, absolute vs normalised, events on or off.

    The whole state is kept as one JSON blob rather than a column per option
    on purpose: the compare page grows new controls regularly, and a preset
    saved today should keep working when it does (unknown keys are simply
    ignored on load, missing ones fall back to the page default).
    """
    name = models.CharField(max_length=120, unique=True)
    config = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class TaggedEntity(models.Model):
    """Tagging + funnel-stage assignment for one real Meta advertising object,
    keyed by its Meta id. Deliberately an *overlay*: names, hierarchy and
    metrics all stay in the synced Airbyte data, and only the decisions
    (which stage, which tags) live here, so a re-sync never fights with what
    was tagged by hand.

    Tags resolve down the hierarchy campaign -> ad set -> ad, per dimension:
    an ad set that carries a tag of dimension "Audience" overrides its
    campaign's Audience tag, while dimensions it says nothing about are
    inherited unchanged. Same for ads under an ad set. This is what makes it
    possible to tag broadly at the top and refine only where it matters.

    Organic posts are deliberately NOT stored here - they're tagged through
    ContentPiece (the editorial calendar), so the same post carries one set
    of tags whether it's edited from the calendar or from the tagging page.
    """
    KIND_CHOICES = [
        ('campaign', 'Campagna'),
        ('ad_set', 'Ad set'),
        ('ad', 'Ad'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    external_id = models.CharField(max_length=100, db_index=True, help_text='ID Meta reale.')
    stage = models.ForeignKey(FunnelStage, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='tagged_entities')
    tags = models.ManyToManyField(Tag, blank=True, related_name='tagged_entities')
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('kind', 'external_id')]
        ordering = ['kind', 'id']

    def __str__(self):
        return f'{self.get_kind_display()} {self.external_id}'


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
