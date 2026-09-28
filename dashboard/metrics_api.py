"""API di sola lettura con cui la piattaforma (interfaccia-utenti) legge le
metriche di JAMES. Solo aggregati: nessun identificativo, nessun dato personale.
"""

import re
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .analytics import monthly_leads

# `[0-9]` e non `\d`, che accetta anche le cifre non ASCII; `fullmatch` e non
# `$`, che lascia passare un a capo in coda.
MESE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])')
MAX_MESI = 36


def _authorized(request):
    """Lo stesso schema di `ped_api._authorized`, con la chiave della
    piattaforma: Bearer, confronto a tempo costante, e chiave non impostata =
    nessuno passa. Il confronto è fra byte perché `compare_digest` sulle
    stringhe solleva un errore se l'header porta caratteri non ASCII."""
    expected = settings.PLATFORM_METRICS_API_KEY
    header = request.headers.get('Authorization', '')
    supplied = header[len('Bearer '):] if header.startswith('Bearer ') else ''
    return bool(expected and supplied and secrets.compare_digest(
        expected.encode('utf-8'), supplied.encode('utf-8')))


def _month_number(value):
    """Il mese come numero progressivo, per contare i mesi fra due date; None
    se il valore non è un mese YYYY-MM."""
    if not MESE.fullmatch(value):
        return None
    year, month = value.split('-')
    return int(year) * 12 + int(month)


def _bad_request(message):
    return JsonResponse({'ok': False, 'error': message}, status=400)


@csrf_exempt
def lead_mensili(request):
    """GET /api/v1/metrics/lead-mensili/?da=YYYY-MM&a=YYYY-MM

    I lead entrati per mese e fonte, con call e convertiti della stessa
    coorte (vedi `analytics.monthly_leads`). Le definizioni viaggiano nella
    risposta, così chi la mostra non deve riscriverle.
    """
    if not _authorized(request):
        return JsonResponse({'ok': False, 'error': 'Chiave assente o errata.'},
                            status=401)
    if request.method != 'GET':
        return JsonResponse({'ok': False, 'error': 'Metodo non consentito.'},
                            status=405)
    da, a = request.GET.get('da', ''), request.GET.get('a', '')
    first, last = _month_number(da), _month_number(a)
    if first is None or last is None:
        return _bad_request('da e a devono essere mesi nel formato YYYY-MM.')
    if first > last:
        return _bad_request('da non può venire dopo a.')
    if last - first + 1 > MAX_MESI:
        return _bad_request(f'Al massimo {MAX_MESI} mesi per richiesta.')
    return JsonResponse({
        'generato_il': timezone.localtime().isoformat(timespec='seconds'),
        'da': da, 'a': a,
        **monthly_leads(da, a),
    })
