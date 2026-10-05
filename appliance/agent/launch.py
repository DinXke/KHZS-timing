#!/usr/bin/env python3
"""Starter van de KHZS-systeemdienst (staat vast in het image, verandert bijna nooit).

Kiest de systeemdienst uit de actieve update (/opt/khzs/current/appliance/agent) zodat ook netwerk-, hotspot- en
routerfuncties met een gewone update vernieuwd worden. Is die versie kapot (compileert niet) of crasht ze drie keer
na elkaar, dan start de reserveversie uit het image (/opt/khzs/agent). Zo blijft het beheer altijd bereikbaar.
"""
import os
import py_compile
import sys

NEW = "/opt/khzs/current/appliance/agent/khzs_agent.py"
FALLBACK = "/opt/khzs/agent/khzs_agent.py"
FAILS = "/var/lib/khzs/agent-fails"


def fails():
    try:
        return int(open(FAILS).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def choose():
    n = fails()
    if os.path.exists(NEW) and n < 3:
        try:
            py_compile.compile(NEW, cfile="/tmp/khzs_agent_check.pyc", doraise=True)
            return NEW, n
        except py_compile.PyCompileError as e:
            print("systeemdienst uit de update compileert niet:", e, flush=True)
    return FALLBACK, n


script, n = choose()
os.makedirs(os.path.dirname(FAILS), exist_ok=True)
with open(FAILS, "w") as f:                 # de dienst zet dit na 60 s stabiel draaien terug op 0
    f.write(str(n + 1 if script == NEW else n))
print(f"systeemdienst: {script}" + (" (reserve)" if script == FALLBACK else ""), flush=True)
os.environ["KHZS_AGENT_SCRIPT"] = script
os.execv(sys.executable, [sys.executable, "-u", script])
