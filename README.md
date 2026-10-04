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
| `/publiek` (ook `/`) | publiek | enkel eindtijden en officiële tussentijden; geen fouten/markeringen (server-side gefilterd) |
| `/jury` | jurytafel | alles: manueel/verdacht/gecorrigeerd, foutenlijst, jurymodus standaard aan |
| `/settings` | beheer | drempels en weergave-opties |

Licht/donker: knop rechtsboven (Auto volgt het toestel).

## Jurymodus
Knop "Jurymodus" (of `/?jury=1`): links (2/3) de huidige reeks, rechts de vorige reeksen (`overview_heats`, standaard 2)
met fouten/opmerkingen bovenaan, uitslag en enkel de officiële tussentijden (4 per regel).
Officieel: 50/100/400/800 (instelbaar via `official_split_distances`) bij enkelvoudige slagen,
50 bij 200 wissel, 100 bij 400 wissel, bij aflossingen enkel de eerste zwemmer (incl. diens eindtijd).

## Testen zonder tijdsysteem
`replay_test.bat` speelt `sample_capture2.pcapng` af aan 5× snelheid op poort 8090 (vereist Wireshark/tshark).

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
