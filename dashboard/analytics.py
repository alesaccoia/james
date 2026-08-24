"""Aggregations over generic subject events; never returns row-level identities."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .models import SubjectEvent


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
    totals = defaultdict(int)
    revenue = Decimal(0)
    for event in events.iterator():
        totals[event.event_type] += 1
        day = event.occurred_at.date().isoformat()
        if event.event_type == 'lead_created':
            daily[day]['leads'] += 1
            lead_campaign = (event.dimensions.get('marketing.campaign_id') or
                             event.dimensions.get('marketing.utm_campaign') or
                             'unattributed')
            lead_campaigns[lead_campaign] += 1
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
