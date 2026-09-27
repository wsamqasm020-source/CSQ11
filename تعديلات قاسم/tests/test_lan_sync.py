import datetime
import os
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import database
import lan_sync
from lan_sync import lan_sync_blueprint


class LanSyncTests(unittest.TestCase):
    def setUp(self):
        if database.IS_POSTGRES:
            self.skipTest('LAN sync tests require SQLite')
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = database.DATABASE
        self.original_snapshot_time = lan_sync._last_snapshot_at
        database.DATABASE = os.path.join(self.temp_dir.name, 'local.sqlite')
        lan_sync._last_snapshot_at = 0
        database.init_database()

    def tearDown(self):
        database.DATABASE = self.original_database
        lan_sync._last_snapshot_at = self.original_snapshot_time
        self.temp_dir.cleanup()

    def add_test_records(self, student_code='LANSTUDENT01'):
        student_id = database.add_student(
            'LAN Test Student', 'test', 'first', 'morning',
            student_code, f'CODE:{student_code}'
        )
        subject_id = database.add_subject(
            'LAN Test Subject', 'ignored', 'test', 'test', 'first', 'morning'
        )
        subject = next(item for item in database.get_all_subjects() if item['id'] == subject_id)
        return student_id, subject

    def make_sync_client(self):
        app = Flask('lan-sync-test')
        app.register_blueprint(lan_sync_blueprint)
        app.testing = True
        return app.test_client()

    def test_cloud_replay_is_authenticated_and_idempotent(self):
        student_id, subject = self.add_test_records()
        event = {
            'event_id': str(uuid.uuid4()),
            'student_code': 'LANSTUDENT01',
            'subject_code': subject['code'],
            'scanned_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        client = self.make_sync_client()

        with patch.dict(os.environ, {'LAN_SYNC_TOKEN': 'test-token'}):
            self.assertEqual(client.get('/api/lan-sync/snapshot').status_code, 403)
            headers = {'X-LAN-SYNC-TOKEN': 'test-token'}
            first = client.post('/api/lan-sync/attendance', headers=headers, json={'events': [event]})
            second = client.post('/api/lan-sync/attendance', headers=headers, json={'events': [event]})

        self.assertTrue(first.get_json()['results'][0]['success'])
        self.assertTrue(second.get_json()['results'][0]['already_synced'])
        self.assertEqual(len(database.get_attendance_by_student(student_id)), 1)

    def test_local_outbox_preserves_wall_clock_time(self):
        student_id, subject = self.add_test_records()
        local_scan = datetime.datetime(
            2026, 5, 4, 0, 15,
            tzinfo=datetime.timezone(datetime.timedelta(hours=3))
        )
        event_id = str(uuid.uuid4())
        self.assertTrue(database.record_attendance(
            student_id, subject['id'], 'morning', local_scan, event_id
        ))

        event = database.get_pending_lan_attendance()[0]
        synced_time = datetime.datetime.fromisoformat(event['scanned_at'])
        self.assertEqual(synced_time.date(), datetime.date(2026, 5, 4))
        self.assertEqual(synced_time.hour, 0)
        self.assertIsNotNone(synced_time.utcoffset())
        self.assertTrue(database.mark_lan_attendance_synced(event_id))
        self.assertEqual(database.get_pending_lan_attendance(), [])

    def test_student_qr_scan_records_local_attendance_once(self):
        from app import app as attendance_app

        student_id, subject = self.add_test_records()
        qr_session = database.create_subject_qr_session(subject['id'], duration_minutes=30)
        event_id = str(uuid.uuid4())
        qr_data = f"SUBJECT:{subject['id']}|SESSION:{qr_session['token']}"
        client = attendance_app.test_client()
        with client.session_transaction() as session:
            session['student_id'] = student_id

        first = client.post('/student/api/scan-attendance', json={
            'qr_data': qr_data,
            'event_id': event_id
        })
        replay = client.post('/student/api/scan-attendance', json={
            'qr_data': qr_data,
            'event_id': event_id
        })

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()['success'])
        self.assertTrue(replay.get_json()['already_synced'])
        self.assertEqual(len(database.get_attendance_by_student(student_id)), 1)

    def test_snapshot_merge_remaps_ids_and_deduplicates(self):
        student_id, subject = self.add_test_records()
        event_id = str(uuid.uuid4())
        database.record_attendance(
            student_id, subject['id'], 'morning', datetime.datetime.now(), event_id
        )
        conn = database.get_db_connection()
        conn.execute('UPDATE attendance_sync_events SET cloud_synced = 1 WHERE event_id = ?', (event_id,))
        conn.commit()
        conn.close()
        snapshot = database.export_lan_sync_snapshot()

        local_path = os.path.join(self.temp_dir.name, 'mirror.sqlite')
        database.DATABASE = local_path
        database.init_database()
        database.add_student('ID collision', 'test', 'first', 'morning', 'LOCALSTUDENT01', 'CODE:LOCALSTUDENT01')
        database.add_subject('ID collision', 'ignored', 'test', 'test', 'first', 'morning')

        database.apply_lan_sync_snapshot(snapshot)
        database.apply_lan_sync_snapshot(snapshot)

        local_student = database.get_student_by_code('LANSTUDENT01')
        local_subject = next(item for item in database.get_all_subjects() if item['code'] == subject['code'])
        self.assertIsNotNone(local_student)
        self.assertEqual(len(database.get_attendance_by_student(local_student['id'])), 1)
        self.assertEqual(len(database.get_all_students()), 2)
        self.assertEqual(len(database.get_all_subjects()), 2)
        self.assertEqual(database.get_pending_lan_attendance(), [])

    def test_local_worker_marks_only_cloud_accepted_events(self):
        student_id, subject = self.add_test_records()
        event_id = str(uuid.uuid4())
        database.record_attendance(
            student_id, subject['id'], 'morning', datetime.datetime.now(), event_id
        )
        calls = []

        def fake_request(path, payload=None):
            calls.append(path)
            if path == 'snapshot':
                return {'success': True, 'snapshot': {}}
            return {'results': [{'event_id': event_id, 'success': True}]}

        with patch.dict(os.environ, {
            'LAN_SYNC_URL': 'https://cloud.example/api/lan-sync',
            'LAN_SYNC_TOKEN': 'test-token'
        }), patch.object(lan_sync, '_remote_request', side_effect=fake_request):
            result = lan_sync.sync_lan_data_once()

        self.assertEqual(calls, ['snapshot', 'attendance'])
        self.assertEqual(result['synced_events'], 1)
        self.assertEqual(database.get_pending_lan_attendance(), [])

    def test_remote_sync_refuses_plain_http(self):
        with patch.dict(os.environ, {
            'LAN_SYNC_URL': 'http://cloud.example/api/lan-sync',
            'LAN_SYNC_TOKEN': 'test-token'
        }):
            with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
                lan_sync._remote_request('snapshot')

    def test_network_page_advertises_the_actual_listener_port(self):
        with patch('threading.Thread.start'):
            from app import app as attendance_app

        client = attendance_app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'admin'

        fake_socket = SimpleNamespace(
            connect=lambda _address: None,
            getsockname=lambda: ('192.168.137.1', 0),
            close=lambda: None
        )
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='Started')):
            with patch('socket.socket', return_value=fake_socket):
                response = client.get(
                    '/api/network-info',
                    environ_overrides={'SERVER_PORT': '5150'}
                )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['server_port'], '5150')
        self.assertEqual(data['student_url'], 'http://192.168.137.1:5150')
        self.assertEqual(data['teacher_url'], 'http://192.168.137.1:5150')


if __name__ == '__main__':
    unittest.main()
