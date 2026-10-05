# KHZS Timing – kastje (Raspberry Pi 3B of nieuwer)

Eén image voor alle kastjes. Na het flashen kies je per kastje de **rol**:

| Rol | Wat | HDMI-scherm |
|---|---|---|
| **uit** (standaard) | enkel beheer | statusscherm met naam, IP-adressen en waar het beheer staat |
| **server** | live timing (SwimTime UDP/26, simulator, doorsturen) | jury / publiek / oproepkamer van dit kastje |
| **scherm** | toont een andere bron | bv. `https://timing.khzs.be/callroom` of `http://khzs-server.local/jury` |

## Flashen
1. Image bouwen (WSL): `bash appliance/build-image.sh` (eerste keer ± 1 uur; daarna enkel de KHZS-stap: `ONLY_KHZS=1 bash appliance/build-image.sh`).
2. SD-kaart in de kaartlezer, dan **als administrator**: `powershell -ExecutionPolicy Bypass -File .\appliance\flash-sd.ps1`
   (of Raspberry Pi Imager → "Use custom" → `dist\khzs-timing-*.img.xz`).
3. Kaart in de Pi, stroom erop (voeding ≥ 2,5 A).

## Eerste keer verbinden
- **Ethernet** aan een netwerk met DHCP → beheer op `http://khzs-timing.local/settings` of het IP op het HDMI-scherm.
- **Geen netwerk?** Na 1 minuut start de Wi-Fi **KHZS-Timing** (wachtwoord `zwemclub`); je gsm opent vanzelf het beheer.
- Beheerderscode: `zwemclub` (wijzig ze in Systeem → Beveiliging).

## Instellen zonder netwerk: `khzs-instellingen.txt`
Steek de kaart in de laptop: de schijf **bootfs** bevat `khzs-instellingen.txt` (Kladblok). Pas aan, sla op,
kaart terug in de Pi. Wachtwoorden worden na het toepassen weer leeggemaakt; fouten komen als `# FOUT` bovenaan.
`herstel = netwerk` zet alle netwerkinstellingen terug naar de standaard.

## Nooit buitengesloten
- De **noodhotspot** kan niet uit: zonder netwerk start hij altijd na de ingestelde tijd.
- Elke **netwerkwijziging** moet binnen 2 minuten bevestigd worden in het beheer, anders komt de vorige terug.
- **SSH-wachtwoordlogin** kan pas uit als een sudo-gebruiker een sleutel heeft; de laatste sudo-gebruiker kan niet weg.
- **Seriële console** (GPIO 14/15, 115200 8N1, 3,3 V) en `khzs-instellingen.txt` werken altijd.
- Een kapotte systeemdienst uit een update valt automatisch terug op de reserveversie uit het image.

## Updates (zonder opnieuw te flashen)
- Pakket maken: `python tools/build_release.py` → `dist/khzs-timing-<versie>.zip`.
- Uploaden in **Updates**, of als asset van een GitHub-release (privé repo + token met enkel leesrechten).
- Zelftest vóór het overschakelen; start de nieuwe versie niet, dan komt de vorige terug. Nooit tijdens een reeks.
- Wijzigingen aan het systeem zelf (diensten, NetworkManager, pakketten, migraties in `appliance/system/`)
  worden pas toegepast na **bevestiging** in Updates.

## Router
Ethernet: WAN (DHCP/vast) · LAN (DHCP-server + NAT) · uit. Wi-Fi: client · hotspot · client+hotspot · uit.
USB: 4G-router of gsm-tethering als WAN. WAN-keuze: automatisch (de verbinding met internet) of vast.
Internet kan volledig uit (lokaal netwerk blijft werken). DHCP-bereik, leasetijd en DNS per LAN.
Wi-Fi met aanmeldpagina: **Hotspot-login** = browser op de Pi die je vanuit het beheer bedient.

## Beheer op afstand (van thuis, via de cloudserver)
- Op het kastje: **Doorsturen** → adres + token van de publieke server en **Beheer op afstand toestaan**
  (of in `khzs-instellingen.txt`: `publieke_server`, `publieke_server_token`, `beheer_op_afstand = aan`).
- Het kastje houdt zelf uitgaande HTTPS-verbindingen open (ethernet, wifi, 4G of hotspot – wat internet heeft);
  er moet niets opengezet worden. Achter een wifi met aanmeldpagina eerst aanmelden (Hotspot-login).
- Op `https://timing.khzs.be/settings` aanmelden met het **beheerwachtwoord van de cloudserver** → **Kastjes** →
  *Beheer openen*. Daarna vraagt het kastje zijn eigen beheerderscode. Alles werkt, ook console en updates.
- Netwerkwijzigingen op afstand: niet bevestigd binnen 2 minuten (bv. omdat de verbinding wegviel) = teruggedraaid.

## Op het kastje (beheer)
- **Simulator**: volledige nep-wedstrijd, ingebouwd; begint opnieuw na de laatste reeks; echte data stopt hem.
- **Infoscherm**: geen wedstrijd · volgende wedstrijd · pauze (tot een uur of een duur) · … of **Automatisch**
  volgens de agenda. Ook op de cloudserver zelf in te stellen (beheerwachtwoord), en altijd op `/info`.
- **Netwerkschijven**: Windows-shares altijd **alleen-lezen** koppelen (`/mnt/<naam>`) en de SwimTime-database
  kiezen: rechtstreeks lezen (mdbtools) of via een kopie.
- **Console**: root-terminal in de browser. **Cloudserver**: status, logboek, updates, console van de cloudserver.
- **Rol & scherm**: HDMI-resolutie (automatisch via de tv of vast) en draaiing (staand scherm).
- Opstartscherm: `sudo khzs splash 20` toont het zonder herstart; `sudo khzs splash install` installeert het opnieuw.

## Netwerk in het zwembad
- Standaard: eigen router met DHCP, SwimTime-pc en kastje(s) eraan → niets in te stellen (ethernet = WAN, DHCP).
- Zonder router: kastje ethernet op **LAN** = DHCP-server + NAT; toestellen en **vaste adressen** in Netwerk › DHCP.
- SwimTime-broadcast: op alle verbindingen; in Gegevens › SwimTime-ontvangst zie je **automatisch** via welke
  verbinding en van welke pc ze binnenkomt, met één klik vast te zetten (verbinding en/of afzender).
- WAN-voorkeur ethernet > Wi-Fi > USB-4G, automatisch omschakelen bij verlies van internet; NetworkManager blijft
  eindeloos opnieuw proberen; ingeplugde 4G-dongle wordt meteen opgenomen.
- Kastjes vinden elkaar (UDP 2627). **Maar één server per netwerk**: een tweede wordt geweigerd/gemeld.
  Schermkastje: bron *Automatisch: de server op dit netwerk* + pagina publiek/oproepkamer/jury, en een
  **reservebron** (bv. `auto`) als de cloud wegvalt; het scherm herstelt zich zelf na een foutpagina.
- **Naam** per toestel (Systeem › Dit toestel): zichtbaar in het beheer en klein rechtsonder op het HDMI-scherm.

## Paden op de Pi
`/opt/khzs/current` (code, symlink) · `/opt/khzs/releases/` · `/opt/khzs/agent/` (starter + reserve) ·
`/var/lib/khzs/` (instellingen, historiek, `system.json`) · diensten `khzs-timing`, `khzs-agent`, `khzs-kiosk`.
