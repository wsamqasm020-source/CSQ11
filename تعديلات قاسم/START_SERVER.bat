@echo off
chcp 65001 >nul
cls

echo ==========================================
echo    تشغيل نظام الحضور
echo ==========================================
echo.

REM تفعيل البيئة الافتراضية
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

echo [*] جاري تشغيل السيرفر...
echo [i] الرابط: http://127.0.0.1:5000
echo.

python app.py

pause
