# peers-ctl stop — Projekt-Loop sauber stoppen

## NAME
peers-ctl stop — sendet SIGTERM an eine laufende Projekt-Loop, wartet
auf graceful Exit, eskaliert nach der Grace-Periode zu SIGKILL.

## SYNOPSIS
```
peers-ctl stop <name> [--grace-s SEKUNDEN]
```

## BESCHREIBUNG
Schlägt die getrackte PID für `<name>` in der Registry nach, signaled
sie mit SIGTERM, wartet bis zu `--grace-s` Sekunden auf Exit und
eskaliert zu SIGKILL falls noch lebend. Container-Runs werden via
`podman stop` signaled statt POSIX-Kill.

Registry-Eintrag wird aktualisiert (PID weg, State → `idle`), sobald
der Prozess als tot bestätigt ist. War das Projekt nicht running,
Exit 1 mit erklärender stderr-Meldung.

## OPTIONS
- `name` (positional, required) — registrierter Projektname.
- `--grace-s SEKUNDEN` — Grace-Periode vor SIGKILL; default 10.0.

## BEISPIELE
```
peers-ctl stop meine-app
peers-ctl stop meine-app --grace-s 30
```

## DATEIEN
- Schreibt: Registry-Eintrag (PID weg, State → idle).

## UMGEBUNGSVARIABLEN
- `PODMAN_CMD` — Override für Container-Shutdown.

## SIEHE AUCH
- `peers-ctl start --help-man`
- `peers-ctl status --help-man`
- `peers-ctl list --help-man`

## NOTES
- Ein laufender Peer-Subprozess in der Loop kann das SIGTERM erben
  und mitten im Tick abbrechen. Beim nächsten `start` greift die
  Idle-Timeout-/No-Commit-Klassifikation des nächsten Ticks sauber.
- Wenn die Loop schon selbst beendet ist, räumt `stop` den Registry-
  Eintrag trotzdem auf und gibt eine Bestätigung.
