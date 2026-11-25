<role>
Du arbeitest als Projekt-Lead `finanzuebersicht-lead` im Projekt "Finanzuebersicht". Das globale Harness
(`~/.claude/harness`) gibt Ablauf, CLI (`python hx.py`), Agents (`planner`, `builder`, `verifier`) und Skills
(`harness-run`, `harness-verify`, `harness-learn`) vor. Das Projekt-Harness ergaenzt es nur um Projektwissen:
Projektverfassung, Spezifikation, Domaenen-Skill und Validierungsskript. Es ersetzt nichts davon.
Du orchestrierst: Du planst ueber `planner`, setzt selbst um oder delegierst klar umrissene Tasks an `builder`,
laesst ueber `verifier` pruefen und schliesst Tasks nur ueber `hx.py task-done` ab.
</role>

<context>
Umgebung (geprueft am 2026-11-25):
- Projektordner: `c:\00_mldata\programming\OnlineFinanz` (aktuelles Verzeichnis). Enthaelt bereits `SPEC.md`,
  `initialPrompt.md` (diese Datei) und `docs/mockup/*.png`. Kein Git-Repo.
- Windows 11, PowerShell 5.1 als Shell, Git, Node 22, `uv`. Python 3.13/3.14 installiert, 3.12 fehlt
  (per `uv python install 3.12`). Docker Desktop installiert, Engine laeuft derzeit nicht. Playwright fehlt.
- Globale Regeln: `~/.claude/harness/AGENTS.md`. CLI-Referenz: `~/.claude/harness/docs/hx.md`.
  Externe Projekte: `~/.claude/harness/docs/external-projects.md`. Lies alle drei vor dem ersten Schritt.

Projekt: Siehe `SPEC.md` (verbindlich, Anforderungs-IDs `F-*`/`N-*`) und die Mockups in `docs/mockup/`.
Owner: Einzelnutzer, hostet selbst. Ziel ist lokales Testen aus VS Code auf localhost und am Ende ein
Docker-Image auf Docker Hub fuer den Betrieb auf einer VM.
</context>

<decisions>
Diese Entscheidungen stehen fest. Uebernimm sie in M0 als Eintraege in `projects/finanzuebersicht/decisions.md`.

D1 Backend: Python 3.12 (`uv`, `pyproject.toml`, `uv.lock`, `.python-version`), FastAPI, SQLAlchemy 2.x,
   Alembic, Pydantic v2. SQLite als Standard, `DATABASE_URL` fuer PostgreSQL/MySQL.
D2 FinTS: python-fints (LGPL). aqbanking und libfintx sind geprueft und verworfen (CLI-Kopplung bzw. .NET).
   Abruf: Saldo ueber `get_balance`, Umsaetze bevorzugt ueber `get_transactions_xml` (camt, liefert gebuchte
   und vorgemerkte Umsaetze getrennt), Fallback `get_transactions(..., include_pending=True)` (MT940).
   Die FinTS-Produkt-ID kommt aus der Konfiguration (`FINTS_PRODUCT_ID`).
   Termin- und Dauerauftraege (F-AUF) nur lesend. python-fints dokumentiert dafuer keinen fertigen Abruf
   (HKCDB/HKCSB): In M3 zuerst pruefen, sonst als lesende Erweiterung im Projekt umsetzen, Vorgehen nach F-AUF-07.
   Schreibende Auftragsfunktionen von python-fints (Ueberweisung, Lastschrift) werden nie aufgerufen.
D3 TAN-Ablauf in der Web-App: Jeder Sync laeuft als Sync-Job. Liefert python-fints eine `NeedTANResponse`,
   friert der Job den Dialog mit `pause_dialog()` ein und sichert den Client mit `deconstruct()` und die
   Anfrage mit `get_data()`. Der Job geht auf `awaiting_tan`. Der Zustand liegt nur im Arbeitsspeicher
   (Job-Registry, Timeout 5 Minuten, danach verworfen). Das Frontend fragt den Jobstatus ab und zeigt die
   Challenge. `POST /api/sync/jobs/{id}/tan` stellt den Client mit `from_data` wieder her, setzt den Dialog mit
   `resume_dialog()` fort und ruft `send_tan()`. Bei App-Freigabe (decoupled) fragt das Backend
   `send_tan` im Intervall ab, bis die Freigabe da ist oder der Timeout greift. Ein TAN-Bedarf schon bei der
   Dialog-Initialisierung (`init_tan_response`) laeuft ueber denselben Weg. Folge: Der Server laeuft mit genau
   einem Worker-Prozess (in README und Dockerfile festhalten). Die exakten API-Signaturen belegst du ueber
   Context7 (`/websites/python-fints_readthedocs_io_en`) und haeltst sie im Domaenen-Skill fest.
D4 Dubletten: Fingerprint plus Multimengen-Abgleich nach `SPEC.md` F-DUP-01 bis F-DUP-05 (Zusammenfuehren als
   Markierung, nie physisches Loeschen).
D5 Suche: portables SQL ueber SQLAlchemy (normalisierte Suchspalte, LIKE, Indizes), keine
   DB-spezifische Volltextsuche (F-SUC-06).
D6 Frontend: React + TypeScript + Vite + Tailwind CSS, TanStack Query. Entwicklung: Vite-Dev-Server mit
   Proxy `/api` -> Uvicorn. Produktion: FastAPI liefert `frontend/dist` aus, ein Image, ein Prozess.
   Design nach SPEC F-DES mit Tailwind CSS v4 (CSS-first, ohne `tailwind.config.js`):
   - Alle Tokens (Farben Light/Dark, Radien, Abstaende, Schriftstufen, Schatten, Touch-Groessen) einmal als
     CSS-Variablen in `@theme` in `frontend/src/styles/tokens.css`. Tailwind erzeugt daraus die Klassen
     (`rounded-card`, `bg-surface-1`, `text-amount` ...). Das ist die eine Quelle nach F-DES-09.
   - Dark-Mode ueber eine Klasse am Wurzelelement (Standard aus `prefers-color-scheme`, manuell umschaltbar, F-UI-02).
   - Touch-Modus als eigene Variante `touch:` ueber `@custom-variant` mit `@media (pointer: coarse)` (F-MOB-01), damit
     Groessen je Komponente lesbar bleiben: `h-row touch:h-row-touch`.
   - Durchsetzung von F-DES-09: `scripts/validate.py` (Stufe `lint`) bricht ab, wenn in `frontend/src` freie Werte statt
     Tokens stehen (Tailwind-Klammerwerte wie `bg-[#...]`, `rounded-[...]`, `p-[...]` sowie Hex-Farben ausserhalb von
     `tokens.css`).
   - Tailwind-Syntax fuer die eingesetzte Version vor dem Einbau ueber Context7 belegen.
   Komponenten auf Basis zugaenglicher Grundbausteine (Radix UI, im Stil von shadcn/ui), Bottom-Sheet
   ueber eine passende Drawer-Komponente, Symbole aus einem Satz (Lucide). Schrift Inter ueber das npm-Paket
   `@fontsource-variable/inter` (wird mitgebaut, kein Abruf von Google). Bibliotheksversionen und Namen vor dem
   Einbau ueber Context7 belegen, Auswahl in `decisions.md` festhalten.
D7 Tests: ruff, mypy (strict fuer `backend/`), pytest, pytest-playwright (Chromium) gegen die lokal
   gestartete App mit Demo-Daten, Viewports Desktop 1440x900 (Maus) und Mobile 390x844 mit `has_touch` und `is_mobile` (Touch-Gesten,
   Messung der Trefferflaechen nach F-MOB-15). Touch-Groessen kommen aus zentralen Design-Tokens, die ueber
   `pointer: coarse` umschalten, nicht aus Einzelwerten pro Komponente. Frontend: `tsc --noEmit`
   und ESLint.
D8 Bankzugriffe in Tests ausschliesslich ueber Mocks der python-fints-Klassen (Dialog-Skripte: Happy-Path,
   TAN, App-Freigabe, ING-90-Tage, Consorsbank 9800/9010, falsche PIN, Saldo ohne Umsaetze, Auftragsbestand
   vorhanden/leer/nicht unterstuetzt). Echte Logins
   fuehrt nur der Owner durch.
D9 Docker: multi-stage Dockerfile (Node-Build -> `python:3.12-slim`), non-root, Volume `/data` fuer DB,
   Schluesseldatei und `/data/backups`, Healthcheck auf `/api/health/ready`, `deploy/docker-compose.yml` fuer die VM. Push nach Docker Hub nur als
   Task mit Risiko `high`.
D10 MCP-Server: offizielles MCP-Python-SDK (Paket `mcp`, Version pinnen), Transport Streamable HTTP, in die
   FastAPI-App unter `/mcp` eingehaengt. Die FastAPI-App muss `session_manager.run()` in ihrem eigenen Lifespan
   starten (der Lifespan einer eingehaengten Unter-App laeuft nicht). Klassen- und Methodennamen unterscheiden
   sich zwischen SDK v1 (`FastMCP`) und v2 (`MCPServer`): vor der Umsetzung ueber Context7
   (`/modelcontextprotocol/python-sdk`) fuer die gepinnte Version belegen und im Domaenen-Skill festhalten.
   Bearer-Token-Pruefung als Middleware vor `/mcp` (N-SEC-06). Die Tools rufen dieselbe Service-Schicht wie die
   REST-API, keine eigene Datenlogik. Tests mit dem MCP-Client des SDK gegen die laufende App.
   Begrenzungen nach F-MCP-11 zentral (Rate-Limit und Gleichzeitigkeit je Token, Zeitraum-, Seiten- und
   Laufzeitgrenzen in der Service-Schicht), nicht pro Tool einzeln.
D11 Backup: Format nach F-BAK-01 (ZIP, Manifest, JSON-Lines-Abzug je Tabelle, bei SQLite zusaetzlich Kopie ueber die
   SQLite-Backup-API). Restore immer aus dem logischen Abzug, danach Alembic-Migration. Verschluesselung mit
   `cryptography` (passwortbasiert abgeleiteter Schluessel, authentifizierte Verschluesselung). SMB ueber `smbprotocol`
   (Modul `smbclient`), kein Mount. Zeitplaner im App-Prozess (APScheduler 3.x oder eigener asyncio-Planer, Entscheidung
   in `decisions.md`), passt zur Vorgabe "ein Worker-Prozess" aus D3. Tests: lokales Ziel echt, SMB ueber Mock der
   Bibliothek; ein Samba-Container fuer einen echten SMB-Test nur in der Stufe `docker`.
D12 Gleichzeitigkeit: Versionsspalte je aenderbarem Datensatz (SQLAlchemy `version_id_col`), 409 mit aktuellem Stand
   bei Konflikt. Kennzeichen-Endpunkte mit Setz-Semantik (F-CON-01). Sperre je Bankzugang fuer Syncs (F-SYN-11).
   Aenderungsbenachrichtigung per Server-Sent Events (`/api/events`), Frontend invalidiert betroffene TanStack-Query-
   Schluessel. SQLite im WAL-Modus mit `busy_timeout` (N-DB-04).
</decisions>

<task>
PHASE 0 = MILESTONE M0: PROJEKT-HARNESS AUFBAUEN. Danach anhalten.

0.1 Geruest ueber das globale Harness erzeugen (ueberschreibt nichts):
    `python "$env:USERPROFILE\.claude\harness\scripts\new_project.py" . --id finanzuebersicht --title "Finanzuebersicht"`
    Ergebnis pruefen: `hx.py`, `AGENTS.md`, `CLAUDE.md`, `.claude/agents/finanzuebersicht-lead.md`,
    `.claude/settings.json`, `projects/finanzuebersicht/`. Danach `git init` und `.gitignore`
    (`.venv/`, `node_modules/`, `frontend/dist/`, `data/`, `*.key`, `.env*`, `test-results/`, Caches).

0.2 Projekt-`AGENTS.md` ausfuellen (der erzeugte Abschnitt "Harness" bleibt). Inhalt, knapp und eindeutig:
    - Ziel, Umfang und Nicht-Ziele mit Verweis auf `SPEC.md`.
    - Stack nach D1 bis D12 und Verzeichnisstruktur: `backend/` (Python-Paket `finue`), `frontend/`,
      `tests/` (`unit/`, `integration/`, `e2e/`, `fixtures/`), `scripts/`, `deploy/`, `docs/`.
    - Abweichungen vom globalen Harness, ausdruecklich benannt:
      a) "Python nur Standardbibliothek" gilt fuer `hx.py`, nicht fuer Projektcode.
      b) Der Owner erteilt hiermit den Auftrag: Nach jedem abgenommenen Milestone wird lokal committet
         (`M<n>: <Titel>`). Kein Push ins Git-Remote, kein `docker push` ohne `task-approve`.
    - Sicherheitsregeln: N-SEC-01 bis N-SEC-06, kein Screen-Scraping, keine Cloud-Dienste, keine echten
      Bankzugriffe durch Agents, keine echten Kontodaten in Tests oder Fixtures.
    - Risikostufen im Projekt: `low` Standard; `medium` fuer Krypto, Auth, Sync-Logik, Dublettenlogik,
      Migrationen, Backup/Restore, Gleichzeitigkeit; `high` fuer Docker-Hub-Push und alles mit echten Zugangsdaten
      (auch echte SMB-Ziele).
    - Review-Checkliste fuer `verifier` (zusaetzlich zur globalen Pruefung): keine Secrets in Code, Logs oder
      Fixtures; kein rohes SQL; F-DUP-Abgleich durch Tests belegt; Fehlerfaelle nach F-SYN-05 mit Klartext;
      keine Reste von Kategorien oder Ueberweisungen; Auftraege nur lesend (kein Aufruf schreibender
      FinTS-Geschaeftsvorfaelle); MCP-Tools nur im Umfang von F-MCP-02 bis F-MCP-04, nichts aus F-MCP-06,
      Token-Pruefung und Schreibprotokoll getestet; neue UI-Elemente im Touch-Modus nach F-MOB-02 bemessen,
      keine Funktion nur per Mouseover; nur Design-Tokens, keine fest eingetragenen Farben, Radien oder Abstaende
      (F-DES-09), keine Anfragen an fremde Server aus dem Frontend; jeder aendernde Endpunkt mit Versionspruefung oder Setz-Semantik
      (F-CON-01/02) und Konflikttest; keine unbegrenzten Abfragen (Seiten-, Zeitraum- und Laufzeitgrenzen);
      Backup ohne Schluesseldatei und ohne Klartext-Passwoerter; Anforderungs-IDs in Tests referenziert.
    - Konventionen: Code-Bezeichner Englisch, UI-Texte, Doku und Kommentare Deutsch, Dateinamen ASCII.

0.3 Projekt-Agent `.claude/agents/finanzuebersicht-lead.md` ergaenzen: Milestone-Ablauf (siehe <loop>),
    Verweis auf `SPEC.md`, Domaenen-Skill und `scripts/validate.py`. Keine weiteren Projekt-Agents anlegen.

0.4 `.claude/settings.json` ergaenzen: Allow fuer `PowerShell(python hx.py *)`, `PowerShell(uv run *)`,
    `PowerShell(uv sync*)`, `PowerShell(npm run *)`, `PowerShell(git add *)`, `PowerShell(git commit *)`,
    zusaetzlich zur vorhandenen Bash-Regel. Deny fuer `git push` und `docker push` in beiden Shells. Bestehende
    Deny-Regeln (`.env`, `*.key`, `*.pem`) bleiben.

0.5 Domaenen-Skill `.claude/skills/finanzuebersicht-fints/` anlegen (nur fuer dieses Projekt):
    - `SKILL.md`: Frontmatter `name: finanzuebersicht-fints` (gleich Ordnername), `description` einzeilig,
      hoechstens 1024 Zeichen, mit Ausloesern (FinTS, TAN, Sync, Import/Export CSV/JSON/Markdown, Dubletten, Bankfehler).
      Inhalt: wann welche Referenz zu lesen ist.
    - `references/fints.md`: PIN/TAN, Dialog-Initialisierung, TAN-Verfahren, Saldo- vs. Umsatzabruf,
      camt vs. MT940, vorgemerkte Umsaetze, Pause/Resume nach D3, Produktregistrierung. API-Signaturen
      mit Quelle aus Context7.
    - `references/banken.md`: die 7 Banken nach SPEC Abschnitt 3, Fehlercode-Mapping (Code -> Klartext ->
      Aktion). Jede Angabe ist als "belegt (Quelle)" oder "Owner-Vorgabe, unbelegt" gekennzeichnet.
      Endpunkte nur mit Quelle.
    - `references/csv.md`: Kodierung, Trennzeichen, Dezimal- und Datumsformate, Mapping-Mechanik,
      Vorlagen-Format, PayPal-Hilfszeilen, Export-Profile.
    - `references/dubletten.md`: Normalisierung, Fingerprint, Multimengen-Abgleich, Verdachtsdubletten,
      Vormerkungen, mit Beispielen inkl. zwei gleicher Buchungen am selben Tag.

0.6 `scripts/validate.py` anlegen: deterministisches Pruefskript, Standardbibliothek, Aufruf
    `python scripts/validate.py <stufe>`, Exit 0 nur wenn alle Pruefungen der Stufe bestehen. Ergebnis als
    lesbarer Bericht auf stdout. Stufen sind kumulativ (m3 prueft auch m1 und m2):
    - `m0`: Pflichtdateien vorhanden; Skill-Frontmatter gueltig (Regeln aus 0.5); `AGENTS.md` enthaelt die
      Pflichtabschnitte aus 0.2; Git-Repo vorhanden; `python hx.py status finanzuebersicht` Exit 0.
    - `m1` ff.: werden mit dem jeweiligen Milestone implementiert. Bis dahin endet eine nicht umgesetzte
      Stufe mit Exit 1 und der Meldung "Stufe nicht implementiert". Sie darf nie stillschweigend bestehen.
    - Spaeter geplante Bausteine: `lint` (ruff, eslint), `types` (mypy, tsc), `unit`, `integration`
      (FinTS-Mocks), `roundtrip` (CSV und JSON: Export -> Re-Import ergibt 0 neue Buchungen, JSON verlustfrei; Markdown-Ausgabe
      gegen Referenzdatei), `smoke` (App mit leerer DB, `/api/health`), `perf` (N-PERF-01),
      `e2e` (pytest-playwright, Screenshots nach `test-results/`; enthaelt die Design-Pruefung nach F-DES-11 mit
      axe-core in Light und Dark), `mcp` (MCP-Client gegen laufende App:
      Tool-Liste, Lese- und Schreib-Tools, 401 ohne Token, verbotene Tools fehlen, 429 bei Ueberschreitung),
      `concurrency` (409 bei veralteter Version, Setz-Semantik, zweiter Sync-Anstoss liefert laufenden Job, Ereignis
      per SSE in zweiter Sitzung), `backup` (Backup -> Restore ergibt identischen Datenstand, verschluesselt und
      unverschluesselt, Restore eines Backups im Format der Vorversion, Abbruch bei neuerer Version, Sicherheits-Backup
      vor Restore, Aufbewahrung, SMB per Mock), `docker` (Build, Start, Healthcheck, e2e gegen Container, SMB-Backup
      gegen Samba-Container).

0.7 Milestones im Harness anlegen: pro Milestone ein Goal mit `hx.py goal-add` (M0 bis M10, siehe <milestones>).
    `projects/finanzuebersicht/plan.md` enthaelt die Milestone-Tabelle (Milestone, Goal-ID, Abnahme-Stufe,
    Anforderungs-IDs). Tasks legst du nur fuer M0 an. Spaetere Milestones zerlegt `planner` erst beim Start.
    M0-Tasks mit `--verify "python scripts/validate.py m0"` bzw. engeren Pruefbefehlen.

0.8 M0 abschliessen: alle M0-Tasks ueber `hx.py task-done`, Pruefung durch `verifier` (Skill
    `harness-verify`), `hx.py status finanzuebersicht`, Skill `harness-learn`, `handoff.md` mit naechstem
    Schritt und offenen Owner-Eingaben (SPEC Abschnitt 7), Commit `M0: Projekt-Harness`. Dann anhalten.
</task>

<milestones>
| M | Inhalt | Anforderungen | Abnahme |
|---|---|---|---|
| M0 | Projekt-Harness: Geruest, AGENTS.md, Skill, validate.py, Goals, Decisions | - | `validate.py m0` |
| M1 | Fundament: Python 3.12 via uv, FastAPI-Skelett, Konfiguration, Datenmodell mit Versionsspalten (Bankzugang, Bank-Stammdaten inkl. Website, Konto mit Icon, Gruppe, Buchung inkl. Status/Notiz/geprueft/abgestimmt mit Zeitpunkt und Herkunft/externe Referenz/Fingerprint/Index/zusammengefuehrt, Dubletten-Entscheidung, Auftrag, Import-Lauf mit Status, Fehler-Mapping, Import-Vorlage, Sitzung, MCP-Token, Backup-Job und -Lauf, Aenderungsprotokoll), Alembic, SQLite WAL, `/api/health` und `/api/health/ready`, JSON-Logs, Demo-Daten-Befehl, VS-Code-Launch fuer Backend | N-DB-*, N-OPS-01, N-OPS-04..06 | `validate.py m1` (lint, types, unit, smoke) |
| M2 | Import-Kern und Datei-Import: Normalisierung, Fingerprint, Multimengen-Abgleich, Verdachtsdubletten, Import-Lauf-Protokoll, CSV-Erkennung, JSON-Import (eigenes Format nach F-EXP-04 und fremdes JSON per Mapping), Vorlagen, Uebernahme von Kennzeichen, Vorschau-API; JSON-Schema und `docs/export-format.md`, Versionskonvertierung mit Test-Datei je Version | F-DUP-01..04, F-IMP-*, F-EXP-04, F-EXP-07 | `validate.py m2` (+ roundtrip Import-Teil) |
| M3 | FinTS-Sync. Erster Task: Machbarkeitspruefung F-AUF-07 mit Ergebnis in `decisions.md` und ggf. Markierung "zurueckgestellt" in SPEC (Owner-Freigabe). Dann: Bankzugang, PIN-Verschluesselung, Sync-Jobs, Sperre je Bankzugang, TAN-Ablauf nach D3, Abbruch und Zeitueberschreitung, Batch- und Einzelsync, Retry nach Mapping, Fehler-Mapping-Daten, Vormerkungen, Auftragsabruf je nach Pruefergebnis, Mock-Tests nach D8, Checkliste `docs/live-test-fints.md` fuer den Owner | F-SYN-*, F-AUF-04/05/07, F-CON-05, N-SEC-01..03 | `validate.py m3` (+ integration) |
| M4 | Service-Schicht und REST-API, Suche und Export (CSV, JSON, Markdown): Konten/Gruppen-CRUD, Archiv, Loeschen, Icon-Upload, Buchungsliste mit Tagessalden, Kennzeichen mit Setz-Semantik einzeln und Batch (Ergebnis je Buchung), optimistisches Sperren, Dubletten zusammenfuehren/ignorieren/rueckgaengig, Abstimmungsuebersicht, Suche mit Zeitraum, Filtern und Sortierung, Export-Profile, Roundtrip, SSE-Ereignisse, Sitzungsverwaltung | F-KTO-01..07,10,11, F-BUC-*, F-DUP-05, F-ABS-*, F-SUC-*, F-EXP-*, F-CON-*, N-PERF-01 | `validate.py m4` (+ roundtrip, perf, concurrency) |
| M5 | Frontend-Grundlayout: drei Spalten nach Mockup, Kontenliste mit Gruppen und Gesamtsaldo, Buchungsliste (Auftragsbereich nur bei positiver Pruefung F-AUF-07), Detail Einzelbuchung, Kennzeichen per Klick in der Zeile, Leerzustaende und Startseite, Live-Aktualisierung per SSE, Dark/Light, VS-Code-Launch fuer Frontend. Zuerst das Design-System: Tokens, Schrift, Radien, Grundkomponenten, Seite `/design`, `docs/design.md`; danach die Bildschirme. Von Beginn an Touch-faehig: Design-Tokens fuer Touch-Modus, Stapel-Navigation mit Verlauf, Bottom-Sheet-Komponente, sichere Bereiche, Trefferflaechen-Test | F-DES-*, F-UI-01..07, F-BUC-*, F-AUF-01..03 (bedingt), F-DET-01, F-MOB-01..06, F-MOB-09, F-MOB-12, F-MOB-15 | `validate.py m5` (+ e2e Desktop und Mobile) |
| M6 | Frontend-Interaktion: Auswahl und Summe, Batch-Aktionen inkl. abgestimmt, Abstimmungsanzeige im Kontokopf, Suche mit Zeitraum, Filtern und Sortierung, Sync-UI mit TAN-Dialog in allen Sitzungen, Abbruch, Import-Assistent (CSV/JSON), Export-Dialog (CSV/JSON/Markdown), Markdown in Zwischenablage, Konfliktdialog bei 409 | F-SEL-*, F-DET-02, F-ABS-02..04, F-SUC-*, F-SYN-04, F-SYN-10, F-IMP-01, F-EXP-01..05, F-CON-02..04 | `validate.py m6` |
| M7 | Kontenverwaltung und Feinschliff: Anlage-Assistent, Gruppen bearbeiten, Archivieren, Loeschen mit Export-Dialog, Icons (Upload, Web-Abruf auf Aktion, Initialen), Bank-Stammdaten, Fehler-Mapping-Pflege, Badge, Verdachtsdubletten-UI (zusammenfuehren, ignorieren, rueckgaengig), Sitzungsverwaltung, Statusseite, kontouebergreifende Auftragsuebersicht (bedingt), Touch-Feinschliff (Auswahlmodus per langem Druck, Wisch-Aktionen, Herunterziehen zum Aktualisieren, passende Bildschirmtastaturen, Vollbild-Dialoge, Querformat, Web-App-Manifest) | F-KTO-*, F-DUP-03, F-DUP-05, F-SYN-09, F-AUF-06, F-UI-03, F-UI-08, F-CON-06, F-MOB-07, F-MOB-08, F-MOB-10, F-MOB-11, F-MOB-13, F-MOB-14 | `validate.py m7` (+ e2e Mobile inkl. Gesten) |
| M8 | Backup und Restore nach D11: Backup-Format, Verschluesselung, manuelles Backup mit Download, Restore-Ablauf mit Sicherheits-Backup und Wartungsmodus, Backup-Jobs (lokal, SMB), Zeitplaner mit Nachholen, Aufbewahrung, Verbindungstest, Protokoll, Backup vor Migration, UI unter Einstellungen -> Backup | F-BAK-*, F-UI-05 (Restore-Einstieg), N-DB-03 | `validate.py m8` (+ backup) |
| M9 | MCP-Server nach D10: Lese-Tools, Berichte, Schreib-Tools, Backup-Tools, Sync-Anstoss ohne TAN-Annahme, Begrenzungen, Token-Verwaltung (API und UI), Schreibprotokoll, Abschalter, Anleitung `docs/mcp.md` mit Client-Konfiguration | F-MCP-*, N-SEC-06 | `validate.py m9` (+ mcp) |
| M10 | Sicherheit und Betrieb: Anmeldung mit Fehlversuchsbremse, Log-Filter-Test (inkl. Tokens und Passwoerter), Dockerfile, Compose, README inkl. Backup-Konzept, Reverse Proxy und HTTPS fuer `/mcp`; Push nach Docker Hub als separater `high`-Task | N-SEC-*, N-OPS-02..03 | `validate.py m10` (+ docker, mcp gegen Container, SMB gegen Samba-Container) |
</milestones>

<loop>
Ablauf je Milestone ab M1, erst auf ausdrueckliche Anweisung des Owners ("weiter mit M<n>"):
1. `harness-run` fuer das Goal des Milestones. `planner` zerlegt in Tasks mit DoD, Risiko, Anforderungs-IDs
   und engem `--verify`-Befehl. Der letzte Task "M<n> Abnahme" haengt von allen ab und prueft mit
   `python scripts/validate.py m<n>`. Die Stufe `m<n>` wird im ersten Task des Milestones implementiert.
2. Umsetzung je Task durch dich oder `builder`, Abschluss nur ueber `hx.py task-done`.
3. Exit 2 bei `task-done`: Lies die Evidenz unter `projects/finanzuebersicht/artifacts/`. Du gibst den Fehler
   samt Traceback bzw. Playwright-Screenshot gezielt an den zustaendigen Ausfuehrenden zurueck oder behebst ihn
   selbst. Subagents rufen sich nicht gegenseitig auf; die Rueckgabe steuerst du. Gleicher Fehler zweimal:
   `hx.py learn --gap <klasse>` und Guardrail oder Test ergaenzen.
4. Tasks mit Risiko `medium`/`high` und die Abnahme prueft `verifier` mit der Review-Checkliste aus `AGENTS.md`.
5. Abnahme gruen: `harness-learn`, `hx.py status`, `handoff.md`, Commit `M<n>: <Titel>`, Statusmeldung, anhalten.
Reihenfolge und Status stehen ausschliesslich im Harness (Goals, Tasks, `plan.md`, `status.md`). Es gibt
keine zweite Planungsdatei. Tasks eines Milestones greifen nicht auf Anforderungen spaeterer Milestones vor.
</loop>

<constraints>
- Nicht alles in einem Durchlauf: nach M0 anhalten, danach je Milestone auf Anweisung.
- Keine echten Bankzugriffe, keine automatisierten Logins, keine echten Zugangs- oder Kontodaten in Repo, Tests,
  Logs oder Evidenz. Mocks werden nie durch echte Zugriffe ersetzt.
- Keine Datei ausserhalb des Projektordners aendern, insbesondere nichts im globalen Harness. Verbesserungen
  am globalen Harness als Eintrag in die Improve-Queue (`hx.py learn --improve`).
- Pfade mit Leerzeichen quoten. Verifikationsbefehle ohne verschachteltes Quoting, lieber Skriptaufruf.
- Dateien mit `encoding="utf-8"` schreiben, in PowerShell `-Encoding UTF8`.
</constraints>

<edge_cases>
- M3: Echter Banksync ist nicht automatisch pruefbar. M3 ist abgenommen, wenn alle Mock-Tests gruen sind und
  `docs/live-test-fints.md` (Schritt-fuer-Schritt-Pruefung je Bank mit erwartetem Ergebnis) existiert und in
  `handoff.md` als Owner-Aufgabe steht.
- Reicht python-fints fuer eine Bankbesonderheit nicht (z. B. Sparkassen-Darlehen ohne Umsaetze): Konto nur mit
  Saldo unterstuetzen, `hx.py learn --gap` mit Vermerk "offen", weiterarbeiten.
- Fehlende Owner-Eingaben (SPEC Abschnitt 7) blockieren nicht: generische Loesung bauen (z. B. Export-Profil
  ohne Finanzblick-Vorlage, Initialen statt Bank-Icon), offenen Punkt in `handoff.md` fuehren.
- Docker-Engine laeuft nicht: Stufe `docker` meldet das klar und scheitert. M10-Abnahme erst, wenn der Owner
  Docker Desktop gestartet hat. M1 bis M9 brauchen kein Docker.
- Machbarkeitspruefung F-AUF-07 negativ: Ergebnis in `decisions.md`, Vorschlag zur Markierung "zurueckgestellt"
  in `SPEC.md` dem Owner vorlegen (SPEC-Aenderung nur mit Freigabe), F-AUF-05 umsetzen, weiterarbeiten.
- Echtes SMB-Ziel: wird nie von Agents eingetragen oder getestet. Live-Test des Backup-Jobs als Owner-Aufgabe in
  `handoff.md` mit Checkliste `docs/live-test-backup.md`.
- Playwright-Browser fehlt: in M5 per `uv run playwright install chromium` installieren, nicht ueberspringen.
</edge_cases>

<output_format>
- Nach M0: angelegte Dateien als Liste, Bericht von `validate.py m0`, Pfad der Evidenz, offene Owner-Eingaben,
  Architektur in hoechstens 10 Zeilen. Dann anhalten.
- Nach jedem weiteren Milestone, kurz: was gebaut wurde, was mit welcher Stufe verifiziert wurde (Evidenzpfad),
  Eintraege in `FAILURE.md`, Commit-Hash, naechster Milestone.
</output_format>
