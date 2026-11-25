# Lokale Entwicklung

Kurzanleitung fuer den lokalen Start (N-OPS-01). Voraussetzungen: Python 3.12 und
[uv](https://docs.astral.sh/uv/); fuer das Frontend zusaetzlich Node.js (`frontend/`).

Alle folgenden Befehle laufen im Projektwurzelverzeichnis. Die Entwicklungsdatenbank ist
`data/dev.db`; `DATABASE_URL` wird explizit gesetzt, weil ein relativer SQLite-Pfad sonst
gegen das jeweilige Prozess-Arbeitsverzeichnis aufgeloest wird und schnell eine zweite Datei
entsteht.

## Backend

```powershell
uv sync                              # Abhaengigkeiten installieren
$env:DATABASE_URL = "sqlite:///data/dev.db"
uv run python -m finue demo --reset  # Schema migrieren und Demo-Daten anlegen
uv run python -m finue serve         # Server starten (genau ein Worker, D3)
```

Der Server laeuft danach auf `http://127.0.0.1:8000`; Gesundheitspruefung unter
`http://127.0.0.1:8000/api/health`.

## VS Code

- Task **Demo-Daten** legt den Demo-Bestand an (entspricht `uv run python -m finue demo --reset`).
- F5 startet die Launch-Konfiguration **Backend uvicorn** auf `http://127.0.0.1:8000` gegen
  dieselbe Entwicklungsdatenbank `data/dev.db` (`DATABASE_URL=sqlite:///data/dev.db`).
- Launch und Task laufen beide im Projektwurzelverzeichnis (`cwd = ${workspaceFolder}`) und
  setzen `DATABASE_URL` identisch; so oeffnen F5 und der Task garantiert dieselbe Datei.
  Die Launch-Konfiguration setzt zusaetzlich `PYTHONPATH=${workspaceFolder}/backend`.
