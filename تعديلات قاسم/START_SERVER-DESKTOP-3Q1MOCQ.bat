@echo off
chcp 65001 >nul
cls
cd /d "%~dp0"

echo ==========================================
echo    تشغيل نظام الحضور
echo ==========================================
echo.

REM تفعيل بيئة مساحة العمل أولاً
if exist "%~dp0..\.venv\Scripts\activate.bat" (
    call "%~dp0..\.venv\Scripts\activate.bat"
) else (
    if exist "%~dp0.venv\Scripts\activate.bat" (
        call "%~dp0.venv\Scripts\activate.bat"
    ) else (
        if exist "%~dp0venv\Scripts\activate.bat" (
            call "%~dp0venv\Scripts\activate.bat"
        )
    )
)

echo [*] جاري تشغيل السيرفر...
echo [i] الرابط: http://127.0.0.1:5000
echo [i] رابط الطلاب يظهر في صفحة الشبكة بعد تشغيل الهوتسبوت
echo.

python app.py

pause
