# Handoff

## Next actions

- Auf Anweisung des Owners: "weiter mit M1" (Goal G-002). Ablauf siehe `.claude/agents/finanzuebersicht-lead.md`.
- M1 Task 1: Stufe `m1` in `scripts/validate.py` implementieren (lint, types, unit, smoke), Python 3.12 per `uv python install 3.12`.
- M3 Task 1: Machbarkeitspruefung F-AUF-07. `init_tan_response` und Codes fuer falsche PIN/Sperre belegen.
- M5: `uv run playwright install chromium`.

## Blockers

- Keine fuer M1. Docker-Engine laeuft nicht, erst fuer M10 noetig.

## Open questions (Owner-Eingaben, SPEC Abschnitt 7)

- FinTS-Produkt-ID (Live-Test FinTS, M3).
- Anonymisierte CSV-Beispiele N26, Easybank, PayPal, Finanzblick-Export in `data/samples/` (M2, M4).
- Bank-Icons bzw. Websites je Bank (M7).
- SMB-Ziel fuer Backups (Live-Test Backup, `docs/live-test-backup.md`, M8).
- Docker-Hub-Repository und `docker login` (M10).
- Bank-Endpunkte (nur ING als Owner-Vorgabe, unbelegt) und Fehlercodes: Quelle oder Live-Test noetig.
