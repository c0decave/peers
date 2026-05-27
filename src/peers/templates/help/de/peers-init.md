# peers init — Control-Plane bootstrappen

## NAME
peers init — initialisiert ein `.peers/`-Verzeichnis im Zielprojekt,
damit die Loop dagegen starten kann.

## SYNOPSIS
```
peers [-C <dir>] init [--force] [--driver {orchestrator,hooks,sessions}]
                      [--install] [--modes <liste>] [--lang <lang>]
```

## BESCHREIBUNG
`peers init` legt frische Control-Plane unter `<target>/.peers/` an:
`config.yaml` (Peer-Roster + Health/Budget), `goals.yaml` (Gates),
leere `log/runs.jsonl`, und `checks/verify_self_review.py`. Die
`.gitignore` des Targets wird um `.peers/` erweitert (falls noch
nicht drin), HEAD wird als `peers-baseline` getaggt (Rollback-Anker),
und ein `goals.sha256`-Snapshot wird geschrieben, damit spätere Ticks
Goal-Mutationen erkennen können.

Mit `--modes=<a,b,c>` werden die genannten Modes VORHER aufgelöst
(Conflict-/Cycle-Detection). Klappt das, wird `goals.yaml` mit dem
Merge-Resultat überschrieben, die Check-Skripte werden nach
`.peers/checks/` kopiert, und `modes-applied.txt` als Audit-Trail
geschrieben.

`--driver=hooks` legt zusätzlich Stop-Hook-Snippets unter
`.peers/hooks/` für claude (`settings.json`) und codex (`config.toml`)
ab. Mit `--install` werden die Snippets direkt in die User-Konfig
gemerged (Backup mit Timestamp).

Refuses: `/` oder `$HOME` als Target, symlink-`.peers/` oder
`-.gitignore`, bestehende `.peers/` ohne `--force`.

## OPTIONS
- `--force` — bestehende `.peers/` überschreiben.
- `--driver {orchestrator,hooks,sessions}` — default `orchestrator`.
  `hooks` legt Stop-Hook-Snippets ab; `sessions` wählt den tmux-
  Sessions-Driver.
- `--install` — (mit `--driver=hooks`) Stop-Hooks direkt in
  `~/.claude/settings.json` + `~/.codex/config.toml` mergen.
- `--modes <liste>` — kommaseparierte Mode-Namen (`audit`, `security`,
  `thorough`, ...). Verfügbare: `peers-ctl modes list`.
- `--lang <lang>` — `python` (default), `js`, `rust`, `go`. Wählt
  die sprach-spezifischen Audit-Checks.
- `--audit-templates` — DEPRECATED-Alias für `--modes=audit`.

## BEISPIELE
```
# Standard-Scaffold im CWD.
peers init

# Audit + Security für ein JS-Projekt.
peers -C ./meine-app init --modes=audit,security --lang=js

# Nach Mode-Wechsel re-initialisieren (überschreibt .peers/).
peers init --force --modes=audit,thorough

# Hook-Driver mit Auto-Install in die Host-Konfig.
peers init --driver=hooks --install
```

## DATEIEN
Angelegt unter `<target>/.peers/`:
- `config.yaml` — Peer-Roster, Comm-Channel, Health, Budget.
- `goals.yaml` — Hard- + Soft-Gates (Merge-Ergebnis bei `--modes`).
- `checks/verify_self_review.py` — Default-`self-review-on-handoff`-Gate.
- `checks/*.py` — Mode-Check-Skripte.
- `log/runs.jsonl` — leer, bereit für Tick 1.
- `goals.sha256` — Anti-Tamper-Snapshot.
- `modes-applied.txt` — Audit-Trail (nur mit `--modes`).
- `hooks/` — Stop-Hook-Snippets (nur mit `--driver=hooks`).

Außerdem:
- `<target>/.gitignore` — `.peers/`-Eintrag wird ergänzt + committet
  (falls nötig).
- Git-Tag `peers-baseline` auf HEAD.

## UMGEBUNGSVARIABLEN
- `PEERS_MODES_DIR` — extra Discovery-Verzeichnis für
  `peers.modes.discover()` (zusätzlich zu Bundled +
  `~/.config/peers/modes/`).
- `HOME` — wird gegen Init in `$HOME` selbst geprüft.

## SIEHE AUCH
- `peers run --help-man`
- `peers-ctl modes list` / `peers-ctl modes show <name>`
- `docs/HOWTO-audit-and-fix.md` — kompletter Audit-Workflow.

## NOTES
- `peers-baseline` wird nur gesetzt, wenn `<target>` ein Git-Repo mit
  mindestens einem Commit ist. Sonst: Notice und weiter — vorher
  `git init && git commit --allow-empty` ist empfohlen.
- Die `.gitignore`-Änderung wird mit Trailer `Peer: peers-init`
  committet, damit `dirty_worktree` auf Tick 0 nicht anschlägt.
