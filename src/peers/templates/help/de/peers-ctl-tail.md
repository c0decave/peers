# peers-ctl tail — Projekt-Controller-Log live mitlesen

## NAME
peers-ctl tail — tail-and-follow auf das Controller-Logfile eines
Projekts. Druckt erst den vorhandenen Inhalt, streamt dann neue
Zeilen. Wie `tail -f`, mit Safe-I/O-Schutz.

## SYNOPSIS
```
peers-ctl tail <name>
```

## BESCHREIBUNG
Löst den Log-Pfad für `<name>` über die Registry auf, öffnet ihn mit
No-Symlink-Follow-Schutz, druckt die letzten 20 Zeilen, dann loop:
neue Daten lesen, drucken, 0.5s schlafen, wiederholen. Ctrl-C bricht
ab (Exit 0).

Das ist der Controller-seitige stdout/stderr-Stream des `peers run`-
Prozesses — Peer-Subprozess-Output, Substrate-Prints, Exceptions.
Für strukturierte pro-Tick-Historie: `peers replay <N>` gegen das
Projekt direkt.

## OPTIONS
- `name` (positional, required) — registrierter Projektname.

## BEISPIELE
```
# Live mitschauen während `peers-ctl start` in einer anderen Shell läuft.
peers-ctl tail meine-app

# Bonus: durch grep pipen.
peers-ctl tail meine-app | grep -i 'error\|halt'
```

## DATEIEN
- Liest: `$XDG_CONFIG_HOME/peers-ctl/logs/<name>.log` (oder
  pro-Projekt-Pfad aus Registry-Eintrag).

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Config-Root-Override.

## SIEHE AUCH
- `peers-ctl logs --help-man` — nicht-streamend, last-N.
- `peers replay --help-man` — strukturiertes pro-Tick-JSONL.

## NOTES
- Die Datei muss schon existieren wenn `tail` startet. Wenn man's
  vor `peers-ctl start` aufruft → „log not yet written"-stderr.
- Sleep-Cadence ist fix bei 0.5s — reicht für menschliches Tailing.
