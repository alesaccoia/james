import json
from decimal import Decimal, InvalidOperation

from django.db import models, transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (AnalyticsSource, FieldDefinition, IngestionLog,
                     MetricSnapshot, SubjectEvent)


PROHIBITED_FIELD_NAMES = {
    'email', 'email_address', 'phone', 'phone_number', 'telephone',
    'first_name', 'last_name', 'full_name', 'contact_name',
}


def _error(message, status=400, **extra):
    return JsonResponse({'ok': False, 'error': message, **extra}, status=status)


def _body(request):
    try:
        value = json.loads(request.body or b'{}')
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError('Request body must be a JSON object.')
    return value


def _source_from_request(request):
    slug = request.headers.get('X-Source-Slug', '')
    source = AnalyticsSource.objects.filter(slug=slug, is_active=True).first()
    auth = request.headers.get('Authorization', '')
    raw_key = auth[len('Bearer '):] if auth.startswith('Bearer ') else request.headers.get('X-Api-Key', '')
    if source is None or not source.check_api_key(raw_key):
        return None
    return source


def _field_map(source):
    rows = FieldDefinition.objects.filter(is_active=True).filter(
        models.Q(source__isnull=True) | models.Q(source=source))
    return {row.key: row for row in rows}


def _validate_scalar(definition, value):
    if isinstance(value, (dict, list)):
        raise ValueError(f'{definition.key} must be a scalar value.')
    if definition.sensitivity == 'prohibited':
        raise ValueError(f'{definition.key} is prohibited by the data contract.')
    if definition.data_type == 'number':
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f'{definition.key} must be numeric.')
    elif definition.data_type == 'boolean' and not isinstance(value, bool):
        raise ValueError(f'{definition.key} must be boolean.')
    elif definition.data_type == 'date' and not parse_date(str(value)):
        raise ValueError(f'{definition.key} must be an ISO date.')
    elif definition.data_type == 'datetime' and not parse_datetime(str(value)):
        raise ValueError(f'{definition.key} must be an ISO datetime.')
    elif definition.data_type == 'enum' and value not in definition.enum_values:
        raise ValueError(f'{definition.key} is not an allowed enum value.')


def _validate_fields(source, dimensions, measures):
    definitions = _field_map(source)
    for role, values in (('dimension', dimensions), ('measure', measures)):
        if not isinstance(values, dict):
            raise ValueError(f'{role}s must be an object.')
        for key, value in values.items():
            if key.rsplit('.', 1)[-1].lower() in PROHIBITED_FIELD_NAMES:
                raise ValueError(f'{key} is direct contact information and cannot be ingested.')
            definition = definitions.get(key)
            if definition is None:
                raise ValueError(f'Unregistered field: {key}.')
            if definition.role != role:
                raise ValueError(f'{key} is registered as {definition.role}, not {role}.')
            _validate_scalar(definition, value)


def _register_fields(source, rows):
    if not isinstance(rows, list):
        raise ValueError('fields must be an array.')
    created = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each field definition must be an object.')
        namespace = str(row.get('namespace') or '').strip()
        name = str(row.get('name') or '').strip()
        if not namespace or not name:
            raise ValueError('Field namespace and name are required.')
        if name.lower() in PROHIBITED_FIELD_NAMES:
            raise ValueError(f'{namespace}.{name} is prohibited.')
        defaults = {
            'data_type': row.get('data_type'),
            'role': row.get('role'),
            'sensitivity': row.get('sensitivity', 'internal'),
            'aggregation': row.get('aggregation', 'none'),
            'description': row.get('description', ''),
            'enum_values': row.get('enum_values', []),
            'schema_version': int(row.get('schema_version', 1)),
            'is_active': True,
        }
        field, was_created = FieldDefinition.objects.get_or_create(
            source=source, namespace=namespace, name=name, defaults=defaults)
        if not was_created:
            immutable = ('data_type', 'role', 'sensitivity')
            for attr in immutable:
                if getattr(field, attr) != defaults[attr]:
                    raise ValueError(
                        f'Cannot silently change {field.key} {attr}; create a new field/version.')
        created += int(was_created)
    return created


@csrf_exempt
@require_POST
def ingest_events(request):
    source = _source_from_request(request)
    if source is None:
        return _error('Invalid source or API key.', status=401)
    log = IngestionLog.objects.create(source=source, kind='events')
    try:
        payload = _body(request)
        events = payload.get('events', [])
        if not isinstance(events, list):
            raise ValueError('events must be an array.')
        created = updated = stale = 0
        with transaction.atomic():
            _register_fields(source, payload.get('fields', []))
            for row in events:
                if not isinstance(row, dict):
                    raise ValueError('Each event must be an object.')
                if row.get('schema_version') != 1:
                    raise ValueError('Unsupported event schema_version.')
                subject = str(row.get('external_subject_id') or '')
                if source.identity_mode == 'aggregate_only' and subject:
                    raise ValueError('aggregate_only sources cannot send subject IDs.')
                occurred_at = parse_datetime(str(row.get('occurred_at') or ''))
                if occurred_at is None:
                    raise ValueError('occurred_at must be an ISO datetime.')
                dimensions = row.get('dimensions', {})
                measures = row.get('measures', {})
                _validate_fields(source, dimensions, measures)
                event_id = str(row.get('event_id') or '')
                version = int(row.get('event_version') or 0)
                event_type = str(row.get('event_type') or '')
                if not event_id or version < 1 or not event_type:
                    raise ValueError('event_id, event_version and event_type are required.')
                event = SubjectEvent.objects.filter(source=source, event_id=event_id).first()
                defaults = {
                    'event_version': version,
                    'external_subject_id': subject,
                    'event_type': event_type,
                    'occurred_at': occurred_at,
                    'dimensions': dimensions,
                    'measures': measures,
                }
                if event is None:
                    SubjectEvent.objects.create(source=source, event_id=event_id, **defaults)
                    created += 1
                elif version < event.event_version:
                    stale += 1
                elif version == event.event_version:
                    current = {key: getattr(event, key) for key in defaults}
                    if current != defaults:
                        raise ValueError(f'Conflicting replay for {event_id} version {version}.')
                    stale += 1
                else:
                    for key, value in defaults.items():
                        setattr(event, key, value)
                    event.save(update_fields=[*defaults, 'updated_at'])
                    updated += 1
        log.records_received = len(events)
        log.records_created = created
        log.records_updated = updated
        log.records_stale = stale
        log.save()
        return JsonResponse({'ok': True, 'received': len(events), 'created': created,
                             'updated': updated, 'stale': stale})
    except Exception as exc:
        log.ok = False
        log.error = str(exc)
        log.save(update_fields=['ok', 'error'])
        return _error(str(exc))


@csrf_exempt
@require_POST
def ingest_snapshots(request):
    source = _source_from_request(request)
    if source is None:
        return _error('Invalid source or API key.', status=401)
    log = IngestionLog.objects.create(source=source, kind='snapshots')
    try:
        payload = _body(request)
        snapshots = payload.get('snapshots', [])
        if not isinstance(snapshots, list):
            raise ValueError('snapshots must be an array.')
        created = updated = 0
        with transaction.atomic():
            _register_fields(source, payload.get('fields', []))
            for row in snapshots:
                dimensions = row.get('dimensions', {})
                measures = row.get('measures', {})
                _validate_fields(source, dimensions, measures)
                as_of = parse_datetime(str(row.get('as_of') or ''))
                key = str(row.get('snapshot_key') or '')
                if row.get('schema_version') != 1 or as_of is None or not key:
                    raise ValueError('Invalid snapshot schema_version, key or as_of.')
                _, was_created = MetricSnapshot.objects.update_or_create(
                    source=source, snapshot_key=key,
                    defaults={'as_of': as_of, 'dimensions': dimensions, 'measures': measures})
                created += int(was_created)
                updated += int(not was_created)
        log.records_received = len(snapshots)
        log.records_created = created
        log.records_updated = updated
        log.save()
        return JsonResponse({'ok': True, 'received': len(snapshots),
                             'created': created, 'updated': updated})
    except Exception as exc:
        log.ok = False
        log.error = str(exc)
        log.save(update_fields=['ok', 'error'])
        return _error(str(exc))
