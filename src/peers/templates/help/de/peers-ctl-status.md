# peers-ctl status — Status eines oder aller Projekte

## NAME
peers-ctl status — druckt detaillierten Status für ein einzelnes
Projekt (JSON + embedded `peers status`), oder fällt ohne Name auf
`peers-ctl list` zurück.

## SYNOPSIS
```
peers-ctl status [<name>] [--no-reconcile]
```

## BESCHREIBUNG
Ohne Argument: wie `peers-ctl list` (Tabelle aller Projekte + State).

Mit `<name>`: reconciliert die Registry, schlägt das Projekt nach
und druckt:

1. Den Registry-Eintrag als eingerücktes JSON.
2. (Wenn `.peers/state.json` existiert) Den Output von
   `peers -C <pfad> status`, damit auch der In-Loop-Iterations-/Lock-/
   Goal-Status sichtbar wird.

## OPTIONS
- `name` (positional, optional) — registrierter Projektname. Wenn
  weggelassen → List-View.
- `--no-reconcile` — Registry-State drucken, ohne vorher PID/Container-
  Liveness zu prüfen.

## BEISPIELE
```
# Multi-Projekt-Sicht.
peers-ctl status

# Deep-View für ein Projekt.
peers-ctl status meine-app
```

## DATEIEN
- Liest: `$XDG_CONFIG_HOME/peers-ctl/projects.json`,
  `<projekt>/.peers/state.json`.

## UMGEBUNGSVARIABLEN
Keine direkt.

## SIEHE AUCH
- `peers-ctl list --help-man`
- `peers-ctl dashboard --help-man`
- `peers status --help-man` (das embedded pro-Projekt-Kommando).

## NOTES
- Der eingebettete `peers status`-Call nutzt dieselbe Lock-Probe-
  Logik wie das Standalone-Kommando — läuft die Loop gerade, sieht
  man die Live-PID; war sie ungesund gestorben, sieht man „stale".
