@echo off
title ALGE Live Timing
cd /d "%~dp0"
where python >nul 2>nul || (echo Python niet gevonden. Installeer Python 3 en probeer opnieuw. & pause & exit /b 1)
python livetiming.py --open %*
if errorlevel 1 pause
