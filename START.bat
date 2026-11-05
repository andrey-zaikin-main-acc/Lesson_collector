@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe lesson_collector.py
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Python Launcher ne naiden. Ustanovite Python 3.12 ili novee.
        pause
        exit /b 1
    )
    py -3.12 lesson_collector.py
)
set "LESSON_COLLECTOR_EXIT=%ERRORLEVEL%"
if not "%LESSON_COLLECTOR_EXIT%"=="0" (
    echo.
    echo Konvertatsiya zavershilas s oshibkoi.
)
echo.
pause
exit /b %LESSON_COLLECTOR_EXIT%
