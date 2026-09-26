"""
قاعدة بيانات لتطبيق تسجيل الحضور
يدعم SQLite محلياً و PostgreSQL على الاستضافة
"""

import json
import hashlib
import os
from datetime import datetime, timezone

DATABASE_URL = os.environ.get('DATABASE_URL', '')

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    IS_POSTGRES = True
    # Railway يضع postgres:// لكن psycopg2 يحتاج postgresql://
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    import sqlite3
    IS_POSTGRES = False
    _DB_DIR = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')
    DATABASE = os.path.join(_DB_DIR, 'attendance.db') if _DB_DIR else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attendance.db')


def get_db_connection():
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        import sqlite3
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn


def _ph():
    """Placeholder - ? للـ SQLite و %s للـ PostgreSQL"""
    return '%s' if IS_POSTGRES else '?'


def _autoincrement():
    return 'SERIAL' if IS_POSTGRES else 'INTEGER'


def _ignore():
    return 'ON CONFLICT DO NOTHING' if IS_POSTGRES else 'OR IGNORE'


def _migrate_sqlite_to_postgres(conn):
    if not IS_POSTGRES:
        return

    sqlite_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attendance.db')
    if not os.path.isfile(sqlite_path):
        return

    import sqlite3
    from psycopg2 import sql

    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()

    lock_id = 74289153021
    migration_name = 'sqlite-attendance-db-v1'
    cursor.execute('SELECT pg_advisory_lock(%s)', (lock_id,))
    conn.commit()
    try:
        cursor.execute('SELECT 1 FROM app_migrations WHERE name = %s', (migration_name,))
        if cursor.fetchone():
            return

        tables = (
            'students', 'subjects', 'attendance', 'users', 'settings',
            'grades', 'access_codes', 'active_subject_qr_sessions', 'promotion_log'
        )
        imported_total = 0
        source_uri = f"file:{sqlite_path.replace(os.sep, '/')}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            source.row_factory = sqlite3.Row
            for table in tables:
                exists = source.execute(
                    'SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?',
                    ('table', table)
                ).fetchone()
                if not exists:
                    continue

                source_columns = [
                    row['name'] for row in source.execute(f'PRAGMA table_info("{table}")')
                ]
                cursor.execute(sql.SQL('SELECT * FROM {} LIMIT 0').format(sql.Identifier(table)))
                target_columns = {column[0] for column in cursor.description}
                columns = [column for column in source_columns if column in target_columns]
                if 'id' not in columns:
                    continue

                identifiers = sql.SQL(', ').join(sql.Identifier(column) for column in columns)
                placeholders = sql.SQL(', ').join(sql.Placeholder() for _ in columns)
                if table == 'users':
                    updates = [column for column in columns if column != 'id']
                    conflict_action = sql.SQL('DO UPDATE SET {}').format(
                        sql.SQL(', ').join(
                            sql.SQL('{} = EXCLUDED.{}').format(sql.Identifier(column), sql.Identifier(column))
                            for column in updates
                        )
                    )
                else:
                    conflict_action = sql.SQL('DO NOTHING')
                insert = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) {}').format(
                    sql.Identifier(table), identifiers, placeholders, conflict_action
                )

                imported_rows = 0
                for row in source.execute(f'SELECT * FROM "{table}"'):
                    cursor.execute(insert, tuple(row[column] for column in columns))
                    imported_rows += max(cursor.rowcount, 0)
                imported_total += imported_rows
                if imported_rows:
                    print(f'[DB Migration] {table}: {imported_rows} rows')

                cursor.execute('SELECT pg_get_serial_sequence(%s, %s) AS sequence_name', (table, 'id'))
                sequence = cursor.fetchone()['sequence_name']
                if sequence:
                    cursor.execute(sql.SQL('SELECT MAX(id) AS max_id FROM {}').format(sql.Identifier(table)))
                    max_id = cursor.fetchone()['max_id']
                    if max_id is not None:
                        cursor.execute('SELECT setval(%s, %s, true)', (sequence, max_id))

        cursor.execute('INSERT INTO app_migrations (name) VALUES (%s) ON CONFLICT (name) DO NOTHING', (migration_name,))
        conn.commit()
        print(f'[DB Migration] SQLite import complete: {imported_total} rows')
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))
        conn.commit()

def init_database():
    """تهيئة قاعدة البيانات وإنشاء الجداول"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    PK = 'SERIAL PRIMARY KEY' if IS_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    IGNORE = 'ON CONFLICT DO NOTHING' if IS_POSTGRES else 'OR IGNORE'

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS students (
            id {PK},
            full_name TEXT NOT NULL,
            department TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            group_name TEXT NOT NULL,
            student_code TEXT UNIQUE NOT NULL,
            qr_code TEXT NOT NULL,
            qr_image TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS subjects (
            id {PK},
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            department TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            grade TEXT DEFAULT 'الأولى',
            course_type TEXT DEFAULT 'أول',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS attendance (
            id {PK},
            student_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            group_name TEXT,
            attendance_date DATE DEFAULT CURRENT_DATE,
            attendance_time TIME DEFAULT CURRENT_TIME
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_sync_events (
            event_id TEXT PRIMARY KEY,
            student_id INTEGER,
            subject_id INTEGER NOT NULL,
            scanned_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance_sync_events (
            event_id TEXT PRIMARY KEY,
            student_id INTEGER,
            subject_id INTEGER NOT NULL,
            scanned_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS users (
            id {PK},
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY {'DEFAULT 1' if IS_POSTGRES else ''},
            data TEXT NOT NULL DEFAULT '{{}}'
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS grades (
            id {PK},
            student_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            grade REAL NOT NULL,
            max_grade REAL DEFAULT 100,
            exam_type TEXT DEFAULT 'نهائي',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS access_codes (
            id {PK},
            student_id INTEGER NOT NULL,
            access_code TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS active_subject_qr_sessions (
            id {PK},
            subject_id INTEGER NOT NULL,
            session_token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')


    admin_pw = hashlib.sha256('admin'.encode()).hexdigest()
    if IS_POSTGRES:
        cursor.execute('''
            INSERT INTO users (id, username, password)
            VALUES (1, 'admin', %s)
            ON CONFLICT DO NOTHING
        ''', (admin_pw,))
    else:
        cursor.execute('''
            INSERT OR IGNORE INTO users (id, username, password) 
            VALUES (1, 'admin', ?)
        ''', (admin_pw,))
    
    conn.commit()

    # Migration: أضف qr_image إذا ما موجود
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS qr_image TEXT")
        else:
            cursor.execute('ALTER TABLE students ADD COLUMN qr_image TEXT')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف profile_image إذا ما موجود
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS profile_image TEXT")
        else:
            cursor.execute('ALTER TABLE students ADD COLUMN profile_image TEXT')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف password للطلاب إذا ما موجود
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS password TEXT")
        else:
            cursor.execute('ALTER TABLE students ADD COLUMN password TEXT')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف expires_at لجدول active_subject_qr_sessions
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE active_subject_qr_sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP")
        else:
            cursor.execute('ALTER TABLE active_subject_qr_sessions ADD COLUMN expires_at TIMESTAMP')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف grade و course_type لجدول subjects إذا ما موجودين
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE subjects ADD COLUMN IF NOT EXISTS grade TEXT DEFAULT 'الأولى'")
        else:
            cursor.execute("ALTER TABLE subjects ADD COLUMN grade TEXT DEFAULT 'الأولى'")
        conn.commit()
    except Exception:
        pass

    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE subjects ADD COLUMN IF NOT EXISTS course_type TEXT DEFAULT 'أول'")
        else:
            cursor.execute("ALTER TABLE subjects ADD COLUMN course_type TEXT DEFAULT 'أول'")
        conn.commit()
    except Exception:
        pass

    # Migration: إنشاء جدول promotion_log إذا ما موجود
    try:
        PK = 'SERIAL PRIMARY KEY' if IS_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS promotion_log (
                id {PK},
                from_grade TEXT NOT NULL,
                to_grade TEXT NOT NULL,
                student_ids TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف password للطلاب
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS password TEXT")
        else:
            cursor.execute('ALTER TABLE students ADD COLUMN password TEXT')
        conn.commit()
    except Exception:
        pass

    # Migration: أضف profile_image للطلاب
    try:
        if IS_POSTGRES:
            cursor.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS profile_image TEXT")
        else:
            cursor.execute('ALTER TABLE students ADD COLUMN profile_image TEXT')
        conn.commit()
    except Exception:
        pass

    if IS_POSTGRES:
        _migrate_sqlite_to_postgres(conn)

    conn.close()

def add_student(full_name, department, teacher_name, group_name, student_code, qr_code, qr_image=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    p = '%s' if IS_POSTGRES else '?'
    try:
        cursor.execute(f'''
            INSERT INTO students (full_name, department, teacher_name, group_name, student_code, qr_code, qr_image)
            VALUES ({p},{p},{p},{p},{p},{p},{p})
        ''', (full_name, department, teacher_name, group_name, student_code, qr_code, qr_image))
        conn.commit()
        if IS_POSTGRES:
            cursor.execute('SELECT lastval()')
            student_id = cursor.fetchone()[0]
        else:
            student_id = cursor.lastrowid
        return student_id
    except Exception as e:
        print(f"Error adding student: {e}")
        return None
    finally:
        conn.close()

def _q(sql):
    """تحويل ? إلى %s للـ PostgreSQL"""
    return sql.replace('?', '%s') if IS_POSTGRES else sql


def get_student_by_id(student_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(_q('SELECT * FROM students WHERE id = ?'), (student_id,))
    student = cursor.fetchone()
    conn.close()
    return dict(student) if student else None


def get_student_by_code(student_code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(_q('SELECT * FROM students WHERE student_code = ?'), (student_code,))
    student = cursor.fetchone()
    conn.close()
    return dict(student) if student else None

def get_all_students():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM students ORDER BY full_name ASC')
        students = cursor.fetchall()
        conn.close()
        return [dict(s) for s in students] if students else []
    except Exception as e:
        print(f'[DB Error] get_all_students: {e}')
        return []

def delete_student(student_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('DELETE FROM attendance WHERE student_id = ?'), (student_id,))
        cursor.execute(_q('DELETE FROM students WHERE id = ?'), (student_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] delete_student: {e}')
        return False

# ... باقي الدوال تبقى كما هي (add_subject, get_all_subjects, etc.)

def add_subject(name, code, department, teacher_name, grade='الأولى', course_type='أول'):
    import uuid as _uuid
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # نولد code فريد دائماً بغض النظر عن القيمة المرسلة
        unique_code = _uuid.uuid4().hex[:12].upper()
        cursor.execute(_q('''
            INSERT INTO subjects (name, code, department, teacher_name, grade, course_type)
            VALUES (?,?,?,?,?,?)
        '''), (name, unique_code, department, teacher_name, grade, course_type))
        conn.commit()
        if IS_POSTGRES:
            cursor.execute('SELECT lastval()')
            subject_id = cursor.fetchone()[0]
        else:
            subject_id = cursor.lastrowid
        return subject_id
    except Exception as e:
        print(f"Error adding subject: {e}")
        return None
    finally:
        conn.close()

def get_all_subjects():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM subjects ORDER BY created_at DESC')
        subjects = cursor.fetchall()
        conn.close()
        return [dict(s) for s in subjects] if subjects else []
    except Exception as e:
        print(f'[DB Error] get_all_subjects: {e}')
        return []

def get_subjects_by_department(department):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT * FROM subjects WHERE department = ?'), (department,))
        subjects = cursor.fetchall()
        conn.close()
        return [dict(s) for s in subjects] if subjects else []
    except Exception as e:
        print(f'[DB Error] get_subjects_by_department: {e}')
        return []

def delete_subject(subject_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('DELETE FROM attendance WHERE subject_id = ?'), (subject_id,))
        cursor.execute(_q('DELETE FROM subjects WHERE id = ?'), (subject_id,))
        try:
            cursor.execute(_q('DELETE FROM subject_names_cache WHERE subject_id = ?'), (subject_id,))
        except Exception:
            pass
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] delete_subject: {e}')
        return False

def attendance_sync_event_exists(event_id):
    if not event_id:
        return False
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(_q('SELECT 1 FROM attendance_sync_events WHERE event_id = ?'), (event_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def mark_attendance_sync_event(event_id, subject_id, scanned_at):
    conn = get_db_connection()
    cursor = conn.cursor()
    event_time = scanned_at.astimezone(timezone.utc).replace(tzinfo=None) if scanned_at.tzinfo else scanned_at
    values = (event_id, subject_id, event_time.strftime('%Y-%m-%d %H:%M:%S'))
    if IS_POSTGRES:
        cursor.execute('''
            INSERT INTO attendance_sync_events (event_id, subject_id, scanned_at)
            VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING
        ''', values)
    else:
        cursor.execute('''
            INSERT OR IGNORE INTO attendance_sync_events (event_id, subject_id, scanned_at)
            VALUES (?, ?, ?)
        ''', values)
    conn.commit()
    inserted = cursor.rowcount > 0
    conn.close()
    return inserted


def record_attendance(student_id, subject_id, group_name=None, recorded_at=None, event_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    scanned_at = recorded_at or datetime.now()
    scan_local = scanned_at
    event_time = scanned_at.astimezone(timezone.utc).replace(tzinfo=None) if scanned_at.tzinfo else scanned_at
    scanned_date = scan_local.date().isoformat()
    scanned_time = scan_local.time().replace(tzinfo=None, microsecond=0).isoformat()

    if event_id:
        event_values = (event_id, student_id, subject_id, event_time.strftime('%Y-%m-%d %H:%M:%S'))
        if IS_POSTGRES:
            cursor.execute('''
                INSERT INTO attendance_sync_events (event_id, student_id, subject_id, scanned_at)
                VALUES (%s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING
            ''', event_values)
        else:
            cursor.execute('''
                INSERT OR IGNORE INTO attendance_sync_events (event_id, student_id, subject_id, scanned_at)
                VALUES (?, ?, ?, ?)
            ''', event_values)
        if cursor.rowcount == 0:
            conn.commit()
            conn.close()
            return True

    cursor.execute(_q('''
        SELECT * FROM attendance 
        WHERE student_id = ? AND subject_id = ? AND attendance_date = ?
    '''), (student_id, subject_id, scanned_date))
    if cursor.fetchone():
        conn.rollback()
        conn.close()
        return False
    cursor.execute(_q('''
        INSERT INTO attendance (student_id, subject_id, group_name, attendance_date, attendance_time)
        VALUES (?, ?, ?, ?, ?)
    '''), (student_id, subject_id, group_name, scanned_date, scanned_time))
    conn.commit()
    conn.close()
    return True


def record_attendance_for_subject(subject_id, group_name=None, recorded_at=None, event_id=None):
    """تسجيل حضور مادة كاملة: يضيف حضور لكل طلاب نفس المادة في اليوم الحالي."""
    try:
        if event_id and attendance_sync_event_exists(event_id):
            return {'success': True, 'saved': 0, 'total': 0, 'already_synced': True}

        subject = None
        for item in get_all_subjects():
            if item.get('id') == int(subject_id):
                subject = item
                break

        if not subject:
            return {'success': False, 'message': 'المادة غير موجودة'}

        students = get_all_students()
        matched_students = []
        for student in students:
            try:
                if (
                    (student.get('department') == subject.get('department')) and
                    (student.get('teacher_name') == subject.get('teacher_name')) and
                    (student.get('group_name') == subject.get('course_type'))
                ):
                    matched_students.append(student)
            except Exception:
                continue

        if not matched_students:
            return {'success': False, 'message': 'لا يوجد طلاب مرتبطون بهذه المادة'}

        saved = 0
        for student in matched_students:
            if record_attendance(
                student.get('id'), subject_id, group_name or student.get('group_name'), recorded_at
            ):
                saved += 1

        if event_id:
            mark_attendance_sync_event(event_id, subject_id, recorded_at or datetime.now())

        return {'success': True, 'saved': saved, 'total': len(matched_students), 'message': f'تم تسجيل حضور {saved} طالباً في المادة'}
    except Exception as e:
        print(f'[DB Error] record_attendance_for_subject: {e}')
        return {'success': False, 'message': str(e)}

def get_attendance_by_subject(subject_id, date=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if date:
            cursor.execute(_q('''
                SELECT a.*, s.full_name, s.student_code, s.department, s.teacher_name, s.group_name
                FROM attendance a JOIN students s ON a.student_id = s.id
                WHERE a.subject_id = ? AND a.attendance_date = ?
                ORDER BY a.attendance_time DESC
            '''), (subject_id, date))
        else:
            cursor.execute(_q('''
                SELECT a.*, s.full_name, s.student_code, s.department, s.teacher_name, s.group_name
                FROM attendance a JOIN students s ON a.student_id = s.id
                WHERE a.subject_id = ?
                ORDER BY a.attendance_date DESC, a.attendance_time DESC
            '''), (subject_id,))
        attendance = cursor.fetchall()
        conn.close()
        return [dict(r) for r in attendance] if attendance else []
    except Exception as e:
        print(f'[DB Error] get_attendance_by_subject: {e}')
        return []

def get_attendance_by_student(student_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('''
            SELECT a.*, sub.name as subject_name, sub.teacher_name as subject_teacher
            FROM attendance a JOIN subjects sub ON a.subject_id = sub.id
            WHERE a.student_id = ?
            ORDER BY a.attendance_date DESC, a.attendance_time DESC
        '''), (student_id,))
        attendance = cursor.fetchall()
        conn.close()
        return [dict(r) for r in attendance] if attendance else []
    except Exception as e:
        print(f'[DB Error] get_attendance_by_student: {e}')
        return []

def get_students_attendance_status(subject_id, date=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if not date:
            date = datetime.now().date()
        cursor.execute(_q('SELECT grade FROM subjects WHERE id = ?'), (subject_id,))
        subject_row = cursor.fetchone()
        subject_grade = subject_row['grade'] if subject_row else None
        if subject_grade:
            cursor.execute(_q('''
                SELECT s.id, s.full_name, s.student_code, s.department, s.teacher_name, s.group_name,
                       CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END as is_present,
                       ? as attendance_date
                FROM students s
                LEFT JOIN attendance a ON s.id = a.student_id AND a.subject_id = ? AND a.attendance_date = ?
                WHERE s.teacher_name = ?
                ORDER BY s.full_name ASC
            '''), (date, subject_id, date, subject_grade))
        else:
            cursor.execute(_q('''
                SELECT s.id, s.full_name, s.student_code, s.department, s.teacher_name, s.group_name,
                       CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END as is_present,
                       ? as attendance_date
                FROM students s
                LEFT JOIN attendance a ON s.id = a.student_id AND a.subject_id = ? AND a.attendance_date = ?
                ORDER BY s.full_name ASC
            '''), (date, subject_id, date))
        students = cursor.fetchall()
        conn.close()
        return [dict(s) for s in students] if students else []
    except Exception as e:
        print(f'[DB Error] get_students_attendance_status: {e}')
        return []

def get_subject_latest_attendance_date(subject_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT MAX(attendance_date) as latest_date FROM attendance WHERE subject_id = ?'), (subject_id,))
        row = cursor.fetchone()
        conn.close()
        return row['latest_date'] if row and row['latest_date'] else None
    except Exception as e:
        print(f'[DB Error] get_subject_latest_attendance_date: {e}')
        return None

def get_subject_latest_attendance_date_before(subject_id, date):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT MAX(attendance_date) as latest_date FROM attendance WHERE subject_id = ? AND attendance_date <= ?'), (subject_id, date))
        row = cursor.fetchone()
        conn.close()
        return row['latest_date'] if row and row['latest_date'] else None
    except Exception as e:
        print(f'[DB Error] get_subject_latest_attendance_date_before: {e}')
        return None

def get_attendance_count_by_subject_date(subject_id, date):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT COUNT(*) as count FROM attendance WHERE subject_id = ? AND attendance_date = ?'), (subject_id, date))
        row = cursor.fetchone()
        conn.close()
        return row['count'] if row else 0
    except Exception as e:
        print(f'[DB Error] get_attendance_count_by_subject_date: {e}')
        return 0

def get_today_attendance_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        today = datetime.now().date()
        cursor.execute(_q('''
            SELECT COUNT(DISTINCT student_id) as total_students, COUNT(*) as total_records
            FROM attendance WHERE attendance_date = ?
        '''), (today,))
        stats = cursor.fetchone()
        conn.close()
        return dict(stats) if stats else {'total_students': 0, 'total_records': 0}
    except Exception as e:
        print(f'[DB Error] get_today_attendance_stats: {e}')
        return {'total_students': 0, 'total_records': 0}

def get_last_attendance_time(student_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(_q('''
        SELECT attendance_date, attendance_time FROM attendance 
        WHERE student_id = ? ORDER BY attendance_date DESC, attendance_time DESC LIMIT 1
    '''), (student_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        date_str = str(result['attendance_date'])
        time_str = str(result['attendance_time'] or '00:00:00')
        if len(time_str) == 5:
            time_str += ':00'
        try:
            return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None

def get_student_group_name(student_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT group_name FROM students WHERE id = ?'), (student_id,))
        result = cursor.fetchone()
        conn.close()
        return result['group_name'] if result else 'غير معروف'
    except Exception as e:
        print(f'[DB Error] get_student_group_name: {e}')
        return 'غير معروف'

def verify_user(username, password):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        hashed = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute(_q('SELECT * FROM users WHERE username = ? AND password = ?'), (username, hashed))
        user = cursor.fetchone()
        conn.close()
        return dict(user) if user else None
    except Exception as e:
        print(f'[DB Error] verify_user: {e}')
        return None

def change_user_password(username, current_password, new_password):
    conn = get_db_connection()
    cursor = conn.cursor()
    hashed_current = hashlib.sha256(current_password.encode()).hexdigest()
    cursor.execute(_q('SELECT * FROM users WHERE username = ? AND password = ?'), (username, hashed_current))
    if not cursor.fetchone():
        conn.close()
        return False
    hashed_new = hashlib.sha256(new_password.encode()).hexdigest()
    cursor.execute(_q('UPDATE users SET password = ? WHERE username = ?'), (hashed_new, username))
    conn.commit()
    conn.close()
    return True

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, username, created_at FROM users')
    users = cursor.fetchall()
    conn.close()
    return [dict(u) for u in users]

def add_new_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        hashed = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute(_q('INSERT INTO users (username, password) VALUES (?,?)'), (username, hashed))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

def delete_user_by_username(username):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('DELETE FROM users WHERE username = ?'), (username,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] delete_user_by_username: {e}')
        return False

def get_settings():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT data FROM settings WHERE id = 1')
    result = cursor.fetchone()
    conn.close()
    return json.loads(result['data']) if result else {}

def save_settings(settings_dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    settings_json = json.dumps(settings_dict)
    if IS_POSTGRES:
        cursor.execute('INSERT INTO settings (id, data) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET data = %s', (settings_json, settings_json))
    else:
        cursor.execute('INSERT OR REPLACE INTO settings (id, data) VALUES (1, ?)', (settings_json,))
    conn.commit()
    conn.close()


def add_grade(student_id, subject_id, grade, max_grade=100, exam_type='نهائي', notes=''):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('INSERT INTO grades (student_id, subject_id, grade, max_grade, exam_type, notes) VALUES (?,?,?,?,?,?)'),
                       (student_id, subject_id, grade, max_grade, exam_type, notes))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] add_grade: {e}')
        return False

def get_student_grades(student_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('''
            SELECT g.*, s.name as subject_name, s.teacher_name
            FROM grades g JOIN subjects s ON g.subject_id = s.id
            WHERE g.student_id = ? ORDER BY g.created_at DESC
        '''), (student_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[DB Error] get_student_grades: {e}')
        return []

def generate_access_code(student_id):
    import random, string
    from datetime import timedelta
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        code = ''.join(random.choices(string.digits, k=6))
        expires = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(_q('DELETE FROM access_codes WHERE student_id = ?'), (student_id,))
        cursor.execute(_q('INSERT INTO access_codes (student_id, access_code, expires_at) VALUES (?,?,?)'),
                       (student_id, code, expires))
        conn.commit()
        conn.close()
        return code
    except Exception as e:
        print(f'[DB Error] generate_access_code: {e}')
        return None

def get_all_active_codes():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ac.*, s.full_name as student_name, s.student_code
            FROM access_codes ac JOIN students s ON ac.student_id = s.id
            ORDER BY ac.created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[DB Error] get_all_active_codes: {e}')
        return []

def delete_grade(grade_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('DELETE FROM grades WHERE id = ?'), (grade_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] delete_grade: {e}')
        return False

def delete_access_code(code_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('SELECT student_id FROM access_codes WHERE id = ?'), (code_id,))
        row = cursor.fetchone()
        if row:
            cursor.execute(_q('DELETE FROM grades WHERE student_id = ?'), (row['student_id'],))
        cursor.execute(_q('DELETE FROM access_codes WHERE id = ?'), (code_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] delete_access_code: {e}')
        return False



# ═════ جلسات QR للمواد - لتوليد QR فريد في كل مرة ═════

def create_subject_qr_session(subject_id, duration_minutes=30):
    """ينشئ جلسة QR جديدة للمادة - يلغي الجلسات القديمة تلقائياً
    
    Args:
        subject_id: معرف المادة
        duration_minutes: مدة صلاحية QR بالدقائق (افتراضي 30 دقيقة)
    """
    import uuid
    from datetime import timedelta
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Keep expired QR sessions briefly so offline scans can be validated at scan time.
        from datetime import timedelta
        retention_cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(_q('DELETE FROM active_subject_qr_sessions WHERE expires_at <= ?'), (retention_cutoff,))
        
        # أنشئ جلسة جديدة بـ token فريد
        session_token = uuid.uuid4().hex
        
        # حساب وقت انتهاء الصلاحية
        expires_at = datetime.now() + timedelta(minutes=duration_minutes)
        
        cursor.execute(_q('INSERT INTO active_subject_qr_sessions (subject_id, session_token, expires_at) VALUES (?,?,?)'),
                       (subject_id, session_token, expires_at.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return {
            'token': session_token,
            'expires_at': expires_at.strftime('%Y-%m-%d %H:%M:%S'),
            'duration_minutes': duration_minutes
        }
    except Exception as e:
        print(f'[DB Error] create_subject_qr_session: {e}')
        return None


def verify_subject_qr_session(subject_id, session_token):
    """يتحقق من صلاحية الـ session_token للمادة - يجب أن يكون موجوداً وغير منتهي"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute(_q('''
            SELECT * FROM active_subject_qr_sessions 
            WHERE subject_id = ? AND session_token = ? AND expires_at > ?
        '''), (subject_id, session_token, now))
        
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        print(f'[DB Error] verify_subject_qr_session: {e}')
        return False


def verify_subject_qr_session_at(subject_id, session_token, scanned_at):
    """Verify that a queued scan occurred while its subject QR session was valid."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('''
            SELECT created_at, expires_at FROM active_subject_qr_sessions
            WHERE subject_id = ? AND session_token = ?
        '''), (subject_id, session_token))
        result = cursor.fetchone()
        conn.close()
        if not result or not result['expires_at']:
            return False

        created_at = result['created_at']
        expires_at = result['expires_at']
        if not isinstance(created_at, datetime):
            created_at = datetime.fromisoformat(str(created_at))
        if not isinstance(expires_at, datetime):
            expires_at = datetime.fromisoformat(str(expires_at))
        return created_at <= scanned_at <= expires_at
    except Exception as e:
        print(f'[DB Error] verify_subject_qr_session_at: {e}')
        return False


def cleanup_expired_qr_sessions():
    """ينظف الجلسات المنتهية من قاعدة البيانات"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(_q('DELETE FROM active_subject_qr_sessions WHERE expires_at <= ?'), (cutoff,))
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted_count
    except Exception as e:
        print(f'[DB Error] cleanup_expired_qr_sessions: {e}')
        return 0


# ═════ إعدادات الطالب ═════

def update_student_profile_image(student_id, image_path):
    """تحديث صورة الملف الشخصي للطالب"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('UPDATE students SET profile_image = ? WHERE id = ?'), (image_path, student_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] update_student_profile_image: {e}')
        return False


def update_student_password(student_id, current_password, new_password):
    """تحديث كلمة مرور الطالب (مؤقتاً - الطلاب حالياً ما عندهم كلمات مرور)"""
    try:
        # حالياً الطلاب ما عندهم كلمات مرور
        # هذا placeholder للمستقبل
        # يمكن إضافة حقل password في جدول students لاحقاً
        return True
    except Exception as e:
        print(f'[DB Error] update_student_password: {e}')
        return False


def set_student_password(student_id, new_password):
    """تعيين كلمة مرور جديدة للطالب مباشرة"""
    try:
        import hashlib
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # تشفير كلمة المرور
        hashed_password = hashlib.sha256(new_password.encode()).hexdigest()
        
        # تحديث كلمة المرور في قاعدة البيانات
        cursor.execute(_q('UPDATE students SET password = ? WHERE id = ?'), (hashed_password, student_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] set_student_password: {e}')
        return False


def reset_student_password(student_id):
    """إعادة تعيين كلمة مرور الطالب - تمسح الرمز حتى يقبل أي شي في المرة الجاية"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('UPDATE students SET password = NULL WHERE id = ?'), (student_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f'[DB Error] reset_student_password: {e}')
        return False


def promote_students_grade(from_grade, to_grade):
    """ترقية المرحلة الدراسية لجميع الطلاب من مرحلة إلى أخرى مع حفظ سجل للتراجع"""
    try:
        import json
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # جلب IDs الطلاب المتأثرين أولاً
        cursor.execute(_q('SELECT id FROM students WHERE teacher_name = ?'), (from_grade,))
        affected_ids = [row['id'] for row in cursor.fetchall()]
        
        if not affected_ids:
            conn.close()
            return 0
        
        # إنشاء جدول السجل إذا ما موجود
        try:
            PK = 'SERIAL PRIMARY KEY' if IS_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS promotion_log (
                    id {PK},
                    from_grade TEXT NOT NULL,
                    to_grade TEXT NOT NULL,
                    student_ids TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
        except Exception:
            pass
        
        # تنفيذ الترقية
        cursor.execute(_q('UPDATE students SET teacher_name = ? WHERE teacher_name = ?'), (to_grade, from_grade))
        count = cursor.rowcount
        
        # حفظ سجل التراجع
        cursor.execute(_q('INSERT INTO promotion_log (from_grade, to_grade, student_ids) VALUES (?,?,?)'),
                      (from_grade, to_grade, json.dumps(affected_ids)))
        
        conn.commit()
        conn.close()
        print(f'[DB] ✓ Promoted {count} students from {from_grade} to {to_grade}')
        return count
    except Exception as e:
        print(f'[DB Error] promote_students_grade: {e}')
        return 0


def get_last_promotion_log():
    """جلب آخر عملية ترقية لعكسها"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(_q('''
            SELECT * FROM promotion_log 
            ORDER BY created_at DESC LIMIT 1
        '''))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f'[DB Error] get_last_promotion_log: {e}')
        return None


def undo_last_promotion():
    """التراجع عن آخر عملية ترقية"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # جلب آخر عملية
        cursor.execute(_q('''
            SELECT * FROM promotion_log 
            ORDER BY created_at DESC LIMIT 1
        '''))
        log = cursor.fetchone()
        
        if not log:
            conn.close()
            return 0, 'لا توجد عملية ترقية سابقة'
        
        log = dict(log)
        from_grade = log['to_grade']   # عكس الاتجاه
        to_grade = log['from_grade']   # عكس الاتجاه
        student_ids = log['student_ids']  # الطلاب المتأثرين فقط
        
        import json
        ids_list = json.loads(student_ids)
        
        if not ids_list:
            conn.close()
            return 0, 'لا توجد بيانات للتراجع'
        
        # إرجاع الطلاب المتأثرين فقط
        count = 0
        for sid in ids_list:
            cursor.execute(_q('UPDATE students SET teacher_name = ? WHERE id = ? AND teacher_name = ?'),
                          (to_grade, sid, from_grade))
            if cursor.rowcount > 0:
                count += 1
        
        # حذف سجل الترقية
        cursor.execute(_q('DELETE FROM promotion_log WHERE id = ?'), (log['id'],))
        
        conn.commit()
        conn.close()
        return count, f'تم إرجاع {count} طالب من {from_grade} إلى {to_grade}'
    except Exception as e:
        print(f'[DB Error] undo_last_promotion: {e}')
        return 0, str(e)
