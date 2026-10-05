@echo off
rem Simuleert een volledige wedstrijd voor de live timing (start.bat moet al draaien).
rem Opent het bedieningsvenster (http://127.0.0.1:8199). Voorbeelden:  simulator.bat --manual    simulator.bat --speed 4 --seed 1
cd /d "%~dp0"
chcp 65001 >nul
python simulator.py %*
pause
