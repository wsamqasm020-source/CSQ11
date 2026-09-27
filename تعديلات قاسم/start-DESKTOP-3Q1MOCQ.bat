@echo off
cd /d "%~dp0"
echo ==========================================
echo    نظام تسجيل الحضور بالـ QR
echo ==========================================
echo.
echo جاري تشغيل التطبيق...
echo.
if exist "%~dp0..\.venv\Scripts\activate.bat" (
	call "%~dp0..\.venv\Scripts\activate.bat"
) else (
	if exist "%~dp0.venv\Scripts\activate.bat" (
		call "%~dp0.venv\Scripts\activate.bat"
	) else (
		if exist "%~dp0venv\Scripts\activate.bat" call "%~dp0venv\Scripts\activate.bat"
	)
)
python app.py
echo.
pause
