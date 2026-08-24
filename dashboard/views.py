import json
from collections import defaultdict
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .analytics import commercial_metrics, performance_metrics
from .models import (AirbyteRecord, BudgetLine, BudgetPlan, ChannelCadence,
                     ComparePreset, ContentPiece, FunnelKPI, FunnelStage,
                     FunnelStageSource, MarketingEvent, SubjectEvent, Tag, TagDimension,
                     TaggedEntity)


@login_required
def data_commercial_metrics(request):
    return JsonResponse(commercial_metrics(
        source=request.GET.get('source') or None,
        start=request.GET.get('start') or None,
        end=request.GET.get('end') or None,
        dormant_days=max(1, int(request.GET.get('dormant_days') or 60))))


@login_required
def commercial(request):
    return render(request, 'dashboard/commercial.html')


@login_required
def conversions(request):
    return render(request, 'dashboard/conversions.html')


@login_required
def data_performance_metrics(request):
    return JsonResponse(performance_metrics(
        source=request.GET.get('source') or 'wundt',
        start=request.GET.get('start') or None,
        end=request.GET.get('end') or None))

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


def _google_ads_series(value_field, divisor=1):
    totals = defaultdict(float)
    for d in _stream_rows('gads_campaign'):
        date_s = str(d.get('segments_date') or '')[:10]
        if date_s:
            totals[date_s] += _num(d.get(value_field)) / divisor
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
METRIC_INFO = {
    'fb_spend': ('Spesa Meta Ads', 'Meta Ads', 'eur'),
    'fb_impressions': ('Impression Meta Ads', 'Meta Ads', 'count'),
    'fb_clicks': ('Click Meta Ads', 'Meta Ads', 'count'),
    'fb_leads': ('Lead Meta Ads (Instant Form)', 'Meta Ads', 'count'),
    'gads_spend': ('Spesa Google Ads', 'Google Ads', 'eur'),
    'gads_impressions': ('Impression Google Ads', 'Google Ads', 'count'),
    'gads_clicks': ('Click Google Ads', 'Google Ads', 'count'),
    'gads_conversions': ('Conversioni Google Ads', 'Google Ads', 'count'),
    'ga_sessions': ('Sessioni sito', 'Google Analytics', 'count'),
    'ga_users': ('Utenti totali', 'Google Analytics', 'count'),
    'ga_new_users': ('Nuovi utenti', 'Google Analytics', 'count'),
    'ga_pageviews': ('Pageview', 'Google Analytics', 'count'),
    'ga_leads': ('Lead generati (GA4)', 'Google Analytics', 'count'),
    'crm_leads': ('Lead CRM', 'WUNDT CRM', 'count'),
    'crm_new_customers': ('Nuovi clienti', 'WUNDT CRM', 'count'),
    'crm_revenue': ('Incassi', 'WUNDT CRM', 'eur'),
}


def _build_metrics():
    crm = performance_metrics()
    crm_daily = crm.get('daily', [])
    series = {
        'fb_spend': _daily_series('fb_ads_insights', 'date_start', 'spend'),
        'fb_impressions': _daily_series('fb_ads_insights', 'date_start', 'impressions'),
        'fb_clicks': _daily_series('fb_ads_insights', 'date_start', 'clicks'),
        'fb_leads': _fb_lead_series(),
        'gads_spend': _google_ads_series('metrics_cost_micros', 1_000_000),
        'gads_impressions': _google_ads_series('metrics_impressions'),
        'gads_clicks': _google_ads_series('metrics_clicks'),
        'gads_conversions': _google_ads_series('metrics_conversions'),
        'ga_sessions': _daily_series('ga_website_overview', 'date', 'sessions', ga_dates=True),
        'ga_users': _daily_series('ga_website_overview', 'date', 'totalUsers', ga_dates=True),
        'ga_new_users': _daily_series('ga_website_overview', 'date', 'newUsers', ga_dates=True),
        'ga_pageviews': _daily_series('ga_website_overview', 'date', 'screenPageViews', ga_dates=True),
        'ga_leads': _event_series('generate_lead'),
        'crm_leads': [{'date': d['date'], 'value': d['leads']} for d in crm_daily],
        'crm_new_customers': [{'date': d['date'], 'value': d['new_customers']}
                              for d in crm_daily],
        'crm_revenue': [{'date': d['date'], 'value': d['revenue_eur']} for d in crm_daily],
    }
    return {key: {'label': info[0], 'source': info[1], 'unit': info[2],
                  'series': series[key]} for key, info in METRIC_INFO.items()}


@login_required
def help_page(request):
    return render(request, 'dashboard/help.html')


@login_required
def dashboard_redirect(request):
    return redirect('dashboard:compare')


@login_required
def compare(request):
    cfg = {
        'data_url': reverse('dashboard:data_compare'),
        'home_url': reverse('dashboard:data_home'),
        'calendario_url': reverse('dashboard:calendario'),
        'metrics': {key: {'label': info[0], 'source': info[1], 'unit': info[2]}
                    for key, info in METRIC_INFO.items()},
    }
    return render(request, 'dashboard/compare.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.exists() or SubjectEvent.objects.exists(),
    })


@login_required
def data_home(request):
    """The always-on homepage blocks: site analytics, per-campaign daily
    delivery, CPL, and where the editorial calendar stands right now."""
    today = timezone.now().date()
    today_s = today.isoformat()
    horizon = (today + timedelta(days=14)).isoformat()

    # --- GA4 sessions/users, daily
    analytics = []
    for d in _stream_rows('ga_website_overview'):
        date_s = _ga_date(d.get('date'))
        if not date_s:
            continue
        analytics.append({'date': date_s, 'sessions': _num(d.get('sessions')),
                          'users': _num(d.get('totalUsers'))})
    analytics.sort(key=lambda r: r['date'])

    # --- per-campaign daily spend/leads, plus a blended daily CPL
    per_campaign = defaultdict(lambda: defaultdict(lambda: {'spend': 0.0, 'leads': 0.0}))
    daily = defaultdict(lambda: {'spend': 0.0, 'leads': 0.0, 'impressions': 0.0})
    for d in _stream_rows('fb_ads_insights'):
        date_s = str(d.get('date_start') or '')[:10]
        if not date_s:
            continue
        name = d.get('campaign_name') or '(senza nome)'
        spend, leads = _num(d.get('spend')), _fb_action_value(d, 'lead')
        per_campaign[name][date_s]['spend'] += spend
        per_campaign[name][date_s]['leads'] += leads
        daily[date_s]['spend'] += spend
        daily[date_s]['leads'] += leads
        daily[date_s]['impressions'] += _num(d.get('impressions'))

    for d in _stream_rows('gads_campaign'):
        date_s = str(d.get('segments_date') or '')[:10]
        if not date_s:
            continue
        name = f"Google · {d.get('campaign_name') or '(senza nome)'}"
        spend = _num(d.get('metrics_cost_micros')) / 1_000_000
        conversions = _num(d.get('metrics_conversions'))
        per_campaign[name][date_s]['spend'] += spend
        per_campaign[name][date_s]['leads'] += conversions
        daily[date_s]['spend'] += spend
        daily[date_s]['leads'] += conversions
        daily[date_s]['impressions'] += _num(d.get('metrics_impressions'))

    campaigns = [{
        'name': name,
        'total_spend': sum(v['spend'] for v in days.values()),
        'series': [{'date': dt, **vals} for dt, vals in sorted(days.items())],
    } for name, days in per_campaign.items()]
    campaigns.sort(key=lambda c: -c['total_spend'])

    # CPL carries spend and leads so any bucketing stays a weighted ratio.
    cpl = [{'date': dt, 'spend': v['spend'], 'leads': v['leads'],
            'value': (v['spend'] / v['leads']) if v['leads'] else None}
           for dt, v in sorted(daily.items())]

    # --- editorial calendar: what's coming and what's being worked on
    def _piece(p):
        return {'id': p.pk, 'title': p.title, 'channel': p.channel,
                'format': p.get_content_format_display(), 'status': p.status,
                'status_label': p.get_status_display(), 'owner': p.owner,
                'date': p.effective_date.isoformat(),
                'stage': p.stage.name if p.stage else None,
                'tags': [{'name': t.name, 'color': t.color or None} for t in p.tags.all()]}

    qs = ContentPiece.objects.select_related('stage').prefetch_related('tags')
    # The three buckets are deliberately mutually exclusive, in priority order,
    # so nothing shows up twice and each column is a distinct call to action:
    #   in ritardo    -> doveva uscire e non è uscito
    #   in lavorazione-> ci si sta lavorando, data ancora davanti
    #   in arrivo     -> pianificato a breve ma non ancora iniziato
    active = qs.exclude(status__in=['pubblicato', 'archiviato'])
    late = [_piece(p) for p in active.filter(planned_date__lt=today_s).order_by('planned_date')[:12]]
    future = active.filter(planned_date__gte=today_s)
    in_progress = [_piece(p) for p in future.filter(status__in=['brief', 'produzione', 'pronto'])
                   .order_by('planned_date')[:12]]
    upcoming = [_piece(p) for p in future.filter(status='idea', planned_date__lte=horizon)
                .order_by('planned_date')[:12]]

    return JsonResponse({
        'analytics': analytics,
        'campaigns': campaigns[:12],
        'cpl': cpl,
        'calendar': {'upcoming': upcoming, 'in_progress': in_progress, 'late': late,
                     'total': qs.count()},
    })


@login_required
def data_compare(request):
    metrics = _build_metrics()
    return JsonResponse({'metrics': metrics})


@login_required
def compare_presets(request):
    """List, save and delete Confronto presets.

    GET    -> every preset
    POST   -> {name, config} creates or overwrites by name
    DELETE -> ?name=... removes one
    """
    if request.method == 'POST':
        payload = json.loads(request.body or '{}')
        name = (payload.get('name') or '').strip()[:120]
        if not name:
            return JsonResponse({'ok': False, 'error': 'Serve un nome'}, status=400)
        preset, created = ComparePreset.objects.update_or_create(
            name=name, defaults={'config': payload.get('config') or {}})
        return JsonResponse({'ok': True, 'created': created, 'id': preset.pk})

    if request.method == 'DELETE':
        name = (request.GET.get('name') or '').strip()
        deleted, _ = ComparePreset.objects.filter(name=name).delete()
        return JsonResponse({'ok': bool(deleted)})

    return JsonResponse({'presets': [
        {'name': p.name, 'config': p.config} for p in ComparePreset.objects.all()]})


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


# ------------------------------------------------- metriche e tagging Meta
# The measurement engine behind the funnel: which raw metric each KPI reads,
# from which kind of entity, and how it rolls up. Paid and organic are kept
# strictly separate — "impression" from a boosted campaign and "impression"
# from an organic post are different things and must never be silently added
# together, so they're different metric keys entirely.


def _fb_action_list_total(data, field):
    """Several Meta insight fields (video_p100_watched_actions, ...) come back
    shaped like `actions`: a list of {action_type, value}. Summed into one."""
    return sum(_num(a.get('value')) for a in (data.get(field) or []))


# key -> (label, source, extractor). `source` decides which row collection the
# extractor is fed: 'paid' rows are fb_ads_insights records, 'organic' rows are
# the normalised post dicts from _facebook_posts()/_instagram_posts().
METRIC_REGISTRY = {
    # --- Meta paid (fb_ads_insights) ---
    'impressions': ('Impression (paid)', 'paid', lambda d: _num(d.get('impressions'))),
    'reach': ('Reach (paid)', 'paid', lambda d: _num(d.get('reach'))),
    'spend': ('Spesa', 'paid', lambda d: _num(d.get('spend'))),
    'clicks': ('Click', 'paid', lambda d: _num(d.get('clicks'))),
    'inline_link_clicks': ('Click sul link', 'paid', lambda d: _num(d.get('inline_link_clicks'))),
    'leads': ('Lead (Instant Form)', 'paid', lambda d: _fb_action_value(d, 'lead')),
    'video_p100': ('Video visti al 100%', 'paid',
                   lambda d: _fb_action_list_total(d, 'video_p100_watched_actions')),
    'video_p25': ('Video visti al 25%', 'paid',
                  lambda d: _fb_action_list_total(d, 'video_p25_watched_actions')),
    'video_plays': ('Riproduzioni video', 'paid',
                    lambda d: _fb_action_list_total(d, 'video_play_actions')),
    'estimated_ad_recallers': ('Ricordo stimato', 'paid',
                               lambda d: _num(d.get('estimated_ad_recallers'))),

    # --- organico (post Facebook + Instagram) ---
    # Facebook Pages no longer exposes reach at all (see the Facebook page for
    # the full story), so organic_reach only ever counts Instagram posts —
    # it is NOT comparable with paid reach and is labelled accordingly.
    'organic_posts': ('Post pubblicati', 'organic', lambda p: 1),
    'organic_reach': ('Reach organica (solo IG)', 'organic', lambda p: _num(p.get('reach'))),
    'organic_engagement': ('Interazioni organiche', 'organic', lambda p: _num(p.get('engagement'))),
    'organic_likes': ('Mi piace organici', 'organic', lambda p: _num(p.get('likes'))),
    'organic_comments': ('Commenti organici', 'organic', lambda p: _num(p.get('comments'))),
    'organic_shares': ('Condivisioni organiche', 'organic', lambda p: _num(p.get('shares'))),
    'organic_media_view': ('Visualizzazioni media (FB)', 'organic', lambda p: _num(p.get('media_view'))),

    # --- CRM wundt ---
    'clients_acquired': ('Clienti acquisiti', 'wundt', None),
}


def _meta_hierarchy(insight_rows):
    """{ad_id: adset_id} and {adset_id: campaign_id} plus display names, read
    off the insight rows themselves so it always reflects what actually ran
    (an entity with no delivery simply isn't there)."""
    ad_parent, adset_parent = {}, {}
    names = {'campaign': {}, 'ad_set': {}, 'ad': {}}
    for d in insight_rows:
        cid, asid, aid = (str(d.get('campaign_id') or ''), str(d.get('adset_id') or ''),
                          str(d.get('ad_id') or ''))
        if cid:
            names['campaign'].setdefault(cid, d.get('campaign_name') or cid)
        if asid:
            names['ad_set'].setdefault(asid, d.get('adset_name') or asid)
            if cid:
                adset_parent[asid] = cid
        if aid:
            names['ad'].setdefault(aid, d.get('ad_name') or aid)
            if asid:
                ad_parent[aid] = asid
    return ad_parent, adset_parent, names


def _resolve_tags(kind, external_id, taggings, ad_parent, adset_parent):
    """Effective tags for one Meta object, walking up the hierarchy.

    Per dimension: the nearest level that says anything about a dimension
    wins outright for that dimension; dimensions nobody mentions stay empty.
    Returns (tags_by_dimension, inherited_from) so the UI can show what was
    inherited rather than set directly.
    """
    chain = [(kind, external_id)]
    if kind == 'ad':
        asid = ad_parent.get(external_id)
        if asid:
            chain.append(('ad_set', asid))
            cid = adset_parent.get(asid)
            if cid:
                chain.append(('campaign', cid))
    elif kind == 'ad_set':
        cid = adset_parent.get(external_id)
        if cid:
            chain.append(('campaign', cid))

    by_dim, inherited_from = {}, {}
    for level, (ck, cid) in enumerate(chain):
        tagging = taggings.get((ck, cid))
        if not tagging:
            continue
        level_by_dim = defaultdict(list)
        for tag in tagging['tags']:
            level_by_dim[tag['dimension_slug']].append(tag)
        for dim, tags in level_by_dim.items():
            if dim in by_dim:      # a nearer level already decided this dimension
                continue
            by_dim[dim] = tags
            inherited_from[dim] = None if level == 0 else ck
    return by_dim, inherited_from


def _resolve_stage(kind, external_id, taggings, ad_parent, adset_parent):
    """Effective funnel stage, inherited the same way as tags."""
    chain = [(kind, external_id)]
    if kind == 'ad':
        asid = ad_parent.get(external_id)
        if asid:
            chain.append(('ad_set', asid))
            cid = adset_parent.get(asid)
            if cid:
                chain.append(('campaign', cid))
    elif kind == 'ad_set':
        cid = adset_parent.get(external_id)
        if cid:
            chain.append(('campaign', cid))
    for level, (ck, cid) in enumerate(chain):
        tagging = taggings.get((ck, cid))
        if tagging and tagging['stage_id']:
            return tagging['stage_id'], (None if level == 0 else ck)
    return None, None


def _load_taggings():
    """{(kind, external_id): {stage_id, tags[]}} for every tagged Meta object."""
    out = {}
    for te in TaggedEntity.objects.prefetch_related('tags__dimension'):
        out[(te.kind, te.external_id)] = {
            'stage_id': te.stage_id,
            'notes': te.notes,
            'tags': [{'id': t.pk, 'name': t.name, 'color': t.color or None,
                      'dimension': t.dimension.name, 'dimension_slug': t.dimension.slug}
                     for t in te.tags.all()],
        }
    return out


def _paid_rows_for_stage(stage_id, insight_rows, taggings, ad_parent, adset_parent, level=None):
    """fb_ads_insights rows whose ad resolves to this funnel stage.

    Matching happens at ad level and walks up, so tagging a campaign is enough
    to capture everything under it, while an ad set or a single ad can still be
    pulled into a different stage on its own.
    """
    rows = []
    for d in insight_rows:
        aid = str(d.get('ad_id') or '')
        asid = str(d.get('adset_id') or '')
        cid = str(d.get('campaign_id') or '')
        if level == 'campaign':
            resolved, _ = _resolve_stage('campaign', cid, taggings, ad_parent, adset_parent)
        elif level == 'ad_set':
            resolved, _ = _resolve_stage('ad_set', asid, taggings, ad_parent, adset_parent)
        elif level == 'ad':
            resolved, _ = _resolve_stage('ad', aid, taggings, ad_parent, adset_parent)
        else:
            resolved, _ = _resolve_stage('ad', aid, taggings, ad_parent, adset_parent)
            if resolved is None and asid:
                resolved, _ = _resolve_stage('ad_set', asid, taggings, ad_parent, adset_parent)
            if resolved is None and cid:
                resolved, _ = _resolve_stage('campaign', cid, taggings, ad_parent, adset_parent)
        if resolved == stage_id:
            rows.append(d)
    return rows


def _organic_posts_for_stage(stage_id, organic_posts, pieces_by_key):
    """Organic posts assigned to this stage through their ContentPiece — the
    same record the editorial calendar edits, so tagging in either place is
    tagging the same thing."""
    out = []
    for p in organic_posts:
        key = (p.get('permalink') or '').rstrip('/')
        piece = pieces_by_key.get(key)
        if piece and piece['stage_id'] == stage_id:
            out.append(p)
    return out


def _kpi_series(kpi, paid_rows, organic_rows, date_key_paid='date_start'):
    """Daily series for one computed KPI.

    For ratio KPIs the numerator and denominator are carried alongside the
    value, so the frontend can re-derive the ratio per week/month bucket
    instead of averaging daily ratios (which would be wrong).
    """
    entry = METRIC_REGISTRY.get(kpi.metric)
    if kpi.source == 'wundt':
        if kpi.metric == 'clients_acquired':
            return [{**p, 'num': p['value'], 'den': None} for p in _client_acquired_series()]
        return []
    if not entry:
        return []
    _, _, extractor = entry

    if kpi.source == 'organic':
        rows, date_of = organic_rows, (lambda r: r.get('date'))
    else:
        rows, date_of = paid_rows, (lambda r: str(r.get(date_key_paid) or '')[:10])

    den_entry = METRIC_REGISTRY.get(kpi.metric_denominator) if kpi.metric_denominator else None
    num_by_date, den_by_date, count_by_date = defaultdict(float), defaultdict(float), defaultdict(int)
    for r in rows:
        date = date_of(r)
        if not date:
            continue
        num_by_date[date] += extractor(r)
        count_by_date[date] += 1
        if den_entry:
            den_by_date[date] += den_entry[2](r)

    series = []
    for date in sorted(num_by_date):
        num = num_by_date[date]
        if kpi.aggregation == 'ratio':
            den = den_by_date.get(date, 0)
            value = (num / den * kpi.scale) if den else None
            series.append({'date': date, 'value': value, 'num': num, 'den': den})
        elif kpi.aggregation == 'avg':
            n = count_by_date[date] or 1
            series.append({'date': date, 'value': num / n * kpi.scale, 'num': num, 'den': n})
        else:
            series.append({'date': date, 'value': num * kpi.scale, 'num': num, 'den': None})
    return series


# Legacy name-matched computed KPIs, kept so KPIs created before the
# source/metric fields existed keep working until they're reconfigured.
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
    ad_parent, adset_parent, _names = _meta_hierarchy(insight_rows)
    taggings = _load_taggings()

    organic_posts = [{**p, 'platform': 'Facebook'} for p in _facebook_posts()] + \
                    [{**p, 'platform': 'Instagram'} for p in _instagram_posts()]
    pieces_by_key = {}
    for piece in ContentPiece.objects.all():
        key = (piece.external_permalink or '').rstrip('/')
        if key:
            pieces_by_key[key] = {'stage_id': piece.stage_id}

    stages = []
    for stage in FunnelStage.objects.filter(is_active=True).prefetch_related('kpis__values', 'sources'):
        campaign_rows = _stage_campaign_rows(stage, insight_rows)
        drilldown = _stage_drilldown(stage, insight_rows)
        stage_organic = _organic_posts_for_stage(stage.pk, organic_posts, pieces_by_key)
        kpis = []
        for kpi in stage.kpis.filter(is_active=True):
            if kpi.is_computed:
                paid_rows = _paid_rows_for_stage(stage.pk, insight_rows, taggings,
                                                 ad_parent, adset_parent,
                                                 level=kpi.entity_level or None)
                series = _kpi_series(kpi, paid_rows, stage_organic)
                computed = True
            elif FUNNEL_COMPUTED_KPIS.get(kpi.name):
                series = FUNNEL_COMPUTED_KPIS[kpi.name](campaign_rows)
                computed = True
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
                'aggregation': kpi.aggregation if kpi.is_computed else ('sum' if kpi.unit == 'count' else 'avg'),
                'scale': kpi.scale,
                'source': kpi.source,
                'metric': kpi.metric,
                'metric_label': (METRIC_REGISTRY.get(kpi.metric) or ('', '', None))[0],
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


# ------------------------------------------------- pianificazione e calendario
# The planning layer: a user-defined tag taxonomy that budget, campaigns and
# content all hang off, so the same cuts (audience, Think-Feel-Do, pilastro
# creativo...) answer both "where is the money going" and "what did we publish
# and how did it do". See the July 2026 strategy deck for where the taxonomy
# and the intended splits come from.


def _spend_by_source(insight_rows, sources, period_start=None, period_end=None):
    """{source_pk: spend} over the given window, for sources linked to a real
    Meta id. Sources with no external_id can't be reconciled and are skipped
    (they stay meaningful as intent, just not as actuals)."""
    out = {}
    for source in sources:
        if not source.external_id:
            continue
        total = 0.0
        for d in _fb_insight_rows_for_source(source, insight_rows):
            date = str(d.get('date_start') or '')[:10]
            if not date:
                continue
            if period_start and date < period_start:
                continue
            if period_end and date > period_end:
                continue
            total += _num(d.get('spend'))
        out[source.pk] = total
    return out


def _spend_by_tag_dimension(insight_rows, taggings, ad_parent, adset_parent,
                            period_start=None, period_end=None):
    """Real spend attributed to each tag, dimension by dimension.

    Two rules make the numbers add up:

    * An entity carrying several tags of the *same* dimension splits its spend
      equally among them. Tagging a campaign both "Genitori" and "Studenti"
      means half each, not the whole amount counted twice.
    * Across *different* dimensions the full spend is counted once per
      dimension, so each dimension's shares are comparable with the split
      planned for that same axis.

    Tags resolve per ad through the campaign -> ad set -> ad inheritance, so
    tagging a campaign is enough to attribute everything running under it.

    Returns (spend per tag id, tagged total per dimension, spend carrying no tag).
    """
    spend_by_tag = defaultdict(float)
    dim_totals = defaultdict(float)
    untagged = 0.0
    tagged_total = 0.0
    cache = {}

    for d in insight_rows:
        day = str(d.get('date_start') or '')[:10]
        if not day:
            continue
        if period_start and day < period_start:
            continue
        if period_end and day > period_end:
            continue
        spend = _num(d.get('spend'))
        if not spend:
            continue

        aid = str(d.get('ad_id') or '')
        asid = str(d.get('adset_id') or '')
        cid = str(d.get('campaign_id') or '')
        key = (aid, asid, cid)
        if key not in cache:
            # _resolve_tags already walks ad -> ad set -> campaign; only fall
            # back when the row has no ad id at all.
            if aid:
                by_dim, _ = _resolve_tags('ad', aid, taggings, ad_parent, adset_parent)
            elif asid:
                by_dim, _ = _resolve_tags('ad_set', asid, taggings, ad_parent, adset_parent)
            elif cid:
                by_dim, _ = _resolve_tags('campaign', cid, taggings, ad_parent, adset_parent)
            else:
                by_dim = {}
            cache[key] = by_dim
        by_dim = cache[key]

        if not by_dim:
            untagged += spend
            continue
        tagged_total += spend
        for dim_slug, tags in by_dim.items():
            share = spend / len(tags)
            for t in tags:
                spend_by_tag[t['id']] += share
            dim_totals[dim_slug] += spend

    return spend_by_tag, dim_totals, untagged, tagged_total


def _tag_payload(dimensions):
    return [{
        'id': dim.pk,
        'name': dim.name,
        'slug': dim.slug,
        'description': dim.description,
        'allow_multiple': dim.allow_multiple,
        'tags': [{
            'id': t.pk,
            'name': t.name,
            'slug': t.slug,
            'description': t.description,
            'color': t.color or None,
            'target_share': t.target_share,
        } for t in dim.tags.filter(is_active=True)],
    } for dim in dimensions]


@login_required
def pianificazione(request):
    cfg = {'data_url': reverse('dashboard:data_pianificazione'),
           'tagging_url': reverse('dashboard:tagging')}
    return render(request, 'dashboard/pianificazione.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': TagDimension.objects.exists(),
    })


@login_required
def data_pianificazione(request):
    plan_id = request.GET.get('plan')
    plans = list(BudgetPlan.objects.all())
    plan = next((p for p in plans if str(p.pk) == plan_id), None) or \
        next((p for p in plans if p.is_active), None) or (plans[0] if plans else None)

    dimensions = list(TagDimension.objects.filter(is_active=True).prefetch_related('tags'))
    insight_rows = list(_stream_rows('fb_ads_insights'))
    sources = list(FunnelStageSource.objects.prefetch_related('tags').select_related('stage'))

    period_start = plan.period_start.isoformat() if plan else None
    period_end = plan.period_end.isoformat() if plan else None
    spend_by_source = _spend_by_source(insight_rows, sources, period_start, period_end)

    # Planned lines, each resolved to euro + percent, with actual spend attached
    # when the line points at a real Meta object.
    lines = []
    if plan:
        for line in plan.lines.select_related('stage', 'source').prefetch_related('tags'):
            lines.append({
                'id': line.pk,
                'label': line.label,
                'stage': line.stage.name if line.stage else None,
                'stage_slug': line.stage.slug if line.stage else None,
                'stage_id': line.stage_id,
                'tags': [{'id': t.pk, 'name': t.name, 'dimension': t.dimension.name,
                          'dimension_slug': t.dimension.slug, 'color': t.color or None}
                         for t in line.tags.all()],
                'tag_ids': [t.pk for t in line.tags.all()],
                'percent': line.resolved_percent,
                'amount': line.resolved_amount,
                'percent_raw': line.percent,
                'amount_raw': line.amount,
                'is_media': line.is_media,
                'source': line.source.name if line.source else None,
                'source_id': line.source_id,
                'actual_spend': spend_by_source.get(line.source_id) if line.source_id else None,
                'notes': line.notes,
                'order': line.order,
            })

    # Actual spend sliced by tag, resolved per ad through the tagging overlay
    # and its campaign -> ad set -> ad inheritance (the same resolution the
    # Tagging board shows), restricted to the plan's period.
    ad_parent, adset_parent, _n = _meta_hierarchy(insight_rows)
    taggings = _load_taggings()
    spend_by_tag, dim_totals, untagged_spend, tagged_spend_total = _spend_by_tag_dimension(
        insight_rows, taggings, ad_parent, adset_parent, period_start, period_end)

    # The same split applied to the *planned* side, so a budget line tagged
    # "Genitori" actually shows up as planned Genitori budget - which is what
    # you'd expect looking at the plan table right above.
    planned_by_tag, planned_dim_totals = defaultdict(float), defaultdict(float)
    for line in lines:
        amount = line['amount'] or 0
        if not amount or not line['is_media'] or not line['tags']:
            continue
        by_dim = defaultdict(list)
        for t in line['tags']:
            by_dim[t['dimension_slug']].append(t)
        for dim_slug, tags in by_dim.items():
            share = amount / len(tags)
            for t in tags:
                planned_by_tag[t['id']] += share
            planned_dim_totals[dim_slug] += amount

    stages = []
    for stage in FunnelStage.objects.filter(is_active=True).prefetch_related('sources', 'kpis'):
        planned = sum(l['amount'] or 0 for l in lines
                      if l['stage_slug'] == stage.slug and l['is_media'])
        planned_pct = sum(l['percent'] or 0 for l in lines
                          if l['stage_slug'] == stage.slug and l['is_media'])
        actual = sum(spend_by_source.get(s.pk, 0) for s in stage.sources.all())
        stages.append({
            'id': stage.pk,
            'name': stage.name,
            'slug': stage.slug,
            'description': stage.description,
            'planned_amount': planned,
            'planned_percent': planned_pct,
            'actual_spend': actual,
            'kpis': [{
                'id': k.pk, 'name': k.name, 'unit': k.unit, 'target_value': k.target_value,
            } for k in stage.kpis.filter(is_active=True)],
        })

    return JsonResponse({
        'plans': [{'id': p.pk, 'name': p.name, 'period_start': p.period_start.isoformat(),
                   'period_end': p.period_end.isoformat(), 'total_budget': p.total_budget,
                   'is_active': p.is_active, 'notes': p.notes} for p in plans],
        'plan': ({'id': plan.pk, 'name': plan.name, 'total_budget': plan.total_budget,
                  'period_start': period_start, 'period_end': period_end, 'notes': plan.notes}
                 if plan else None),
        'dimensions': _tag_payload(dimensions),
        'lines': lines,
        'sources': [{'id': s.pk, 'name': s.name, 'stage': s.stage.name} for s in sources],
        'stages': stages,
        'spend_by_tag': {str(k): v for k, v in spend_by_tag.items()},
        'dimension_totals': dict(dim_totals),
        'planned_by_tag': {str(k): v for k, v in planned_by_tag.items()},
        'planned_dimension_totals': dict(planned_dim_totals),
        'tagged_spend_total': tagged_spend_total,
        'untagged_spend': untagged_spend,
    })


@login_required
def budget_line_save(request):
    """Create or update one budget-plan line from the inline editor on
    Pianificazione, so tagging a line doesn't require the Django admin."""
    if request.method != 'POST':
        return redirect('dashboard:pianificazione')

    plan = BudgetPlan.objects.filter(pk=(request.POST.get('plan') or '').strip()).first()
    if not plan:
        messages.error(request, 'Piano non valido.')
        return redirect('dashboard:pianificazione')

    line_id = (request.POST.get('id') or '').strip()
    line = BudgetLine.objects.filter(pk=line_id, plan=plan).first() if line_id else BudgetLine(plan=plan)

    label = (request.POST.get('label') or '').strip()
    if not label:
        messages.error(request, "La voce deve avere un'etichetta.")
        return redirect(f"{reverse('dashboard:pianificazione')}?plan={plan.pk}")
    line.label = label

    stage_id = (request.POST.get('stage') or '').strip()
    line.stage_id = int(stage_id) if stage_id else None

    source_id = (request.POST.get('source') or '').strip()
    line.source_id = int(source_id) if source_id else None

    # Percent and amount are alternative ways to size a line; whichever field
    # was actually filled in wins, the other is cleared so resolved_amount /
    # resolved_percent don't get confused about which one is authoritative.
    percent_raw = (request.POST.get('percent') or '').strip()
    amount_raw = (request.POST.get('amount') or '').strip()
    if amount_raw:
        line.amount, line.percent = float(amount_raw), None
    elif percent_raw:
        line.percent, line.amount = float(percent_raw), None
    else:
        line.percent = line.amount = None

    line.is_media = bool(request.POST.get('is_media'))
    line.notes = (request.POST.get('notes') or '').strip()[:300]
    order_raw = (request.POST.get('order') or '').strip()
    if order_raw:
        line.order = int(order_raw)
    line.save()
    line.tags.set([t for t in request.POST.getlist('tags') if t])

    messages.success(request, f'Voce "{line.label}" salvata.')
    return redirect(f"{reverse('dashboard:pianificazione')}?plan={plan.pk}")


@login_required
def budget_line_delete(request, pk):
    if request.method == 'POST':
        line = BudgetLine.objects.filter(pk=pk).first()
        if line:
            plan_id, label = line.plan_id, line.label
            line.delete()
            messages.success(request, f'Voce "{label}" eliminata.')
            return redirect(f"{reverse('dashboard:pianificazione')}?plan={plan_id}")
    return redirect('dashboard:pianificazione')


def _published_post_index():
    """Real published Facebook + Instagram posts, keyed by both permalink and
    post id, so a ContentPiece can be matched by whichever the user pasted."""
    index = {}
    for platform, posts in (('Facebook', _facebook_posts()), ('Instagram', _instagram_posts())):
        for p in posts:
            entry = {**p, 'platform': platform}
            if p.get('permalink'):
                index[p['permalink'].rstrip('/')] = entry
            if p.get('id'):
                index[str(p['id'])] = entry
    return index


@login_required
def calendario(request):
    cfg = {'data_url': reverse('dashboard:data_calendario')}
    return render(request, 'dashboard/calendario.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': ChannelCadence.objects.exists(),
    })


@login_required
def data_calendario(request):
    post_index = _published_post_index()
    dimensions = list(TagDimension.objects.filter(is_active=True).prefetch_related('tags'))

    pieces = []
    for piece in ContentPiece.objects.select_related('stage', 'campaign_source').prefetch_related('tags'):
        key = (piece.external_permalink or '').rstrip('/') or str(piece.external_post_id or '')
        match = post_index.get(key) if key else None
        pieces.append({
            'id': piece.pk,
            'title': piece.title,
            'channel': piece.channel,
            'format': piece.content_format,
            'format_label': piece.get_content_format_display(),
            'status': piece.status,
            'status_label': piece.get_status_display(),
            'owner': piece.owner,
            'planned_date': piece.planned_date.isoformat(),
            'published_date': piece.published_date.isoformat() if piece.published_date else None,
            'date': piece.effective_date.isoformat(),
            'stage': piece.stage.name if piece.stage else None,
            'stage_slug': piece.stage.slug if piece.stage else None,
            'tags': [{'id': t.pk, 'name': t.name, 'dimension': t.dimension.name,
                      'dimension_slug': t.dimension.slug, 'color': t.color or None}
                     for t in piece.tags.all()],
            'tag_ids': [t.pk for t in piece.tags.all()],
            'campaign_source': piece.campaign_source.name if piece.campaign_source else None,
            'brief': piece.brief,
            'hook': piece.hook,
            'permalink': piece.external_permalink or (match or {}).get('permalink'),
            # Real metrics, only when the piece is actually linked to a synced post.
            'metrics': ({
                'platform': match['platform'],
                'reach': match.get('reach'),
                'engagement': match.get('engagement'),
                'likes': match.get('likes'),
                'comments': match.get('comments'),
            } if match else None),
        })

    cadences = [{
        'channel': c.channel, 'label': c.label, 'target_min': c.target_min,
        'target_max': c.target_max, 'period': c.period, 'role': c.role,
    } for c in ChannelCadence.objects.filter(is_active=True)]

    # Published posts not yet attached to any calendar entry — offered as
    # one-click links so the calendar can be reconciled with what really went out.
    linked_keys = {(p['permalink'] or '').rstrip('/') for p in pieces if p['permalink']}
    unlinked = sorted(
        ({'platform': e['platform'], 'date': e['date'], 'text': e['text'][:140],
          'permalink': e['permalink'], 'type': e['type'],
          'engagement': e.get('engagement'), 'reach': e.get('reach')}
         for key, e in post_index.items()
         if e.get('permalink') and key == e['permalink'].rstrip('/')
         and e['permalink'].rstrip('/') not in linked_keys),
        key=lambda r: r['date'], reverse=True)[:60]

    return JsonResponse({
        'pieces': pieces,
        'cadences': cadences,
        'dimensions': _tag_payload(dimensions),
        'stages': [{'id': s.pk, 'name': s.name, 'slug': s.slug}
                   for s in FunnelStage.objects.filter(is_active=True)],
        'formats': [{'value': v, 'label': l} for v, l in ContentPiece.FORMAT_CHOICES],
        'statuses': [{'value': v, 'label': l} for v, l in ContentPiece.STATUS_CHOICES],
        'unlinked_posts': unlinked,
    })


@login_required
def content_piece_save(request):
    """Create or update one calendar entry. Kept as a plain form POST (no JSON
    API) to match how the events page already works."""
    if request.method != 'POST':
        return redirect('dashboard:calendario')

    piece_id = (request.POST.get('piece_id') or '').strip()
    title = (request.POST.get('title') or '').strip()
    planned_date = (request.POST.get('planned_date') or '').strip()
    channel = (request.POST.get('channel') or '').strip()
    if not title or not planned_date or not channel:
        messages.error(request, 'Titolo, canale e data pianificata sono obbligatori.')
        return redirect('dashboard:calendario')

    published_date = (request.POST.get('published_date') or '').strip() or None
    stage_id = (request.POST.get('stage') or '').strip() or None
    source_id = (request.POST.get('campaign_source') or '').strip() or None
    fields = {
        'title': title,
        'channel': channel,
        'content_format': (request.POST.get('content_format') or 'post').strip(),
        'status': (request.POST.get('status') or 'idea').strip(),
        'owner': (request.POST.get('owner') or '').strip(),
        'planned_date': planned_date,
        'published_date': published_date,
        'stage_id': stage_id,
        'campaign_source_id': source_id,
        'brief': (request.POST.get('brief') or '').strip(),
        'hook': (request.POST.get('hook') or '').strip(),
        'external_permalink': (request.POST.get('external_permalink') or '').strip(),
        'notes': (request.POST.get('notes') or '').strip(),
    }
    tag_ids = request.POST.getlist('tags')

    if piece_id:
        piece = ContentPiece.objects.filter(pk=piece_id).first()
        if not piece:
            messages.error(request, 'Uscita non trovata.')
            return redirect('dashboard:calendario')
        for k, v in fields.items():
            setattr(piece, k, v)
        piece.save()
        messages.success(request, f'Uscita "{title}" aggiornata.')
    else:
        piece = ContentPiece.objects.create(**fields)
        messages.success(request, f'Uscita "{title}" aggiunta.')
    piece.tags.set(Tag.objects.filter(pk__in=tag_ids))
    return redirect('dashboard:calendario')


@login_required
def content_piece_delete(request, pk):
    if request.method == 'POST':
        piece = ContentPiece.objects.filter(pk=pk).first()
        if piece:
            title = piece.title
            piece.delete()
            messages.success(request, f'Uscita "{title}" eliminata.')
    return redirect('dashboard:calendario')


@login_required
def tag_save(request):
    """Inline tag creation from the planning page — the taxonomy is meant to be
    grown as the strategy evolves, not fixed at seed time."""
    if request.method != 'POST':
        return redirect('dashboard:pianificazione')
    dimension_id = (request.POST.get('dimension') or '').strip()
    name = (request.POST.get('name') or '').strip()
    dimension = TagDimension.objects.filter(pk=dimension_id).first()
    if not dimension or not name:
        messages.error(request, 'Dimensione e nome del tag sono obbligatori.')
        return redirect('dashboard:pianificazione')

    share = (request.POST.get('target_share') or '').strip()
    slug = slugify(name)[:50] or 'tag'
    base, i = slug, 2
    while Tag.objects.filter(dimension=dimension, slug=slug).exists():
        slug = f'{base}-{i}'
        i += 1
    Tag.objects.create(
        dimension=dimension, name=name, slug=slug,
        target_share=float(share) if share else None,
        description=(request.POST.get('description') or '').strip(),
        color=(request.POST.get('color') or '').strip(),
        order=(dimension.tags.count() or 0) + 1)
    messages.success(request, f'Tag "{name}" aggiunto a {dimension.name}.')
    return redirect('dashboard:pianificazione')


# ------------------------------------------------------------- piani budget

MESI_IT = {1: 'gennaio', 2: 'febbraio', 3: 'marzo', 4: 'aprile', 5: 'maggio', 6: 'giugno',
           7: 'luglio', 8: 'agosto', 9: 'settembre', 10: 'ottobre', 11: 'novembre', 12: 'dicembre'}


def _month_bounds(year, month):
    """(first day, last day) of a month, without pulling in a date library."""
    start = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def _month_label(year, month):
    return f'{MESI_IT[month]} {year}'


def _spend_by_month_and_stage(insight_rows, taggings, ad_parent, adset_parent):
    """{month: {'total': x, 'by_stage': {stage_id|None: y}}} over every synced
    day, so past months can be reviewed once their campaigns are tagged —
    the whole point of being able to look backwards."""
    out = defaultdict(lambda: {'total': 0.0, 'by_stage': defaultdict(float)})
    stage_cache = {}
    for d in insight_rows:
        day = str(d.get('date_start') or '')[:10]
        if len(day) < 7:
            continue
        spend = _num(d.get('spend'))
        if not spend:
            continue
        aid, asid, cid = (str(d.get('ad_id') or ''), str(d.get('adset_id') or ''),
                          str(d.get('campaign_id') or ''))
        cache_key = (aid, asid, cid)
        if cache_key not in stage_cache:
            resolved, _ = _resolve_stage('ad', aid, taggings, ad_parent, adset_parent)
            if resolved is None and asid:
                resolved, _ = _resolve_stage('ad_set', asid, taggings, ad_parent, adset_parent)
            if resolved is None and cid:
                resolved, _ = _resolve_stage('campaign', cid, taggings, ad_parent, adset_parent)
            stage_cache[cache_key] = resolved
        month = day[:7]
        out[month]['total'] += spend
        out[month]['by_stage'][stage_cache[cache_key]] += spend
    return out


@login_required
def piani(request):
    cfg = {'data_url': reverse('dashboard:data_piani'),
           'save_url': reverse('dashboard:piano_save'),
           'detail_url': reverse('dashboard:pianificazione')}
    return render(request, 'dashboard/piani.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': True,
    })


@login_required
def data_piani(request):
    insight_rows = list(_stream_rows('fb_ads_insights'))
    ad_parent, adset_parent, _n = _meta_hierarchy(insight_rows)
    taggings = _load_taggings()
    by_month = _spend_by_month_and_stage(insight_rows, taggings, ad_parent, adset_parent)

    plans = []
    covered_months = set()
    for p in BudgetPlan.objects.prefetch_related('lines__stage'):
        # A plan can span any range; sum the months it touches.
        actual, by_stage = 0.0, defaultdict(float)
        for month, data in by_month.items():
            m_start, _ = _month_bounds(int(month[:4]), int(month[5:7]))
            if p.period_start <= m_start <= p.period_end:
                covered_months.add(month)
                actual += data['total']
                for sid, v in data['by_stage'].items():
                    by_stage[sid] += v
        lines = list(p.lines.all())
        planned = sum(l.resolved_amount or 0 for l in lines if l.is_media)
        plans.append({
            'id': p.pk, 'name': p.name,
            'period_start': p.period_start.isoformat(), 'period_end': p.period_end.isoformat(),
            'total_budget': p.total_budget, 'is_active': p.is_active,
            'n_lines': len(lines), 'planned_amount': planned, 'actual_spend': actual,
            'by_stage': {str(k) if k else 'none': v for k, v in by_stage.items()},
            'notes': p.notes,
        })

    # Months that really had spend but nobody planned - the gap the user wants
    # to be able to fill in retroactively.
    orphans = []
    for month, data in sorted(by_month.items()):
        if month in covered_months:
            continue
        y, m = int(month[:4]), int(month[5:7])
        orphans.append({'month': month, 'label': _month_label(y, m), 'spend': data['total'],
                        'by_stage': {str(k) if k else 'none': v for k, v in data['by_stage'].items()}})

    return JsonResponse({
        'plans': sorted(plans, key=lambda p: p['period_start']),
        'months_without_plan': orphans,
        'stages': [{'id': s.pk, 'name': s.name, 'slug': s.slug}
                   for s in FunnelStage.objects.filter(is_active=True)],
    })


@login_required
def piano_save(request):
    """Create a plan for a month, optionally copying another plan's lines.

    Copying carries over the whole structure (labels, stages, tags, source
    links, percentages) but re-resolves fixed amounts against the new total,
    so duplicating a plan into a month with a different budget keeps the
    intended split rather than the old euro figures.
    """
    if request.method != 'POST':
        return redirect('dashboard:piani')

    month = (request.POST.get('month') or '').strip()          # YYYY-MM
    name = (request.POST.get('name') or '').strip()
    budget_raw = (request.POST.get('total_budget') or '').strip()
    copy_from = (request.POST.get('copy_from') or '').strip()

    try:
        year, mon = int(month[:4]), int(month[5:7])
        start, end = _month_bounds(year, mon)
    except (ValueError, IndexError, KeyError):
        messages.error(request, 'Mese non valido: usa il formato AAAA-MM.')
        return redirect('dashboard:piani')

    if BudgetPlan.objects.filter(period_start=start).exists():
        messages.error(request, f'Esiste già un piano che parte dal {start}.')
        return redirect('dashboard:piani')

    try:
        total = float(budget_raw) if budget_raw else 0.0
    except ValueError:
        total = 0.0

    plan = BudgetPlan.objects.create(
        name=name or f'Piano media {_month_label(year, mon)}',
        period_start=start, period_end=end, total_budget=total)

    copied = 0
    if copy_from:
        source = BudgetPlan.objects.filter(pk=copy_from).prefetch_related('lines__tags').first()
        if source:
            for line in source.lines.all():
                new_line = BudgetLine.objects.create(
                    plan=plan, label=line.label, stage=line.stage, source=line.source,
                    percent=line.percent,
                    # A fixed amount only keeps its meaning if the budget is the
                    # same; otherwise convert it to the equivalent share.
                    amount=(line.amount if (line.amount is not None and
                                            source.total_budget == plan.total_budget) else None),
                    is_media=line.is_media, order=line.order, notes=line.notes)
                if line.amount is not None and source.total_budget and source.total_budget != total:
                    new_line.percent = line.amount / source.total_budget * 100
                    new_line.save(update_fields=['percent'])
                new_line.tags.set(line.tags.all())
                copied += 1

    messages.success(request, f'Piano "{plan.name}" creato'
                              + (f' copiando {copied} voci da "{source.name}".' if copy_from and copied else '.'))
    return redirect(f'{reverse("dashboard:pianificazione")}?plan={plan.pk}')


@login_required
def piano_delete(request, pk):
    if request.method == 'POST':
        plan = BudgetPlan.objects.filter(pk=pk).first()
        if plan:
            name = plan.name
            plan.delete()
            messages.success(request, f'Piano "{name}" eliminato.')
    return redirect('dashboard:piani')


# ------------------------------------------------------------ tagging Meta


def _is_boosted(name):
    """Meta models a boosted post as a campaign literally named `Post: "..."`,
    so that prefix is the only signal distinguishing it from a campaign built
    in Ads Manager."""
    return (name or '').startswith('Post:')


@login_required
def tagging(request):
    cfg = {'data_url': reverse('dashboard:data_tagging'),
           'save_url': reverse('dashboard:tagging_save')}
    return render(request, 'dashboard/tagging.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': AirbyteRecord.objects.filter(stream='fb_ads_insights').exists(),
    })


@login_required
def data_tagging(request):
    insight_rows = list(_stream_rows('fb_ads_insights'))
    ad_parent, adset_parent, names = _meta_hierarchy(insight_rows)
    taggings = _load_taggings()

    # Aggregate real delivery per entity, so every card carries the numbers
    # that justify where it belongs in the funnel.
    def _blank():
        return {'impressions': 0.0, 'reach': 0.0, 'spend': 0.0, 'clicks': 0.0, 'leads': 0.0}

    totals = {'campaign': defaultdict(_blank), 'ad_set': defaultdict(_blank), 'ad': defaultdict(_blank)}
    for d in insight_rows:
        vals = {'impressions': _num(d.get('impressions')), 'reach': _num(d.get('reach')),
                'spend': _num(d.get('spend')), 'clicks': _num(d.get('clicks')),
                'leads': _fb_action_value(d, 'lead')}
        for kind, field in (('campaign', 'campaign_id'), ('ad_set', 'adset_id'), ('ad', 'ad_id')):
            eid = str(d.get(field) or '')
            if not eid:
                continue
            for k, v in vals.items():
                totals[kind][eid][k] += v

    def _entity(kind, eid):
        own = taggings.get((kind, eid))
        by_dim, inherited = _resolve_tags(kind, eid, taggings, ad_parent, adset_parent)
        stage_id, stage_inherited = _resolve_stage(kind, eid, taggings, ad_parent, adset_parent)
        return {
            'kind': kind,
            'id': eid,
            'name': names[kind].get(eid, eid),
            'own_stage_id': own['stage_id'] if own else None,
            'stage_id': stage_id,
            'stage_inherited_from': stage_inherited,
            'own_tag_ids': [t['id'] for t in own['tags']] if own else [],
            'effective_tags': [t for tags in by_dim.values() for t in tags],
            'inherited_dimensions': {k: v for k, v in inherited.items() if v},
            'metrics': dict(totals[kind].get(eid, _blank())),
        }

    campaigns, boosted = [], []
    for cid in names['campaign']:
        e = _entity('campaign', cid)
        e['children'] = []
        for asid, parent_cid in adset_parent.items():
            if parent_cid != cid:
                continue
            a = _entity('ad_set', asid)
            a['children'] = [_entity('ad', aid) for aid, p in ad_parent.items() if p == asid]
            a['children'].sort(key=lambda x: -x['metrics']['impressions'])
            e['children'].append(a)
        e['children'].sort(key=lambda x: -x['metrics']['impressions'])
        (boosted if _is_boosted(e['name']) else campaigns).append(e)

    campaigns.sort(key=lambda x: -x['metrics']['spend'])
    boosted.sort(key=lambda x: -x['metrics']['impressions'])

    # Organic posts, tagged through their ContentPiece so the editorial
    # calendar and this page edit the very same record.
    pieces = {}
    for piece in ContentPiece.objects.prefetch_related('tags__dimension'):
        key = (piece.external_permalink or '').rstrip('/')
        if key:
            pieces[key] = piece
    organic = []
    for p in ([{**x, 'platform': 'Facebook'} for x in _facebook_posts()] +
              [{**x, 'platform': 'Instagram'} for x in _instagram_posts()]):
        key = (p.get('permalink') or '').rstrip('/')
        piece = pieces.get(key)
        organic.append({
            'kind': 'organic_post',
            'id': key,
            'piece_id': piece.pk if piece else None,
            'name': (p.get('text') or '(senza testo)')[:120],
            'platform': p['platform'],
            'date': p.get('date'),
            'permalink': p.get('permalink'),
            'stage_id': piece.stage_id if piece else None,
            'own_stage_id': piece.stage_id if piece else None,
            'own_tag_ids': [t.pk for t in piece.tags.all()] if piece else [],
            'effective_tags': ([{'id': t.pk, 'name': t.name, 'color': t.color or None,
                                 'dimension': t.dimension.name, 'dimension_slug': t.dimension.slug}
                                for t in piece.tags.all()] if piece else []),
            'inherited_dimensions': {},
            'metrics': {'impressions': 0.0, 'reach': _num(p.get('reach')), 'spend': 0.0,
                        'clicks': 0.0, 'leads': 0.0,
                        'engagement': _num(p.get('engagement'))},
        })
    organic.sort(key=lambda x: x['date'] or '', reverse=True)

    return JsonResponse({
        'campaigns': campaigns,
        'boosted': boosted,
        'organic': organic,
        'stages': [{'id': s.pk, 'name': s.name, 'slug': s.slug, 'order': s.order}
                   for s in FunnelStage.objects.filter(is_active=True)],
        'dimensions': _tag_payload(list(TagDimension.objects.filter(is_active=True)
                                        .prefetch_related('tags'))),
    })


@login_required
def tagging_save(request):
    """Persist one entity's stage and/or tags. Paid objects write to
    TaggedEntity; organic posts write to their ContentPiece, creating one on
    the fly for a post that was published but never planned in the calendar —
    which is what keeps the two pages editing a single shared record."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    payload = json.loads(request.body or '{}')
    kind = payload.get('kind')
    eid = (payload.get('id') or '').strip()
    if not kind or not eid:
        return JsonResponse({'ok': False, 'error': 'kind e id obbligatori'}, status=400)

    stage_id = payload.get('stage_id') or None
    tag_ids = payload.get('tag_ids')

    if kind == 'organic_post':
        piece = ContentPiece.objects.filter(external_permalink__startswith=eid).first()
        if not piece:
            piece = ContentPiece.objects.create(
                title=(payload.get('name') or 'Post pubblicato')[:250],
                channel=(payload.get('platform') or 'instagram').lower(),
                content_format='post',
                status='pubblicato',
                planned_date=payload.get('date') or timezone.now().date(),
                published_date=payload.get('date') or None,
                external_permalink=eid,
                notes='Creato dalla pagina Tagging per agganciare un post pubblicato.')
        if 'stage_id' in payload:
            piece.stage_id = stage_id
            piece.save(update_fields=['stage_id'])
        if tag_ids is not None:
            piece.tags.set(Tag.objects.filter(pk__in=tag_ids))
        return JsonResponse({'ok': True, 'piece_id': piece.pk})

    if kind not in {'campaign', 'ad_set', 'ad'}:
        return JsonResponse({'ok': False, 'error': f'kind non valido: {kind}'}, status=400)

    entity, _ = TaggedEntity.objects.get_or_create(kind=kind, external_id=eid)
    if 'stage_id' in payload:
        entity.stage_id = stage_id
        entity.save(update_fields=['stage_id', 'updated_at'])
    if tag_ids is not None:
        entity.tags.set(Tag.objects.filter(pk__in=tag_ids))
    return JsonResponse({'ok': True})


@login_required
def data_events(request):
    rows = [{
        'name': e.name,
        'date': e.date.isoformat(),
        'scope': e.scope,
        'notes': e.notes,
    } for e in MarketingEvent.objects.all()]
    return JsonResponse({'events': rows})
