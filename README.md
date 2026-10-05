# ALGE SwimTime – read-only live timing

Luistert naar de scorebord-broadcast van ALGE SwimTime en toont die live in de browser.
Enkel Python 3 (standaardbibliotheek), geen installatie nodig.

## Installeren op een andere pc
1. Pak de zip uit, bv. naar `C:lge-livetiming`.
2. Dubbelklik `install.bat` (rechtermuisklik > *Als administrator uitvoeren* om ook de firewallregels te zetten).
   Installeert Python 3 indien nodig (winget) en controleert de Access-driver voor de database-koppeling.
3. Start met `start.bat`. Laat Windows Python toe op het netwerk als het erom vraagt.

## Starten
Dubbelklik `start.bat`. De server start en de browser opent.
In het consolevenster: **o** = publiek, **j** = jury, **s** = instellingen, **q** = stoppen.
De netwerkadressen voor andere toestellen worden bij het starten getoond (poort 8080).

Draai de server bij voorkeur op een **bekabelde** pc: via Wi-Fi gaan broadcasts verloren.
Windows Firewall moet inkomend UDP/26 en TCP/8080 voor Python toelaten (vraag bij eerste start).
De server gebruikt standaard enkel een UDP-socket. `--source tshark|auto` (Npcap) NIET gebruiken op beheerde toestellen:
tshark veroorzaakt meldingen in Cisco XDR.

## Weergaven
| Adres | Voor wie | Inhoud |
|---|---|---|
| `/` (ook `/publiek`) | publiek | enkel eindtijden en officiële tussentijden; geen fouten/markeringen (server-side gefilterd) |
| `/jury` | jurytafel | alles: manueel/verdacht/gecorrigeerd, foutenlijst, jurymodus standaard aan |
| `/settings` | beheer | drempels en weergave-opties |

Licht/donker: knop rechtsboven (Auto volgt het toestel).

## Publieke server (relay) – zend-modus en relay-modus
Zelfde programma, twee rollen:

| Rol | Waar | Start |
|---|---|---|
| **Zender** (zwembad) | laptop op het ALGE-netwerk; ontvangt UDP/26 en de databasekopie, stuurt de verwerkte stand **uitgaand** via HTTPS door | `start.bat` met in `/settings` → "Doorsturen": adres van de publieke server + token |
| **Relay** (internet) | kleine Linux-server (LXC/VPS) achter HTTPS; bedient `/` (publiek) en `/callroom` voor iedereen, en `/jury`, `/settings` achter een wachtwoordformulier | `python3 livetiming.py --relay-server --http-port 8080` met `relay_token` en `jury_password` in `settings.json` |

- De zender heeft geen open poort nodig (geen cloudflared, geen port-forward aan het zwembad). Valt het internet weg, dan blijven de lokale pagina's op de laptop werken; de relay toont de laatste stand en meldt "geen gegevens".
- Op de relay: `POST /ingest` (header `X-Ingest-Token`), login via `/login` (cookie 12 u), `/logout`. Wijzigen van instellingen en database-sync kan voorlopig enkel op de laptop (fase 2).
- Zet HTTPS ervoor (Caddy of een bestaande reverse proxy); de login-cookie krijgt `Secure` als `X-Forwarded-Proto: https` meekomt.
- Publieke pagina is `noindex`; toon er geen geboortedata of fouten (gebeurt al server-side).
- **In gebruik:** https://timing.khzs.be = LXC 133 `khzs-livetiming` (10.10.30.133) op Proxmox `pve01`, met cloudflared in dezelfde container
  (Cloudflare-route `timing.khzs.be` → `http://localhost:8080`). Code bijwerken: `python tools/pve_deploy.py deploy` (leest `proxmox.env`;
  geheimen in `relay/relay.env`, beide niet in git). Status: `python tools/pve_deploy.py status`.
- Op de zend-laptop: `/settings` → *Doorsturen*: adres `https://timing.khzs.be` + `RELAY_TOKEN` uit `relay/relay.env`.
  Via de opdrachtregel: `--relay https://timing.khzs.be --relay-token=<token>` (met `=`, het token kan met een `-` beginnen).
- Onder elke pagina staat dat het geen officiële tijden zijn.

## Simulator (testen zonder SwimTime)
`simulator.bat` (of `python simulator.py`) speelt een wedstrijd na met een verzonnen programma, zwemmers en clubs, en opent
een **bedieningsvenster in SwimTime-stijl** op http://127.0.0.1:8199 (enkel deze pc; bezette poort = automatisch de volgende).
Start eerst `start.bat`. De simulator stuurt naar de TCP-invoer van de live timing (`feed_port`, standaard 2626, enkel deze pc),
niet via UDP/26 en nooit naar SwimTime of het netwerk.

**Bedieningsvenster** – links het programma (klik = reeks laden), midden de banen met reactietijd, 50m-tussentijden, eindtijd en
plaats, rechts de wedstrijdklok, de status en het logboek.

| Knop / toets | Wat |
|---|---|
| Volgende reeks · `V` | startlijst laden (scorebord: Startlist, daarna "klaar") |
| START · `S` | startsignaal: klok loopt, reactietijden |
| Aantik · `1`–`8` | touchpad van die baan indrukken (volgende 50 m) |
| Manueel | tijd ingeven (manueel ophogen, komt laat binnen → rode M in de live timing) |
| Wis | tijden van een baan wissen (-1) |
| Stop klok · `E` | klok stoppen (gebeurt ook automatisch als iedereen binnen is) |
| Uitslag · `U` | rangschikking tonen (einde reeks) |
| Reset · `R` | valse start: klok en tijden terug, opnieuw startklaar |
| Volgende stap · `N`, Pauze · spatie | volgende logische stap / alles bevriezen |

Vinkjes: **Automatisch wedstrijdverloop** (laden, starten, uitslag, volgende vanzelf) en **Zwemmers tikken zelf aan**
(realistisch zwemmen; uit = alles zelf aantikken). Snelheid ×1 tot ×32.

| Optie | Betekenis |
|---|---|
| `--manual` | starten zonder automatisch verloop (zelf bedienen) |
| `--speed 4` | beginsnelheid |
| `--events 12 --per-event 18` | grootte van het programma |
| `--pause 25 --result-time 15` | automatisch verloop: seconden tot de start / uitslag op het scherm |
| `--manual-chance 0.06 --dns-chance 0.03` | kans op een manueel ingegeven eindtijd / niet gestart |
| `--extra-touch-chance 0.15` | kans per zwemmer op een extra (te vroege) tik tijdens de race; de echte tik corrigeert ze (✎ in de jury) |
| `--seed 7` | telkens hetzelfde programma |
| `--no-gui` | enkel console (spatie, n, +/-, a, q) |

Alle pagina's tonen dan bovenaan **SIMULATIE**; de oproepkamer toont het programma van de simulator; er wordt niets met de
SwimTime-database gekoppeld. Komt er daarna echte SwimTime-data binnen (na 5 s stilte van de simulator), dan worden de
simulatiereeksen automatisch uit de historiek gewist.

## Telefoon
Op een gsm (smal of laag scherm) toont de publieke weergave enkel de huidige reeks, compact en zo groot mogelijk zodat
alle banen op het scherm passen; past het niet, dan vallen eerst de aflossingsleden en dan de tussentijden weg.
Het tussenscherm heeft een staande variant (titel op twee regels, namen onder elkaar in het water).

## Oproepkamer
`/callroom` volgt de stroom live → startblok → oproepkamer:
- **groene balk bovenaan**: de reeks die na de live reeks komt en al **aan de startblok** staat (wedstrijd, reeks, geplande tijd en de namen per baan op één regel);
- **links (amber)**: de reeksen daarna, die in de **oproepkamer** zitten (aantal = `callroom_heats`);
- **rechts (blauw)**: de **live** reeks (en eventueel de vorige).

Bij een nieuwe reeks kondigt het tussenscherm de reeks aan die **nu de oproepkamer binnen mag** (de reeks die net in de
oproepkamerlijst bijkomt), met de namen in het water. Instelbaar via `callroom_splash_heat` (0 = die nieuwe reeks, 1 = de eerste in de oproepkamer, …).

## Jurymodus
Knop "Jurymodus" (of `/?jury=1`): links (2/3) de huidige reeks, rechts de vorige reeksen (`overview_heats`, standaard 2)
met fouten/opmerkingen bovenaan, uitslag en enkel de officiële tussentijden (4 per regel).
Officieel: 50/100/400/800 (instelbaar via `official_split_distances`) bij enkelvoudige slagen,
50 bij 200 wissel, 100 bij 400 wissel, bij aflossingen enkel de eerste zwemmer (incl. diens eindtijd).

## Testen zonder tijdsysteem
Gebruik `simulator.bat` (zie hierboven). `replay_test.bat` (pcap-replay) vereist tshark en is niet bruikbaar op beheerde toestellen.

## Instellingen
Via `/settings` of `settings.json`. Wijzigen kan vanaf deze pc, of elders met de beheerderscode
(`admin_token`). Verzoeken via cloudflared gelden altijd als extern.

## Protocol (UDP/26 broadcast, ASCII/UTF-8, `sleutel<TAB>waarde`, eindigt op CR)
| Bericht | Betekenis |
|---|---|
| `Time 1 RunningTime x` | wedstrijdklok, ~10×/s; `-1` = reset |
| `Time 1 Ready 0` | klaar voor start |
| `Time n ReactionTime x` | reactietijd baan n |
| `Time n LaneTime x Rank r Lap l Points p` | split/eindtijd baan n; `-1` = wissen |
| `Layout Startlist / LaneOriented / RankOriented` | weergave op het scorebord |
| `Meet/Session/Event/Heat …`, `Competitor n <veld> <waarde>`, `Record0-2 …` | startlijstcontext |

Tijden in 1/10 000 s (ook tijd-van-dag). Datums = `(jaar<<16)|(maand<<8)|dag`.

**Afgeleide controles** (staan niet expliciet in de broadcast):
- *Manueel ingegeven* (rood **M**): de tijd komt meer dan `manual_threshold_s` later binnen dan start + tijd.
  Paneeltijden komen binnen 0,1–0,3 s binnen, manuele tijden pas na seconden.
- *Verdacht* (oranje **!**): negatief zwemmen buiten de marge, of een segment verschilt meer dan `max_seg_diff_s` met het vorige.
- Plaatsen worden zelf herberekend: SwimTime stuurt bij een manuele tijd de plaatsen van de andere banen niet opnieuw.

Niet beschikbaar in de broadcast: handtijden (peer), het verschil peer/paneel, en correcties na het doorschakelen naar de volgende reeks.
Aflossingen (overnametijden) zijn nog niet gecaptured.

Ruwe pakketten worden gelogd in `logs/` (opnieuw af te spelen met `--replay logs/raw_….log`).
