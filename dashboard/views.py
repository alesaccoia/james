import json
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from .models import AirbyteRecord, FunnelStage, MarketingEvent

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


# -------------------------------------------------------------- Meta organic
# Facebook Page + Instagram Business Account organic content, via the
# 'source-facebook-pages' / 'source-instagram-organic' Airbyte connectors
# (see docs/airbyte-*-connector/README.md in wundt).

FB_PAGE_METRIC_LABELS = {
    'page_total_actions': 'Azioni sulla pagina',
    'page_post_engagements': 'Interazioni sui post',
    'page_fan_adds_by_paid_non_paid_unique': 'Nuovi follower (netti)',
    'page_media_view': 'Visualizzazioni media',
    'page_views_total': 'Visite alla pagina',
    'page_video_views': 'Visualizzazioni video',
}

IG_METRIC_LABELS = {
    'reach': 'Reach',
    'total_interactions': 'Interazioni totali',
    'likes': 'Mi piace',
    'comments': 'Commenti',
    'saved': 'Salvataggi',
    'shares': 'Condivisioni',
}


def _meta_insight_value(v):
    """A Meta insights 'values' entry's value is usually a number, but some
    metrics (e.g. post_reactions_by_type_total) return an object of
    {reaction_type: count} instead - summed into one total either way."""
    val = v.get('value')
    if isinstance(val, dict):
        return sum(_num(x) for x in val.values())
    return _num(val)


def _meta_insight_rows(stream):
    """Flatten a Meta page/account-level insights stream (one row per
    metric, each holding a 'values' time series) into flat
    {metric, date, value} rows."""
    rows = []
    for d in _stream_rows(stream):
        metric = d.get('name')
        for v in (d.get('values') or []):
            date = str(v.get('end_time') or '')[:10]
            if not date:
                continue
            rows.append({'metric': metric, 'date': date, 'value': _meta_insight_value(v)})
    return rows


def _meta_parent_id_from_insight_id(insight_id):
    """post_insights/ig_media_insights ids look like '{post_or_media_id}/insights/{metric}/{period}'."""
    return (insight_id or '').split('/insights/')[0]


def _facebook_posts():
    """One row per Facebook post, with its post_insights metrics summed in
    (post_insights is per-metric, so several raw rows collapse into one)."""
    metrics_by_post = {}
    for d in _stream_rows('mentor_meta_pages_post_insights'):
        post_id = _meta_parent_id_from_insight_id(d.get('id'))
        metric = d.get('name')
        total = sum(_meta_insight_value(v) for v in (d.get('values') or []))
        metrics_by_post.setdefault(post_id, {})[metric] = metrics_by_post.setdefault(post_id, {}).get(metric, 0) + total

    posts = []
    for d in _stream_rows('mentor_meta_pages_post'):
        pid = d.get('id')
        date = str(d.get('created_time') or '')[:10]
        if not date:
            continue
        m = metrics_by_post.get(pid, {})
        shares = _num((d.get('shares') or {}).get('count'))
        clicks = m.get('post_clicks', 0)
        reactions = m.get('post_reactions_by_type_total', 0)
        permalink = d.get('permalink_url')
        # Facebook's post object has no dedicated Reel/Post field - the
        # permalink path is the only reliable signal (/reel/... vs /posts/...).
        post_type = 'Reel' if permalink and '/reel/' in permalink else 'Post'
        posts.append({
            'id': pid,
            'date': date,
            'text': (d.get('message') or d.get('story') or '')[:280],
            'permalink': permalink,
            'type': post_type,
            'shares': shares,
            'media_view': m.get('post_media_view', 0),
            'clicks': clicks,
            'reactions': reactions,
            'engagement': shares + clicks + reactions,
            'reach': None,  # Meta has no page/post-level reach metric left for Facebook Pages
        })
    posts.sort(key=lambda p: p['date'], reverse=True)
    return posts


def _instagram_posts():
    """One row per Instagram media item, with its ig_media_insights metrics
    summed in (insights is per-metric, so several raw rows collapse into one)."""
    metrics_by_media = {}
    for d in _stream_rows('mentor_ig_page_ig_media_insights'):
        media_id = _meta_parent_id_from_insight_id(d.get('id'))
        metric = d.get('name')
        total = sum(_meta_insight_value(v) for v in (d.get('values') or []))
        metrics_by_media.setdefault(media_id, {})[metric] = metrics_by_media.setdefault(media_id, {}).get(metric, 0) + total

    posts = []
    for d in _stream_rows('mentor_ig_page_ig_media'):
        mid = d.get('id')
        date = str(d.get('timestamp') or '')[:10]
        if not date:
            continue
        m = metrics_by_media.get(mid, {})
        posts.append({
            'id': mid,
            'date': date,
            'text': (d.get('caption') or '')[:280],
            'permalink': d.get('permalink'),
            'type': d.get('media_product_type') or d.get('media_type') or 'post',
            'thumbnail': d.get('thumbnail_url') or d.get('media_url'),
            'reach': m.get('reach', 0),
            'likes': m.get('likes', d.get('like_count') or 0),
            'comments': m.get('comments', d.get('comments_count') or 0),
            'saved': m.get('saved', 0),
            'shares': m.get('shares', 0),
            'engagement': m.get('total_interactions', 0),
        })
    posts.sort(key=lambda p: p['date'], reverse=True)
    return posts


@login_required
def facebook_page(request):
    streams = ['mentor_meta_pages_page', 'mentor_meta_pages_post',
               'mentor_meta_pages_post_insights', 'mentor_meta_pages_page_insights']
    cfg = {'data_url': reverse('dashboard:data_facebook'), 'metric_labels': FB_PAGE_METRIC_LABELS}
    return render(request, 'dashboard/facebook.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream__in=streams).exists(),
    })


@login_required
def data_facebook(request):
    page_rows = list(_stream_rows('mentor_meta_pages_page'))
    page = page_rows[0] if page_rows else None
    page_profile = None
    if page:
        page_profile = {
            'name': page.get('name'),
            'fan_count': _num(page.get('fan_count')),
            'followers_count': _num(page.get('followers_count')),
            'category': page.get('category'),
            'link': page.get('link'),
        }
    return JsonResponse({
        'page': page_profile,
        'page_insights': _meta_insight_rows('mentor_meta_pages_page_insights'),
        'posts': _facebook_posts(),
    })


@login_required
def instagram_page(request):
    streams = ['mentor_ig_page_ig_media', 'mentor_ig_page_ig_media_insights']
    cfg = {'data_url': reverse('dashboard:data_instagram'), 'metric_labels': IG_METRIC_LABELS}
    return render(request, 'dashboard/instagram.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream__in=streams).exists(),
    })


@login_required
def data_instagram(request):
    return JsonResponse({'posts': _instagram_posts()})


@login_required
def meta_posts(request):
    streams = ['mentor_meta_pages_post', 'mentor_ig_page_ig_media']
    cfg = {'data_url': reverse('dashboard:data_meta_posts')}
    return render(request, 'dashboard/meta_posts.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream__in=streams).exists(),
    })


@login_required
def data_meta_posts(request):
    rows = [{**p, 'platform': 'Facebook'} for p in _facebook_posts()] + \
           [{**p, 'platform': 'Instagram'} for p in _instagram_posts()]
    rows.sort(key=lambda r: r['date'], reverse=True)
    return JsonResponse({'posts': rows})


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

    # Note: GA4's age/gender demographic reports require Google Signals to be
    # enabled on the property — without it the API returns nothing for them
    # (and Airbyte burns through rate limit retries for no data), so those
    # two streams are deliberately not synced. Country/city still work fine.
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
        'events': events, 'pages': pages, 'sources': sources, 'geo': geo,
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


# --------------------------------------------------------- Marketing events

@login_required
def events(request):
    if request.method == 'POST':
        event_id = (request.POST.get('event_id') or '').strip()
        name = (request.POST.get('name') or '').strip()
        date = (request.POST.get('date') or '').strip()
        scope = (request.POST.get('scope') or '').strip()
        notes = (request.POST.get('notes') or '').strip()
        if not name or not date:
            messages.error(request, 'Nome e data sono obbligatori.')
            return redirect(f'{reverse("dashboard:events")}?modifica={event_id}' if event_id else 'dashboard:events')
        if event_id:
            updated = MarketingEvent.objects.filter(pk=event_id).update(
                name=name, date=date, scope=scope, notes=notes)
            messages.success(request, f'Evento "{name}" aggiornato.' if updated else 'Evento non trovato.')
        else:
            MarketingEvent.objects.create(name=name, date=date, scope=scope, notes=notes)
            messages.success(request, f'Evento "{name}" aggiunto.')
        return redirect('dashboard:events')

    edit_event = None
    edit_id = request.GET.get('modifica')
    if edit_id:
        edit_event = MarketingEvent.objects.filter(pk=edit_id).first()

    return render(request, 'dashboard/events.html', {
        'events': MarketingEvent.objects.all()[:200],
        'edit_event': edit_event,
    })


@login_required
def event_delete(request, pk):
    if request.method == 'POST':
        event = MarketingEvent.objects.filter(pk=pk).first()
        if event:
            name = event.name
            event.delete()
            messages.success(request, f'Evento "{name}" eliminato.')
    return redirect('dashboard:events')


# --------------------------------------------------------------- Funnel

def _client_acquired_series():
    """Daily count of leads that reached client_acquired, from the wundt
    lead status event log (mentor_lead_status_events, via Airbyte)."""
    totals = defaultdict(int)
    for data in _stream_rows('mentor_lead_status_events'):
        if data.get('to_status') != 'client_acquired':
            continue
        date = str(data.get('created_at') or '')[:10]
        if not date:
            continue
        totals[date] += 1
    return [{'date': d, 'value': v} for d, v in sorted(totals.items())]


# FunnelStageSource.kind -> the matching id/name fields on fb_ads_insights.
FB_INSIGHT_ID_FIELD = {'campaign': 'campaign_id', 'ad_set': 'adset_id', 'ad': 'ad_id'}
FB_INSIGHT_NAME_FIELD = {'campaign': 'campaign_name', 'ad_set': 'adset_name', 'ad': 'ad_name'}


def _fb_insight_rows_for_source(source, insight_rows):
    """Rows from a pre-fetched fb_ads_insights list matching one
    FunnelStageSource — by Meta id when linked (exact), else by name
    (fallback for sources set up before external_id existed)."""
    id_field = FB_INSIGHT_ID_FIELD.get(source.kind)
    if not id_field:
        return []
    if source.external_id:
        return [d for d in insight_rows if str(d.get(id_field) or '') == source.external_id]
    name_field = FB_INSIGHT_NAME_FIELD[source.kind]
    return [d for d in insight_rows if d.get(name_field) == source.name]


def _stage_campaign_rows(stage, insight_rows):
    """Flat per-source, per-day rows (source name/kind/id, date, spend,
    leads) for every Meta campaign/ad set/ad tagged onto this stage. Raw
    material for both the computed KPIs below and the per-campaign
    breakdown table — left unaggregated so the frontend can bucket/filter
    it by period and by day/week/month itself, like the Meta Ads page."""
    by_key = {}
    for source in stage.sources.all():
        for d in _fb_insight_rows_for_source(source, insight_rows):
            date = str(d.get('date_start') or '')[:10]
            if not date:
                continue
            key = (source.pk, date)
            row = by_key.setdefault(key, {'source': source.name, 'kind': source.kind,
                                          'external_id': source.external_id, 'date': date,
                                          'spend': 0.0, 'leads': 0.0})
            row['spend'] += _num(d.get('spend'))
            row['leads'] += _fb_action_value(d, 'lead')
    return sorted(by_key.values(), key=lambda r: (r['date'], r['source']))


def _fb_children_rows(insight_rows, parent_field, parent_id, child_id_field, child_name_field):
    """Per-day rows for each child entity found in fb_ads_insights under a
    Meta parent id (ad sets under a campaign, or ads under an ad set) —
    dynamic, doesn't require an explicit FunnelStageSource per child. This
    is what powers "drill down to ad set / ad level" without having to
    attach every single one by hand."""
    by_key = {}
    for d in insight_rows:
        if str(d.get(parent_field) or '') != parent_id:
            continue
        date = str(d.get('date_start') or '')[:10]
        cid = str(d.get(child_id_field) or '')
        if not date or not cid:
            continue
        key = (cid, date)
        row = by_key.setdefault(key, {'source': d.get(child_name_field) or cid,
                                      'external_id': cid, 'date': date,
                                      'spend': 0.0, 'leads': 0.0})
        row['spend'] += _num(d.get('spend'))
        row['leads'] += _fb_action_value(d, 'lead')
    return sorted(by_key.values(), key=lambda r: (r['date'], r['source']))


def _stage_drilldown(stage, insight_rows):
    """{meta_id: [child rows]} - ad sets under each campaign attached to
    this stage, and ads under each of those ad sets (whether the ad set
    was explicitly attached or just discovered under an attached
    campaign). Keyed by Meta id so the frontend can expand any row in the
    breakdown table one level deeper, recursively."""
    drilldown = {}
    for source in stage.sources.all():
        if source.kind == 'campaign' and source.external_id:
            drilldown[source.external_id] = _fb_children_rows(
                insight_rows, 'campaign_id', source.external_id, 'adset_id', 'adset_name')
        elif source.kind == 'ad_set' and source.external_id:
            drilldown[source.external_id] = _fb_children_rows(
                insight_rows, 'adset_id', source.external_id, 'ad_id', 'ad_name')
    for rows in list(drilldown.values()):
        for r in rows:
            if r['external_id'] not in drilldown:
                drilldown[r['external_id']] = _fb_children_rows(
                    insight_rows, 'adset_id', r['external_id'], 'ad_id', 'ad_name')
    return drilldown


def _leads_series_from_rows(campaign_rows):
    totals = defaultdict(float)
    for r in campaign_rows:
        totals[r['date']] += r['leads']
    return [{'date': d, 'value': v} for d, v in sorted(totals.items())]


def _cpl_series_from_rows(campaign_rows):
    spend, leads = defaultdict(float), defaultdict(float)
    for r in campaign_rows:
        spend[r['date']] += r['spend']
        leads[r['date']] += r['leads']
    return [{'date': d, 'value': spend[d] / leads[d]} for d in sorted(spend) if leads[d]]


# Funnel KPIs computed from real Airbyte data instead of manual entry, keyed
# by KPI name (matched within whatever stage it's defined on). Add an entry
# here whenever another KPI gets a real data source wired up. Not every KPI
# can be computed this way yet - e.g. "Tasso di contatto" and "Booking rate"
# would need per-lead campaign attribution on the wundt side, which isn't
# captured today (lead.extra is empty), so those stay manual.
FUNNEL_COMPUTED_KPIS = {
    'Clienti nuovi paganti': lambda campaign_rows: _client_acquired_series(),
    'Lead validi': _leads_series_from_rows,
    'CPL': _cpl_series_from_rows,
}


@login_required
def funnel(request):
    cfg = {'data_url': reverse('dashboard:data_funnel')}
    return render(request, 'dashboard/funnel.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': FunnelStage.objects.exists(),
    })


@login_required
def data_funnel(request):
    insight_rows = list(_stream_rows('fb_ads_insights'))
    stages = []
    for stage in FunnelStage.objects.filter(is_active=True).prefetch_related('kpis__values', 'sources'):
        campaign_rows = _stage_campaign_rows(stage, insight_rows)
        drilldown = _stage_drilldown(stage, insight_rows)
        kpis = []
        for kpi in stage.kpis.filter(is_active=True):
            computed_fn = FUNNEL_COMPUTED_KPIS.get(kpi.name)
            if computed_fn:
                series, computed = computed_fn(campaign_rows), True
            else:
                series = [{'date': v.date.isoformat(), 'value': v.value, 'note': v.note}
                         for v in sorted(kpi.values.all(), key=lambda v: v.date)]
                computed = False
            kpis.append({
                'id': kpi.pk,
                'name': kpi.name,
                'unit': kpi.unit,
                'target_value': kpi.target_value,
                'computed': computed,
                'series': series,
            })
        stages.append({
            'id': stage.pk,
            'name': stage.name,
            'slug': stage.slug,
            'description': stage.description,
            'kpis': kpis,
            'sources': [{'kind': s.kind, 'kind_label': s.get_kind_display(),
                        'name': s.name, 'external_id': s.external_id}
                       for s in stage.sources.all()],
            'campaign_rows': campaign_rows,
            'drilldown': drilldown,
        })
    return JsonResponse({'stages': stages})


@login_required
def data_events(request):
    rows = [{
        'name': e.name,
        'date': e.date.isoformat(),
        'scope': e.scope,
        'notes': e.notes,
    } for e in MarketingEvent.objects.all()]
    return JsonResponse({'events': rows})
