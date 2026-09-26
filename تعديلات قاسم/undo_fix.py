import sqlite3

conn = sqlite3.connect('attendance.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# عرض الوضع الحالي
cursor.execute('SELECT teacher_name, COUNT(*) as cnt FROM students GROUP BY teacher_name')
print('المراحل الموجودة حالياً:')
for row in cursor.fetchall():
    print(f"  {row['teacher_name']}: {row['cnt']} طالب")

# أدخل عدد الطلاب اللي تبي ترجعهم
# مثلاً لو 294 طالب انتقلوا من الأولى للثانية
COUNT_TO_UNDO = 294  # غير هذا الرقم

print(f"\nجاري إرجاع {COUNT_TO_UNDO} طالب من الثانية للأولى...")

# رجّع آخر COUNT_TO_UNDO طالب انضافوا للثانية
cursor.execute('''
    UPDATE students SET teacher_name = 'الأولى'
    WHERE id IN (
        SELECT id FROM students 
        WHERE teacher_name = 'الثانية'
        ORDER BY id DESC
        LIMIT ?
    )
''', (COUNT_TO_UNDO,))

count = cursor.rowcount
conn.commit()

print(f"تم رجوع {count} طالب للمرحلة الأولى!")

# عرض الوضع بعد التعديل
cursor.execute('SELECT teacher_name, COUNT(*) as cnt FROM students GROUP BY teacher_name')
print('\nالمراحل بعد التعديل:')
for row in cursor.fetchall():
    print(f"  {row['teacher_name']}: {row['cnt']} طالب")

conn.close()
