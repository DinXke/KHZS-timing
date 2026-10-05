"""Wedstrijdsimulator voor de live timing – bootst ALGE SwimTime na, zonder echte SwimTime.

Stuurt dezelfde berichten als de SwimTime-scorebordbroadcast (Layout/Meet/Session/Event/Record/Heat/
Competitor/Time) naar de TCP-invoer van livetiming.py (standaard tcp://127.0.0.1:2626), met een nep
programma, nep zwemmers en clubs, startlijsten, start, realistisch zwemmen (reactie + 50m-tussentijden),
einde reeks, rangschikking en volgende reeks. Alle namen en clubs zijn verzonnen.

Gebruik:  python simulator.py [--speed 4] [--events 12] [--pause 20] [--manual] [--seed 7]
Opent een bedieningsvenster in SwimTime-stijl (http://127.0.0.1:8199, enkel deze pc): programma, startlijst,
START (startsignaal), aantikken per baan, manueel ophogen, wissen, einde, uitslag, volgende reeks, reset.
Console: spatie = pauze · n = volgende stap · + / - = snelheid · a = automatisch aan/uit · q = stoppen

Er wordt NOOIT naar SwimTime of het wedstrijdnetwerk gestuurd: standaard enkel naar deze pc (TCP).
--udp HOST:PORT stuurt UDP-unicast (bv. 127.0.0.1:26); broadcastadressen worden geweigerd.
"""
import argparse
import ipaddress
import json
import os
import random
import socket
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import msvcrt          # Windows: toetsen zonder Enter
except ImportError:
    msvcrt = None

# ------------------------------------------------------------------ verzonnen gegevens
CLUBS = [("ZCT", "ZC Testerlo"), ("DSI", "Dolfijn Simulo"), ("GSP", "Golfslag Proefdorp"),
         ("ZKN", "Zwemkring Nepstad"), ("WRD", "Waterrat Demoville"), ("AQF", "Aqua Fictief")]
FIRST_F = """Lotte Emma Noor Lien Fien Marie Elise Febe Hanne Jana Lore Nina Amber Julie Saar Ines Kato Mila Roos Zoë
Lisa Axelle Margot Yana Anna Louise Olivia Ella Hélène Charlotte Jade Sofie Laura Eline Lena Lina Nora Paulien Silke Tess
Vera Wiebe Yasmine Zita Amélie Bo Camille Daphné Esther Fleur Griet Hannelore Iris Jolien Kaat Liesbeth Manon Nele Oona
Pien Quinty Rani Sanne Tine Ulla Valerie Wendy Xena Ylke Zara Aline Babette Cato Dorien Evi Floor Gitte Hind Isaura
Jente Kiara Lieze Marthe Nathalie Odile Pauline Rosalie Selma Tamara Ute Vienna Ayla Elif Meryem Sara Lucía Mia
Chloë Victoria Estelle Lucie Anouk Merel Ilse Leen Femke Joke Birgit Ellen Klaartje""".split()
FIRST_M = """Lars Wout Senne Jef Mats Arne Bram Lukas Robbe Thibo Jasper Milan Daan Stan Kobe Noah Vic Ruben Jonas Seppe
Warre Lander Tuur Finn Arthur Louis Jules Victor Mathis Lowie Rune Cas Mauro Nand Ward Bavo Stijn Pieter Thomas Simon
Matthias Gilles Hannes Jarne Kasper Lennert Maarten Niels Olivier Quinten Rik Siebe Toon Ugo Viktor Wannes Xander Yorben
Zeno Alexander Basiel Cedric Dries Emiel Ferre Gust Hendrik Ilias Joren Kian Leon Mohamed Nathan Otis Pepijn Rayan
Sander Tibo Vince Wolf Yusuf Adam Amir Bilal Elias Hamza Ismail Omar Youssef Mateo Lucas Hugo Raf Koen Dirk Bert Lowiek
Emile Jordy Sem Tijs Mattis Briek Lex""".split()
LAST = """Vermeulen Peeters Claes Wouters Jacobs Maes Mertens Willems Goossens Janssens Van Damme Lemmens Hermans Smets
Aerts Michiels Desmet Martens Bogaerts Cools Verhoeven Thys Vandenberghe Geerts Hendrickx Stevens Coppens Daems Lenaerts
Nijs Dubois Lambert Dupont Leclercq Van de Velde Declercq De Smet De Backer Hoste Verstraete Verbeke Coppieters Lambrecht
Vanhoutte De Clercq Pauwels Segers Van Hoof Bosmans Baert Van Acker Dewulf Mortier Vercammen Somers Van Looy Joris
Van Dyck Huysmans Verlinden Wuyts Van Gestel Moens Nuyts Gielen Bastiaens Vanderstraeten Indekeu Schepers Kenis
Swinnen Vanhove Vos Steegmans Thijs Vrancken Jorissen Reynders Nelissen Bollen Dirix Vanherck Leysen Bloemen Engelen
Ceyssens Vandeweyer Moors Pieters Raskin Driesen Hoebers Daniëls Vanmechelen Coenen Wijnants Lambrechts Theunis
Gijbels Vanhees Ramaekers Peters Boonen Feyen Lowet Vandormael Bijnens Stas Pirotte Cuypers Nulens Kerkhofs Cox
Hoydonckx Vanoppen Bamps Bours Corstjens Vanheusden Hendrikx Schurmans Lismont Liebens Duchateau Vleugels Snijders
El Amrani Benali Yilmaz Kaya Demir Öztürk Mahieu Delvaux Rousseau Lejeune Simon Laurent Fontaine Martin Bernard
Leroy Renard Gilson Kumar Nguyen Kowalski Novak Rossi Fernández García Silva Haddad Mansour Diallo Traoré Mukendi
Van den Broeck Van Aelst Van Gorp Van Rompuy Op de Beeck In 't Ven Ten Haaf D'Hondt Van Hecke Verschueren""".splitlines()
_MULTI = ("Van Damme", "Van de Velde", "De Smet", "De Backer", "De Clercq", "Van Hoof", "Van Acker", "Van Looy",
          "Van Dyck", "Van Gestel", "El Amrani", "Van den Broeck", "Van Aelst", "Van Gorp", "Van Rompuy", "Op de Beeck",
          "In 't Ven", "Ten Haaf", "Van Hecke")


def _split_names(lines):
    txt = " ".join(lines)
    for m in _MULTI:
        txt = txt.replace(m, m.replace(" ", "_"))
    return sorted({w.replace("_", " ") for w in txt.split()})


LAST = _split_names(LAST)
# basistempo per 50 m (s) voor een goede clubzwemmer, heren; dames ×1.08
PACE = {"Vrije slag": 30.5, "Rugslag": 35.0, "Schoolslag": 39.0, "Vlinderslag": 33.5, "Wisselslag": 36.0}
DIST_FACTOR = {50: 1.00, 100: 1.06, 200: 1.11, 400: 1.16, 800: 1.20, 1500: 1.23}
IM_ORDER = ["Vlinderslag", "Rugslag", "Schoolslag", "Vrije slag"]

DEFAULT_PROGRAM = [
    ("50m Vrije slag", "dames"), ("50m Vrije slag", "heren"), ("100m Schoolslag", "dames"),
    ("100m Schoolslag", "heren"), ("200m Wisselslag", "dames"), ("200m Wisselslag", "heren"),
    ("100m Rugslag", "dames"), ("100m Rugslag", "heren"), ("50m Vlinderslag", "dames"),
    ("50m Vlinderslag", "heren"), ("400m Vrije slag", "dames"), ("400m Vrije slag", "heren"),
    ("4x50m Vrije slag", "gemengd"), ("100m Vrije slag", "dames"), ("100m Vrije slag", "heren"),
    ("4x50m Wisselslag", "gemengd"),
]
def lane_numbers(lanes, first=None):
    """Baannummers van het bad: 10 banen = 0–9, anders 1..n."""
    if first is None:
        first = 0 if lanes >= 10 else 1
    return list(range(first, first + lanes))


def seed_order(nums):
    """Reeksindeling: snelste in de middelste baan, dan afwisselend rechts/links naar buiten
    (8 banen 1–8: 4,5,3,6,2,7,1,8 · 10 banen 0–9: 4,5,3,6,2,7,1,8,0,9 · 10 banen 1–10: 5,6,4,7,3,8,2,9,1,10)."""
    m = (len(nums) - 1) // 2
    out = [nums[m]]
    for k in range(1, len(nums)):
        for i in (m + k, m - k):
            if 0 <= i < len(nums) and nums[i] not in out:
                out.append(nums[i])
    return out


def alge_date(d):
    return (d.year << 16) | (d.month << 8) | d.day


def tod(dt):
    return (dt.hour * 3600 + dt.minute * 60 + dt.second) * 10000


def fmt(t):
    m, s = divmod(t / 10000, 60)
    return f"{int(m)}:{s:05.2f}" if m else f"{s:.2f}"


# ------------------------------------------------------------------ programma opbouwen
class Swimmer:
    def __init__(self, rnd, gender, idx):
        self.gender = gender
        self.first = rnd.choice(FIRST_F if gender == "F" else FIRST_M)
        self.last = rnd.choice(LAST)
        self.club = rnd.choice(CLUBS)[0]
        self.code = f"{self.club}/{90000 + idx:05d}/{rnd.randint(8, 13):02d}"
        by = date.today().year - rnd.randint(11, 19)
        self.birthday = alge_date(date(by, rnd.randint(1, 12), rnd.randint(1, 28)))
        self.ability = rnd.uniform(0.92, 1.30)          # 1.0 = basistempo; hoger = trager
        self.strength = {k: rnd.uniform(0.95, 1.07) for k in PACE}


def parse_disc(disc):
    legs, rest = 1, disc
    if "x" in disc.split("m")[0]:
        a, rest = disc.split("x", 1)
        legs = int(a)
    dist = int(rest.split("m")[0])
    stroke = rest.split("m", 1)[1].strip()
    return legs, dist, stroke


def predicted(sw, stroke, dist, rnd):
    """Voorspelde eindtijd (s) + verdeling over de 50m-stukken."""
    g = 1.08 if sw.gender == "F" else 1.0
    n = max(1, dist // 50)
    pace = []
    for i in range(n):
        st = IM_ORDER[i * 4 // n] if stroke == "Wisselslag" and n >= 4 else (
            IM_ORDER[i] if stroke == "Wisselslag" else stroke)
        base = PACE[st] * sw.strength[st]
        p = base * g * sw.ability * DIST_FACTOR.get(dist, 1.2)
        p *= 0.93 if i == 0 else (1.0 + 0.012 * i if i < n - 1 else 0.985 + 0.012 * i)  # start, verval, eindspurt
        pace.append(p)
    return pace


class Program:
    def __init__(self, rnd, n_events, lanes, per_event, start_dt, first_lane=None):
        self.rnd = rnd
        self.lanes = lanes
        pool = [Swimmer(rnd, "F", i) for i in range(110)] + [Swimmer(rnd, "M", 200 + i) for i in range(110)]
        seen = set()
        for sw in pool:                    # geen twee zwemmers met dezelfde naam, weinig dubbele familienamen
            tries = 0
            while (sw.first, sw.last) in seen or (tries < 3 and any(sw.last == l for _, l in seen)):
                sw.first = rnd.choice(FIRST_F if sw.gender == "F" else FIRST_M)
                sw.last = rnd.choice(LAST)
                tries += 1
            seen.add((sw.first, sw.last))
        self.events = []
        t = start_dt
        progs = (DEFAULT_PROGRAM * 3)[:n_events]
        for evnr, (disc, cat) in enumerate(progs, 1):
            legs, dist, stroke = parse_disc(disc)
            full = f"{disc} {cat}"
            heats = []
            if legs > 1:
                teams = []
                for code, _ in CLUBS:
                    for letter in "12"[:rnd.randint(1, 2)]:
                        members = rnd.sample([s for s in pool if s.club == code] or pool, 4)
                        strokes = (["Rugslag", "Schoolslag", "Vlinderslag", "Vrije slag"] if stroke == "Wisselslag"
                                   else [stroke] * 4)
                        legp = [predicted(m, st, dist, rnd) for m, st in zip(members, strokes)]
                        legp = [[p * (0.97 if j > 0 else 1.0) for p in lp] for j, lp in enumerate(legp)]  # vliegende wissel
                        entry = sum(sum(lp) for lp in legp) * rnd.uniform(0.99, 1.04)
                        teams.append({"team": f"{code} {letter}", "club": code, "members": members, "pace": [p for lp in legp for p in lp],
                                      "entry": entry})
                entries = sorted(teams, key=lambda e: e["entry"])
            else:
                cands = [s for s in pool if cat == "gemengd" or s.gender == ("F" if cat == "dames" else "M")]
                chosen = rnd.sample(cands, min(len(cands), per_event + rnd.randint(-per_event // 3, per_event // 3)))
                entries = []
                for s in chosen:
                    pace = predicted(s, stroke, dist, rnd)
                    entries.append({"swimmer": s, "pace": pace, "entry": sum(pace) * rnd.uniform(0.98, 1.05)})
                entries.sort(key=lambda e: e["entry"])
            # reeksindeling: snelste reeks laatst, middenbanen voor de snelsten
            # entries staan van snel naar traag: de snelsten (één volle reeks) vormen de laatste reeks, de rest komt eerst
            chunks = [entries[i:i + lanes] for i in range(0, len(entries), lanes)][::-1]
            if len(chunks) > 1 and len(chunks[0]) < 3:          # minstens 3 in de eerste reeks
                need = 3 - len(chunks[0])
                chunks[0] = chunks[1][-need:] + chunks[0]
                chunks[1] = chunks[1][:-need]
            order = seed_order(lane_numbers(lanes, first_lane))
            # zwemvolgorde traag -> snel, nummering van hoog naar laag: de snelste reeks is reeks 1 en zwemt als laatste
            for pos, chunk in enumerate(chunks):
                hnr = len(chunks) - pos
                lanemap = {}
                for e, ln in zip(sorted(chunk, key=lambda e: e["entry"]), order):
                    lanemap[ln] = e
                est = max(e["entry"] for e in chunk) + 75
                heats.append({"number": hnr, "name": f"Serie {hnr}", "lanes": lanemap, "start": t})
                t += timedelta(seconds=est)
            self.events.append({"number": evnr, "discipline": full, "legs": legs, "dist": dist, "stroke": stroke,
                                "heats": heats, "start": heats[0]["start"]})
            t += timedelta(seconds=60)

    def schedule_json(self):
        out = []
        for ev in self.events:
            for h in ev["heats"]:
                lanes = []
                for ln, e in sorted(h["lanes"].items()):
                    if "team" in e:
                        lanes.append({"lane": ln, "firstName": "", "lastName": "", "club": e["club"], "team": e["team"],
                                      "entryTime": int(e["entry"] * 10000), "status": ""})
                    else:
                        s = e["swimmer"]
                        lanes.append({"lane": ln, "firstName": s.first, "lastName": s.last, "club": s.club, "team": "",
                                      "entryTime": int(e["entry"] * 10000), "status": ""})
                out.append({"session": "1", "date": date.today().isoformat(),
                            "event": {"number": str(ev["number"]), "discipline": ev["discipline"]},
                            "heat": {"number": str(h["number"]), "name": h["name"], "distance": ev["legs"] * ev["dist"],
                                     "startTime": h["start"].strftime("%H:%M")},
                            "relay": ev["legs"] > 1, "finished": bool(h.get("finished")), "lanes": lanes, "lanesKnown": True})
        return out


# ------------------------------------------------------------------ verzenden
class Sender:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.sock = None
        self.next_try = 0
        self.on_connect = None
        self.sent = 0
        self.muted = False                 # uitval simuleren: niets versturen
        self.udp = None
        self.direct = getattr(args, "direct", None)   # ingebouwd in de server: berichten rechtstreeks afleveren
        if self.direct:
            self.sock = True
        elif getattr(args, "udp", None):
            host, port = args.udp.rsplit(":", 1)
            ip = ipaddress.ip_address(socket.gethostbyname(host))
            if not args.allow_broadcast and (str(ip).endswith(".255") or ip.is_multicast or str(ip) == "255.255.255.255"):
                sys.exit(f"geweigerd: {ip} lijkt een broadcastadres. Gebruik --allow-broadcast enkel op een testnetwerk zonder SwimTime.")
            self.udp = (str(ip), int(port))
            self.usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _connect(self):
        if time.time() < self.next_try:
            return False
        self.next_try = time.time() + 2
        try:
            s = socket.create_connection((self.args.host, self.args.port), timeout=2)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
            print(f"\n[verbonden met tcp://{self.args.host}:{self.args.port}]")
            if self.on_connect:
                threading.Thread(target=self.on_connect, daemon=True).start()
            return True
        except OSError:
            return False

    def send(self, msg):
        if self.muted:
            return
        if self.direct:
            self.direct(msg)
            self.sent += 1
            return
        if self.udp:
            self.usock.sendto(msg.encode("utf-8") + b"\r", self.udp)
            self.sent += 1
            return
        with self.lock:
            if self.sock is None and not self._connect():
                return
            try:
                self.sock.sendall(msg.encode("utf-8") + b"\r\n")
                self.sent += 1
            except OSError:
                print("\n[verbinding met de live timing verbroken – opnieuw proberen]")
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


# ------------------------------------------------------------------ simulator-engine
class LaneRun:
    def __init__(self, ln, entry):
        self.ln, self.entry = ln, entry
        self.reset()

    def reset(self):
        self.plan = []          # geplande aantiktijden per lap (s, wedstrijdtijd)
        self.react = None
        self.lap = 0            # aantal laps al getikt
        self.offset = 0.0       # verschuiving na een manuele aantikking
        self.splits = {}        # lap -> {"t": s, "manual": bool}
        self.dns = False
        self.manual_final = False
        self.spurious = None     # (lapindex, tijd): paneel registreert te vroeg (golf, hand van de buur)


class Engine:
    STATES = {"idle": "Geen reeks", "startlist": "Startklaar", "running": "Loopt", "finished": "Gestopt",
              "result": "Uitslag", "done": "Programma afgewerkt"}

    def __init__(self, args):
        self.a = args
        self.rnd = random.Random(args.seed)
        start = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=2)
        self.first_lane = 0 if args.lanes >= 10 else 1           # 10 banen = 0–9
        self.lane_nums = lane_numbers(args.lanes, self.first_lane)
        self.loop_prog = bool(getattr(args, "loop", True))   # na de laatste reeks opnieuw beginnen
        self.prog = Program(self.rnd, args.events, args.lanes, args.per_event, start, self.first_lane)
        self.order = [(ev, h) for ev in self.prog.events for h in ev["heats"]]
        self.lock = threading.RLock()
        self.tx = Sender(args)
        self.tx.on_connect = self.resync
        self.state = "idle"
        self.idx = -1
        self.rt = 0.0
        self.speed = args.speed
        self.paused = False
        self.auto = not args.manual          # wedstrijdverloop automatisch
        self.auto_swim = True                 # zwemmers tikken zelf aan
        self.extra_touch = args.extra_touch_chance > 0
        self.wait_left = 3.0
        self.finish_at = None
        self.lanes = {}
        self.pending = []                     # (wedstrijdtijd, bericht)
        self.log = []
        self.quit = False
        self.layout = None

    # ---- hulp
    @property
    def cur(self):
        return self.order[self.idx] if 0 <= self.idx < len(self.order) else (None, None)

    def laps(self):
        ev, _ = self.cur
        return ev["legs"] * ev["dist"] // 50 if ev else 0

    def note(self, txt):
        self.log.insert(0, f"{datetime.now():%H:%M:%S}  {txt}")
        del self.log[30:]

    def program_msg(self):
        return "#SIMPROGRAM " + json.dumps(self.prog.schedule_json(), ensure_ascii=False, separators=(",", ":"))

    def send_block(self, layout):
        ev, h = self.cur
        self.layout = layout
        for m in Sim_block(self, ev, h, layout):
            self.tx.send(m)

    def resync(self):
        time.sleep(0.2)
        with self.lock:
            self.tx.send(self.program_msg())
            if self.layout and self.cur[0]:
                self.send_block(self.layout)
                for ln, L in self.lanes.items():
                    for lap, s in sorted(L.splits.items()):
                        self.tx.send(self.lane_msg(ln, lap, s["t"]))

    def lane_msg(self, ln, lap, t):
        rank = 1 + sum(1 for o in self.lanes.values() if lap in o.splits and o.splits[lap]["t"] < t)
        return f"Time\t{ln}\tLaneTime\t{int(round(t, 2) * 10000)}\tRank\t{rank}\tLap\t{lap}\tPoints\t{lap * 50:.2f}"

    # ---- bediening (zoals in SwimTime)
    def load(self, i):
        with self.lock:
            if not 0 <= i < len(self.order):
                return
            self.idx = i
            ev, h = self.cur
            self.lanes = {ln: LaneRun(ln, e) for ln, e in h["lanes"].items()}
            self.rt, self.pending, self.finish_at = 0.0, [], None
            self.send_block("Startlist")
            for ln in self.lane_nums:
                self.tx.send(f"Time\t{ln}\tLaneTime\t-1\tRank\t-1\tLap\t0\tPoints\t0.00")
            self.state = "startlist"
            self.wait_left = self.a.pause
            self.note(f"Startlijst geladen: wedstrijd {ev['number']} {ev['discipline']} – {h['name']}")

    def start(self):
        with self.lock:
            if self.state != "startlist":
                return
            rnd = self.rnd
            laps = self.laps()
            for L in self.lanes.values():
                L.reset()
                if rnd.random() < self.a.dns_chance:
                    L.dns = True
                    continue
                L.react = rnd.uniform(0.55, 0.85)
                t, form = L.react, rnd.gauss(1.0, 0.015)
                for lap in range(laps):
                    t += L.entry["pace"][lap] * form * rnd.gauss(1.0, 0.008)
                    L.plan.append(t)
                L.manual_final = rnd.random() < self.a.manual_chance
                if self.extra_touch and rnd.random() < max(self.a.extra_touch_chance, 0.0):
                    k = rnd.randrange(laps)
                    prev = L.plan[k - 1] if k else L.react
                    L.spurious = (k, prev + (L.plan[k] - prev) * rnd.uniform(0.45, 0.9))
                self.pending.append((L.react + 0.3, f"Time\t{L.ln}\tReactionTime\t{int(L.react * 10000)}\tRank\t-1\tLap\t0\tPoints\t0.00"))
            self.rt, self.finish_at = 0.0, None
            self.send_block("LaneOriented")
            self.state = "running"
            self.note("START")

    def touch(self, ln, t=None, manual=False):
        """Aantikken op het paneel (of manueel ingeven) voor de volgende lap van baan ln."""
        with self.lock:
            L = self.lanes.get(ln)
            if not L or self.state not in ("running", "finished") or L.lap >= self.laps():
                return
            lap = L.lap + 1
            t = self.rt if t is None else t
            if L.plan and lap - 1 < len(L.plan):
                L.offset = t - L.plan[lap - 1]
            L.splits[lap] = {"t": t, "manual": manual}
            L.lap = lap
            self.tx.send(self.lane_msg(ln, lap, t))
            if not manual:
                self.note(f"Baan {ln}: aangetikt lap {lap} – {fmt(t * 10000)}")

    def manual(self, ln, seconds, lap=None):
        """Manueel ophogen: tijd komt (laat) binnen, zoals SwimTime bij een ontbrekende paneeltijd."""
        with self.lock:
            L = self.lanes.get(ln)
            if not L or seconds is None:
                return
            if lap is None or lap > L.lap:
                self.touch(ln, seconds, manual=True)
            else:
                L.splits[lap] = {"t": seconds, "manual": True}
                self.tx.send(self.lane_msg(ln, lap, seconds))
            self.note(f"Baan {ln}: manueel {fmt(seconds * 10000)}")

    def clear(self, ln):
        with self.lock:
            L = self.lanes.get(ln)
            if not L:
                return
            self.tx.send(f"Time\t{ln}\tLaneTime\t-1\tRank\t-1\tLap\t0\tPoints\t0.00")
            L.splits.clear()
            L.lap = 0
            L.plan = []           # deze baan zwemt niet meer automatisch verder
            self.note(f"Baan {ln}: tijden gewist")

    def reset(self):
        """Valse start / reset: klok terug, tijden weg, opnieuw startklaar."""
        with self.lock:
            if self.state not in ("running", "finished"):
                return
            for ln, L in self.lanes.items():
                L.reset()
                self.tx.send(f"Time\t{ln}\tLaneTime\t-1\tRank\t-1\tLap\t0\tPoints\t0.00")
            self.rt, self.pending, self.finish_at = 0.0, [], None
            self.state = "startlist"
            self.wait_left = self.a.pause
            self.note("Reset (valse start)")

    def finish(self):
        with self.lock:
            if self.state != "running":
                return
            for t, m in sorted(self.pending):          # nog openstaande manuele tijden meteen versturen
                self.tx.send(m)
            self.pending = []
            self.state = "finished"
            self.wait_left = 3.0
            self.note("Klok gestopt")

    def ranking(self):
        with self.lock:
            if self.state not in ("finished", "running"):
                return
            if self.state == "running":
                self.finish()
            ev, h = self.cur
            self.send_block("RankOriented")
            h["finished"] = True
            self.tx.send(f"#SIMFINISHED {ev['number']} {h['number']}")
            self.state = "result"
            self.wait_left = self.a.result_time
            best = sorted(((L.splits[self.laps()]["t"], ln) for ln, L in self.lanes.items() if self.laps() in L.splits))[:3]
            self.note("Uitslag: " + ", ".join(f"baan {ln} {fmt(t * 10000)}" for t, ln in best))

    def next(self):
        with self.lock:
            if self.idx + 1 >= len(self.order):
                if self.loop_prog and self.auto:
                    self.note("Einde van het programma – de simulatie begint opnieuw")
                    self.new_program()
                    return
                self.state = "done"
                self.note("Einde van het programma")
                return
            self.load(self.idx + 1)

    def prev(self):
        with self.lock:
            self.load(max(0, self.idx - 1))

    def step(self):
        """Volgende logische stap (toets n / automatisch verloop)."""
        with self.lock:
            s = self.state
            if s in ("idle", "result"):
                self.next()
            elif s == "startlist":
                self.start()
            elif s == "running":
                for L in self.lanes.values():     # iedereen meteen laten aantikken volgens plan
                    while L.plan and L.lap < len(L.plan):
                        self.rt = max(self.rt, L.plan[L.lap] + L.offset)
                        self._auto_touch(L)
                self.finish()
            elif s == "finished":
                self.ranking()

    def set_outage(self, on):
        """Uitval van SwimTime/netwerk nabootsen: alles stilleggen; bij herstel de volledige stand opnieuw sturen."""
        with self.lock:
            self.tx.muted = bool(on)
            self.note("UITVAL: er wordt niets meer verzonden" if on else "Uitval voorbij: stand opnieuw verzonden")
        if not on:
            threading.Thread(target=self.resync, daemon=True).start()

    def new_program(self):
        with self.lock:
            self.rnd = random.Random(self.a.seed) if self.a.seed else random.Random()
            start = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=2)
            self.prog = Program(self.rnd, self.a.events, self.a.lanes, self.a.per_event, start, self.first_lane)
            self.order = [(ev, h) for ev in self.prog.events for h in ev["heats"]]
            self.idx, self.state, self.lanes, self.pending, self.rt = -1, "idle", {}, [], 0.0
            self.layout, self.wait_left = None, 3.0
            self.tx.send(self.program_msg())
            self.note(f"Nieuw programma: {len(self.prog.events)} wedstrijden, {len(self.order)} reeksen")

    # ---- tijdsverloop
    def _auto_touch(self, L):
        lap = L.lap + 1
        t = L.plan[L.lap] + L.offset
        if lap == len(L.plan) and L.manual_final:
            # geen paneeltijd: de operator hoogt enkele seconden later manueel op
            L.plan = L.plan[:L.lap]
            self.pending.append((t + self.rnd.uniform(4, 9), ("manual", L.ln, t)))
            return
        was = L.splits.get(lap, {}).get("t") if L.splits.get(lap, {}).get("spurious") else None
        L.splits[lap] = {"t": t, "manual": False, "was": was}
        L.lap = lap
        self.tx.send(self.lane_msg(L.ln, lap, t))
        if was is not None:
            self.note(f"Baan {L.ln}: echte tik {fmt(t * 10000)} vervangt valse tik {fmt(was * 10000)}")

    def tick(self, dt):
        with self.lock:
            if self.paused or self.state == "done":
                return
            if self.state == "running":
                self.rt += dt * self.speed
                for L in self.lanes.values():
                    sp = L.spurious
                    if sp and L.lap == sp[0] and self.rt >= sp[1]:
                        L.spurious = None
                        if self.extra_touch:
                            lap = sp[0] + 1
                            L.splits[lap] = {"t": sp[1], "manual": False, "spurious": True}
                            self.tx.send(self.lane_msg(L.ln, lap, sp[1]))
                            self.note(f"Baan {L.ln}: extra tik op het paneel bij {fmt(sp[1] * 10000)} (te vroeg)")
                if self.auto_swim:
                    for L in self.lanes.values():
                        while L.plan and L.lap < len(L.plan) and self.rt >= L.plan[L.lap] + L.offset + 0.15:
                            self._auto_touch(L)
                due = [p for p in self.pending if p[0] <= self.rt]
                self.pending = [p for p in self.pending if p[0] > self.rt]
                for _, m in sorted(due, key=lambda p: p[0]):
                    if isinstance(m, tuple):
                        _, ln, t = m
                        L = self.lanes[ln]
                        L.splits[L.lap + 1] = {"t": t, "manual": True}
                        L.lap += 1
                        self.tx.send(self.lane_msg(ln, L.lap, t))
                        self.note(f"Baan {ln}: geen paneeltijd – manueel opgehoogd {fmt(t * 10000)}")
                    else:
                        self.tx.send(m)
                laps = self.laps()
                done = all(L.dns or L.lap >= laps for L in self.lanes.values())
                if done and not self.pending:
                    if self.finish_at is None:
                        self.finish_at = self.rt + 2.0
                    elif self.rt >= self.finish_at:
                        self.finish()
            elif self.auto and self.state in ("idle", "startlist", "finished", "result"):
                self.wait_left -= dt * self.speed
                if self.wait_left <= 0:
                    self.step()

    def heartbeat(self):
        while not self.quit:
            s = self.state
            if s == "running":
                self.tx.send(f"Time\t1\tRunningTime\t{int(self.rt * 10000)}\tRank\t-1\tLap\t0\tPoints\t0.00")
            elif s == "startlist":
                self.tx.send("Time\t1\tReady\t0\tRank\t-1\tLap\t0\tPoints\t0.00")
            else:
                self.tx.send("Time\t1\tRunningTime\t-1\tRank\t-1\tLap\t0\tPoints\t0.00")
            time.sleep(0.1)

    def loop(self):
        last = time.time()
        while not self.quit:
            time.sleep(0.02)
            now = time.time()
            self.tick(now - last)
            last = now

    # ---- voor de GUI
    def snapshot(self):
        with self.lock:
            ev, h = self.cur
            laps = self.laps()
            lanes = []
            for ln in self.lane_nums:
                L = self.lanes.get(ln)
                e = L.entry if L else None
                if e and "team" in e:
                    name, club = e["team"], e["club"]
                    members = [f"{m.first} {m.last}" for m in e["members"]]
                elif e:
                    name, club, members = f"{e['swimmer'].last} {e['swimmer'].first}", e["swimmer"].club, []
                else:
                    name, club, members = "", "", []
                splits = [{"lap": lap, "t": round(s["t"], 2), "manual": s["manual"], "spurious": bool(s.get("spurious")),
                           "was": round(s["was"], 2) if s.get("was") is not None else None} for lap, s in sorted(L.splits.items())] if L else []
                final = L.splits.get(laps) if L else None
                rank = None
                if final:
                    rank = 1 + sum(1 for o in self.lanes.values() if laps in o.splits and o.splits[laps]["t"] < final["t"])
                lanes.append({"lane": ln, "name": name, "club": club, "members": members,
                              "entry": round(e["entry"], 2) if e else None,
                              "react": round(L.react, 2) if L and L.react is not None and self.state != "startlist" else None,
                              "splits": splits, "final": round(final["t"], 2) if final else None,
                              "manual": bool(final and final["manual"]), "rank": rank,
                              "dns": bool(L and L.dns), "lap": L.lap if L else 0})
            prog = []
            for i, (pe, ph) in enumerate(self.order):
                prog.append({"i": i, "event": pe["number"], "disc": pe["discipline"], "heat": ph["name"],
                             "n": len(ph["lanes"]), "finished": bool(ph.get("finished")), "start": ph["start"].strftime("%H:%M")})
            return {"meet": self.a.meet_name, "state": self.state, "stateText": self.STATES[self.state],
                    "rt": round(self.rt, 2), "speed": self.speed, "paused": self.paused, "auto": self.auto,
                    "autoSwim": self.auto_swim, "loop": self.loop_prog, "laneNums": self.lane_nums, "extraTouch": self.extra_touch, "outage": self.tx.muted, "wait": round(max(0, self.wait_left), 1) if self.auto else None,
                    "idx": self.idx, "event": ev and {"number": ev["number"], "discipline": ev["discipline"], "laps": laps,
                                                     "distance": ev["legs"] * ev["dist"], "relay": ev["legs"] > 1},
                    "heat": h and {"name": h["name"], "start": h["start"].strftime("%H:%M")},
                    "lanes": lanes, "program": prog, "log": self.log[:12],
                    "link": {"connected": self.tx.sock is not None or bool(self.tx.udp), "sent": self.tx.sent,
                             "target": "ingebouwd in de server" if self.tx.direct else
                             f"udp://{self.tx.udp[0]}:{self.tx.udp[1]}" if self.tx.udp else f"tcp://{self.a.host}:{self.a.port}"}}

    def command(self, c):
        cmd = c.get("cmd")
        ln = c.get("lane")
        with self.lock:
            if cmd == "start": self.start()
            elif cmd == "next": self.next()
            elif cmd == "prev": self.prev()
            elif cmd == "load": self.load(int(c.get("i", 0)))
            elif cmd == "finish": self.finish()
            elif cmd == "ranking": self.ranking()
            elif cmd == "reset": self.reset()
            elif cmd == "step": self.step()
            elif cmd == "touch": self.touch(int(ln))
            elif cmd == "manual": self.manual(int(ln), parse_time(c.get("time")), c.get("lap"))
            elif cmd == "clear": self.clear(int(ln))
            elif cmd == "pause": self.paused = not self.paused
            elif cmd == "auto": self.auto = bool(c.get("on")); self.wait_left = max(self.wait_left, 2.0)
            elif cmd == "autoswim": self.auto_swim = bool(c.get("on"))
            elif cmd == "extratouch": self.extra_touch = bool(c.get("on"))
            elif cmd == "outage": self.set_outage(not self.tx.muted)
            elif cmd == "newprog": self.new_program()
            elif cmd == "loop": self.loop_prog = bool(c.get("on"))
            elif cmd == "speed": self.speed = max(0.25, min(64.0, float(c.get("value", 1))))
            else:
                return False
        return True


def parse_time(v):
    """'1:05.32', '65.32' of '65,32' -> seconden."""
    if v is None:
        return None
    try:
        v = str(v).strip().replace(",", ".")
        if ":" in v:
            m, s = v.split(":", 1)
            return int(m) * 60 + float(s)
        return float(v)
    except ValueError:
        return None


def Sim_block(eng, ev, h, layout):
    """Het volledige blok dat SwimTime bij elke weergavewissel stuurt."""
    a = eng.a
    msgs = [f"Layout\t{layout}", f"Meet\tMeetName\t{a.meet_name}", "Meet\tCity\tSimulatiestad", "Meet\tNation\tBEL",
            f"Meet\tDate\t{alge_date(date.today())}", "Session\tSessionNumber\t1", "Session\tSessionName\tSimulatie",
            f"Event\tEventName\t{ev['number']}", f"Event\tDiscipline\t{ev['discipline']}",
            f"Event\tStartTime\t{tod(ev['start'])}"]
    for r in range(3):
        for k in ("RecordType", "RecordName", "RecordDate", "AthleteLastName", "AthleteFirstName", "AthleteNation",
                  "ClubName", "RecordTime"):
            msgs.append(f"Record{r}\t{k}\t" + ("0" if k == "RecordTime" else str(alge_date(date(2010, 1, 1))) if k == "RecordDate" else ""))
    laps = ev["legs"] * ev["dist"] // 50
    msgs += [f"Heat\tHeatNumber\t{h['number']}", f"Heat\tHeatName\t{h['name']}", "Heat\tDescription\t",
             f"Heat\tStartTime\t{tod(h['start'])}", f"Heat\tDistanceM\t{ev['dist']}", "Heat\tSpeedMS\t0.00",
             f"Heat\tLaps\t{laps}"]
    for ln in eng.lane_nums:
        e = h["lanes"].get(ln)
        f = dict(RelayName="", HorseName="", FirstName="", LastName="", Code="", Birthday="0", Gender="",
                 Nation="", TeamName="", Class="", ClubName="", Sponsor="")
        if e and "team" in e:
            f.update(RelayName=e["team"], TeamName=e["team"], ClubName=e["club"], Nation="BEL")
        elif e:
            s = e["swimmer"]
            f.update(FirstName=s.first, LastName=s.last, Code=s.code, Birthday=str(s.birthday), Gender=s.gender,
                     Nation="BEL", ClubName=s.club)
        for k, v in f.items():
            msgs.append(f"Competitor\t{ln}\t{k}\t{v}")
    return msgs


# ------------------------------------------------------------------ bedieningsvenster (enkel deze pc)
GUI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "simulator.html")


def gui_server(eng, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                return self._send(200, open(GUI_FILE, "rb").read(), "text/html; charset=utf-8")
            if p == "/api/state":
                return self._send(200, json.dumps(eng.snapshot(), ensure_ascii=False).encode("utf-8"))
            self._send(404, b"{}")

        def do_POST(self):
            if self.path.split("?")[0] != "/api/cmd":
                return self._send(404, b"{}")
            try:
                c = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except ValueError:
                return self._send(400, b'{"ok":false}')
            ok = eng.command(c)
            self._send(200 if ok else 400, json.dumps({"ok": ok}).encode())

    for p in range(port, port + 20):               # poort bezet (bv. door een ander programma)? volgende proberen
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"geen vrije poort voor het bedieningsvenster gevonden ({port}–{port + 19})")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return p


def console_keys(eng):
    if not msvcrt:
        return
    while not eng.quit:
        if msvcrt.kbhit():
            k = msvcrt.getwch().lower()
            if k == " ":
                eng.command({"cmd": "pause"})
            elif k == "n":
                eng.command({"cmd": "step"})
            elif k in "+=":
                eng.command({"cmd": "speed", "value": eng.speed * 2})
            elif k in "-_":
                eng.command({"cmd": "speed", "value": eng.speed / 2})
            elif k == "a":
                eng.command({"cmd": "auto", "on": not eng.auto})
            elif k == "q":
                eng.quit = True
        time.sleep(0.03)


def console_status(eng):
    last = ""
    while not eng.quit:
        s = eng.snapshot()
        ev = s["event"]
        txt = (f"[{'auto' if s['auto'] else 'manueel'} x{s['speed']:g}{' PAUZE' if s['paused'] else ''}] {s['stateText']}"
               + (f" · wedstrijd {ev['number']} {ev['discipline']} – {s['heat']['name']}" if ev else "")
               + (f" · {fmt(s['rt'] * 10000)}" if s["state"] == "running" else "")
               + (f" · volgende stap over {s['wait']:.0f}s" if s["wait"] is not None and s["state"] != "running" else "")
               + ("" if s["link"]["connected"] else " · NIET verbonden met de live timing"))
        if txt != last:
            sys.stdout.write("\r" + txt[:118].ljust(118))
            sys.stdout.flush()
            last = txt
        time.sleep(0.25)


def main():
    ap = argparse.ArgumentParser(description="Simuleert een volledige zwemwedstrijd voor de live timing (zonder SwimTime).")
    ap.add_argument("--host", default="127.0.0.1", help="pc waarop livetiming.py draait (standaard deze pc)")
    ap.add_argument("--port", type=int, default=2626, help="TCP-invoerpoort van livetiming.py (feed_port)")
    ap.add_argument("--udp", help="in plaats van TCP: UDP-unicast naar HOST:PORT (bv. 127.0.0.1:26)")
    ap.add_argument("--allow-broadcast", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--speed", type=float, default=1.0, help="snelheid (1 = echte tijd, 4 = vier keer sneller)")
    ap.add_argument("--events", type=int, default=12, help="aantal wedstrijden in het programma")
    ap.add_argument("--per-event", type=int, default=18, help="gemiddeld aantal deelnemers per wedstrijd")
    ap.add_argument("--lanes", type=int, default=8, help="aantal banen (4–10; 10 banen = 0–9)")
    ap.add_argument("--no-loop", dest="loop", action="store_false", help="na de laatste reeks stoppen i.p.v. opnieuw beginnen")
    ap.add_argument("--pause", type=float, default=25, help="seconden tussen startlijst en start (automatisch verloop)")
    ap.add_argument("--result-time", type=float, default=15, help="seconden dat de uitslag blijft staan (automatisch verloop)")
    ap.add_argument("--manual", action="store_true", help="geen automatisch verloop: zelf starten, stoppen, volgende reeks")
    ap.add_argument("--manual-chance", type=float, default=0.06, help="kans dat een eindtijd manueel (laat) binnenkomt")
    ap.add_argument("--extra-touch-chance", type=float, default=0.15, help="kans per zwemmer op een extra (te vroege) tik tijdens de race")
    ap.add_argument("--dns-chance", type=float, default=0.03, help="kans dat een ingeschreven zwemmer niet start")
    ap.add_argument("--seed", type=int, help="vaste seed = telkens hetzelfde programma")
    ap.add_argument("--meet-name", default="SIMULATIE – Testwedstrijd")
    ap.add_argument("--gui-port", type=int, default=8199, help="poort van het bedieningsvenster (enkel deze pc)")
    ap.add_argument("--no-gui", action="store_true", help="geen bedieningsvenster, enkel console")
    ap.add_argument("--no-browser", action="store_true", help="bedieningsvenster niet automatisch openen")
    a = ap.parse_args()

    eng = Engine(a)
    eng.tx.send(eng.program_msg())
    n_heats = len(eng.order)
    print(f"Simulatie: {len(eng.prog.events)} wedstrijden, {n_heats} reeksen, {a.lanes} banen. Alle namen zijn verzonnen.")
    print(f"Stuurt naar de live timing via {'udp://' + a.udp if a.udp else f'tcp://{a.host}:{a.port}'}")
    if not a.no_gui:
        url = f"http://127.0.0.1:{gui_server(eng, a.gui_port)}/"
        print(f"Bedieningsvenster: {url}")
        if not a.no_browser:
            webbrowser.open(url)
    print("Console: spatie = pauze · n = volgende stap · +/- = snelheid · a = automatisch aan/uit · q = stoppen\n")
    threading.Thread(target=eng.heartbeat, daemon=True).start()
    threading.Thread(target=console_keys, args=(eng,), daemon=True).start()
    threading.Thread(target=console_status, args=(eng,), daemon=True).start()
    try:
        eng.loop()
    except KeyboardInterrupt:
        pass
    print("\nGestopt.")


if __name__ == "__main__":
    main()
