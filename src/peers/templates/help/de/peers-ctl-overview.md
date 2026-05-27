# peers-ctl — Multi-Projekt-Controller für peers-Loops

## NAME
peers-ctl — startet, stoppt, monitort und reportet mehrere peers-Loops
von einem Host aus. Jedes Projekt wird einmal registriert und danach
per Kurzname angesprochen.

## SYNOPSIS
```
peers-ctl [--config-dir <dir>] <subkommando> [optionen]
peers-ctl --help-man [--de|--en]
peers-ctl <subkommando> --help-man [--de|--en]
```

## BESCHREIBUNG
`peers-ctl` ist die Controller-Schicht über der pro-Projekt-`peers`-
Substrate. Hält eine kleine Registry (`projects.json` unter
`$XDG_CONFIG_HOME/peers-ctl/`) mit Name→Pfad-Mapping, spawnt/trackt
`peers run`-Prozesse (Host oder Container), tailt deren Logs, prunet
alte Logs und rendert ein Multi-Projekt-Dashboard.

Standardablauf:

1. `peers-ctl new meine-app --container --modes=audit` — Scaffold + Register.
2. `peers-ctl start meine-app --max-ticks 50 --max-usd 5` — Loop starten.
3. `peers-ctl dashboard` / `peers-ctl tail meine-app` — beobachten.
4. `peers-ctl stop meine-app` — sauber stoppen.
5. `peers-ctl report` — Markdown-Rollup über alle Projekte.

## OPTIONS
- `--config-dir <dir>` — Registry/Log-Location überschreiben (default
  `$XDG_CONFIG_HOME/peers-ctl/`, Fallback `~/.config/peers-ctl/`).
- `--version` — Version drucken und beenden.
- `--help-man` — diese Übersicht (oder, nach einem Subkommando, dessen
  Man-Page) drucken.
- `--de` / `--en` — Sprache für `--help-man` erzwingen.

## BEISPIELE
```
# Voller Audit-Run mit Container-Image.
peers-ctl new meine-app --container --modes=audit --spec ./spec.md
PEERS_CTL_PODMAN_NETWORK=host peers-ctl start meine-app --container --max-ticks 50

# Erst Health-Check.
peers-ctl doctor

# Multi-Projekt-Rollup.
peers-ctl dashboard

# Alles älter als eine Woche aufräumen.
peers-ctl prune --older-than-days 7
```

## DATEIEN
- `$XDG_CONFIG_HOME/peers-ctl/projects.json` — Registry.
- `$XDG_CONFIG_HOME/peers-ctl/logs/<projekt>.log` — pro-Projekt-Log.
- `$PEERS_PROJECTS_ROOT/<name>/` — default-Home für Kurznamen.

## UMGEBUNGSVARIABLEN
- `PEERS_PROJECTS_ROOT` — Basisdir für Kurznamen bei `new`/`add`
  (default `~/c0de/peers-c0de/`).
- `XDG_CONFIG_HOME` — Config-Root-Override.
- `PODMAN_CMD` — `podman`-Pfad überschreiben.
- `PEERS_CTL_PODMAN_NETWORK` — Podman-Netzwerkmodus (`host`, `none`, ...).

## SIEHE AUCH
- `peers --help-man` für pro-Projekt-Kommandos.
- `peers-ctl doctor` für Pre-Flight-Checks.
- `docs/HOWTO-audit-and-fix.md` — End-to-End-Audit-Walkthrough.

## NOTES
- Die Registry ist autoritativ für `peers-ctl`-Operationen; Projekte
  deren Pfade verschwinden werden beim nächsten `list`/`status`-Call
  in den State `missing` reconciliert.
- Container-Pfad (`--container`) braucht auf dem Host nur `podman`
  + ein gebautes `peers:dev`-Image — keine Host-`peers`-Installation.
