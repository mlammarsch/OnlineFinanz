# Finanzuebersicht

## Projektbeschreibung

### Ziel, Umfang, Nicht-Ziele

Self-hosted, responsive Web-App fuer einen Nutzer (Owner, hostet selbst): Salden und Umsaetze aller eigenen Bankkonten
an einem Ort, Vorbild Buhl Finanzblick, bewusst reduziert. Verbindlich ist `SPEC.md` (Anforderungs-IDs `F-*`/`N-*`),
Layout nach `docs/mockup/`. Aenderungen an `SPEC.md` nur mit Freigabe des Owners.

Im Umfang: Salden, Umsaetze, Suche, Auswahl mit Summe, Notiz, geprueft/abgestimmt, Import (CSV, JSON), Export (CSV, JSON,
Markdown), Konten und Gruppen, FinTS-Sync nur lesend, Anzeige von Termin- und Dauerauftraegen (bedingt, F-AUF-07),
MCP-Server, Backup und Restore, gleichzeitige Sitzungen.

Nicht-Ziele (auch keine Reste in Code oder UI): Kategorien, Steuer-Kategorien, Schlagworte, Budgets, Ueberweisungen,
Aendern von Auftraegen, direkte Actual-Budget-Schnittstelle, Mehrbenutzer, Cloud-Dienste, Telemetrie, Screen-Scraping,
PSD2-/TPP-Zertifikate, automatisierte Logins ausser FinTS auf Nutzeraktion.

Ziel: lokales Testen aus VS Code auf localhost, am Ende ein Docker-Image auf Docker Hub fuer den Betrieb auf einer VM.

### Stack (Entscheidungen D1 bis D12, Details in `projects/finanzuebersicht/decisions.md`)

- D1 Backend: Python 3.12 (`uv`, `pyproject.toml`, `uv.lock`, `.python-version`), FastAPI, SQLAlchemy 2.x, Alembic,
  Pydantic v2. SQLite Standard, `DATABASE_URL` fuer PostgreSQL/MySQL.
- D2 FinTS: python-fints. Saldo `get_balance`, Umsaetze bevorzugt `get_transactions_xml` (camt), Fallback MT940 mit
  `include_pending=True`. Produkt-ID aus `FINTS_PRODUCT_ID`. Auftraege nur lesend, schreibende Geschaeftsvorfaelle nie.
- D3 TAN-Ablauf: Sync-Jobs, Dialog mit `pause_dialog()`/`deconstruct()` eingefroren, Zustand nur im Arbeitsspeicher
  (Timeout 5 Min). **Der Server laeuft mit genau einem Worker-Prozess** (auch in README und Dockerfile).
- D4 Dubletten: Fingerprint plus Multimengen-Abgleich (F-DUP-01..05), Zusammenfuehren nur als Markierung.
- D5 Suche: portables SQL ueber SQLAlchemy (normalisierte Suchspalte, LIKE, Indizes).
- D6 Frontend: React, TypeScript, Vite, Tailwind CSS v4 (CSS-first, Tokens in `frontend/src/styles/tokens.css`),
  TanStack Query, Radix UI, Lucide, Inter ueber `@fontsource-variable/inter`. Touch-Variante `touch:` ueber
  `pointer: coarse`. Dev: Vite-Proxy `/api` -> Uvicorn. Produktion: FastAPI liefert `frontend/dist` aus.
- D7 Tests: ruff, mypy (strict fuer `backend/`), pytest, pytest-playwright (Chromium; Desktop 1440x900, Mobile 390x844
  mit `has_touch`/`is_mobile`), `tsc --noEmit`, ESLint.
- D8 Bankzugriffe in Tests nur ueber Mocks der python-fints-Klassen.
- D9 Docker: multi-stage, non-root, Volume `/data`, Healthcheck `/api/health/ready`, `deploy/docker-compose.yml`.
- D10 MCP: Paket `mcp` (Version pinnen), Streamable HTTP unter `/mcp`, Bearer-Token-Middleware, gleiche Service-Schicht
  wie REST, Begrenzungen zentral (F-MCP-11).
- D11 Backup: ZIP mit Manifest und JSON-Lines, SQLite-Backup-API, `cryptography`, SMB ueber `smbprotocol`, Zeitplaner im
  App-Prozess.
- D12 Gleichzeitigkeit: Versionsspalte (`version_id_col`), 409 mit Stand, Setz-Semantik, Sync-Sperre je Bankzugang, SSE
  `/api/events`, SQLite WAL mit `busy_timeout`.

### Verzeichnisstruktur

| Pfad | Inhalt |
|---|---|
| `backend/` | Python-Paket `finue` |
| `frontend/` | React-App |
| `tests/` | `unit/`, `integration/`, `e2e/`, `fixtures/` |
| `scripts/` | `validate.py` und weitere Pruefskripte |
| `deploy/` | Compose-Datei fuer die VM |
| `docs/` | Doku, `mockup/`, `design.md`, `export-format.md`, `schemas/` |
| `projects/finanzuebersicht/` | Harness: Goals, Tasks, Memory, Handoff |
| `.claude/skills/finanzuebersicht-fints/` | Domaenen-Skill (FinTS, Banken, CSV, Dubletten) |

### Abweichungen vom globalen Harness

a) "Python nur Standardbibliothek" gilt fuer `hx.py` und `scripts/validate.py`, nicht fuer Projektcode.
b) Der Owner erteilt den Auftrag: Nach jedem abgenommenen Milestone wird lokal committet (`M<n>: <Titel>`). Kein Push ins
   Git-Remote, kein `docker push` ohne `task-approve`.

### Sicherheitsregeln

- N-SEC-01 PINs nur Fernet-verschluesselt, Schluesseldatei per Umgebungsvariable, nie in DB, Repo oder Image.
- N-SEC-02 Keine PIN, TAN, Schluessel, MCP-Tokens, Backup-/SMB-Passwoerter oder Session-Daten in Logs, Fehlern, Protokollen.
- N-SEC-03 Pausierte FinTS-Dialogdaten nur im Arbeitsspeicher.
- N-SEC-04 Anmeldung mit Argon2, Session-Cookie HttpOnly/Secure/SameSite, fortschreitende Wartezeit nach Fehlversuchen.
- N-SEC-05 Ausgehend nur zu konfigurierten FinTS-Endpunkten, Backup-Zielen und (auf Aktion) Bank-Website fuer Icons.
- N-SEC-06 MCP nur mit Bearer-Token (Hash in der DB, mind. 256 Bit, widerrufbar), sonst 401.
- Kein Screen-Scraping, keine Cloud-Dienste, keine echten Bankzugriffe durch Agents, keine echten Kontodaten in Tests
  oder Fixtures. Echte Logins fuehrt nur der Owner durch.

### Risikostufen

- `low`: Standard.
- `medium`: Krypto, Auth, Sync-Logik, Dublettenlogik, Migrationen, Backup/Restore, Gleichzeitigkeit.
- `high`: Docker-Hub-Push und alles mit echten Zugangsdaten (auch echte SMB-Ziele). Nur nach `task-approve`.

### Review-Checkliste fuer `verifier` (zusaetzlich zur globalen Pruefung)

- Keine Secrets in Code, Logs oder Fixtures.
- Kein rohes SQL (N-DB-01).
- F-DUP-Abgleich durch Tests belegt.
- Fehlerfaelle nach F-SYN-05 mit Klartext.
- Keine Reste von Kategorien oder Ueberweisungen.
- Auftraege nur lesend: kein Aufruf schreibender FinTS-Geschaeftsvorfaelle.
- MCP-Tools nur im Umfang F-MCP-02 bis F-MCP-04, nichts aus F-MCP-06; Token-Pruefung und Schreibprotokoll getestet.
- Neue UI-Elemente im Touch-Modus nach F-MOB-02 bemessen, keine Funktion nur per Mouseover.
- Nur Design-Tokens, keine fest eingetragenen Farben, Radien oder Abstaende (F-DES-09); keine Anfragen an fremde Server
  aus dem Frontend.
- Jeder aendernde Endpunkt mit Versionspruefung oder Setz-Semantik (F-CON-01/02) und Konflikttest.
- Keine unbegrenzten Abfragen (Seiten-, Zeitraum- und Laufzeitgrenzen).
- Backup ohne Schluesseldatei und ohne Klartext-Passwoerter.
- Anforderungs-IDs in Tests referenziert.

### Konventionen

Code-Bezeichner Englisch. UI-Texte, Doku und Kommentare Deutsch. Dateinamen ASCII, ohne Leerzeichen.
Dateien mit `encoding="utf-8"` schreiben, in PowerShell `-Encoding UTF8`. Verifikationsbefehle ohne verschachteltes
Quoting, lieber Skriptaufruf (`python scripts/validate.py <stufe>`).

## Harness

- Zentrales Harness: `C:/Users/mlammarsch/OneDrive - Kardex Holding AG/_HarnessProjects/template_GitHub` (Regeln dort in `AGENTS.md`, Referenz `docs/hx.md`).
- Task-Graph und Memory: `projects/finanzuebersicht/` in diesem Ordner. CLI: `python hx.py <befehl>` (Projekt-ID `finanzuebersicht`).
- Skills (`harness-run`, `harness-verify`, `harness-learn`) und Agents (`builder`, `planner`, `verifier`) sind global installiert
  (`python C:/Users/mlammarsch/OneDrive - Kardex Holding AG/_HarnessProjects/template_GitHub/scripts/install_global.py`).
- Projekt-Agent: `finanzuebersicht-lead` kennt dieses Projekt und steuert den Ablauf.

### Kilo Code (Claude Code: diesen Abschnitt ignorieren, die Regeln kommen ueber `CLAUDE.md`)

Kilo loest den `@`-Import in `CLAUDE.md` nicht auf. Verbindliche Kurzfassung der zentralen Harness-Regeln
(Volltext: `~/.claude/harness/AGENTS.md`, CLI-Referenz `~/.claude/harness/docs/hx.md`):

- Zuerst lesen: `projects/finanzuebersicht/status.md`, `queues.md`, `handoff.md`. Tasks nur ueber `python hx.py`
  (Projekt-ID `finanzuebersicht`), Task-Dateien nie von Hand aendern. Abschluss nur ueber `hx.py task-done`, die
  Selbstauskunft des Agenten zaehlt nicht.
- Jeder nicht-triviale Task hat Definition of Done und Verifikationsplan. Gleicher Fehler zweimal: Guardrail oder Test
  ergaenzen. Fehlschlaege per `hx.py learn --gap <klasse>`, Entscheidungen nach `decisions.md`.
- Jeder Lauf endet mit aktualisierten Dateien, Evidenz, naechsten Schritten in `handoff.md` und einem Eintrag in der
  Improve-Queue.
- Einzelagent als Standard. Subagents (`builder`, `planner`, `verifier`, Lead in `.kilo/agents/`) nur fuer Parallelitaet,
  getrennte Pruefung oder isolierten Kontext. Mittlere und hohe Risiken prueft immer `verifier`.
- Risiko `high` nur nach `task-approve`. Keine Secrets lesen, ausgeben oder committen. Keine Commits ausser dem
  Milestone-Commit nach Abnahme (Abweichung b), keine Pushes. Vorhandene Dateien nicht ohne Diff ueberschreiben.
- Kommunikation: Deutsch, praezise, erster Satz Kernaussage, keine Floskeln, keine Schlussfrage, keine ungefragten Angebote.
- Skills `harness-run`, `harness-verify`, `harness-learn` (Kilo laedt sie aus `~/.claude/skills`, Einstellung
  "Claude Code Compatibility" aktiv) und Domaenen-Skill `.claude/skills/finanzuebersicht-fints/`.
- Agents-Pflege: `builder`, `planner`, `verifier` in `.kilo/agents/` sind Kopien aus `~/.claude/harness/.kilo/agents/`
  (bei Harness-Updates neu kopieren). Der Lead existiert doppelt (`.claude/agents/` und `.kilo/agents/`), beide angleichen.
- Windows: PowerShell 5.1 liest UTF-8 ohne BOM als ANSI, immer `-Encoding UTF8`.
