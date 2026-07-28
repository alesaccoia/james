import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from . import sameapi
from .models import Competitor, MonthlyTraffic, TrafficUpload


@login_required
def upload(request):
    if request.method == 'POST':
        f = request.FILES.get('sameapi_file')
        if not f:
            messages.error(request, "Scegli prima l'export CSV SameAPI.")
            return redirect('competitors:upload')
        text = f.read().decode('utf-8-sig', errors='replace')
        try:
            rec = sameapi.apply_upload(text, f.name, uploaded_by=request.user.username)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('competitors:upload')
        messages.success(
            request,
            f'Import completato: {rec.datapoints} datapoint, {rec.domains} domini, '
            f"mesi {', '.join(rec.months)}.")
        return redirect('competitors:traffic')

    months = (MonthlyTraffic.objects.order_by('month')
              .values_list('month', flat=True).distinct())
    return render(request, 'competitors/upload.html', {
        'uploads': TrafficUpload.objects.all()[:15],
        'months': sorted({m.isoformat()[:7] for m in months}),
        'domain_count': MonthlyTraffic.objects.values('domain').distinct().count(),
        'competitors': Competitor.objects.all(),
    })


@login_required
def traffic(request):
    cfg = {'data_url': reverse('competitors:data_traffic')}
    return render(request, 'competitors/traffic.html', {
        'cfg_json': json.dumps(cfg),
        'has_data': MonthlyTraffic.objects.exists(),
    })


@login_required
def data_traffic(request):
    comp_by_domain = {c.domain.lower(): c.name for c in Competitor.objects.exclude(domain='')}
    rows = []
    for t in MonthlyTraffic.objects.all():
        rows.append({
            'month': t.month.isoformat()[:7],
            'domain': t.domain,
            'label': comp_by_domain.get(t.domain.lower(), t.domain),
            'visits': t.visits,
        })
    return JsonResponse({'rows': rows})
