import json
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse

from .models import AirbyteRecord

# One entry per Airbyte stream we know how to turn into marketing KPIs.
# Add a new entry here whenever a new source (Google Ads, TikTok Ads,
# LinkedIn Ads...) starts flowing into AirbyteRecord — the dashboard picks
# it up automatically, grouped together with everything else.
CHANNELS = {
    'fb_ads_insights': {
        'label': 'Meta Ads',
        'date': 'date_start',
        'campaign': 'campaign_name',
        'spend': 'spend',
        'impressions': 'impressions',
        'clicks': 'clicks',
    },
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fb_action_value(data, action_type):
    """Meta's ads_insights 'actions' field is a list of {action_type, value}
    dicts — Instant Form leads show up there as action_type='lead', not as
    its own column/stream."""
    for a in (data.get('actions') or []):
        if a.get('action_type') == action_type:
            return _num(a.get('value'))
    return 0.0


def _ga_date(v):
    """GA4 dates come back as 'YYYYMMDD' strings."""
    v = str(v or '')
    if len(v) == 8 and v.isdigit():
        return f'{v[:4]}-{v[4:6]}-{v[6:]}'
    return v[:10]


def _stream_rows(stream):
    return AirbyteRecord.objects.filter(stream=stream).values_list('data', flat=True)


# --------------------------------------------------------------- Meta Ads

@login_required
def dashboard(request):
    cfg = {'data_url': reverse('dashboard:data_marketing')}
    return render(request, 'dashboard/dashboard.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream__in=CHANNELS.keys()).exists(),
    })


@login_required
def data_marketing(request):
    rows = []
    for stream, cfg in CHANNELS.items():
        for data in _stream_rows(stream):
            date = (data.get(cfg['date']) or '')[:10]
            if not date:
                continue
            rows.append({
                'date': date,
                'channel': cfg['label'],
                'campaign': data.get(cfg['campaign']) or '(senza nome)',
                'spend': _num(data.get(cfg['spend'])),
                'impressions': _num(data.get(cfg['impressions'])),
                'clicks': _num(data.get(cfg['clicks'])),
                'leads': _fb_action_value(data, 'lead'),
            })
    return JsonResponse({'rows': rows})


# ------------------------------------------------------------ Google Analytics

@login_required
def ga4(request):
    cfg = {'data_url': reverse('dashboard:data_ga4')}
    ga4_streams = ['ga_website_overview', 'ga_traffic_acquisition_session_default_channel_grouping_report']
    return render(request, 'dashboard/ga4.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream__in=ga4_streams).exists(),
    })


@login_required
def data_ga4(request):
    overview = []
    for d in _stream_rows('ga_website_overview'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        overview.append({
            'date': date,
            'sessions': _num(d.get('sessions')),
            'totalUsers': _num(d.get('totalUsers')),
            'newUsers': _num(d.get('newUsers')),
            'screenPageViews': _num(d.get('screenPageViews')),
            'bounceRate': _num(d.get('bounceRate')) * 100,
            'avgSessionDuration': _num(d.get('averageSessionDuration')),
        })

    channels = []
    for d in _stream_rows('ga_traffic_acquisition_session_default_channel_grouping_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        channels.append({
            'date': date,
            'channel': d.get('sessionDefaultChannelGrouping') or '(non impostato)',
            'sessions': _num(d.get('sessions')),
            'totalUsers': _num(d.get('totalUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })

    campaigns = []
    for d in _stream_rows('ga_traffic_acquisition_session_campaign_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        campaigns.append({
            'date': date,
            'campaign': d.get('sessionCampaignName') or '(non impostata)',
            'sessions': _num(d.get('sessions')),
            'totalUsers': _num(d.get('totalUsers')),
        })

    events = []
    for d in _stream_rows('ga_conversions_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        events.append({
            'date': date,
            'event': d.get('eventName') or '(sconosciuto)',
            'totalUsers': _num(d.get('totalUsers')),
        })

    pages = []
    for d in _stream_rows('ga_pages_path_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        pages.append({
            'date': date,
            'path': d.get('pagePath') or '/',
            'screenPageViews': _num(d.get('screenPageViews')),
            'totalUsers': _num(d.get('totalUsers')),
            'eventCount': _num(d.get('eventCount')),
            'engagementDuration': _num(d.get('userEngagementDuration')),
        })

    sources = []
    for d in _stream_rows('ga_user_acquisition_first_user_source_medium_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        sources.append({
            'date': date,
            'source': d.get('firstUserSource') or '(direct)',
            'medium': d.get('firstUserMedium') or '(none)',
            'newUsers': _num(d.get('newUsers')),
            'totalUsers': _num(d.get('totalUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })

    age = []
    for d in _stream_rows('ga_demographic_age_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        age.append({
            'date': date,
            'bracket': d.get('userAgeBracket') or '(sconosciuta)',
            'totalUsers': _num(d.get('totalUsers')),
            'newUsers': _num(d.get('newUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })

    gender = []
    for d in _stream_rows('ga_demographic_gender_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        gender.append({
            'date': date,
            'gender': d.get('userGender') or '(sconosciuto)',
            'totalUsers': _num(d.get('totalUsers')),
            'newUsers': _num(d.get('newUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })

    geo = []
    for d in _stream_rows('ga_demographic_country_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        geo.append({
            'date': date,
            'country': d.get('country') or '(sconosciuto)',
            'city': None,
            'totalUsers': _num(d.get('totalUsers')),
            'newUsers': _num(d.get('newUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })
    for d in _stream_rows('ga_demographic_city_report'):
        date = _ga_date(d.get('date'))
        if not date:
            continue
        geo.append({
            'date': date,
            'country': None,
            'city': d.get('city') or '(sconosciuta)',
            'totalUsers': _num(d.get('totalUsers')),
            'newUsers': _num(d.get('newUsers')),
            'engagementRate': _num(d.get('engagementRate')) * 100,
        })

    return JsonResponse({
        'overview': overview, 'channels': channels, 'campaigns': campaigns,
        'events': events, 'pages': pages, 'sources': sources,
        'age': age, 'gender': gender, 'geo': geo,
    })


# --------------------------------------------------- Cross-source comparison

def _daily_series(stream, date_field, value_field, ga_dates=False, agg='sum'):
    totals = defaultdict(float)
    counts = defaultdict(int)
    for d in _stream_rows(stream):
        raw_date = d.get(date_field)
        date = _ga_date(raw_date) if ga_dates else (str(raw_date or '')[:10])
        if not date:
            continue
        totals[date] += _num(d.get(value_field))
        counts[date] += 1
    if agg == 'avg':
        return [{'date': d, 'value': totals[d] / counts[d]} for d in sorted(totals) if counts[d]]
    return [{'date': d, 'value': totals[d]} for d in sorted(totals)]


def _fb_lead_series():
    totals = defaultdict(float)
    for d in _stream_rows('fb_ads_insights'):
        date = str(d.get('date_start') or '')[:10]
        if not date:
            continue
        totals[date] += _fb_action_value(d, 'lead')
    return [{'date': d, 'value': totals[d]} for d in sorted(totals)]


def _event_series(event_name):
    totals = defaultdict(float)
    for d in _stream_rows('ga_conversions_report'):
        if d.get('eventName') != event_name:
            continue
        date = _ga_date(d.get('date'))
        if not date:
            continue
        totals[date] += _num(d.get('totalUsers'))
    return [{'date': d, 'value': totals[d]} for d in sorted(totals)]


# Registry of comparable metrics across every connected source. Each entry
# knows how to build its own daily {date, value} series; the compare view
# just lets the user pick any combination to overlay on one chart. Add an
# entry here whenever a new source/metric should be selectable for
# cross-channel comparison.
def _build_metrics():
    return {
        'fb_spend': {'label': 'Spesa Meta Ads', 'source': 'Meta Ads', 'unit': 'eur',
                     'series': _daily_series('fb_ads_insights', 'date_start', 'spend')},
        'fb_impressions': {'label': 'Impression Meta Ads', 'source': 'Meta Ads', 'unit': 'count',
                            'series': _daily_series('fb_ads_insights', 'date_start', 'impressions')},
        'fb_clicks': {'label': 'Click Meta Ads', 'source': 'Meta Ads', 'unit': 'count',
                      'series': _daily_series('fb_ads_insights', 'date_start', 'clicks')},
        'fb_leads': {'label': 'Lead Meta Ads (Instant Form)', 'source': 'Meta Ads', 'unit': 'count',
                     'series': _fb_lead_series()},
        'ga_sessions': {'label': 'Sessioni sito', 'source': 'Google Analytics', 'unit': 'count',
                        'series': _daily_series('ga_website_overview', 'date', 'sessions', ga_dates=True)},
        'ga_users': {'label': 'Utenti totali', 'source': 'Google Analytics', 'unit': 'count',
                     'series': _daily_series('ga_website_overview', 'date', 'totalUsers', ga_dates=True)},
        'ga_new_users': {'label': 'Nuovi utenti', 'source': 'Google Analytics', 'unit': 'count',
                         'series': _daily_series('ga_website_overview', 'date', 'newUsers', ga_dates=True)},
        'ga_pageviews': {'label': 'Pageview', 'source': 'Google Analytics', 'unit': 'count',
                         'series': _daily_series('ga_website_overview', 'date', 'screenPageViews', ga_dates=True)},
        'ga_leads': {'label': 'Lead generati (GA4)', 'source': 'Google Analytics', 'unit': 'count',
                     'series': _event_series('generate_lead')},
    }


@login_required
def compare(request):
    metrics = _build_metrics()
    cfg = {
        'data_url': reverse('dashboard:data_compare'),
        'metrics': {k: {'label': v['label'], 'source': v['source'], 'unit': v['unit']}
                    for k, v in metrics.items()},
    }
    return render(request, 'dashboard/compare.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': any(m['series'] for m in metrics.values()),
    })


@login_required
def data_compare(request):
    metrics = _build_metrics()
    return JsonResponse({'metrics': metrics})
