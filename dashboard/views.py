import json

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
        for data in AirbyteRecord.objects.filter(stream=stream).values_list('data', flat=True):
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
            })
    return JsonResponse({'rows': rows})
