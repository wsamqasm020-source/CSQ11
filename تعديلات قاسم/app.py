"""
تطبيق تسجيل الحضور بالـ QR
Flask Web Application
"""
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, session, send_from_directory
from functools import wraps
import qrcode
import io
import base64
import os
import hashlib
from datetime import datetime, timedelta, timezone
from database import (
    init_database, add_student, get_student_by_code, get_student_by_id,
    get_all_students, add_subject, get_all_subjects, get_subjects_by_department,
record_attendance, record_attendance_for_subject, get_attendance_by_subject, get_attendance_by_student,
    get_today_attendance_stats, delete_student, delete_subject,
    get_student_group_name, get_last_attendance_time, verify_user, change_user_password,
    get_settings, save_settings, get_all_users, add_new_user, delete_user_by_username,
    get_students_attendance_status, get_subject_latest_attendance_date, get_subject_latest_attendance_date_before,
    get_attendance_count_by_subject_date, attendance_sync_event_exists,
    verify_subject_qr_session_at
)


app = Flask(__name__)
app.secret_key = 'qr_attendance_secret_key_2026'

# ✅ إبقاء الجلسة لمدة 7 أيام
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_PERMANENT'] = True

# ✅ تهيئة قاعدة البيانات - تشغيل في background بعد بدء الـ server
import threading

def initialize_app():
    try:
        with app.app_context():
            init_database()
            print('[App] ✅ Database initialized successfully')
            try:
                from database import cleanup_expired_qr_sessions
                deleted = cleanup_expired_qr_sessions()
                if deleted > 0:
                    print(f'[Cleanup] تم حذف {deleted} جلسة QR منتهية')
            except Exception as e:
                print(f'[Cleanup Error] {e}')
    except Exception as e:
        print(f'[App] ❌ Database init error: {e}')

# تشغيل init في thread منفصل حتى لا يعيق بدء gunicorn
threading.Thread(target=initialize_app, daemon=True).start()

# فترة المنع بين تسجيلات الحضور (بالدقائق)
COOLDOWN_MINUTES = 15


def login_required(f):
    """ديكوريتور للتحقق من تسجيل الدخول"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # مسح أي رسائل قديمة لتجنب التكرار
            session.pop('_flashes', None)
            flash('يرجى تسجيل الدخول أولاً', 'error')
            return redirect(url_for('teacher_login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """ديكوريتور للتحقق من أن المستخدم هو الـ admin"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('username') != 'admin':
            flash('هذه الصفحة متاحة فقط للمدير', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def generate_qr_code(data):
    """توليد QR code من البيانات - دالة موحدة"""
    qr = qrcode.QRCode(
        version=3,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    
    return qr.make_image(fill_color="black", back_color="white")


def qr_to_base64(img):
    """تحويل صورة QR إلى base64"""
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{img_str}"


def generate_student_code(department, teacher_name, group_name):
    """توليد رقم طالب فريد"""
    dept_prefix = department[:2].upper() if department else 'XX'
    teacher_prefix = teacher_name[:2].upper() if teacher_name else 'XX'
    group_prefix = group_name[:2].upper() if group_name else 'XX'
    
    prefix = f"{dept_prefix}{teacher_prefix}{group_prefix}"
    
    students = get_all_students()
    max_num = 0
    for student in students:
        if student['student_code'].startswith(prefix):
            try:
                num = int(student['student_code'][-3:])
                max_num = max(max_num, num)
            except:
                pass
    
    new_num = max_num + 1
    return f"{prefix}{new_num:03d}"


def check_attendance_cooldown(student_id):
    """التحقق من فترة المنع بين تسجيلات الحضور"""
    last_attendance = get_last_attendance_time(student_id)
    
    if not last_attendance:
        return True, 0
    
    current_time = datetime.now()
    elapsed_seconds = (current_time - last_attendance).total_seconds()
    cooldown_seconds = COOLDOWN_MINUTES * 60
    
    if elapsed_seconds < cooldown_seconds:
        remaining_seconds = int(cooldown_seconds - elapsed_seconds)
        return False, remaining_seconds
    
    return True, 0


def format_remaining_time(seconds):
    """تنسيق الوقت المتبقي بالدقائق والثواني"""
    minutes = seconds // 60
    secs = seconds % 60
    if minutes > 0:
        return f"{minutes} دقيقة و {secs} ثانية"
    return f"{secs} ثانية"


def parse_attendance_scan_time(data):
    event_id = str(data.get('event_id') or '').strip()
    if event_id:
        import uuid
        try:
            event_id = str(uuid.UUID(event_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError('معرّف مزامنة غير صالح') from exc

    if not data.get('offline_sync'):
        return event_id or None, datetime.now()
    if not event_id:
        raise ValueError('معرّف المزامنة مطلوب')

    scanned_at_value = data.get('scanned_at')
    try:
        scanned_at = datetime.fromisoformat(str(scanned_at_value).replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise ValueError('وقت المسح غير صالح') from exc

    now = datetime.now()
    utc_scanned_at = scanned_at.astimezone(timezone.utc).replace(tzinfo=None) if scanned_at.tzinfo else scanned_at
    if utc_scanned_at < now - timedelta(days=7) or utc_scanned_at > now + timedelta(minutes=5):
        raise ValueError('انتهت صلاحية سجل الحضور غير المتصل')
    return event_id, scanned_at


# ============================================
# 🔥 ROUTE مهم جداً للـ Service Worker
# ============================================

@app.route('/health')
def health_check():
    """Health check endpoint لـ Railway"""
    return jsonify({'status': 'ok', 'message': 'running'}), 200


@app.route('/sw.js')
def service_worker():
    """تقديم Service Worker"""
    response = send_from_directory('static', 'sw.js', mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@app.route('/offline.html')
def offline_page():
    """صفحة Offline"""
    return send_from_directory('static', 'offline.html')


# ============================================
# Routes تسجيل الدخول (بدون حماية)
# ============================================

@app.route('/')
def unified_index():
    """الصفحة الرئيسية الموحدة"""
    # إذا المستخدم مسجل دخول كمدرس
    if 'user_id' in session:
        return redirect(url_for('index'))
    # إذا المستخدم مسجل دخول كطالب
    elif 'student_id' in session:
        return redirect(url_for('student_dashboard'))
    
    # جلب شعار النظام من الإعدادات (نفس صورة القسم)
    settings_data = get_settings()
    logo_path = settings_data.get('dept_image') or settings_data.get('system_logo') or '/static/icons/icon-192x192.png'
    
    # إضافة timestamp لتجنب cache المتصفح
    import time
    logo_path_with_ts = f"{logo_path}?v={int(time.time())}" if '/uploads/' in logo_path else logo_path
    
    # طباعة للتأكد
    print(f'[unified_index] Logo path from DB: {logo_path}')
    print(f'[unified_index] Logo path with timestamp: {logo_path_with_ts}')
    
    # جلب رسائل الخطأ إن وجدت
    error_msg = session.pop('login_error', None)
    active_tab = session.pop('active_tab', 'teacher')
    
    # عرض صفحة اختيار البوابة بدون تعقيد
    return render_template('unified_login.html', logo_path=logo_path_with_ts, error_msg=error_msg, active_tab=active_tab)


# ============================================
# Routes المدرس (موقع الأستاذ الأصلي)
# ============================================

@app.route('/teacher/login', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def teacher_login():
    """تسجيل دخول المدرس - يعالج POST ويرجع للصفحة الموحدة دائماً"""
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = verify_user(username, password)
        
        if user:
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        else:
            # نحفظ الخطأ في الـ session ونرجع للصفحة الموحدة
            session['login_error'] = 'اسم المستخدم أو كلمة المرور غير صحيحة'
            session['active_tab'] = 'teacher'
            return redirect(url_for('unified_index'))
    
    return redirect(url_for('unified_index'))


@app.route('/logout')
@app.route('/teacher/logout')
def teacher_logout():
    """تسجيل خروج المدرس"""
    session.pop('user_id', None)
    session.pop('username', None)
    flash('تم تسجيل الخروج بنجاح', 'success')
    return redirect(url_for('unified_index'))


# ============================================
# Routes الطالب (بوابة الطلاب)
# ============================================

@app.route('/student/login', methods=['GET', 'POST'])
def student_login():
    """تسجيل دخول الطالب - يعالج POST ويرجع للصفحة الموحدة دائماً"""
    if 'student_id' in session:
        return redirect(url_for('student_dashboard'))
    
    if request.method == 'POST':
        # يقبل كلا الاسمين القديم والجديد
        student_code_input = (request.form.get('student_id') or request.form.get('student_code') or '').strip()
        password_input = (request.form.get('password') or '').strip()
        
        if not student_code_input:
            session['login_error'] = 'الرقم الجامعي مطلوب'
            session['active_tab'] = 'student'
            return redirect(url_for('unified_index'))
        
        # البحث بالـ student_code (الرقم الجامعي) مو بالـ id الرقمي
        student = get_student_by_code(student_code_input)
        
        if student:
            # التحقق من كلمة المرور
            import hashlib
            stored_password = student.get('password')
            
            # إذا الطالب ما عنده كلمة مرور (أول مرة) → يقبل أي شي
            if not stored_password:
                # تسجيل الدخول بنجاح
                session.permanent = True
                session['student_id'] = student['id']
                session['student_name'] = student['full_name']
                session['student_grade'] = student['teacher_name']
                session['student_study_type'] = student['group_name']
                return redirect(url_for('student_dashboard'))
            else:
                # التحقق من كلمة المرور المشفرة
                hashed_input = hashlib.sha256(password_input.encode()).hexdigest()
                if hashed_input == stored_password:
                    # تسجيل الدخول بنجاح
                    session.permanent = True
                    session['student_id'] = student['id']
                    session['student_name'] = student['full_name']
                    session['student_grade'] = student['teacher_name']
                    session['student_study_type'] = student['group_name']
                    return redirect(url_for('student_dashboard'))
                else:
                    session['login_error'] = 'كلمة المرور غير صحيحة'
                    session['active_tab'] = 'student'
                    return redirect(url_for('unified_index'))
        else:
            session['login_error'] = 'الرقم الجامعي غير صحيح، تأكد وحاول مجدداً'
            session['active_tab'] = 'student'
            return redirect(url_for('unified_index'))
    
    return redirect(url_for('unified_index'))


@app.route('/student/logout')
def student_logout():
    """تسجيل خروج الطالب"""
    session.pop('student_id', None)
    session.pop('student_name', None)
    session.pop('student_grade', None)
    session.pop('student_study_type', None)
    return redirect(url_for('unified_index'))


@app.route('/student/dashboard')
def student_dashboard():
    """لوحة تحكم الطالب"""
    if 'student_id' not in session:
        return redirect(url_for('student_login'))
    
    student_id = session['student_id']
    student = get_student_by_id(student_id)
    
    if not student:
        session.clear()
        return redirect(url_for('student_login'))
    
    # جلب سجل الحضور
    attendance = get_attendance_by_student(student_id)
    
    # جلب جميع المواد
    all_subjects = get_all_subjects()
    
    # حساب إحصائيات الحضور لكل مادة
    subject_attendance = {}
    for record in attendance:
        subject_id = record['subject_id']
        if subject_id not in subject_attendance:
            subject_attendance[subject_id] = {
                'name': record.get('subject_name', f'مادة #{subject_id}'),
                'teacher': record.get('subject_teacher', ''),
                'grade': record.get('grade', ''),
                'days': 0
            }
        subject_attendance[subject_id]['days'] += 1
    
    # إحصائيات عامة
    total_days = len(set(f"{r['attendance_date']}-{r['subject_id']}" for r in attendance))
    
    stats = {
        'total_days': total_days,
        'subject_attendance': list(subject_attendance.values())
    }
    
    return render_template('student_dashboard.html',
                         student=student,
                         attendance=attendance,
                         stats=stats,
                         subjects=all_subjects,
                         subject_attendance=subject_attendance)


@app.route('/student/qr-image')
def student_qr_image():
    """توليد صورة QR للطالب"""
    if 'student_id' not in session:
        return '', 403
    
    student = get_student_by_id(session['student_id'])
    if not student:
        return '', 404
    
    qr_text = student.get('qr_code') or student.get('student_code')
    qr_img = generate_qr_code(qr_text)
    
    buffer = io.BytesIO()
    qr_img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return send_file(buffer, mimetype='image/png')


@app.route('/student/scan')
def student_scan_page():
    """صفحة ماسح QR للطالب - يمسح QR المادة ليسجل حضوره"""
    if 'student_id' not in session:
        return redirect(url_for('student_login'))

    student = get_student_by_id(session['student_id'])
    if not student:
        session.clear()
        return redirect(url_for('student_login'))

    return render_template('student_scan.html', student=student)


@app.route('/student/api/scan-attendance', methods=['POST'])
def student_api_scan_attendance():
    """API: الطالب يمسح QR المادة ليسجل حضوره بنفسه"""
    if 'student_id' not in session:
        return jsonify({'success': False, 'message': 'يرجى تسجيل الدخول أولاً'}), 401

    data = request.get_json(force=True, silent=True)
    if not data or 'qr_data' not in data:
        return jsonify({'success': False, 'message': 'بيانات غير صالحة'}), 400

    try:
        event_id, scanned_at = parse_attendance_scan_time(data)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    if event_id and attendance_sync_event_exists(event_id):
        return jsonify({'success': True, 'already_synced': True, 'message': 'تمت مزامنة الحضور مسبقاً'})

    qr_data = str(data.get('qr_data', '')).strip()

    # يجب أن يكون QR خاص بمادة
    if not qr_data.startswith('SUBJECT:'):
        return jsonify({'success': False, 'message': 'هذا QR ليس خاصاً بمادة'}), 400

    import re
    # استخراج subject_id و session_token
    match_id = re.search(r'SUBJECT:(\d+)', qr_data)
    match_session = re.search(r'SESSION:([a-f0-9]+)', qr_data)
    
    if not match_id:
        return jsonify({'success': False, 'message': 'تعذر قراءة معرف المادة من QR'}), 400

    subject_id = int(match_id.group(1))
    session_token = match_session.group(1) if match_session else None

    # ✅ تحقق من صلاحية الـ session
    if session_token and data.get('offline_sync'):
        if not verify_subject_qr_session_at(subject_id, session_token, scanned_at):
            return jsonify({
                'success': False,
                'message': 'كان QR منتهي الصلاحية وقت المسح أو تعذر التحقق منه',
                'expired': True
            }), 403
    elif session_token:
        from database import verify_subject_qr_session
        if not verify_subject_qr_session(subject_id, session_token):
            return jsonify({
                'success': False,
                'message': 'هذا QR غير صالح أو انتهت صلاحيته. اطلب من الأستاذ توليد QR جديد.',
                'expired': True
            }), 403

    student_id = session['student_id']

    # التحقق من فترة المنع
    can_record, remaining_seconds = check_attendance_cooldown(student_id)
    if not can_record:
        remaining_time = format_remaining_time(remaining_seconds)
        return jsonify({
            'success': False,
            'message': f'انتظر {remaining_time} قبل التسجيل مجدداً',
            'cooldown_active': True,
            'remaining_seconds': remaining_seconds
        }), 429

    # التحقق من وجود المادة
    subjects = get_all_subjects()
    subject = next((s for s in subjects if s.get('id') == subject_id), None)
    if not subject:
        return jsonify({'success': False, 'message': 'المادة غير موجودة'}), 404

    student = get_student_by_id(student_id)
    group_name = student.get('group_name', 'غير معروف') if student else 'غير معروف'

    # تسجيل الحضور
    success = record_attendance(
        student_id, subject_id, group_name, recorded_at=scanned_at, event_id=event_id
    )

    if success:
        return jsonify({
            'success': True,
            'message': 'تم تسجيل حضورك بنجاح',
            'subject_name': subject.get('name', ''),
            'subject_teacher': subject.get('teacher_name', '')
        })
    else:
        return jsonify({
            'success': False,
            'message': 'أنت مسجل حضور مسبقاً لهذه المادة اليوم',
            'already_registered': True
        }), 400


@app.route('/student/api/grades', methods=['POST'])
def student_api_grades():
    """API للتحقق من كود الوصول وعرض الدرجات"""
    if 'student_id' not in session:
        return jsonify({'success': False, 'message': 'غير مصرح'}), 401
    
    data = request.get_json()
    if not data or 'code' not in data:
        return jsonify({'success': False, 'message': 'الكود مطلوب'}), 400
    
    student_id = session['student_id']
    code = data['code']
    
    # التحقق من الكود (يجب إضافة دالة verify_access_code في database.py)
    # مؤقتاً نفترض أن أي كود مكون من 6 أرقام صحيح
    if len(code) == 6 and code.isdigit():
        from database import get_student_grades
        grades = get_student_grades(student_id)
        return jsonify({
            'success': True,
            'grades': grades,
            'message': 'تم التحقق بنجاح'
        })
    else:
        return jsonify({
            'success': False,
            'message': 'كود غير صحيح'
        }), 400


@app.route('/student/settings')
def student_settings():
    """صفحة إعدادات الطالب"""
    if 'student_id' not in session:
        return redirect(url_for('student_login'))
    
    student = get_student_by_id(session['student_id'])
    if not student:
        session.clear()
        return redirect(url_for('student_login'))
    
    return render_template('student_settings.html', student=student)


@app.route('/student/update-profile', methods=['POST'])
def student_update_profile():
    """تحديث صورة الملف الشخصي للطالب"""
    if 'student_id' not in session:
        return redirect(url_for('student_login'))
    
    student_id = session['student_id']
    
    if 'profile_image' in request.files:
        file = request.files['profile_image']
        if file and file.filename:
            import time
            from werkzeug.utils import secure_filename
            
            # حفظ الصورة
            upload_dir = os.path.join(app.root_path, 'static', 'uploads', 'profiles')
            os.makedirs(upload_dir, exist_ok=True)
            
            # حذف الصورة القديمة
            student = get_student_by_id(student_id)
            if student and student.get('profile_image'):
                old_image = student['profile_image']
                if '/uploads/profiles/' in old_image:
                    old_path = os.path.join(app.root_path, old_image.lstrip('/'))
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except:
                            pass
            
            # حفظ الصورة الجديدة
            ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'jpg'
            filename = f'student_{student_id}_{int(time.time())}.{ext}'
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)
            
            # تحديث قاعدة البيانات
            new_image_path = f'/static/uploads/profiles/{filename}'
            from database import update_student_profile_image
            update_student_profile_image(student_id, new_image_path)
            
            flash('تم تحديث صورة الملف الشخصي بنجاح!', 'success')
        else:
            flash('لم يتم اختيار صورة', 'error')
    else:
        flash('لم يتم اختيار صورة', 'error')
    
    return redirect(url_for('student_settings'))


@app.route('/student/change-password', methods=['POST'])
def student_change_password():
    """تغيير كلمة مرور الطالب"""
    if 'student_id' not in session:
        return redirect(url_for('student_login'))
    
    student_id = session['student_id']
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    # التحقق من تطابق كلمة المرور الجديدة
    if new_password != confirm_password:
        flash('كلمة المرور الجديدة غير متطابقة', 'error')
        return redirect(url_for('student_settings'))
    
    # التحقق من طول كلمة المرور
    if len(new_password) < 6:
        flash('يجب أن تكون كلمة المرور 6 أحرف على الأقل', 'error')
        return redirect(url_for('student_settings'))
    
    # تحديث كلمة المرور مباشرة بدون التحقق من القديمة
    from database import set_student_password
    success = set_student_password(student_id, new_password)
    
    if success:
        flash('تم تغيير كلمة المرور بنجاح!', 'success')
    else:
        flash('حدث خطأ أثناء تغيير كلمة المرور', 'error')
    
    return redirect(url_for('student_settings'))


@app.route('/change-password', methods=['POST'])
def change_password():
    """تغيير كلمة المرور"""
    username = request.form.get('username')
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    if new_password != confirm_password:
        flash('كلمة المرور الجديدة غير متطابقة', 'error')
        return redirect(url_for('teacher_login'))
    
    if len(new_password) < 6:
        flash('يجب أن تكون كلمة المرور 6 أحرف على الأقل', 'error')
        return redirect(url_for('teacher_login'))
    
    if change_user_password(username, current_password, new_password):
        flash('تم تغيير كلمة المرور بنجاح!', 'success')
    else:
        flash('اسم المستخدم أو كلمة المرور الحالية غير صحيحة', 'error')
    
    return redirect(url_for('teacher_login'))


# ============================================
# Routes المحمية (تتطلب تسجيل دخول)
# ============================================

@app.route('/index')
@login_required
def index():
    """الصفحة الرئيسية"""
    return render_template('index.html')


@app.route('/generate')
@login_required
def generate_page():
    """صفحة توليد QR"""
    return render_template('generate.html')


@app.route('/api/generate-qr', methods=['POST'])
@login_required
def generate_qr():
    """توليد QR Code وحفظه في قاعدة البيانات، مع دعم نوعين: طالب أو مادة."""
    try:
        data = request.get_json(force=True, silent=True)

        if not data:
            return jsonify({'success': False, 'message': 'بيانات غير صالحة'}), 400

        full_name = (data.get('full_name') or '').strip()
        department = (data.get('department') or '').strip()
        teacher_name = (data.get('teacher_name') or '').strip()
        group_name = (data.get('group_name') or '').strip()
        subject_id = data.get('subject_id')
        qr_mode = data.get('qr_mode', 'student')

        if subject_id:
            try:
                subject_id = int(subject_id)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': 'معرف المادة غير صحيح'}), 400

            subject = next((s for s in get_all_subjects() if s.get('id') == subject_id), None)
            if subject:
                department = department or subject.get('department', '')
                teacher_name = teacher_name or subject.get('teacher_name', '')
                group_name = group_name or subject.get('course_type', '')

        if qr_mode == 'subject':
            # استخرج subject_id من البيانات المرسلة
            if not subject_id:
                return jsonify({'success': False, 'message': 'يرجى اختيار المادة أولاً لتوليد QR'}), 400

            subjects_list = get_all_subjects()
            subject = next((s for s in subjects_list if s.get('id') == subject_id), None)
            if not subject:
                return jsonify({'success': False, 'message': 'المادة غير موجودة في قاعدة البيانات'}), 404

            # ✅ أنشئ جلسة QR جديدة فريدة (تلغي القديمة تلقائياً) - صالحة 30 دقيقة
            from database import create_subject_qr_session
            session_data = create_subject_qr_session(subject_id, duration_minutes=30)
            
            if not session_data:
                return jsonify({'success': False, 'message': 'فشل إنشاء جلسة QR'}), 500

            session_token = session_data['token']
            expires_at = session_data['expires_at']

            dept  = subject.get('department', '')
            tname = subject.get('teacher_name', '')
            gname = subject.get('course_type', '')
            sname = subject.get('name', '')

            # ✅ QR يحتوي على subject_id + session_token
            qr_content = f"SUBJECT:{subject_id}|SESSION:{session_token}|DEPT:{dept}|TEACHER:{tname}|GROUP:{gname}|NAME:{sname}"
            qr_img = generate_qr_code(qr_content)
            buffered = io.BytesIO()
            qr_img.save(buffered, format='PNG')
            qr_base64 = base64.b64encode(buffered.getvalue()).decode()

            return jsonify({
                'success': True,
                'qr_code': f"data:image/png;base64,{qr_base64}",
                'subject_id': subject_id,
                'subject': subject,
                'session_token': session_token,
                'expires_at': expires_at,
                'duration_minutes': 30,
                'message': 'تم توليد QR جديد - صالح لمدة 30 دقيقة',
                'mode': 'subject'
            })

        if not full_name:
            full_name = f"طالب {datetime.now().strftime('%d%m%y%H%M%S')}"

        if not all([department, teacher_name, group_name]):
            return jsonify({'success': False, 'message': 'يرجى اختيار المرحلة والمادة والكورس بشكل صحيح'}), 400

        import uuid
        student_code = str(uuid.uuid4())[:8].upper()

        qr_content = f"CODE:{student_code}|NAME:{full_name}|DEPT:{department}|TEACHER:{teacher_name}|GROUP:{group_name}"
        if subject_id:
            qr_content += f"|SUBJECT:{subject_id}"

        qr_img = generate_qr_code(qr_content)

        buffered = io.BytesIO()
        qr_img.save(buffered, format="PNG")
        qr_base64 = base64.b64encode(buffered.getvalue()).decode()

        student_id = add_student(full_name, department, teacher_name, group_name, student_code, qr_content, qr_base64)

        if not student_id:
            return jsonify({'success': False, 'message': 'فشل إضافة الطالب'}), 400

        student = get_student_by_id(student_id)

        if not student:
            return jsonify({'success': False, 'message': 'خطأ: لم يتم حفظ الطالب'}), 400

        print(f'[API] ✅ Student added: {full_name} ({student_code})')

        return jsonify({
            'success': True,
            'qr_code': f"data:image/png;base64,{qr_base64}",
            'student_code': student_code,
            'student': student,
            'message': 'تم التوليد والحفظ بنجاح',
            'mode': 'student'
        })

    except Exception as e:
        import traceback
        print(f"[API Error] generate_qr: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/scanner')
@login_required
def scanner_page():
    """صفحة ماسح QR"""
    subjects = get_all_subjects()
    return render_template('scanner.html', subjects=subjects)


@app.route('/api/scan-qr', methods=['POST'])
@login_required
def api_scan_qr():
    """API لمسح QR code وتسجيل الحضور، يدعم QR الطالب وQR المادة."""
    try:
        data = request.get_json(force=True, silent=True)
    except Exception as e:
        print(f'[API] JSON Parse Error: {e}')
        return jsonify({'success': False, 'message': 'بيانات غير صالحة'}), 400

    if not data:
        return jsonify({'success': False, 'message': 'بيانات غير صالحة'}), 400

    try:
        event_id, scanned_at = parse_attendance_scan_time(data)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    qr_data = str(data.get('qr_data') or '').strip()
    subject_id = data.get('subject_id')
    mode = (data.get('mode') or 'student').strip().lower()

    # إذا كان QR خاص بمادة، استخرج subject_id من الـ QR مباشرة
    if qr_data.startswith('SUBJECT:'):
        import re
        match = re.match(r'^SUBJECT:(\d+)', qr_data)
        if match:
            subject_id = int(match.group(1))
        elif not subject_id:
            return jsonify({'success': False, 'message': 'تعذر استخراج معرف المادة من QR'}), 400

    if not qr_data:
        return jsonify({'success': False, 'message': 'بيانات غير كاملة'}), 400

    if event_id and attendance_sync_event_exists(event_id):
        return jsonify({'success': True, 'already_synced': True, 'message': 'تمت مزامنة الحضور مسبقاً'})

    # subject_id مطلوب فقط لـ QR الطالب
    if not qr_data.startswith('SUBJECT:') and not subject_id:
        return jsonify({'success': False, 'message': 'اختر المادة أولاً'}), 400

    if subject_id:
        try:
            subject_id = int(subject_id)
        except (ValueError, TypeError) as e:
            print(f'[API] Subject ID conversion error: {e}')
            return jsonify({'success': False, 'message': 'subject_id غير صالح'}), 400

    try:
        print(f'[API] Processing QR: {qr_data}, Subject: {subject_id}, Mode: {mode}')

        if qr_data.startswith('SUBJECT:'):
            subject_result = record_attendance_for_subject(
                subject_id, group_name='مادة', recorded_at=scanned_at, event_id=event_id
            )
            if not subject_result.get('success'):
                return jsonify({'success': False, 'message': subject_result.get('message', 'فشل تسجيل حضور المادة')}), 400
            return jsonify({
                'success': True,
                'message': subject_result.get('message', 'تم تسجيل حضور المادة بنجاح'),
                'mode': 'subject',
                'saved': subject_result.get('saved', 0),
                'total': subject_result.get('total', 0)
            })

        student_code = None
        if 'CODE:' in qr_data:
            code_start = qr_data.find('CODE:') + 5
            code_end = qr_data.find('|', code_start)
            if code_end == -1:
                code_end = len(qr_data)
            student_code = qr_data[code_start:code_end].strip()
        else:
            student_code = qr_data.strip()

        print(f'[API] Extracted student code: {student_code}')

        student = None
        try:
            students = get_all_students()
            if not students:
                students = []
        except Exception as e:
            print(f'[API] Error getting students: {e}')
            students = []

        for s in students:
            if (s.get('qr_code') == qr_data or
                s.get('student_code') == student_code or
                student_code in str(s.get('qr_code', ''))):
                student = s
                break

        if not student:
            return jsonify({'success': False, 'message': f'الطالب غير موجود (Code: {student_code})'}), 404

        student_id = student.get('id')
        if not student_id:
            return jsonify({'success': False, 'message': 'خطأ: بيانات الطالب غير صحيحة'}), 400

        can_record, remaining_seconds = check_attendance_cooldown(student_id)
        if not can_record:
            remaining_time = format_remaining_time(remaining_seconds)
            return jsonify({
                'success': False,
                'message': f'⏳ فترة المنع! يرجى الانتظار {remaining_time} قبل إعادة التسجيل',
                'cooldown_active': True,
                'remaining_seconds': remaining_seconds,
                'remaining_time_formatted': remaining_time
            }), 429

        group_name = student.get('group_name', 'غير معروف')
        success = record_attendance(
            student_id, subject_id, group_name, recorded_at=scanned_at, event_id=event_id
        )

        if success:
            subjects = get_all_subjects() or []
            subject = next((s for s in subjects if s.get('id') == subject_id), None)
            return jsonify({
                'success': True,
                'message': f"✅ تم تسجيل حضور {student.get('full_name', 'الطالب')} بنجاح",
                'student': student,
                'subject': subject,
                'group_name': group_name,
                'mode': 'student'
            })

        return jsonify({
            'success': False,
            'message': 'الطالب مسجل حضور مسبقاً لهذا اليوم في هذه المادة',
            'already_registered': True,
            'student': student,
            'mode': 'student'
        }), 400

    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f'[ERROR] {error_msg}')
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'خطأ في السيرفر: {error_msg}'}), 500


@app.route('/students')
@login_required
def students_page():
    """صفحة قائمة الطلاب"""
    students = get_all_students()
    return render_template('students.html', students=students)


@app.route('/student/<int:student_id>')
@login_required
def student_detail(student_id):
    """صفحة تفاصيل الطالب - ✅ استخدام الصورة المخزنة"""
    student = get_student_by_id(student_id)
    if not student:
        flash('الطالب غير موجود', 'error')
        return redirect(url_for('students_page'))
    
    attendance = get_attendance_by_student(student_id)
    
    # ✅ استخدام الصورة المخزنة في قاعدة البيانات
    qr_base64 = student.get('qr_image')
    
    # ✅ للطلاب القدامى الذين ليس لديهم صورة مخزنة، نولدها مؤقتاً
    if not qr_base64:
        qr_img = generate_qr_code(student['qr_code'])
        qr_base64 = qr_to_base64(qr_img).replace('data:image/png;base64,', '')
    
    return render_template('student_detail.html', 
                         student=student, 
                         attendance=attendance,
                         qr_code=f"data:image/png;base64,{qr_base64}")

@app.route('/subjects')
@login_required
def subjects_page():
    """صفحة المواد"""
    subjects = get_all_subjects()
    return render_template('subjects.html', subjects=subjects)

@app.route('/api/add-subject', methods=['POST'])
@login_required
def api_add_subject():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'لم يتم استلام أي بيانات'}), 400
        
        name = (data.get('name') or '').strip()
        department = (data.get('department') or '').strip() or 'عام'
        teacher_name = (data.get('teacher_name') or '').strip()
        grade = (data.get('grade') or 'الأولى').strip()
        course_type = (data.get('course_type') or 'أول').strip()
        
        if not all([name, teacher_name, grade, course_type]):
            return jsonify({'success': False, 'message': 'يرجى ملء اسم المادة والأستاذ والمرحلة والكورس'}), 400
        
        import uuid
        import time
        code = str(uuid.uuid4())[:6].upper() + str(int(time.time()))[-4:]
        
        subject_id = add_subject(name, code, department, teacher_name, grade, course_type)
        
        if subject_id:
            return jsonify({'success': True, 'subject_id': subject_id})
        else:
            return jsonify({'success': False, 'message': 'حدث خطأ في إضافة المادة'}), 400
            
    except Exception as e:
        return jsonify({'success': False, 'message': f'خطأ في الخادم: {str(e)}'}), 500


@app.route('/attendance')
@login_required
def attendance_page():
    """صفحة سجل الحضور"""
    subjects = get_all_subjects()
    today = datetime.now().strftime('%Y-%m-%d')
    today_stats = get_today_attendance_stats()
    
    return render_template('attendance.html', 
                         subjects=subjects,
                         today=today,
                         today_stats=today_stats)


@app.route('/api/attendance/<int:subject_id>')
@login_required
def api_get_attendance(subject_id):
    """API للحصول على سجل الحضور"""
    date = request.args.get('date')
    if not date:
        date = datetime.now().strftime('%Y-%m-%d')

    used_date = date

    # تحقق من وجود حضور فعلي في التاريخ المحدد
    count_in_date = get_attendance_count_by_subject_date(subject_id, date)
    if count_in_date == 0:
        fallback_date = get_subject_latest_attendance_date_before(subject_id, date)
        if fallback_date and fallback_date != date:
            used_date = fallback_date

    attendance = get_students_attendance_status(subject_id, used_date)

    subjects = get_all_subjects()
    subject = next((s for s in subjects if s['id'] == subject_id), None)

    return jsonify({
        'success': True,
        'attendance': attendance,
        'subject': subject,
        'requested_date': date,
        'used_date': used_date,
        'count_in_request_date': count_in_date
    })


@app.route('/api/attendance/latest_date/<int:subject_id>')
@login_required
def api_get_latest_attendance_date(subject_id):
    """API للحصول على أحدث تاريخ حضور للمادة"""
    latest_date = get_subject_latest_attendance_date(subject_id)
    return jsonify({'success': True, 'latest_date': latest_date})


@app.route('/api/delete-student/<int:student_id>', methods=['DELETE'])
@login_required
def api_delete_student(student_id):
    """API لحذف طالب"""
    try:
        success = delete_student(student_id)
        if success:
            return jsonify({'success': True, 'message': 'تم حذف الطالب بنجاح'})
        else:
            return jsonify({'success': False, 'message': 'فشل حذف الطالب'}), 400
    except Exception as e:
        print(f'[API Error] delete_student: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/api/reset-student-password/<int:student_id>', methods=['POST'])
@login_required
def api_reset_student_password(student_id):
    """API لتعيين كلمة مرور جديدة للطالب من قبل المدرس"""
    try:
        student = get_student_by_id(student_id)
        if not student:
            return jsonify({'success': False, 'message': 'الطالب غير موجود'}), 404
        
        data = request.get_json(force=True, silent=True) or {}
        new_password = data.get('new_password', '').strip()
        
        if not new_password or len(new_password) < 6:
            return jsonify({'success': False, 'message': 'كلمة المرور يجب أن تكون 6 أحرف على الأقل'}), 400
        
        from database import set_student_password
        success = set_student_password(student_id, new_password)
        
        if success:
            print(f'[Admin] ✓ Password set for student: {student.get("full_name")} (ID: {student_id})')
            return jsonify({'success': True, 'message': 'تم تعيين كلمة المرور بنجاح'})
        else:
            return jsonify({'success': False, 'message': 'فشل تعيين كلمة المرور'}), 400
    except Exception as e:
        print(f'[API Error] reset_student_password: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/api/promote-students', methods=['POST'])
@login_required
def api_promote_students():
    """API لترقية المرحلة الدراسية لمجموعة طلاب"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        from_grade = data.get('from_grade', '').strip()
        to_grade = data.get('to_grade', '').strip()

        if not from_grade or not to_grade:
            return jsonify({'success': False, 'message': 'يرجى تحديد المرحلتين'}), 400
        if from_grade == to_grade:
            return jsonify({'success': False, 'message': 'المرحلتان متماثلتان'}), 400

        from database import promote_students_grade
        count = promote_students_grade(from_grade, to_grade)

        print(f'[Admin] ✓ Promoted {count} students from {from_grade} to {to_grade}')
        return jsonify({'success': True, 'count': count,
                        'message': f'تم ترقية {count} طالب من {from_grade} إلى {to_grade}'})
    except Exception as e:
        print(f'[API Error] promote_students: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/api/undo-promotion', methods=['POST'])
@login_required
def api_undo_promotion():
    """API للتراجع عن آخر عملية ترقية"""
    try:
        from database import undo_last_promotion
        count, message = undo_last_promotion()
        if count > 0:
            return jsonify({'success': True, 'count': count, 'message': message})
        else:
            return jsonify({'success': False, 'message': message}), 400
    except Exception as e:
        print(f'[API Error] undo_promotion: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/api/check-promotion-log')
@login_required
def api_check_promotion_log():
    """التحقق من وجود سجل ترقية"""
    try:
        from database import get_last_promotion_log
        log = get_last_promotion_log()
        if log:
            return jsonify({'has_log': True, 'from_grade': log['from_grade'], 'to_grade': log['to_grade']})
        return jsonify({'has_log': False})
    except Exception as e:
        return jsonify({'has_log': False})


@app.route('/api/delete-subject/<int:subject_id>', methods=['DELETE'])
@login_required
def api_delete_subject(subject_id):
    """API لحذف مادة"""
    try:
        success = delete_subject(subject_id)
        if success:
            return jsonify({'success': True, 'message': 'تم حذف المادة بنجاح'})
        else:
            return jsonify({'success': False, 'message': 'فشل حذف المادة'}), 400
    except Exception as e:
        print(f'[API Error] delete_subject: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/api/get-subjects')
@login_required
def api_get_subjects():
    """API للحصول على المواد"""
    try:
        department = request.args.get('department')
        
        if department:
            subjects = get_subjects_by_department(department)
        else:
            subjects = get_all_subjects()
        
        if subjects is None:
            subjects = []
        
        return jsonify({'success': True, 'subjects': subjects})
    except Exception as e:
        print(f'[API Error] api_get_subjects: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}', 'subjects': []}), 500

@app.route('/api/get-students')
@login_required
def api_get_students():
    """API للحصول على جميع الطلاب"""
    try:
        students = get_all_students()
        if students is None:
            students = []
        return jsonify({'success': True, 'students': students})
    except Exception as e:
        print(f'[API Error] api_get_students: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}', 'students': []}), 500

# ============================================
# إعدادات النظام
# ============================================
 
import os
from werkzeug.utils import secure_filename

@app.route('/settings')
@login_required
def settings_page():
    """صفحة الإعدادات"""
    from database import get_settings, get_all_users
    import time
    settings = get_settings()
    
    # أضف timestamp لمنع cache الصور
    if settings.get('dept_image') and '/uploads/' in settings.get('dept_image', ''):
        settings['dept_image_url'] = settings['dept_image'] + '?v=' + str(int(time.time()))
    else:
        settings['dept_image_url'] = settings.get('dept_image', '')
    
    if session.get('username') == 'admin':
        users = get_all_users()
    else:
        users = []
    
    return render_template('settings.html', settings=settings, users=users, is_admin=(session.get('username') == 'admin'))


@app.route('/update-settings', methods=['POST'])
@login_required
@admin_required  # بس الـ admin يقدر يحدث الإعدادات العامة
def update_settings():
    """تحديث إعدادات النظام"""
    from database import get_settings, save_settings
    import time
    
    setting_type = request.form.get('setting_type')
    settings = get_settings()
    
    if setting_type == 'general':
        settings['dept_name'] = request.form.get('dept_name', 'قسم علوم الحاسوب')
        settings['dept_subtitle'] = request.form.get('dept_subtitle', 'نظام إدارة الحضور والغياب')
        
        # معالجة صورة القسم (وتستخدم أيضاً كشعار في صفحة تسجيل الدخول)
        if 'dept_image' in request.files:
            file = request.files['dept_image']
            if file and file.filename:
                # المسار المطلق للمجلد
                upload_dir = os.path.join(app.root_path, 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                
                print(f'[Settings] Upload directory: {upload_dir}')
                
                # احذف جميع الصور القديمة من مجلد uploads
                try:
                    for old_file in os.listdir(upload_dir):
                        if old_file.startswith('dept_logo'):
                            old_path = os.path.join(upload_dir, old_file)
                            os.remove(old_path)
                            print(f'[Settings] ✓ Deleted old file: {old_file}')
                except Exception as e:
                    print(f'[Settings] ⚠ Error deleting old files: {e}')
                
                # حفظ الصورة باسم فريد مع timestamp
                ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
                timestamp = int(time.time() * 1000)  # milliseconds للتأكد من الفرادة
                filename = f'dept_logo_{timestamp}.{ext}'
                filepath = os.path.join(upload_dir, filename)
                
                # حفظ الملف
                file.save(filepath)
                print(f'[Settings] ✓ New file saved: {filepath}')
                
                # التحقق من أن الملف تم حفظه
                if os.path.exists(filepath):
                    print(f'[Settings] ✓ File exists! Size: {os.path.getsize(filepath)} bytes')
                else:
                    print(f'[Settings] ✗ ERROR: File NOT saved!')
                
                # حفظ المسار في الإعدادات
                new_logo_path = f'/static/uploads/{filename}'
                settings['dept_image'] = new_logo_path
                settings['system_logo'] = new_logo_path
                
                print(f'[Settings] ✓ Path saved to DB: {new_logo_path}')
    
    save_settings(settings)
    flash('تم حفظ الإعدادات بنجاح! ✓', 'success')
    return redirect(url_for('settings_page'))


@app.route('/add-user', methods=['POST'])
@login_required
@admin_required  # بس الـ admin يقدر يضيف مستخدمين
def add_user():
    """إضافة مستخدم جديد"""
    from database import add_new_user
    
    username = request.form.get('username')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    
    if password != confirm_password:
        flash('كلمات المرور غير متطابقة!', 'error')
        return redirect(url_for('settings_page'))
    
    if len(password) < 6:
        flash('يجب أن تكون كلمة المرور 6 أحرف على الأقل!', 'error')
        return redirect(url_for('settings_page'))
    
    if add_new_user(username, password):
        flash(f'تم إضافة المستخدم {username} بنجاح!', 'success')
    else:
        flash('اسم المستخدم موجود مسبقاً!', 'error')
    
    return redirect(url_for('settings_page'))


@app.route('/delete-user/<username>', methods=['DELETE'])
@login_required
@admin_required  # بس الـ admin يقدر يحذف مستخدمين
def delete_user_route(username):
    """حذف مستخدم"""
    try:
        if username == session.get('username'):
            return jsonify({'success': False, 'message': 'لا يمكنك حذف حسابك الحالي!'})
        
        if username == 'admin':
            return jsonify({'success': False, 'message': 'لا يمكن حذف المستخدم الرئيسي!'})
        
        success = delete_user_by_username(username)
        if success:
            return jsonify({'success': True, 'message': 'تم حذف المستخدم بنجاح'})
        else:
            return jsonify({'success': False, 'message': 'فشل حذف المستخدم'}), 400
    except Exception as e:
        print(f'[API Error] delete_user: {e}')
        return jsonify({'success': False, 'message': f'خطأ: {str(e)}'}), 500


@app.route('/change-password-admin', methods=['POST'])
@login_required
def change_password_admin():
    """تغيير كلمة المرور - أي مستخدم يقدر يغير كلمة مروره"""
    from database import change_user_password
    
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    # التحقق من تطابق كلمات المرور
    if new_password != confirm_password:
        flash('كلمات المرور الجديدة غير متطابقة!', 'error')
        return redirect(url_for('settings_page'))
    
    if len(new_password) < 6:
        flash('يجب أن تكون كلمة المرور 6 أحرف على الأقل!', 'error')
        return redirect(url_for('settings_page'))
    
    # المستخدم يغير بس كلمة مروره، الـ admin يقدر يغير لأي شخص (اختياري)
    username = session.get('username')
    
    if change_user_password(username, current_password, new_password):
        flash('تم تغيير كلمة المرور بنجاح!', 'success')
    else:
        flash('كلمة المرور الحالية غير صحيحة!', 'error')
    
    return redirect(url_for('settings_page'))


# ===== صفحة الدرجات =====

@app.route('/grades')
@login_required
def grades_page():
    from database import get_all_students, get_all_subjects, get_all_active_codes
    students = get_all_students()
    subjects = get_all_subjects()
    codes = get_all_active_codes()
    return render_template('student_grades.html', students=students, subjects=subjects, codes=codes)


@app.route('/api/student/<int:student_id>/generate-code', methods=['POST'])
@login_required
def api_generate_code(student_id):
    from database import generate_access_code, get_student_by_id
    code = generate_access_code(student_id)
    if code:
        student = get_student_by_id(student_id)
        return jsonify({
            'success': True,
            'code': code,
            'student_id': student_id,
            'student_name': student['full_name'] if student else '',
            'student_code': student['student_code'] if student else ''
        })
    return jsonify({'success': False, 'message': 'فشل توليد الكود'})


@app.route('/api/access-code/<int:code_id>/delete', methods=['DELETE'])
@login_required
def api_delete_access_code(code_id):
    from database import delete_access_code
    if delete_access_code(code_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'فشل الحذف'})


@app.route('/api/student/<int:student_id>/add-grade', methods=['POST'])
@login_required
def api_add_grade(student_id):
    from database import add_grade
    data = request.get_json()
    ok = add_grade(
        student_id,
        data.get('subject_id'),
        data.get('grade'),
        data.get('max_grade', 100),
        data.get('exam_type', 'نهائي'),
        data.get('notes', '')
    )
    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'فشل إضافة الدرجة'})


@app.route('/api/student/<int:student_id>/grades')
@login_required
def api_get_student_grades(student_id):
    from database import get_student_grades
    grades = get_student_grades(student_id)
    return jsonify({'success': True, 'grades': grades})


@app.route('/api/grade/<int:grade_id>/delete', methods=['DELETE'])
@login_required
def api_delete_grade(grade_id):
    from database import delete_grade
    if delete_grade(grade_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'فشل الحذف'})


# ============================================
# صفحة الشبكة وتشغيل Hotspot
# ============================================

@app.route('/network')
@login_required
def network_page():
    return render_template('network.html')


@app.route('/api/network-info')
@login_required
def api_network_info():
    import socket, subprocess
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    hotspot_ip = "192.168.137.1"
    hotspot_active = False
    detection_method = "none"

    # الطريقة 1: فحص netsh hosted network (الطريقة القديمة)
    try:
        result = subprocess.run(['netsh', 'wlan', 'show', 'hostednetwork'],
                                capture_output=True, text=True, timeout=3)
        if 'Started' in result.stdout or 'تم بدء' in result.stdout:
            hotspot_active = True
            detection_method = "netsh-hostednetwork"
    except Exception:
        pass

    # الطريقة 2: فحص network adapters عبر PowerShell
    if not hotspot_active:
        try:
            ps_command = '''
            $adapters = Get-NetAdapter | Where-Object {
                ($_.InterfaceDescription -like "*Wi-Fi Direct*" -or 
                 $_.InterfaceDescription -like "*Hosted*" -or
                 $_.InterfaceDescription -like "*Microsoft Wi-Fi Direct*" -or
                 $_.Name -like "*Local Area Connection*") -and 
                $_.Status -eq "Up"
            }
            $adapters.Count
            '''
            result = subprocess.run(
                ['powershell', '-Command', ps_command],
                capture_output=True, text=True, timeout=5)
            count = int(result.stdout.strip() or '0')
            if count > 0:
                hotspot_active = True
                detection_method = "powershell-adapter"
        except Exception:
            pass

    # الطريقة 3: فحص IP address على 192.168.137.x
    if not hotspot_active:
        try:
            ps_command = '''
            $hotspotIP = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
                $_.IPAddress -like "192.168.137.*"
            }
            if ($hotspotIP) { "FOUND" } else { "NONE" }
            '''
            result = subprocess.run(
                ['powershell', '-Command', ps_command],
                capture_output=True, text=True, timeout=3)
            if 'FOUND' in result.stdout:
                hotspot_active = True
                detection_method = "ip-check"
        except Exception:
            pass

    # الطريقة 4: محاولة الاتصال بـ 192.168.137.1
    if not hotspot_active:
        try:
            import socket
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.settimeout(1)
            # محاولة الاتصال بالـ gateway المتوقع
            result = test_socket.connect_ex(('192.168.137.1', 445))  # SMB port
            test_socket.close()
            if result == 0:
    
                hotspot_active = True
                detection_method = "connection-test"
        except Exception:
            pass

    # تحديد IP الطالب
    student_ip = hotspot_ip if hotspot_active else local_ip
    
    return jsonify({
        'success': True,
        'local_ip': local_ip,
        'hotspot_ip': hotspot_ip,
        'hotspot_active': hotspot_active,
        'detection_method': detection_method,
        'student_ip': student_ip,
        'student_url': f'http://{student_ip}:5002',
        'teacher_url': f'http://{local_ip}:5000'
    })


@app.route('/api/start-hotspot')
@login_required
def api_start_hotspot():
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hotspot.ps1')
    try:
        subprocess.Popen(
            ['powershell', '-Command',
             f'Start-Process powershell -ArgumentList \'-ExecutionPolicy Bypass -File "{script}"\' -Verb RunAs'],
            shell=True)
        flash('جاري تشغيل شبكة الحضور...', 'success')
    except Exception as e:
        flash(f'تعذر تشغيل الشبكة: {e}', 'error')
    return redirect(url_for('network_page'))


@app.route('/api/stop-hotspot')
@login_required
def api_stop_hotspot():
    import subprocess
    try:
        subprocess.Popen(
            ['powershell', '-Command',
             'Start-Process powershell -ArgumentList \'-Command "netsh wlan stop hostednetwork"\' -Verb RunAs'],
            shell=True)
        flash('تم إيقاف الشبكة', 'success')
    except Exception as e:
        flash(f'تعذر الإيقاف: {e}', 'error')
    return redirect(url_for('network_page'))


if __name__ == '__main__':
    init_database()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('RAILWAY_ENVIRONMENT') is None  # debug=False على Railway
    app.run(debug=debug, host='0.0.0.0', port=port)
