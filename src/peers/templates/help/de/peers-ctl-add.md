# peers-ctl add — bestehendes Projekt registrieren

## NAME
peers-ctl add — registriert ein bereits existierendes Verzeichnis als
peers-ctl-Projekt, damit es danach per Kurzname ansprechbar ist.

## SYNOPSIS
```
peers-ctl add <pfad> [--name NAME]
```

## BESCHREIBUNG
Löst `<pfad>` auf (Kurzname → `$PEERS_PROJECTS_ROOT/<name>`; Vollpfad
→ verbatim), validiert dass das Resultat ein existierendes Directory
ist, prüft den Namen gegen die Projektnamen-Policy, schreibt den
Eintrag in `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

Der Pfad wird NICHT modifiziert — `peers init` wird NICHT gestartet.
Wenn `<pfad>/.peers/config.yaml` fehlt, gibt's eine Warning mit dem
Hinweis erst `peers -C <pfad> init` zu fahren bevor
`peers-ctl start <name>`.

Für „Scaffold + Register in einem Rutsch" → `peers-ctl new`.

## OPTIONS
- `pfad` (positional, required) — Verzeichnis das registriert wird.
- `--name NAME` — Registry-Name überschreiben (default:
  Directory-Basename).

## BEISPIELE
```
# Kurzname → ~/c0de/peers-c0de/existing-app/
peers-ctl add existing-app

# Vollpfad verbatim.
peers-ctl add /opt/work/big-project --name big

# Add, dann manuell init.
peers-ctl add ~/Code/old-project
peers -C ~/Code/old-project init --modes=audit
```

## DATEIEN
- Schreibt: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## UMGEBUNGSVARIABLEN
- `PEERS_PROJECTS_ROOT` — Basis für Kurznamen-Auflösung.
- `XDG_CONFIG_HOME` — Registry-Location-Override.

## SIEHE AUCH
- `peers-ctl new --help-man` — Scaffold + Init + Register auf einmal.
- `peers init --help-man` — `.peers/`-Plane manuell bootstrappen.
- `peers-ctl remove --help-man` — wieder austragen.

## NOTES
- Projektnamen folgen derselben Validierung wie bei `peers-ctl new`
  (keine Pfad-Separatoren; dateinamen-sicher). Ungültige Namen
  → Exit 2.
- Re-Add desselben Namens wird abgelehnt — erst `peers-ctl remove`.
