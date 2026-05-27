# peers — Multi-Peer-Orchestrierung für LLM-Coding-Agents

## NAME
peers — treibt zwei oder mehr LLM-Coding-Agents (claude, codex, ...)
gegen ein einzelnes Git-Repository, mit Goal-Gates als Stop-Bedingung,
Budget-Caps und einer manipulationssicheren Control-Plane unter
`.peers/`.

## SYNOPSIS
```
peers [-C <dir>] <subkommando> [optionen]
peers --help-man [--de|--en]
peers <subkommando> --help-man [--de|--en]
```

## BESCHREIBUNG
`peers` ist das CLI für eine Schleife kooperierender Coding-Agents.
Im Projekt liegt unter `.peers/` die Konfiguration (`config.yaml`),
die Gate-Definitionen (`goals.yaml`), pro-Peer-State, Run-Log und
optionale Check-Skripte. Pro Tick: nächsten Peer wählen, ihn unter
dem Health-Guard starten (Idle-Timeout + Error-Patterns), nach dem
Commit die Hard-Gates evaluieren, State persistieren, entscheiden ob
weiter.

Zwei CLIs:

- **peers** — arbeitet auf einem Projekt (CWD oder `-C`-Pfad).
- **peers-ctl** — verwaltet mehrere Projekte auf einem Host
  (`peers-ctl --help-man`).

Standardablauf: `peers init` → `.peers/goals.yaml` editieren →
`peers run` → danach `peers verify` und `peers report`.

## OPTIONS
- `-C, --target <dir>` — Zielprojekt statt CWD.
- `--version` — Substrate-Version drucken und beenden.
- `--help-man` — diese Übersicht (oder, nach einem Subkommando, dessen
  Man-Page) drucken.
- `--de` / `--en` — Sprache für `--help-man` erzwingen. Default folgt
  `$LANG`.

## BEISPIELE
```
# Control-Plane im CWD bootstrappen.
peers init

# Audit + Security für ein JS-Projekt.
peers -C ./meine-app init --modes=audit,security --lang=js

# Loop laufen lassen — max 50 Ticks, $5 hart gedeckelt.
peers run --max-ticks 50 --max-usd 5

# Hard-Gates ohne Peer-Beteiligung nochmal prüfen.
peers verify

# Markdown-Rollup.
peers report
```

## DATEIEN
- `.peers/config.yaml` — Peer-Roster, Health, Budget, Comm-Channel.
- `.peers/goals.yaml` — Hard- + Soft-Gates.
- `.peers/state.json` — Turn-Pointer, Budgets, Goal-Status (atomar).
- `.peers/log/runs.jsonl` — Append-Only-Run-Log.
- `.peers/checks/` — Kopien der Mode-Check-Skripte.

## UMGEBUNGSVARIABLEN
- `LANG` — Default-Sprache für `--help-man`.
- `PEERS_PROJECTS_ROOT` — Basisverzeichnis für Kurznamen in peers-ctl
  (default `~/c0de/peers-c0de/`).
- `PODMAN_CMD` — `podman`-Pfad überschreiben (Container-Pfad).
- `PEERS_CTL_PODMAN_NETWORK` — Podman-Netzwerkmodus (`host`, `none`, ...).

## SIEHE AUCH
- `peers init --help-man` und alle weiteren Subkommando-Pages.
- `peers-ctl --help-man` für den Controller-Pfad.
- `docs/HOWTO-audit-and-fix.md` — End-to-End-Audit-Walkthrough.

## NOTES
- Die Substrate erzwingt No-Follow-Safe-I/O auf `.peers/`-Dateien;
  symlink-Manipulationen führen zu „refuse" statt stillem Folgen.
- `peers init` taggt HEAD als `peers-baseline` — jederzeit
  `git reset --hard peers-baseline` als Rollback möglich.
