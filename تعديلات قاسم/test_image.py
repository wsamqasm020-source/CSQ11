"""
سكريبت اختبار سريع للصورة
"""
import os
import sqlite3
import json

# فحص الملفات الموجودة
print("=" * 50)
print("الملفات الموجودة في static/uploads:")
print("=" * 50)
upload_dir = os.path.join('static', 'uploads')
if os.path.exists(upload_dir):
    for file in os.listdir(upload_dir):
        filepath = os.path.join(upload_dir, file)
        size = os.path.getsize(filepath)
        print(f"  ✓ {file} ({size} bytes)")
else:
    print("  ✗ المجلد غير موجود!")

# فحص قاعدة البيانات
print("\n" + "=" * 50)
print("الإعدادات في قاعدة البيانات:")
print("=" * 50)
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute('SELECT data FROM settings WHERE id=1')
result = cursor.fetchone()
if result:
    settings = json.loads(result[0])
    print(f"  dept_image: {settings.get('dept_image', 'غير محدد')}")
    print(f"  system_logo: {settings.get('system_logo', 'غير محدد')}")
    
    # فحص إذا الملف موجود
    dept_image = settings.get('dept_image', '')
    if dept_image:
        file_path = dept_image.lstrip('/')
        if os.path.exists(file_path):
            print(f"  ✓ الملف موجود!")
        else:
            print(f"  ✗ الملف غير موجود: {file_path}")
else:
    print("  ✗ لا توجد إعدادات في قاعدة البيانات")
conn.close()

print("\n" + "=" * 50)
