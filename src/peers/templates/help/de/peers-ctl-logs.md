# peers-ctl logs — letzte N Log-Zeilen drucken

## NAME
peers-ctl logs — druckt die letzten N Zeilen des Controller-Logs
eines Projekts, nicht-streamend.

## SYNOPSIS
```
peers-ctl logs <name> [-n LINES | --lines LINES]
```

## BESCHREIBUNG
Löst den Log-Pfad für `<name>` über die Registry auf (mit Safe-I/O-
Schutz: refused wenn der Pfad das Controller-`logs/`-Dir verlassen
würde), liest die Datei und druckt die letzten `LINES` Zeilen.
Nützlich für Post-Mortem-Peeks ohne Streaming-`tail`-Commitment.

## OPTIONS
- `name` (positional, required) — registrierter Projektname.
- `-n LINES`, `--lines LINES` — Anzahl trailing Lines (default 50;
  muss positiv sein).

## BEISPIELE
```
# Default 50 Zeilen.
peers-ctl logs meine-app

# Letzte 500 Zeilen.
peers-ctl logs meine-app -n 500
```

## DATEIEN
- Liest: `$XDG_CONFIG_HOME/peers-ctl/logs/<name>.log` (oder
  pro-Projekt-Pfad in der Registry).

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Config-Root-Override.

## SIEHE AUCH
- `peers-ctl tail --help-man` — Streaming-Follow.
- `peers report --help-man` — strukturierter pro-Projekt-Rollup.

## NOTES
- `--lines 0` (oder negativ) wird mit Exit 2 abgelehnt.
- Fehlendes Log-File (Projekt nie gestartet) → Exit 1 mit
  „log not yet written"-stderr.
