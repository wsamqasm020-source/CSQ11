import sqlite3

conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()
cursor.execute('SELECT id, full_name, student_code, teacher_name, group_name FROM students LIMIT 10')
students = cursor.fetchall()

print('=' * 70)
print('الطلاب الموجودين في النظام:')
print('=' * 70)

if students:
    for s in students:
        print(f'\nالرقم الجامعي: {s[2]}')
        print(f'الاسم: {s[1]}')
        print(f'المرحلة: {s[3]} - {s[4]}')
        print('-' * 70)
else:
    print('\nلا يوجد طلاب في النظام!')
    print('يجب إضافة طلاب أولاً من صفحة "توليد QR"')

conn.close()
