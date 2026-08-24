"""Aggregations over generic subject events; never returns row-level identities."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from .models import AirbyteRecord, SubjectEvent


def _number(value):
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _money(value):
    return round(float(value), 2)


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
    """WUNDT-parity metrics computed only from JAMES facts and Airbyte spend."""
    all_events = SubjectEvent.objects.filter(source__slug=source).order_by(
        'occurred_at', 'pk')
    first_lead, campaign_by_subject = {}, {}
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

    start_date = start or min((when.date() for when in first_lead.values()),
                              default=None)
    end_date = end or max(
        [when.date() for when in first_lead.values()] +
        [when.date() for when in first_purchase.values()], default=None)
    if isinstance(start_date, str):
        from datetime import date
        start_date = date.fromisoformat(start_date)
    if isinstance(end_date, str):
        from datetime import date
        end_date = date.fromisoformat(end_date)
    if not start_date or not end_date:
        return {'empty': True, 'daily': [], 'funnel': [], 'campaigns': []}

    latest_status = {}
    for subject, history in status_history.items():
        eligible = [row for row in history if row[0].date() <= end_date]
        if eligible:
            latest_status[subject] = eligible[-1][1]
    first_contact = {}
    for subject, history in contact_history.items():
        eligible = [when for when in history if when.date() <= end_date]
        if eligible:
            first_contact[subject] = min(eligible)

    cohort = {subject for subject, when in first_lead.items()
              if start_date <= when.date() <= end_date}
    period_purchases = [(subject, when, value)
                        for subject, rows in purchases_by_subject.items()
                        for when, value in rows
                        if start_date <= when.date() <= end_date]
    new_customers = {subject for subject, when in first_purchase.items()
                     if start_date <= when.date() <= end_date}
    converted_cohort = {subject for subject in cohort
                        if subject in first_purchase and
                        first_purchase[subject].date() <= end_date}

    daily = defaultdict(lambda: {'leads': 0, 'new_customers': 0, 'purchases': 0,
                                 'revenue_eur': Decimal(0), 'spend_eur': Decimal(0)})
    for subject in cohort:
        daily[first_lead[subject].date().isoformat()]['leads'] += 1
    for subject in new_customers:
        daily[first_purchase[subject].date().isoformat()]['new_customers'] += 1
    for _, when, value in period_purchases:
        row = daily[when.date().isoformat()]
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
    repeat = sum(sum(when.date() <= end_date for when, _ in
                     purchases_by_subject[subject]) > 1
                 for subject in new_customers)
    eligible_90 = [subject for subject, when in first_purchase.items()
                   if when.date() + timedelta(days=90) <= end_date]
    revenue_90 = sum((value for subject in eligible_90
                      for when, value in purchases_by_subject[subject]
                      if when <= first_purchase[subject] + timedelta(days=90)), Decimal(0))
    ltv_90 = revenue_90 / len(eligible_90) if eligible_90 else None
    campaign_rows = defaultdict(lambda: {'leads': 0, 'customers': set(),
                                         'purchases': 0, 'revenue': Decimal(0)})
    for subject in cohort:
        campaign_rows[campaign_by_subject.get(subject, 'unattributed')]['leads'] += 1
    for subject, when, value in period_purchases:
        campaign = campaign_by_subject.get(subject, 'unattributed')
        row = campaign_rows[campaign]
        row['customers'].add(subject)
        row['purchases'] += 1
        row['revenue'] += value

    cac_value = spend / len(new_customers) if new_customers else None
    payback_days = []
    if cac_value is not None:
        for subject in new_customers:
            running = Decimal(0)
            for when, value in purchases_by_subject[subject]:
                if when.date() > end_date:
                    break
                running += value
                if running >= cac_value:
                    payback_days.append((when - first_purchase[subject]).days)
                    break

    return {
        'empty': False, 'start': start_date.isoformat(), 'end': end_date.isoformat(),
        'kpis': {
            'leads': len(cohort), 'new_customers': len(new_customers),
            'customers_in_cohort': len(converted_cohort),
            'conversion_rate': round(100 * len(converted_cohort) / len(cohort), 2)
            if cohort else None,
            'purchases': len(period_purchases), 'revenue_eur': _money(revenue),
            'spend_eur': _money(spend),
            'cac_eur': _money(cac_value) if cac_value is not None else None,
            'average_realized_ltv_eur': _money(revenue / len(paying)) if paying else None,
            'mature_ltv_90_eur': _money(ltv_90) if ltv_90 is not None else None,
            'repeat_rate': round(100 * repeat / len(new_customers), 2)
            if new_customers else None,
            'median_cac_payback_days': round(median(payback_days), 1)
            if payback_days else None,
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
            if row['customers'] and spend_by_campaign.get(campaign) else None,
        } for campaign, row in campaign_rows.items()),
            key=lambda row: (-row['revenue_eur'], -row['leads'])),
        'coverage': {
            'lead_subjects': len(first_lead),
            'status_subjects': len(latest_status),
            'contact_subjects': len(first_contact),
            'spend_days': sum(bool(row['spend_eur']) for row in daily.values()),
        },
    }
