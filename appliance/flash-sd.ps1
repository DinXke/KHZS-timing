# KHZS Timing – image naar een SD-kaart schrijven.

# Rechtermuisklik > "Uitvoeren met PowerShell" ALS ADMINISTRATOR, of in een admin-PowerShell:

#   powershell -ExecutionPolicy Bypass -File .\appliance\flash-sd.ps1

# Veiligheid: enkel USB-schijven kleiner dan 256 GB, nooit de systeemschijf, en je moet het schijfnummer

# en het woord WISSEN bevestigen.

#   zonder vragen:  ...lash-sd.ps1 -Disk 1 -Bevestig WISSEN [-Log pad] [-NietWachten]

param([int]$Disk = -1, [string]$Bevestig = '', [string]$Log = '', [switch]$NietWachten, [string]$Python = '')

$ErrorActionPreference = 'Stop'

if ($Log) { Start-Transcript -Path $Log -Force | Out-Null }

function Wacht($t) { if (-not $NietWachten) { Read-Host $t } }

$root = Split-Path $PSScriptRoot -Parent



if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {

    Write-Host "Start dit script als administrator (rechtermuisklik > Als administrator uitvoeren)." -ForegroundColor Red

    Wacht "Enter om te sluiten"; exit 1

}

$img = Get-ChildItem (Join-Path $root 'dist') -Filter 'khzs-timing-*.img*' | Where-Object { $_.Name -notlike '*.sha256' } |

    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $img) { Write-Host "Geen image gevonden in dist\." -ForegroundColor Red; Wacht "Enter"; exit 1 }



$disks = Get-Disk | Where-Object { $_.BusType -eq 'USB' -and -not $_.IsBoot -and -not $_.IsSystem -and $_.Size -lt 256GB -and $_.Size -gt 1GB }

if (-not $disks) { Write-Host "Geen geschikte SD-kaart gevonden (USB, < 256 GB)." -ForegroundColor Red; Wacht "Enter"; exit 1 }



Write-Host ""

Write-Host "  Image: $($img.Name)  ($([math]::Round($img.Length/1MB)) MB)" -ForegroundColor Cyan

Write-Host ""

$disks | ForEach-Object {

    $letters = (Get-Partition -DiskNumber $_.Number -ErrorAction SilentlyContinue | Where-Object DriveLetter | ForEach-Object { "$($_.DriveLetter):" }) -join ' '

    Write-Host ("  Schijf {0}: {1}  {2} GB  {3}" -f $_.Number, $_.FriendlyName, [math]::Round($_.Size/1GB,1), $letters)

}

Write-Host ""

$n = if ($Disk -ge 0) { $Disk } else { Read-Host "Schijfnummer van de SD-kaart" }

$d = $disks | Where-Object { $_.Number -eq [int]$n }

if (-not $d) { Write-Host "Ongeldige keuze." -ForegroundColor Red; Wacht "Enter"; exit 1 }

Write-Host ("ALLES op schijf {0} ({1}, {2} GB) wordt gewist." -f $d.Number, $d.FriendlyName, [math]::Round($d.Size/1GB,1)) -ForegroundColor Yellow

$ans = if ($Bevestig) { $Bevestig } else { Read-Host "Typ WISSEN om door te gaan" }

if ($ans -cne 'WISSEN') { Write-Host "Afgebroken."; exit 1 }



Write-Host "Kaart vrijmaken…"

Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Where-Object DriveLetter | ForEach-Object {

    Remove-PartitionAccessPath -DiskNumber $d.Number -PartitionNumber $_.PartitionNumber -AccessPath "$($_.DriveLetter):\" -ErrorAction SilentlyContinue }

Clear-Disk -Number $d.Number -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue

Set-Disk -Number $d.Number -IsOffline $true -ErrorAction SilentlyContinue



Write-Host "Schrijven (enkele minuten)…"

# als administrator vindt Windows soms enkel de Store-snelkoppeling: dan het pad meegeven (-Python) of 'py' gebruiken
$py = if ($Python) { $Python } elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
& $py (Join-Path $PSScriptRoot 'flash_sd.py') $d.Number $img.FullName

$ok = $LASTEXITCODE -eq 0

Set-Disk -Number $d.Number -IsOffline $false -ErrorAction SilentlyContinue

Update-HostStorageCache

if ($ok) {

    Write-Host ""

    Write-Host "Klaar. Op de kaart staat nu de schijf 'bootfs' met khzs-instellingen.txt (verschijnt na de eerste start van de Pi)." -ForegroundColor Green

    Write-Host "Kaart veilig verwijderen, in de Pi steken en stroom aansluiten."

} else { Write-Host "Schrijven mislukt." -ForegroundColor Red }

if ($Log) { Stop-Transcript | Out-Null }

Wacht "Enter om te sluiten"

if (-not $ok) { exit 1 }

