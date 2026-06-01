# peers-ctl list — alle Projekte + State drucken

## NAME
peers-ctl list — druckt eine Tabelle aller registrierten Projekte mit
State, PID (falls running) und Pfad auf Platte.

## SYNOPSIS
```
peers-ctl list
```

## BESCHREIBUNG
Reconciliert die Registry zuerst (drops PIDs von toten Prozessen,
markiert verschwundene Pfade als `missing`), druckt dann eine
Vier-Spalten-Tabelle:

```
NAME                 STATE    PID      PATH
meine-app            running  12345    ~/c0de/peers-c0de/meine-app
alteres-proj         idle              ~/c0de/peers-c0de/alteres-proj
```

States:

- `idle` — registriert, kein `peers run`-Prozess getrackt.
- `running` — `peers run` (Host oder Container) läuft unter
  getrackter PID.
- `missing` — registrierter Pfad existiert nicht mehr; mit
  `peers-ctl remove` austragen.

## OPTIONS
Keine.

## BEISPIELE
```
peers-ctl list
peers-ctl --config-dir /tmp/ctl-test list
```

## DATEIEN
- Liest: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Registry-Location-Override.

## SIEHE AUCH
- `peers-ctl dashboard --help-man` — reichere Sicht mit Goal-Counts
  und Last-Tick-Timestamps.
- `peers-ctl status --help-man` — Single-Projekt-Deep-Status.

## NOTES
- Die PID-Spalte ist leer für nicht-laufende Projekte.
- `list` ist read-only abgesehen vom Reconcile-Step, der nur stale
  PIDs aus der Registry trimmt.
