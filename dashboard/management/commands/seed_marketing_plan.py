"""Seed the tag taxonomy, channel cadences and a first budget plan from the
Mentor communication strategy deck (24 July 2026).

Idempotent: every object is get_or_create'd by a natural key, so running it
again after hand-editing tags in the admin won't clobber the edits. It only
fills in what's missing.

  .venv/bin/python manage.py seed_marketing_plan
  .venv/bin/python manage.py seed_marketing_plan --plan-month 2026-09 --budget 3000
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from dashboard.models import (BudgetLine, BudgetPlan, ChannelCadence, FunnelKPI,
                              FunnelStage, Tag, TagDimension)

# Explicit, so plan names don't depend on the server's locale.
MESI = {1: 'gennaio', 2: 'febbraio', 3: 'marzo', 4: 'aprile', 5: 'maggio', 6: 'giugno',
        7: 'luglio', 8: 'agosto', 9: 'settembre', 10: 'ottobre', 11: 'novembre', 12: 'dicembre'}

# Mentor brand palette, sampled from the deck.
BLU, ARANCIO, GIALLO, OLIVA = '#0d2a8c', '#f96a34', '#ffc83d', '#8a7500'
GRIGIO, VIOLA, VERDE, ROSA = '#94a3b8', '#a78bfa', '#34d399', '#f472b6'

# (slug, name, description, allow_multiple, order, [(slug, name, target_share, color, desc)])
DIMENSIONS = [
    (
        'audience', 'Audience',
        'Chi stiamo parlando. Lo split 70/30 è pesato sul ricavo generato, non sul numero di lead.',
        False, 1,
        [
            ('genitori', 'Genitori', 70.0, BLU,
             'Generano la maggior parte dei lead e valutano il servizio.'),
            ('studenti', 'Studenti', 30.0, ARANCIO,
             "Influenzano la scelta e determinano l'effettiva adozione del servizio."),
        ],
    ),
    (
        'ordine-scolastico', 'Ordine scolastico',
        'Ripartizione sullo storico. Ogni ciclo ha un decision maker diverso.',
        False, 2,
        [
            ('superiori', 'Superiori', 75.0, BLU, 'Decision maker: genitori sulla cinquantina. DSA 3,2%.'),
            ('medie', 'Medie', 12.0, ARANCIO, 'Decision maker: genitori giovani 30-40. DSA 6,7%.'),
            ('universita', 'Università', 11.0, OLIVA, 'Decision maker: genitori ma anche gli studenti stessi. DSA 7,1%.'),
            ('elementari', 'Elementari', 2.0, GRIGIO, 'Decision maker: genitori giovani 30-40. Volume marginale.'),
        ],
    ),
    (
        'think-feel-do', 'Think · Feel · Do',
        'Registro del messaggio. Ogni fase del funnel pesa i tre in proporzioni diverse: '
        'awareness a dominanza Feel, consideration da Think a Feel, conversion da Feel a Do '
        'con una prova razionale.',
        True, 3,
        [
            ('think', 'Think', None, BLU,
             'Parte razionale: capire, valutare, confrontare. Risponde a "ha senso per me?"'),
            ('feel', 'Feel', None, ARANCIO,
             'Parte emotiva: percepire, riconoscersi, fidarsi. Risponde a "mi capisce?"'),
            ('do', 'Do', None, OLIVA,
             'Azione concreta: contattare, lasciare il contatto, provare. Risponde a "vale la pena agire ora?"'),
        ],
    ),
    (
        'pilastro-creativo', 'Pilastro creativo',
        'Mix creativo proposto, coerente con un posizionamento premium. Le quote sono la '
        'ripartizione attesa delle uscite.',
        False, 4,
        [
            ('awareness-culturale', 'Awareness culturale e contenuti ponte', 25.0, BLU,
             'Meme, cultura scolastica, contenuti ponte genitore-studente.'),
            ('educational', 'Educational e problem awareness', 25.0, ARANCIO,
             '"Tre segnali che il problema non è la mancanza di impegno."'),
            ('metodo', 'Metodo e differenziazione', 20.0, OLIVA,
             'Difficoltà, valutazione, matching tutor, percorso su misura, verifica progressi.'),
            ('testimonianze', 'Testimonianze e casi', 15.0, VERDE, 'Social proof, casi concreti, riprova sociale.'),
            ('objection-handling', 'Objection handling', 10.0, VIOLA,
             '"Funzionano online?" · "Non le vuole" · "E se il tutor non fosse quello giusto?"'),
            ('offerta-diretta', 'Offerta diretta', 5.0, ROSA, 'Offerta concreta, basso rischio.'),
        ],
    ),
    (
        'formato-proprietario', 'Formato proprietario',
        'Format ricorrenti riconoscibili. Il brand deve essere riconoscibile entro i primi due '
        'secondi del video: senza brand linkage l\'investimento produce intrattenimento ma poca '
        'memoria di marca.',
        True, 5,
        [
            ('traduzioni-studentesco', 'Traduzioni dal linguaggio studentesco', None, BLU, ''),
            ('registro-horror', 'Registro elettronico horror stories', None, ARANCIO, ''),
            ('minuto-con-tutor', 'Un minuto con il tutor', None, OLIVA, ''),
            ('tutor-creator', 'Tutor come creator', None, VERDE,
             '"L\'errore più comune non è il calcolo: è non capire il passaggio."'),
            ('contenuto-ponte', 'Contenuto ponte genitore-studente', None, VIOLA,
             'Studente: "È andata bene." Genitore: apre il registro elettronico.'),
        ],
    ),
    (
        'bisogno', 'Bisogno / materia',
        'Declinazione per bisogno specifico, usata soprattutto sulle creatività di conversion.',
        True, 6,
        [
            ('matematica-superiori', 'Matematica superiori', None, BLU, ''),
            ('medie-autonomia', 'Medie: più materie e autonomia', None, ARANCIO, ''),
            ('dsa', 'DSA', None, OLIVA, 'Strumenti su misura. Incidenza: superiori 3,2%, medie 6,7%, università 7,1%.'),
            ('recupero-debiti', 'Recupero debiti e lacune', None, VERDE, ''),
            ('preparazione-esami', 'Preparazione esami e verifiche', None, VIOLA, ''),
        ],
    ),
]

# (channel, label, target_min, target_max, period, role, order)
CADENCES = [
    ('instagram', 'Instagram', 2, 3, 'week', 'Awareness studenti e genitori; consideration visiva', 1),
    ('facebook', 'Facebook', 2, 3, 'week', 'Genitori, fiducia e riprova sociale', 2),
    ('meta_paid', 'Meta paid', 1, None, 'week', 'Distribuzione e acquisizione (always-on)', 3),
    ('newsletter_genitori', 'Newsletter genitori', 2, 4, 'month', 'Maturazione e riattivazione', 4),
    ('newsletter_studenti', 'Newsletter studenti', 2, 2, 'month', 'Affinità e attivazione', 5),
    ('google_search', 'Google Search', 0, None, 'month', 'Cattura domanda attiva (test successivo)', 6),
]

# Funnel stage media budget split, from "Il funnel in tre fasi, come sistema unico".
STAGE_SHARES = {'awareness': 30.0, 'consideration': 20.0, 'conversion': 50.0}

# Meta campaign architecture: "Poche campagne stabili, molte varianti creative".
# (label, stage_slug, percent, [tag slugs], is_media, notes)
CAMPAIGN_LINES = [
    ('1. Awareness — genitori', 'awareness', 21.0, ['genitori'], True,
     'Ad set genitori (70% del 30% di awareness). Obiettivo awareness/video, senza form.'),
    ('1. Awareness — studenti', 'awareness', 9.0, ['studenti'], True,
     'Ad set studenti (30% del 30% di awareness). Obiettivo awareness/video, senza form.'),
    ('2. Lead Gen Prospecting', 'conversion', 60.0, ['genitori', 'do'], True,
     'Genitori ampi; segmenti solo se con volume. Obiettivo leads, Instant Form.'),
    ('3. Lead Gen Retargeting', 'conversion', 10.0, ['do'], True,
     'Engager, video viewer, lead non acquistati. Obiettivo leads, Instant Form.'),
    ('CRM / riattivazione', 'crm-retention', None, [], False,
     'Effort operativo, non budget media. Database segmentato, email/contatto diretto.'),
]

# Business targets from "Obiettivi di business entro fine settembre".
# (stage_slug, kpi_name, target_value, note)
KPI_TARGETS = [
    ('conversion', 'Clienti nuovi paganti', 30.0, 'Obiettivo: 30 clienti nuovi paganti al mese entro fine settembre.'),
    ('conversion', 'CPL', 5.0,
     'ESEMPIO dalla presentazione (buy rate 60%, lead->prima lezione 10% => 600 lead => CPL max ~5 EUR). '
     'Buy rate e tasso lead->prenotazione sono oggi ignoti: da verificare prima di trattarlo come target.'),
]


class Command(BaseCommand):
    help = 'Seed tag taxonomy, channel cadences and a budget plan from the July strategy deck.'

    def add_arguments(self, parser):
        parser.add_argument('--plan-month', default=None,
                            help='Mese del piano budget da creare, formato YYYY-MM (default: mese corrente).')
        parser.add_argument('--budget', type=float, default=0.0,
                            help='Budget media totale del periodo, in euro (default: 0, da compilare dopo).')

    @transaction.atomic
    def handle(self, *args, **options):
        n_dim, n_tag = self._seed_tags()
        n_cad = self._seed_cadences()
        n_share = self._seed_stage_shares()
        n_kpi = self._seed_kpi_targets()
        plan, n_lines = self._seed_plan(options['plan_month'], options['budget'])

        self.stdout.write(self.style.SUCCESS(
            f'Seed ok: {n_dim} dimensioni, {n_tag} tag, {n_cad} cadenze, '
            f'{n_share} quote di fase, {n_kpi} target KPI, '
            f'piano "{plan.name}" con {n_lines} voci.'))

    def _seed_tags(self):
        n_dim = n_tag = 0
        for slug, name, desc, multi, order, tags in DIMENSIONS:
            dim, created = TagDimension.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'description': desc, 'allow_multiple': multi, 'order': order})
            n_dim += int(created)
            for i, (tslug, tname, share, color, tdesc) in enumerate(tags, start=1):
                _, tcreated = Tag.objects.get_or_create(
                    dimension=dim, slug=tslug,
                    defaults={'name': tname, 'target_share': share, 'color': color,
                              'description': tdesc, 'order': i})
                n_tag += int(tcreated)
            self.stdout.write(f'  dimensione {name}: {len(tags)} tag')
        return n_dim, n_tag

    def _seed_cadences(self):
        n = 0
        for channel, label, tmin, tmax, period, role, order in CADENCES:
            _, created = ChannelCadence.objects.get_or_create(
                channel=channel,
                defaults={'label': label, 'target_min': tmin, 'target_max': tmax,
                          'period': period, 'role': role, 'order': order})
            n += int(created)
        self.stdout.write(f'  cadenze canale: {len(CADENCES)}')
        return n

    def _seed_stage_shares(self):
        """Store the deck's funnel budget split on the stages themselves, so the
        planning page can show planned-vs-intended even before a budget plan
        exists. Uses the stage description only if it's still empty."""
        n = 0
        for slug, share in STAGE_SHARES.items():
            stage = FunnelStage.objects.filter(slug=slug).first()
            if not stage:
                self.stdout.write(self.style.WARNING(f'  fase "{slug}" non trovata, quota {share}% non applicata'))
                continue
            note = f'Quota budget media prevista: {share:g}%.'
            if note not in (stage.description or ''):
                stage.description = f'{stage.description}\n{note}'.strip()
                stage.save(update_fields=['description'])
                n += 1
        return n

    def _seed_kpi_targets(self):
        """Fill in target_value on existing KPIs that don't have one yet.
        Never overwrites a target already set by hand."""
        n = 0
        for stage_slug, kpi_name, target, note in KPI_TARGETS:
            kpi = FunnelKPI.objects.filter(stage__slug=stage_slug, name=kpi_name).first()
            if not kpi:
                self.stdout.write(self.style.WARNING(f'  KPI "{kpi_name}" ({stage_slug}) non trovato'))
                continue
            if kpi.target_value is None:
                kpi.target_value = target
                kpi.save(update_fields=['target_value'])
                self.stdout.write(f'  KPI {kpi_name}: target {target:g} — {note}')
                n += 1
        return n

    def _seed_plan(self, plan_month, budget):
        if plan_month:
            year, month = (int(x) for x in plan_month.split('-'))
        else:
            today = date.today()
            year, month = today.year, today.month
        start = date(year, month, 1)
        next_month = date(year + (month == 12), (month % 12) + 1, 1)
        end = next_month - timedelta(days=1)

        name = f'Piano media {MESI[month]} {year}'
        plan, _ = BudgetPlan.objects.get_or_create(
            period_start=start,
            defaults={'name': name, 'period_end': end, 'total_budget': budget,
                      'notes': 'Creato da seed_marketing_plan sulla base della strategia del 24 luglio 2026. '
                               'Riallocazione mensile in base al CAC per cliente pagante e alla qualità dei lead.'})

        n = 0
        for i, (label, stage_slug, percent, tag_slugs, is_media, notes) in enumerate(CAMPAIGN_LINES, start=1):
            stage = FunnelStage.objects.filter(slug=stage_slug).first()
            line, created = BudgetLine.objects.get_or_create(
                plan=plan, label=label,
                defaults={'stage': stage, 'percent': percent, 'is_media': is_media,
                          'notes': notes, 'order': i})
            if created:
                line.tags.set(Tag.objects.filter(slug__in=tag_slugs))
                n += 1
        return plan, n
