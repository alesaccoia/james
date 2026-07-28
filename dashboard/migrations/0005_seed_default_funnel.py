from django.db import migrations


# Default funnel seeded from Mentor's own strategy doc: the media/ad funnel
# ("Il funnel in tre fasi, come sistema unico" - Awareness / Consideration /
# Conversion, with budget 30/20/50%) plus CRM/retention as a fourth stage
# (their own "Poche campagne stabili" table treats CRM/riattivazione as a
# fourth budget line, outside Meta). Each stage carries the KPIs from the
# "KPI per livello" table and is pre-linked to the real campaign names from
# their Meta architecture table, so campaign association isn't a blank slate.
# Everything here is just a starting point - stages/KPIs/sources can be freely
# renamed, reordered, added or removed from the funnel page or the admin.
STAGES = [
    {
        'slug': 'awareness', 'name': 'Awareness', 'order': 1,
        'description': 'Reach e memoria del brand. Budget Meta: 30%. Target: genitori 70% / studenti 30%.',
        'kpis': [
            {'name': 'Reach nel target', 'unit': 'count'},
            {'name': 'Frequenza', 'unit': 'ratio'},
            {'name': 'Video completion', 'unit': 'percent'},
            {'name': 'Branded search', 'unit': 'count'},
        ],
        'sources': [
            {'kind': 'campaign', 'name': '1. Awareness', 'notes': 'Obiettivo Meta: awareness/video views. Nessun lead form.'},
        ],
    },
    {
        'slug': 'consideration', 'name': 'Consideration', 'order': 2,
        'description': 'Comprensione, fiducia, intenzione. Budget Meta: 20%. Prevalenza genitori; studenti come influenza.',
        'kpis': [
            {'name': 'CPL', 'unit': 'eur'},
            {'name': 'Lead validi', 'unit': 'count'},
            {'name': 'Tasso di contatto', 'unit': 'percent'},
            {'name': 'Booking rate', 'unit': 'percent'},
        ],
        'sources': [
            {'kind': 'campaign', 'name': '2. Lead Gen Prospecting', 'notes': 'Audience ampia sui genitori; segmenti solo se con volume.'},
            {'kind': 'campaign', 'name': '3. Lead Gen Retargeting', 'notes': 'Engager, video viewer, lead non acquistati.'},
        ],
    },
    {
        'slug': 'conversion', 'name': 'Conversion', 'order': 3,
        'description': 'Lead qualificato, booking, cliente. Budget Meta: 50%. Soprattutto genitori, con Instant Form.',
        'kpis': [
            {'name': 'Show rate', 'unit': 'percent'},
            {'name': 'Lead -> cliente', 'unit': 'percent'},
            {'name': 'Clienti nuovi paganti', 'unit': 'count', 'target_value': 30},
            {'name': 'CAC per cliente pagante', 'unit': 'eur'},
        ],
        'sources': [
            {'kind': 'campaign', 'name': '2. Lead Gen Prospecting', 'notes': 'Stessa campagna guida Consideration e Conversion.'},
            {'kind': 'campaign', 'name': '3. Lead Gen Retargeting', 'notes': 'Stessa campagna guida Consideration e Conversion.'},
        ],
    },
    {
        'slug': 'crm-retention', 'name': 'CRM e retention', 'order': 4,
        'description': 'Nurturing del database dei non acquistati; referral e rinnovo dei clienti esistenti. Fuori Meta.',
        'kpis': [
            {'name': 'Open rate', 'unit': 'percent'},
            {'name': 'Click rate', 'unit': 'percent'},
            {'name': 'Riattivazioni', 'unit': 'count'},
            {'name': 'Clienti recuperati', 'unit': 'count'},
            {'name': 'Inviti referral', 'unit': 'count'},
            {'name': 'Nuovi clienti da referral', 'unit': 'count'},
            {'name': 'CAC referral', 'unit': 'eur'},
            {'name': 'Tasso di rinnovo', 'unit': 'percent'},
        ],
        'sources': [
            {'kind': 'other', 'name': 'CRM / riattivazione', 'notes': 'Database segmentato. Effort operativo, non budget media - fuori Meta (email/contatto diretto).'},
        ],
    },
]


def seed(apps, schema_editor):
    FunnelStage = apps.get_model('dashboard', 'FunnelStage')
    FunnelKPI = apps.get_model('dashboard', 'FunnelKPI')
    FunnelStageSource = apps.get_model('dashboard', 'FunnelStageSource')
    if FunnelStage.objects.exists():
        return  # already customized by the user (or re-run) - don't clobber
    for stage_def in STAGES:
        stage = FunnelStage.objects.create(
            slug=stage_def['slug'], name=stage_def['name'],
            order=stage_def['order'], description=stage_def['description'])
        for i, kpi_def in enumerate(stage_def['kpis'], start=1):
            FunnelKPI.objects.create(
                stage=stage, name=kpi_def['name'], unit=kpi_def['unit'],
                target_value=kpi_def.get('target_value'), order=i)
        for source_def in stage_def.get('sources', []):
            FunnelStageSource.objects.create(
                stage=stage, kind=source_def['kind'], name=source_def['name'],
                notes=source_def.get('notes', ''))


def unseed(apps, schema_editor):
    FunnelStage = apps.get_model('dashboard', 'FunnelStage')
    FunnelStage.objects.filter(slug__in=[s['slug'] for s in STAGES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0004_funnelstage_funnelkpi_funnelstagesource_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
