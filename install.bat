@echo off
setlocal
title ALGE Live Timing - installatie
cd /d "%~dp0"
echo.
echo === ALGE Live Timing - installatie ===
echo.

rem --- 1. Python 3 ---------------------------------------------------------
where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%v in ('python --version') do echo [OK] %%v gevonden
        goto :python_ok
    )
)
echo [..] Python 3 niet gevonden - installeren via winget (enkel voor deze gebruiker)...
where winget >nul 2>nul
if errorlevel 1 (
    echo [!!] winget ontbreekt. Installeer Python 3 handmatig via https://www.python.org/downloads/
    echo      en vink "Add python.exe to PATH" aan. Start daarna install.bat opnieuw.
    goto :einde
)
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo [!!] Installatie mislukt. Installeer Python 3 handmatig via https://www.python.org/downloads/
    goto :einde
)
echo [OK] Python geinstalleerd. Sluit dit venster en start install.bat opnieuw zodat PATH ververst is.
goto :einde
:python_ok

rem --- 2. Access-driver (enkel voor de database-koppeling) ---------------
powershell -NoProfile -Command "if (Get-OdbcDriver -Platform 64-bit -ErrorAction SilentlyContinue | Where-Object Name -like '*Access Driver (*.mdb, *.accdb)*') { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [--] 64-bit Access-driver niet gevonden. Enkel nodig voor de SwimTime-database-koppeling;
    echo      de live timing via UDP werkt zonder. Installeer indien gewenst
    echo      "Microsoft Access Database Engine 2016 Redistributable" ^(64-bit^) van Microsoft.
) else (
    echo [OK] 64-bit Access-driver gevonden ^(database-koppeling mogelijk^)
)

rem --- 3. Firewall (optioneel, enkel als administrator) -------------------
net session >nul 2>nul
if errorlevel 1 (
    echo [--] Niet als administrator gestart: firewallregels overgeslagen.
    echo      Windows vraagt bij de eerste start van start.bat om Python toe te laten - kies dan "Toestaan".
) else (
    netsh advfirewall firewall delete rule name="ALGE live timing UDP 26" >nul 2>nul
    netsh advfirewall firewall delete rule name="ALGE live timing web 8080" >nul 2>nul
    netsh advfirewall firewall add rule name="ALGE live timing UDP 26" dir=in action=allow protocol=UDP localport=26 remoteip=192.168.0.188 profile=any >nul
    netsh advfirewall firewall add rule name="ALGE live timing web 8080" dir=in action=allow protocol=TCP localport=8080 remoteip=localsubnet profile=any >nul
    echo [OK] Firewallregels toegevoegd: UDP 26 van 192.168.0.188, TCP 8080 vanaf het lokale netwerk
)

echo.
echo Klaar. Start de live timing met start.bat
:einde
echo.
pause
