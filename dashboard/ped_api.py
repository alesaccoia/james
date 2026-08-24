"""Service API for the canonical editorial calendar owned by JAMES."""

import json
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.utils.dateparse import parse_date, parse_time
from django.views.decorators.csrf import csrf_exempt

from .models import ContentPiece, EditorialChange


def _authorized(request):
    expected = settings.PED_SERVICE_TOKEN
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _serialize(piece):
    return {'id': piece.pk, 'external_origin': piece.external_origin,
            'external_ref': piece.external_ref, 'title': piece.title,
            'channel': piece.channel, 'content_format': piece.content_format,
            'planned_date': piece.planned_date.isoformat(),
            'planned_time': piece.planned_time.isoformat(timespec='minutes') if piece.planned_time else None,
            'published_date': piece.published_date.isoformat() if piece.published_date else None,
            'status': piece.status, 'owner': piece.owner, 'brief': piece.brief,
            'notes': piece.notes, 'workflow_metadata': piece.workflow_metadata,
            'canonical_version': piece.canonical_version,
            'updated_at': piece.updated_at.isoformat()}


def _payload(request):
    value = json.loads(request.body or b'{}')
    if not isinstance(value, dict):
        raise ValueError('Body must be an object.')
    return value


def _values(row):
    planned_date = parse_date(str(row.get('planned_date') or ''))
    if not planned_date:
        raise ValueError('planned_date must be YYYY-MM-DD.')
    status = str(row.get('status') or 'idea')
    if status not in dict(ContentPiece.STATUS_CHOICES):
        raise ValueError('Invalid status.')
    content_format = str(row.get('content_format') or 'post')
    if content_format not in dict(ContentPiece.FORMAT_CHOICES):
        content_format = 'altro'
    return {'title': str(row.get('title') or '').strip()[:250],
            'channel': str(row.get('channel') or 'social')[:40],
            'content_format': content_format, 'planned_date': planned_date,
            'planned_time': parse_time(str(row.get('planned_time') or '')),
            'published_date': parse_date(str(row.get('published_date') or '')),
            'status': status, 'owner': str(row.get('owner') or '')[:120],
            'brief': str(row.get('brief') or ''), 'notes': str(row.get('notes') or ''),
            'workflow_metadata': row.get('workflow_metadata') or {}}


@csrf_exempt
def editorial_calendar(request):
    if not _authorized(request):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=401)
    if request.method == 'GET':
        rows = ContentPiece.objects.all()
        if request.GET.get('start'):
            rows = rows.filter(planned_date__gte=request.GET['start'])
        if request.GET.get('end'):
            rows = rows.filter(planned_date__lte=request.GET['end'])
        if request.GET.get('origin'):
            rows = rows.filter(external_origin=request.GET['origin'])
        return JsonResponse({'ok': True, 'entries': [_serialize(row) for row in rows]})
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Method not allowed'}, status=405)
    try:
        body = _payload(request)
        origin = str(body.get('external_origin') or '').strip()
        ref = str(body.get('external_ref') or '').strip()
        if not origin or not ref:
            raise ValueError('external_origin and external_ref are required.')
        values = _values(body)
        if not values['title']:
            raise ValueError('title is required.')
        piece = ContentPiece.objects.filter(external_origin=origin, external_ref=ref).first()
        created = piece is None
        if piece is None:
            piece = ContentPiece.objects.create(external_origin=origin, external_ref=ref, **values)
            changes = {key: {'to': str(value)} for key, value in values.items()}
        else:
            expected = body.get('expected_version')
            if expected is not None and int(expected) != piece.canonical_version:
                return JsonResponse({'ok': False, 'error': 'Version conflict',
                                     'current': _serialize(piece)}, status=409)
            changes = {key: {'from': str(getattr(piece, key)), 'to': str(value)}
                       for key, value in values.items() if getattr(piece, key) != value}
            if changes:
                for key, value in values.items():
                    setattr(piece, key, value)
                piece.canonical_version += 1
                piece.save()
        if created or changes:
            EditorialChange.objects.create(
                content=piece, external_origin=origin, external_ref=ref,
                version=piece.canonical_version, operation='created' if created else 'updated',
                changes=changes)
        return JsonResponse({'ok': True, 'created': created, 'entry': _serialize(piece)})
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)


@csrf_exempt
def editorial_calendar_item(request, origin, ref):
    if not _authorized(request):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=401)
    piece = ContentPiece.objects.filter(external_origin=origin, external_ref=ref).first()
    if piece is None:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    if request.method == 'DELETE':
        EditorialChange.objects.create(
            content=piece, external_origin=origin, external_ref=ref,
            version=piece.canonical_version + 1, operation='deleted', changes={})
        piece.delete()
        return JsonResponse({'ok': True})
    if request.method != 'PATCH':
        return JsonResponse({'ok': False, 'error': 'Method not allowed'}, status=405)
    try:
        incoming = _payload(request)
        expected = incoming.get('expected_version')
        if expected is not None and int(expected) != piece.canonical_version:
            return JsonResponse({'ok': False, 'error': 'Version conflict',
                                 'current': _serialize(piece)}, status=409)
        body = {**_serialize(piece), **incoming}
        values = _values(body)
        changes = {key: {'from': str(getattr(piece, key)), 'to': str(value)}
                   for key, value in values.items() if getattr(piece, key) != value}
        for key, value in values.items():
            setattr(piece, key, value)
        if changes:
            piece.canonical_version += 1
            piece.save()
            EditorialChange.objects.create(
                content=piece, external_origin=origin, external_ref=ref,
                version=piece.canonical_version, operation='updated', changes=changes)
        return JsonResponse({'ok': True, 'entry': _serialize(piece)})
    except (ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
