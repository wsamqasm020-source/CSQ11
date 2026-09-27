@echo off
chcp 65001 >nul
cls
echo ====================================
echo   نظام الحضور - تشغيل السيرفر
echo ====================================
echo.

REM تفعيل البيئة الافتراضية
if exist venv\Scripts\activate.bat (
    echo [✓] تفعيل البيئة الافتراضية...
    call venv\Scripts\activate.bat
) else (
    echo [!] تحذير: البيئة الافتراضية غير موجودة
    echo.
)

REM التحقق من تثبيت المكتبات
python -c "import flask" 2>nul
if errorlevel 1 (
    echo [!] Flask غير مثبت، جاري التثبيت...
    pip install -r requirements.txt
)

echo.
echo [✓] جاري تشغيل السيرفر...
echo [i] افتح المتصفح على: http://127.0.0.1:5000
echo [i] للإيقاف: اضغط Ctrl+C
echo.
echo ====================================
echo.

REM تشغيل السيرفر
python app.py

pause
