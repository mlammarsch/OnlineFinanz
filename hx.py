#!/usr/bin/env python3
"""Shim: fuehrt die zentrale harness/hx.py aus, mit diesem Ordner als Wurzel (HX_ROOT).

Aufruf wie ueberall im Harness: python hx.py <befehl>
Reihenfolge der Suche nach dem zentralen Harness: Umgebungsvariable HARNESS_HOME,
eingetragener Pfad, dieser Ordner. Erzeugt von scripts/new_project.py.
"""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMBEDDED = r"C:\Users\mlammarsch\OneDrive - Kardex Holding AG\_HarnessProjects\template_GitHub"

for cand in (os.environ.get("HARNESS_HOME"), EMBEDDED, str(HERE)):
    if cand and (Path(cand) / "harness" / "hx.py").is_file():
        central = Path(cand) / "harness" / "hx.py"
        break
else:
    sys.exit("hx: zentrales Harness nicht gefunden (HARNESS_HOME setzen oder Pfad in hx.py korrigieren)")

os.environ.setdefault("HX_ROOT", str(HERE))
sys.argv[0] = str(central)
runpy.run_path(str(central), run_name="__main__")
