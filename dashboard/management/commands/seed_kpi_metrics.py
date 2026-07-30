"""Wire the funnel KPIs to real synced metrics.

Before this, only three KPIs were computed (by name match); everything else
silently fell back to manual entry and showed nothing — which is why the
Awareness stage looked empty even with campaigns correctly assigned to it.

Idempotent: only fills in KPIs that aren't configured yet, and creates the
few that were missing entirely (notably impressions, which the deck's KPI
list didn't include but which is the base "contatti lordi" number).

  .venv/bin/python manage.py seed_kpi_metrics
  .venv/bin/python manage.py seed_kpi_metrics --force   # reconfigure existing
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from dashboard.models import FunnelKPI, FunnelStage

# stage_slug, kpi name, unit, source, metric, denominator, aggregation, scale, entity_level
# denominator + aggregation='ratio' is what makes cost/rate KPIs correct at any
# granularity: they're recomputed as sum(num)/sum(den) per bucket rather than
# averaged from daily figures.
KPI_SPECS = [
    # --- Awareness: paid reach/impressions plus the organic side, kept apart ---
    ('awareness', 'Impression (paid)', 'count', 'paid', 'impressions', '', 'sum', 1.0, ''),
    ('awareness', 'Reach nel target', 'count', 'paid', 'reach', '', 'sum', 1.0, ''),
    ('awareness', 'Frequenza', 'ratio', 'paid', 'impressions', 'reach', 'ratio', 1.0, ''),
    ('awareness', 'Video completion', 'percent', 'paid', 'video_p100', 'video_plays', 'ratio', 100.0, ''),
    ('awareness', 'Impression organiche', 'count', 'organic', 'organic_media_view', '', 'sum', 1.0, ''),
    ('awareness', 'Reach organica', 'count', 'organic', 'organic_reach', '', 'sum', 1.0, ''),
    ('awareness', 'Interazioni organiche', 'count', 'organic', 'organic_engagement', '', 'sum', 1.0, ''),
    ('awareness', 'Post pubblicati', 'count', 'organic', 'organic_posts', '', 'sum', 1.0, ''),

    # --- Conversion: lead + costo per lead pesato ---
    ('conversion', 'Lead validi', 'count', 'paid', 'leads', '', 'sum', 1.0, ''),
    ('conversion', 'CPL', 'eur', 'paid', 'spend', 'leads', 'ratio', 1.0, ''),
    ('conversion', 'Spesa', 'eur', 'paid', 'spend', '', 'sum', 1.0, ''),
    ('conversion', 'Clienti nuovi paganti', 'count', 'wundt', 'clients_acquired', '', 'sum', 1.0, ''),

    # --- Consideration: click e costo per click, finché non c'è attribuzione per lead ---
    ('consideration', 'Click sul link', 'count', 'paid', 'inline_link_clicks', '', 'sum', 1.0, ''),
    ('consideration', 'CTR', 'percent', 'paid', 'clicks', 'impressions', 'ratio', 100.0, ''),
]


class Command(BaseCommand):
    help = 'Collega i KPI del funnel alle metriche reali sincronizzate.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Riconfigura anche i KPI già collegati a una metrica.')

    @transaction.atomic
    def handle(self, *args, **options):
        force = options['force']
        created = updated = skipped = 0

        for (slug, name, unit, source, metric, denom, agg, scale, level) in KPI_SPECS:
            stage = FunnelStage.objects.filter(slug=slug).first()
            if not stage:
                self.stdout.write(self.style.WARNING(f'  fase "{slug}" non trovata, salto "{name}"'))
                continue

            kpi = stage.kpis.filter(name=name).first()
            if kpi is None:
                kpi = FunnelKPI(stage=stage, name=name, unit=unit,
                                order=(stage.kpis.count() or 0) + 1)
                created += 1
                action = 'creato'
            elif kpi.metric and not force:
                skipped += 1
                continue
            else:
                updated += 1
                action = 'aggiornato'

            kpi.unit = unit
            kpi.source = source
            kpi.metric = metric
            kpi.metric_denominator = denom
            kpi.aggregation = agg
            kpi.scale = scale
            kpi.entity_level = level
            kpi.is_active = True
            kpi.save()
            detail = f'{source}:{metric}' + (f' ÷ {denom}' if denom else '') + (f' ×{scale:g}' if scale != 1 else '')
            self.stdout.write(f'  {stage.name} · {name}: {action} ({detail})')

        self.stdout.write(self.style.SUCCESS(
            f'KPI collegati: {created} creati, {updated} aggiornati, {skipped} già configurati (usa --force per rifarli).'))
