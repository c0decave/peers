# peers-ctl remove — Projekt aus Registry austragen

## NAME
peers-ctl remove — entfernt einen Projekt-Eintrag aus der Controller-
Registry. Lässt die Projekt-Files auf Platte unangetastet.

## SYNOPSIS
```
peers-ctl remove <name>
```

## BESCHREIBUNG
Schlägt `<name>` in der Registry nach und löscht den Eintrag. Das
Projekt-Verzeichnis (und die `.peers/`-Control-Plane) bleiben — mit
`peers-ctl add <pfad>` jederzeit wieder eintragbar.

Wenn unter dem entfernten Namen gerade eine Loop läuft: der Prozess
läuft weiter, der Controller trackt ihn nur nicht mehr. Vor `remove`
am besten erst `peers-ctl stop <name>` für sauberes Shutdown.

## OPTIONS
- `name` (positional, required) — Registry-Name.

## BEISPIELE
```
peers-ctl remove old-project

# Erst stoppen, dann austragen.
peers-ctl stop meine-app
peers-ctl remove meine-app
```

## DATEIEN
- Schreibt: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## UMGEBUNGSVARIABLEN
- `XDG_CONFIG_HOME` — Registry-Location-Override.

## SIEHE AUCH
- `peers-ctl add --help-man`
- `peers-ctl stop --help-man`
- `peers-ctl list --help-man`

## NOTES
- Unbekannte Namen → Exit 1 mit erklärender stderr-Meldung.
- Pro-Projekt-Log-Files unter dem Controller-`logs/`-Dir werden
  NICHT automatisch gelöscht; für Reap-Lauf `peers-ctl prune` nutzen.
