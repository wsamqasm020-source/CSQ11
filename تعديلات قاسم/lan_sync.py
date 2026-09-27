import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

import database
from database import (
    attendance_sync_event_exists,
    get_all_subjects,
    get_student_by_code,
    mark_attendance_sync_event,
    record_attendance,
)


logger = logging.getLogger(__name__)
lan_sync_blueprint = Blueprint('lan_sync', __name__, url_prefix='/api/lan-sync')

_worker_lock = threading.Lock()
_worker_started = False
_last_snapshot_at = 0.0


def _configured_token():
    return os.environ.get('LAN_SYNC_TOKEN', '')


@lan_sync_blueprint.before_request
def require_lan_sync_token():
    expected = _configured_token()
    if not expected:
        return jsonify({'success': False, 'message': 'LAN sync is not configured'}), 503

    provided = request.headers.get('X-LAN-SYNC-TOKEN', '')
    if not hmac.compare_digest(expected, provided):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    return None


@lan_sync_blueprint.get('/snapshot')
def export_snapshot():
    return jsonify({'success': True, 'snapshot': database.export_lan_sync_snapshot()})


def _parse_scanned_at(value):
    try:
        scanned_at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError) as error:
        raise ValueError('وقت المسح غير صالح') from error

    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    scanned_utc = scanned_at.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if scanned_utc < now - timedelta(days=7) or scanned_utc > now + timedelta(minutes=5):
        raise ValueError('انتهت صلاحية سجل الحضور غير المتصل')
    return scanned_at


def _sync_attendance_event(event):
    if not isinstance(event, dict):
        raise ValueError('بيانات الحضور غير صالحة')

    try:
        event_id = str(uuid.UUID(str(event.get('event_id', '')).strip()))
    except (ValueError, AttributeError) as error:
        raise ValueError('معرّف المزامنة غير صالح') from error

    student_code = str(event.get('student_code') or '').strip()
    subject_code = str(event.get('subject_code') or '').strip()
    if not student_code or not subject_code:
        raise ValueError('بيانات الطالب أو المادة غير موجودة')

    scanned_at = _parse_scanned_at(event.get('scanned_at'))
    if attendance_sync_event_exists(event_id):
        return {'event_id': event_id, 'success': True, 'already_synced': True}

    student = get_student_by_code(student_code)
    subject = next(
        (item for item in get_all_subjects() if str(item.get('code')) == subject_code),
        None
    )
    if not student or not subject:
        return {
            'event_id': event_id,
            'success': False,
            'message': 'الطالب أو المادة غير موجودين في قاعدة السحابة'
        }

    subject_id = subject['id']
    saved = record_attendance(
        student['id'],
        subject_id,
        student.get('group_name', 'غير معروف'),
        recorded_at=scanned_at,
        event_id=event_id
    )
    if not saved:
        # Treat an existing same-day attendance row as a completed replay.
        mark_attendance_sync_event(event_id, subject_id, scanned_at)

    return {'event_id': event_id, 'success': True, 'already_registered': not saved}


@lan_sync_blueprint.post('/attendance')
def import_attendance():
    payload = request.get_json(silent=True) or {}
    events = payload.get('events')
    if not isinstance(events, list) or len(events) > 100:
        return jsonify({'success': False, 'message': 'دفعة المزامنة غير صالحة'}), 400

    results = []
    for event in events:
        try:
            results.append(_sync_attendance_event(event))
        except ValueError as error:
            results.append({
                'event_id': event.get('event_id') if isinstance(event, dict) else None,
                'success': False,
                'message': str(error)
            })
        except Exception:
            logger.exception('LAN attendance sync failed')
            results.append({
                'event_id': event.get('event_id') if isinstance(event, dict) else None,
                'success': False,
                'message': 'تعذر مزامنة سجل الحضور'
            })
    return jsonify({'success': True, 'results': results})


def _remote_request(path, payload=None):
    base_url = os.environ.get('LAN_SYNC_URL', '').rstrip('/')
    token = _configured_token()
    if not base_url or not token:
        raise RuntimeError('LAN_SYNC_URL or LAN_SYNC_TOKEN is not configured')
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != 'https' and parsed_url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('LAN_SYNC_URL must use HTTPS')

    body = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {'X-LAN-SYNC-TOKEN': token, 'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(
        f'{base_url}/{path.lstrip("/")}',
        data=body,
        headers=headers,
        method='GET' if body is None else 'POST'
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode('utf-8'))


def sync_lan_data_once():
    global _last_snapshot_at
    if database.IS_POSTGRES:
        return {'snapshot_rows': 0, 'synced_events': 0}

    snapshot_rows = 0
    now = time.monotonic()
    if now - _last_snapshot_at >= 300:
        snapshot_result = _remote_request('snapshot')
        if snapshot_result.get('success') and isinstance(snapshot_result.get('snapshot'), dict):
            snapshot_rows = database.apply_lan_sync_snapshot(snapshot_result['snapshot'])
            _last_snapshot_at = now

    events = database.get_pending_lan_attendance(limit=100)
    if not events:
        return {'snapshot_rows': snapshot_rows, 'synced_events': 0}

    result = _remote_request('attendance', {'events': events})
    synced_events = 0
    for item in result.get('results', []):
        if item.get('success') and item.get('event_id'):
            if database.mark_lan_attendance_synced(item['event_id']):
                synced_events += 1
    return {'snapshot_rows': snapshot_rows, 'synced_events': synced_events}


def _sync_worker():
    while True:
        try:
            sync_lan_data_once()
        except Exception as error:
            logger.info('LAN cloud sync deferred: %s', error)
        time.sleep(30)


def start_lan_sync_worker():
    global _worker_started
    if database.IS_POSTGRES:
        return False
    if not os.environ.get('LAN_SYNC_URL') or not _configured_token():
        return False

    with _worker_lock:
        if _worker_started:
            return True
        worker = threading.Thread(target=_sync_worker, daemon=True, name='lan-cloud-sync')
        worker.start()
        _worker_started = True
    return True
