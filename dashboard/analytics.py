"""Aggregations over generic subject events; never returns row-level identities."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from django.utils import timezone

from .models import AirbyteRecord, SubjectEvent


def _number(value):
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _money(value):
    return round(float(value), 2)


# ------------------------------------------- definizioni allineate a WUNDT
# Le tre costanti qui sotto sono la copia di quelle indurite in WUNDT l'11/9/2026
# (`wundt/analytics/metrics.py`): JAMES e la pagina `/performance/` di WUNDT
# devono dare lo stesso numero per lo stesso periodo, quindi qui non si allarga
# niente. Se una regola cambia di là, cambia anche qui.

# Fonti i cui lead NON nascono dal funnel: li crea `sync_payments
# --create-leads` a partire da una famiglia che era già cliente, retrodatandoli
# al primo incasso. Un lead così esiste perché esiste il pagamento, non
# viceversa: contarlo fra i clienti nuovi abbassa il CAC di 3-6 volte (58
# famiglie su 92 paganti, misurato il 10/9/2026).
NON_FUNNEL_SOURCES = ('piattaforma',)

# Fonti che descrivono una migrazione e non un ingresso: restano fuori dai lead
# entrati, che misurano il funnel di oggi. A luglio 2026 erano 213 su 504.
IMPORT_SOURCES = ('piattaforma', 'import_airtable')

# I potenziali tutor non sono clienti da acquisire.
EXCLUDED_LEAD_TYPES = ('potential_tutor',)


def lead_source_of(dimensions):
    return (dimensions.get('wundt.lead_source') or '').strip().lower()


def is_funnel_lead(dimensions):
    """True se il lead va contato fra quelli ENTRATI nel periodo.

    Fuori i potenziali tutor (`wundt.lead_type`) e le fonti di migrazione
    (`wundt.lead_source`). WUNDT sta aggiungendo `wundt.lead_type` alle
    dimensions: finché non c'è, il lead passa — e la dashboard lo dichiara
    (`definitions.lead_type_note`) invece di fingere un filtro che non ha.
    """
    if dimensions.get('wundt.lead_type') in EXCLUDED_LEAD_TYPES:
        return False
    return lead_source_of(dimensions) not in IMPORT_SOURCES


def _localday(moment):
    """Il giorno in ora locale, come lo conta WUNDT (`timezone.localtime`)."""
    return timezone.localtime(moment).date()


def split_payers(first_lead, first_purchase, lead_source):
    """(acquisizioni, esclusi), entrambi subject -> datetime del 1° pagamento.

    Un pagante è un'ACQUISIZIONE solo quando il lead esisteva già prima di
    pagare: è la differenza fra "abbiamo convertito qualcuno" e "una famiglia
    che avevamo da prima è comparsa nel registro incassi". Due condizioni:

    - la fonte del lead non è in NON_FUNNEL_SOURCES;
    - il giorno del primo pagamento è STRETTAMENTE successivo al giorno di
      creazione del lead. Dei pagamenti si conosce solo il giorno, quindi lo
      stesso giorno resta fuori: su questi dati i casi "stesso giorno" sono
      import.

    Un pagante di cui JAMES non ha nessun `lead_created` non è un'acquisizione:
    non si sa quando è nato il lead, e inventarlo è peggio che escluderlo.
    Gli esclusi non spariscono: tornano indietro e la UI li scrive.
    """
    acquisitions, excluded = {}, {}
    for subject, paid_at in first_purchase.items():
        created = first_lead.get(subject)
        if created is None or lead_source.get(subject, '') in NON_FUNNEL_SOURCES:
            excluded[subject] = paid_at
        elif _localday(paid_at) <= _localday(created):
            excluded[subject] = paid_at
        else:
            acquisitions[subject] = paid_at
    return acquisitions, excluded


# ------------------------------------------------- copertura della spesa
# Latenza normale: Meta riattribuisce fino a ~72h e l'import di JAMES gira ogni
# sei ore, quindi gli ultimi due giorni possono mancare senza che sia rotto
# niente. Oltre, il dato non è in ritardo: manca.
GIORNI_TOLLERANZA_SPESA = 2

# Un canale è "di norma con dato" se ha righe di spesa nel periodo o nel mese
# prima: dopo tanto silenzio è spento, non fermo, e segnalarlo per sempre
# renderebbe il cartello rumore.
GIORNI_CANALE_ATTIVO = 30

# I canali di spesa, con lo stream Airbyte e il campo da cui si legge il giorno.
# Aggiungere un canale qui lo mette dentro al cancello.
SPEND_CHANNELS = (
    {'key': 'meta', 'label': 'Meta', 'stream': 'fb_ads_insights',
     'date_field': 'date_start'},
    {'key': 'google', 'label': 'Google', 'stream': 'gads_campaign',
     'date_field': 'segments_date'},
)


def _as_date(value):
    if not value:
        return None
    return date.fromisoformat(value) if isinstance(value, str) else value


def _it_day(day_iso):
    return '/'.join(reversed(day_iso.split('-')))


def _spend_days(channel):
    """I giorni ISO per cui esiste una riga di spesa di quel canale."""
    days = set()
    rows = AirbyteRecord.objects.filter(
        stream=channel['stream']).values_list('data', flat=True)
    for data in rows.iterator():
        day = str(data.get(channel['date_field']) or '')[:10]
        if len(day) == 10:
            days.add(day)
    return days


def spend_coverage(start, end, today=None):
    """Verdetto sulla copertura della spesa nel periodo, canale per canale.

    La copertura di un canale è il suo ULTIMO giorno con un dato di spesa.
    Scoperti sono i giorni del periodo che stanno dopo quell'ultimo giorno,
    contati fino a IERI: il giorno in corso non è mai scoperto, sta ancora
    arrivando. Un buco *dentro* la finestra coperta non conta, perché Airbyte
    non ritira un giorno già consegnato: lì "spesa zero" è un fatto, non
    un'assenza.

    Verdetto, con `GIORNI_TOLLERANZA_SPESA = 2`:

    - 0 giorni scoperti      -> il numero si stampa;
    - da 1 a 2               -> si stampa con `~` e la nota di copertura;
    - oltre 2                -> non si stampa nessun numero, si stampa il
                                motivo (quale canale è fermo e a quando).

    È la stessa regola della pagina `/performance/` di WUNDT: le due dashboard
    devono rifiutare di dividere per una spesa incompleta negli stessi casi.
    """
    start, end = _as_date(start), _as_date(end)
    today = today or timezone.localdate()
    through = min(end, today - timedelta(days=1))
    active_since = (start - timedelta(days=GIORNI_CANALE_ATTIVO)).isoformat()

    channels, uncovered = [], set()
    for channel in SPEND_CHANNELS:
        days = _spend_days(channel)
        last = max(days) if days else None
        row = {'key': channel['key'], 'label': channel['label'],
               'last_day': last, 'active': bool(last and last >= active_since),
               'days_with_data': sum(start.isoformat() <= day <= end.isoformat()
                                     for day in days),
               'uncovered_days': 0}
        if row['active']:
            missing = _days_between(
                max(start, date.fromisoformat(last) + timedelta(days=1)), through)
            row['uncovered_days'] = len(missing)
            uncovered |= set(missing)
        channels.append(row)

    reasons, notes = [], []
    for row in channels:
        if not row['active']:
            notes.append(f"spesa {row['label']}: nessun dato recente")
            continue
        notes.append(f"spesa {row['label']} aggiornata al {_it_day(row['last_day'])}")
        if row['uncovered_days']:
            reasons.append(f"spesa {row['label']} ferma al {_it_day(row['last_day'])}")

    total = len(uncovered)
    reason = ''
    if total:
        reason = (f"{', '.join(reasons)}: {total} "
                  f"{'giorno' if total == 1 else 'giorni'} del periodo "
                  f"{'non coperto' if total == 1 else 'non coperti'}")
    return {
        'start': start.isoformat(), 'end': end.isoformat(),
        'evaluated_through': through.isoformat() if through >= start else None,
        'tolerance_days': GIORNI_TOLLERANZA_SPESA,
        'channels': channels,
        'uncovered_days': total,
        'uncovered_dates': sorted(uncovered)[:31],
        'reliable': total == 0,
        'approximate': 0 < total <= GIORNI_TOLLERANZA_SPESA,
        'blocked': total > GIORNI_TOLLERANZA_SPESA,
        'reason': reason,
        'note': ' · '.join(notes),
    }


def _days_between(first, last):
    """I giorni ISO da `first` a `last` compresi; vuoto se `last` viene prima."""
    days, current = [], first
    while current <= last:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def commercial_metrics(source=None, start=None, end=None, dormant_days=60):
    """Compute LTV, cohorts, campaign revenue and recovery without exposing IDs."""
    all_events = SubjectEvent.objects.all().order_by('occurred_at', 'pk')
    if source:
        all_events = all_events.filter(source__slug=source)
    events = all_events
    if start:
        events = events.filter(occurred_at__date__gte=start)
    if end:
        events = events.filter(occurred_at__date__lte=end)
    subjects = defaultdict(lambda: {'first': None, 'purchases': [], 'messages': []})
    campaigns = defaultdict(lambda: {'revenue': Decimal(0), 'purchases': 0, 'subjects': set()})
    lead_campaigns = defaultdict(int)
    campaign_touches = defaultdict(list)
    for touch in all_events.filter(event_type='lead_created').iterator():
        campaign = (touch.dimensions.get('marketing.campaign_id') or
                    touch.dimensions.get('marketing.utm_campaign'))
        if campaign and touch.external_subject_id:
            campaign_touches[touch.external_subject_id].append(
                (touch.occurred_at, campaign))
    daily = defaultdict(lambda: {'leads': 0, 'purchases': 0, 'revenue': Decimal(0)})
    lead_facts = {}
    for lead_event in events.filter(event_type='lead_created').iterator():
        key = lead_event.external_subject_id or f'event:{lead_event.pk}'
        campaign = (lead_event.dimensions.get('marketing.campaign_id') or
                    lead_event.dimensions.get('marketing.utm_campaign'))
        fact = lead_facts.setdefault(key, {
            'occurred_at': lead_event.occurred_at, 'campaign': campaign})
        if lead_event.occurred_at < fact['occurred_at']:
            fact['occurred_at'] = lead_event.occurred_at
        if campaign:
            fact['campaign'] = campaign
    for fact in lead_facts.values():
        daily[fact['occurred_at'].date().isoformat()]['leads'] += 1
        lead_campaigns[fact['campaign'] or 'unattributed'] += 1
    totals = defaultdict(int)
    revenue = Decimal(0)
    for event in events.iterator():
        totals[event.event_type] += 1
        day = event.occurred_at.date().isoformat()
        subject = event.external_subject_id
        if subject:
            bucket = subjects[subject]
            bucket['first'] = bucket['first'] or event.occurred_at
            if event.event_type == 'message_sent':
                bucket['messages'].append((event.occurred_at,
                                           event.dimensions.get('messaging.template') or 'unknown'))
        if event.event_type != 'purchase':
            continue
        value = _number(event.measures.get('commerce.revenue_eur'))
        daily[day]['purchases'] += 1
        daily[day]['revenue'] += value
        revenue += value
        if subject:
            subjects[subject]['purchases'].append((event.occurred_at, value))
        campaign = (event.dimensions.get('marketing.campaign_id') or
                    event.dimensions.get('marketing.utm_campaign'))
        if not campaign and subject:
            previous = [item for item in campaign_touches.get(subject, [])
                        if item[0] <= event.occurred_at]
            if previous:
                campaign = previous[-1][1]
        campaign = campaign or 'unattributed'
        campaigns[campaign]['revenue'] += value
        campaigns[campaign]['purchases'] += 1
        if subject:
            campaigns[campaign]['subjects'].add(subject)
    totals['lead_created'] = len(lead_facts)
    paying = {key: value for key, value in subjects.items() if value['purchases']}
    repeat = recovered = 0
    cohorts = defaultdict(lambda: {'subjects': set(), 'customers': set(), 'revenue': Decimal(0)})
    for subject, data in subjects.items():
        cohort = data['first'].date().replace(day=1).isoformat() if data['first'] else None
        if cohort:
            cohorts[cohort]['subjects'].add(subject)
        purchases = data['purchases']
        if not purchases:
            continue
        repeat += int(len(purchases) > 1)
        if cohort:
            cohorts[cohort]['customers'].add(subject)
            cohorts[cohort]['revenue'] += sum((value for _, value in purchases), Decimal(0))
        if any(current[0] - previous[0] >= timedelta(days=dormant_days)
               for previous, current in zip(purchases, purchases[1:])):
            recovered += 1
    customer_count = len(paying)
    as_of = max((data['first'] for data in subjects.values() if data['first']), default=None)
    if events.exists():
        as_of = events.order_by('-occurred_at').values_list('occurred_at', flat=True).first()
    horizons = {}
    for days in (30, 90, 180, 365):
        eligible = [(subject, data) for subject, data in paying.items()
                    if as_of and data['first'] + timedelta(days=days) <= as_of]
        horizon_revenue = sum((value for _, data in eligible for when, value in data['purchases']
                               if when <= data['first'] + timedelta(days=days)), Decimal(0))
        horizons[str(days)] = _money(horizon_revenue / len(eligible)) if eligible else None
    campaign_effects = defaultdict(lambda: {'subjects': set(), 'converted': set(), 'revenue': Decimal(0)})
    for subject, data in subjects.items():
        first_exposure = {}
        for sent_at, template in data['messages']:
            first_exposure[template] = min(sent_at, first_exposure.get(template, sent_at))
        for template, sent_at in first_exposure.items():
            campaign_effects[template]['subjects'].add(subject)
            subsequent = [(when, value) for when, value in data['purchases']
                          if sent_at <= when <= sent_at + timedelta(days=30)]
            if subsequent:
                campaign_effects[template]['converted'].add(subject)
                campaign_effects[template]['revenue'] += sum((value for _, value in subsequent), Decimal(0))
    return {
        'totals': {'events': sum(totals.values()), 'subjects': len(subjects),
                   'customers': customer_count, 'purchases': totals['purchase'],
                   'revenue_eur': _money(revenue),
                   'average_ltv_eur': _money(revenue / customer_count) if customer_count else 0,
                   'mature_ltv_horizons_eur': horizons,
                   'repeat_customers': repeat, 'recovered_customers': recovered},
        'event_types': dict(sorted(totals.items())),
        'daily': [{'date': key, 'leads': value['leads'], 'purchases': value['purchases'],
                   'revenue_eur': _money(value['revenue'])}
                  for key, value in sorted(daily.items())],
        'attribution': {
            'attributed_leads': sum(value for key, value in lead_campaigns.items()
                                    if key != 'unattributed'),
            'unattributed_leads': lead_campaigns['unattributed'],
            'attributed_purchases': sum(value['purchases'] for key, value in campaigns.items()
                                        if key != 'unattributed'),
            'unattributed_purchases': campaigns['unattributed']['purchases'],
            'attributed_revenue_eur': _money(sum((value['revenue'] for key, value in campaigns.items()
                                                  if key != 'unattributed'), Decimal(0))),
            'unattributed_revenue_eur': _money(campaigns['unattributed']['revenue']),
        },
        'campaigns': sorted(({'campaign': key,
                              'revenue_eur': _money(campaigns[key]['revenue']),
                              'leads': lead_campaigns[key],
                              'purchases': campaigns[key]['purchases'],
                              'customers': len(campaigns[key]['subjects'])}
                             for key in set(campaigns) | set(lead_campaigns)),
                            key=lambda row: (-row['revenue_eur'], -row['leads'])),
        'cohorts': [{'month': key, 'subjects': len(value['subjects']),
                     'customers': len(value['customers']), 'revenue_eur': _money(value['revenue']),
                     'average_ltv_eur': _money(value['revenue'] / len(value['customers']))
                     if value['customers'] else 0}
                    for key, value in sorted(cohorts.items())],
        'campaign_effects_30d': sorted(({
            'campaign': key, 'exposed_subjects': len(value['subjects']),
            'converted_subjects': len(value['converted']),
            'conversion_rate': round(len(value['converted']) / len(value['subjects']), 4)
            if value['subjects'] else 0,
            'revenue_eur': _money(value['revenue'])}
            for key, value in campaign_effects.items()), key=lambda row: -row['revenue_eur']),
        'privacy': {'row_level_subjects_returned': False, 'direct_contact_data': False},
    }


FUNNEL_STAGES = (
    (0, 'Lead entrati'), (1, 'Contattati'), (2, 'Hanno risposto'),
    (3, 'Call fissata'), (4, 'Call presenziata'), (5, 'Clienti'),
)
STATUS_RANK = {
    'new': 0,
    'contacted_whatsapp': 1, 'contacted_whatsapp_no_answer': 1,
    'contacted_phone': 1, 'contacted_phone_no_answer': 1, 'dormant': 1,
    'customer_contacted': 2, 'contacted_phone_answered': 2,
    'booked_meeting': 3, 'meeting_attended': 4, 'client_acquired': 5,
}
NON_CONTACT_ACTIONS = {
    'imported', 'automation_started', 'automation_stopped', 'email_event',
    'created', 'assigned', 'snoozed', 'lead_type_changed', 'status_change',
}


def performance_metrics(source='wundt', start=None, end=None):
    """WUNDT-parity metrics computed only from JAMES facts and Airbyte spend.

    "Lead entrati" e "acquisizioni" seguono le definizioni di WUNDT (vedi
    `is_funnel_lead` e `split_payers`): non sono "chi ha un primo pagamento nel
    periodo" e "chi ha un primo lead nel periodo". La chiave `new_customers`
    resta quella di prima perché è la chiave che i preset salvati della pagina
    Confronto hanno dentro (`crm_new_customers`), ma il numero è il numero
    delle ACQUISIZIONI, e le etichette lo dicono.
    """
    all_events = SubjectEvent.objects.filter(source__slug=source).order_by(
        'occurred_at', 'pk')
    first_lead, campaign_by_subject, lead_dims = {}, {}, {}
    first_purchase, purchases_by_subject = {}, defaultdict(list)
    status_history, contact_history = defaultdict(list), defaultdict(list)
    for event in all_events.iterator():
        subject = event.external_subject_id
        if not subject:
            continue
        if event.event_type == 'lead_created':
            first_lead[subject] = min(event.occurred_at,
                                      first_lead.get(subject, event.occurred_at))
            campaign = (event.dimensions.get('marketing.campaign_id') or
                        event.dimensions.get('marketing.utm_campaign'))
            if campaign:
                campaign_by_subject[subject] = campaign
            # Fonte e tipo del lead: fra due `lead_created` dello stesso
            # soggetto vince il primo che li porta (sono duplicati dello stesso
            # lead, non due lead diversi).
            dims = lead_dims.setdefault(subject, {})
            for field in ('wundt.lead_source', 'wundt.lead_type'):
                if field not in dims and event.dimensions.get(field):
                    dims[field] = event.dimensions[field]
        elif event.event_type == 'purchase':
            value = _number(event.measures.get('commerce.revenue_eur'))
            purchases_by_subject[subject].append((event.occurred_at, value))
            first_purchase[subject] = min(
                event.occurred_at, first_purchase.get(subject, event.occurred_at))
        elif event.event_type == 'lead_status_changed':
            status_history[subject].append((
                event.occurred_at,
                event.dimensions.get('wundt.to_status') or 'new'))
        elif event.event_type == 'lead_action':
            action = event.dimensions.get('wundt.action_type') or ''
            if action and action not in NON_CONTACT_ACTIONS:
                contact_history[subject].append(event.occurred_at)

    start_date = _as_date(start) or min(
        (_localday(when) for when in first_lead.values()), default=None)
    end_date = _as_date(end) or max(
        [_localday(when) for when in first_lead.values()] +
        [_localday(when) for when in first_purchase.values()], default=None)
    if not start_date or not end_date:
        return {'empty': True, 'daily': [], 'funnel': [], 'campaigns': []}

    latest_status = {}
    for subject, history in status_history.items():
        eligible = [row for row in history if _localday(row[0]) <= end_date]
        if eligible:
            latest_status[subject] = eligible[-1][1]
    first_contact = {}
    for subject, history in contact_history.items():
        eligible = [when for when in history if _localday(when) <= end_date]
        if eligible:
            first_contact[subject] = min(eligible)

    lead_source = {subject: lead_source_of(dims)
                   for subject, dims in lead_dims.items()}
    acquisitions, excluded = split_payers(first_lead, first_purchase, lead_source)

    cohort = {subject for subject, when in first_lead.items()
              if start_date <= _localday(when) <= end_date
              and is_funnel_lead(lead_dims.get(subject, {}))}
    period_purchases = [(subject, when, value)
                        for subject, rows in purchases_by_subject.items()
                        for when, value in rows
                        if start_date <= _localday(when) <= end_date]
    new_customers = {subject for subject, when in acquisitions.items()
                     if start_date <= _localday(when) <= end_date}
    excluded_payers = {subject for subject, when in excluded.items()
                       if start_date <= _localday(when) <= end_date}
    converted_cohort = {subject for subject in cohort
                        if subject in acquisitions and
                        _localday(acquisitions[subject]) <= end_date}

    daily = defaultdict(lambda: {'leads': 0, 'new_customers': 0,
                                 'excluded_payers': 0, 'purchases': 0,
                                 'revenue_eur': Decimal(0), 'spend_eur': Decimal(0)})
    for subject in cohort:
        daily[_localday(first_lead[subject]).isoformat()]['leads'] += 1
    for subject in new_customers:
        daily[_localday(acquisitions[subject]).isoformat()]['new_customers'] += 1
    for subject in excluded_payers:
        daily[_localday(excluded[subject]).isoformat()]['excluded_payers'] += 1
    for _, when, value in period_purchases:
        row = daily[_localday(when).isoformat()]
        row['purchases'] += 1
        row['revenue_eur'] += value

    spend = Decimal(0)
    spend_by_campaign = defaultdict(Decimal)
    for data in AirbyteRecord.objects.filter(
            stream='fb_ads_insights').values_list('data', flat=True):
        day = str(data.get('date_start') or '')[:10]
        if not day or not start_date.isoformat() <= day <= end_date.isoformat():
            continue
        value = _number(data.get('spend'))
        daily[day]['spend_eur'] += value
        spend += value
        spend_by_campaign[data.get('campaign_name') or '(senza nome)'] += value

    for data in AirbyteRecord.objects.filter(
            stream='gads_campaign').values_list('data', flat=True):
        day = str(data.get('segments_date') or '')[:10]
        if not day or not start_date.isoformat() <= day <= end_date.isoformat():
            continue
        value = _number(data.get('metrics_cost_micros')) / Decimal(1_000_000)
        daily[day]['spend_eur'] += value
        spend += value
        spend_by_campaign[data.get('campaign_name') or '(senza nome)'] += value

    funnel = []
    previous = len(cohort)
    for rank, label in FUNNEL_STAGES:
        count = (len(cohort) if rank == 0 else sum(
            max(STATUS_RANK.get(latest_status.get(subject, 'new'), 0),
                5 if subject in converted_cohort else 0) >= rank
            for subject in cohort))
        funnel.append({'label': label, 'count': count,
                       'rate_from_previous': round(100 * count / previous, 1)
                       if previous else None})
        previous = count

    delays = []
    for subject in cohort:
        contacted = first_contact.get(subject)
        if contacted and contacted >= first_lead[subject]:
            delays.append((contacted - first_lead[subject]).total_seconds() / 3600)

    revenue = sum((value for _, _, value in period_purchases), Decimal(0))
    paying = {subject for subject, _, _ in period_purchases}
    repeat = sum(sum(_localday(when) <= end_date for when, _ in
                     purchases_by_subject[subject]) > 1
                 for subject in new_customers)
    eligible_90 = [subject for subject, when in first_purchase.items()
                   if _localday(when) + timedelta(days=90) <= end_date]
    revenue_90 = sum((value for subject in eligible_90
                      for when, value in purchases_by_subject[subject]
                      if when <= first_purchase[subject] + timedelta(days=90)), Decimal(0))
    ltv_90 = revenue_90 / len(eligible_90) if eligible_90 else None
    campaign_rows = defaultdict(lambda: {'leads': 0, 'customers': set(),
                                         'purchases': 0, 'revenue': Decimal(0)})
    for subject in cohort:
        campaign_rows[campaign_by_subject.get(subject, 'unattributed')]['leads'] += 1
    # Al numeratore del CAC di campagna vanno le acquisizioni, non tutti quelli
    # che hanno pagato: altrimenti il CAC di campagna direbbe una cosa e quello
    # blended un'altra.
    for subject in new_customers:
        campaign_rows[campaign_by_subject.get(subject, 'unattributed')][
            'customers'].add(subject)
    for subject, when, value in period_purchases:
        row = campaign_rows[campaign_by_subject.get(subject, 'unattributed')]
        row['purchases'] += 1
        row['revenue'] += value

    coverage = spend_coverage(start_date, end_date)
    lead_type_available = any('wundt.lead_type' in dims
                              for dims in lead_dims.values())
    cac_value = spend / len(new_customers) if new_customers else None
    payback_days = []
    if cac_value is not None:
        for subject in new_customers:
            running = Decimal(0)
            for when, value in purchases_by_subject[subject]:
                if _localday(when) > end_date:
                    break
                running += value
                if running >= cac_value:
                    payback_days.append((when - acquisitions[subject]).days)
                    break

    return {
        'empty': False, 'start': start_date.isoformat(), 'end': end_date.isoformat(),
        'kpis': {
            'leads': len(cohort), 'new_customers': len(new_customers),
            'excluded_payers': len(excluded_payers),
            'customers_in_cohort': len(converted_cohort),
            'conversion_rate': round(100 * len(converted_cohort) / len(cohort), 2)
            if cohort else None,
            'purchases': len(period_purchases), 'revenue_eur': _money(revenue),
            'spend_eur': _money(spend),
            # Il cancello della copertura: con la spesa incompleta oltre
            # tolleranza queste due non tornano un numero, tornano None, e la UI
            # scrive il motivo. Un CAC sottostimato è peggio di un CAC assente.
            'cac_eur': _money(cac_value)
            if cac_value is not None and not coverage['blocked'] else None,
            'average_realized_ltv_eur': _money(revenue / len(paying)) if paying else None,
            'mature_ltv_90_eur': _money(ltv_90) if ltv_90 is not None else None,
            'repeat_rate': round(100 * repeat / len(new_customers), 2)
            if new_customers else None,
            'median_cac_payback_days': round(median(payback_days), 1)
            if payback_days and not coverage['blocked'] else None,
            'median_first_contact_hours': round(median(delays), 1) if delays else None,
            'contacted_within_24h_rate': round(
                100 * sum(delay <= 24 for delay in delays) / len(cohort), 2)
            if cohort else None,
        },
        'daily': [{'date': day, **{key: _money(value) if isinstance(value, Decimal)
                                  else value for key, value in row.items()}}
                  for day, row in sorted(daily.items())],
        'funnel': funnel,
        'campaigns': sorted(({
            'campaign': campaign, 'leads': row['leads'],
            'customers': len(row['customers']), 'purchases': row['purchases'],
            'revenue_eur': _money(row['revenue']),
            'spend_eur': _money(spend_by_campaign.get(campaign, 0)),
            'cac_eur': _money(spend_by_campaign[campaign] / len(row['customers']))
            if row['customers'] and spend_by_campaign.get(campaign)
            and not coverage['blocked'] else None,
        } for campaign, row in campaign_rows.items()),
            key=lambda row: (-row['revenue_eur'], -row['leads'])),
        'spend_coverage': coverage,
        'definitions': {
            'lead_type_available': lead_type_available,
            'lead_type_note': '' if lead_type_available else (
                'tutor potenziali inclusi: gli eventi non portano ancora la '
                'dimensione wundt.lead_type'),
            'non_funnel_sources': list(NON_FUNNEL_SOURCES),
            'import_sources': list(IMPORT_SOURCES),
        },
        'coverage': {
            'lead_subjects': len(first_lead),
            'status_subjects': len(latest_status),
            'contact_subjects': len(first_contact),
            'spend_days': sum(bool(row['spend_eur']) for row in daily.values()),
            # Paganti di cui JAMES non ha il `lead_created`: sono la differenza
            # possibile fra questo numero e quello di WUNDT, quindi si vede.
            'payers_without_lead': sum(subject not in first_lead
                                       for subject in first_purchase),
        },
    }
