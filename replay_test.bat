@echo off
title ALGE Live Timing (replay)
cd /d "%~dp0"
python livetiming.py --open --replay sample_capture2.pcapng --speed 5 --http-port 8090 --history "%TEMP%\lt_test_history.json" --settings "%TEMP%\lt_test_settings.json"
pause
