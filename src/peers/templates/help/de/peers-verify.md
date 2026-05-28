# peers verify — Hard-Gates + verify.commands standalone re-checken

## NAME
peers verify — fährt jedes Hard-Goal (und alle vom User unter
`verify.commands` deklarierten Kommandos) gegen den aktuellen
Repo-Zustand, ohne Peer-Beteiligung. Schreibt `.peers/VERIFY.md`;
Exit 0 nur wenn alles grün.

## SYNOPSIS
```
peers [-C <dir>] verify
```

## BESCHREIBUNG
Idempotenter Post-Loop-/Pre-Handoff-Check: re-evaluiert jede Hard-Goal-
`cmd:` über die gleiche `GoalEngine` wie die Live-Loop, plus jeden
Eintrag unter `verify.commands:` in `config.yaml`. Pro Zeile:
State (`pass`/`fail`/`timeout`) + Diagnostic + Dauer in ms.

Schreibt eine Markdown-Tabelle nach `.peers/VERIFY.md` und echoed
denselben Inhalt auf stdout. Exit 0 nur wenn jedes Hard-Goal UND
jedes Verify-Kommando grün ist.

Nützlich für:
- Abnahme: hat die Loop die Gates wirklich erfüllt, nicht nur
  „peers run sagte success"?
- CI: als finaler Step, fängt Drift.
- Audit: frisches `VERIFY.md` neben dem Loop-`REPORT.md` produzieren.

## OPTIONS
Keine.

## BEISPIELE
```
# Post-Loop-Abnahme.
peers verify

# CI-Integration (Exit-Code nutzen).
peers -C ./meine-app verify || exit 1
```

## DATEIEN
- Liest: `.peers/config.yaml`, `.peers/goals.yaml`.
- Schreibt: `.peers/VERIFY.md`.

## UMGEBUNGSVARIABLEN
Keine direkt; pro-Check-`cmd:`-Strings können Projekt-Env nutzen.

## SIEHE AUCH
- `peers report --help-man` — breiterer Projekt-Rollup.
- `peers info --help-man` — Konfig-Dump.
- `docs/HOWTO-audit-and-fix_DE.md` — Abschnitt „8) Abnahme".

## NOTES
- Soft-Goals werden NICHT re-evaluiert (brauchen Peer-Reviews).
- Verify-Command-Timeouts respektieren `verify.timeout_s` aus
  `config.yaml`, Fallback `goals.timeout_s` (default 120s).
