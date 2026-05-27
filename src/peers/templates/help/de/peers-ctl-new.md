# peers-ctl new — One-Shot-Scaffold + Init + Register

## NAME
peers-ctl new — legt das Zielverzeichnis an, `git init` + leerer
Initial-Commit, fährt `peers init`, optional `SPEC.md` schreiben,
und registriert das Resultat. Danach: `peers-ctl start <name>` läuft
direkt.

## SYNOPSIS
```
peers-ctl new <pfad> [--name NAME] [--spec TEXT_ODER_PFAD]
              [--driver {orchestrator,hooks,sessions}]
              [--force] [--container]
              [--modes <liste>] [--lang <lang>]
```

## BESCHREIBUNG
End-to-End-Scaffold — von null auf lauffähiges peers-Projekt mit
einem Kommando. Schritte:

1. `<pfad>` auflösen: Kurzname → `$PEERS_PROJECTS_ROOT/<name>`;
   Vollpfad → verbatim.
2. Wenn Target existiert und nicht leer ist → ohne `--force`
   abbrechen.
3. Baseline `README.md` (und optional `SPEC.md`) schreiben.
4. `git init -b main` + leerer Initial-Commit (damit
   `peers-baseline` etwas zum Taggen hat).
5. `peers init [--force] [--driver ...] [--modes ...] [--lang ...]`
   fahren — auf dem Host ODER im `peers:dev`-Container (mit
   `--container`).
6. Projekt in Controller-Registry eintragen.

`--container` ist bevorzugt wenn `peers` nicht auf dem Host
installiert ist: nur `podman` + das `peers:dev`-Image reichen.

## OPTIONS
- `pfad` (positional, required) — Zielverzeichnis.
- `--name NAME` — Registry-Name (default: Directory-Basename).
- `--spec TEXT_ODER_PFAD` — `SPEC.md`-Inhalt als Literal-String ODER
  Pfad zu einer Datei mit dem Inhalt.
- `--driver {orchestrator,hooks,sessions}` — default `orchestrator`.
- `--force` — in non-empty-Dir scaffolden ODER Registry-Eintrag
  überschreiben.
- `--container` — `peers init` im `peers:dev`-Container fahren.
- `--modes <liste>` — kommaseparierte Mode-Namen (`audit`, `security`,
  `thorough`, ...). Siehe `peers-ctl modes list`.
- `--lang <lang>` — `python` (default), `js`, `rust`, `go`.
- `--audit-templates` — DEPRECATED-Alias für `--modes=audit`.

## BEISPIELE
```
# Häufigster Fall: Container, audit + thorough, JS-Projekt.
peers-ctl new meine-app --container --modes=audit,thorough --lang=js \
                        --spec ./meine-app-spec.md

# Kurzname landet in $PEERS_PROJECTS_ROOT.
peers-ctl new quick-test --modes=audit

# Force-Re-Scaffold über bestehenden State.
peers-ctl new meine-app --force --modes=audit,security
```

## DATEIEN
Angelegt unter `<pfad>/`:
- `README.md` — Baseline ("scaffolded by peers-ctl new").
- `SPEC.md` — nur mit `--spec`.
- `.peers/` — via `peers init` (siehe `peers init --help-man`).

Registry-Update:
- `$XDG_CONFIG_HOME/peers-ctl/projects.json` — neuer Eintrag.

## UMGEBUNGSVARIABLEN
- `PEERS_PROJECTS_ROOT` — Basis für Kurznamen-Auflösung.
- `PODMAN_CMD` — `podman`-Pfad-Override (bei `--container`).
- `PEERS_CTL_PODMAN_NETWORK` — Podman-Netzwerkmodus.

## SIEHE AUCH
- `peers init --help-man` — was Schritt 5 im Detail tut.
- `peers-ctl add --help-man` — registrieren ohne scaffolden.
- `peers-ctl modes list` — welche `--modes`-Werte gehen.

## NOTES
- Der leere Initial-Commit ankert `peers-baseline`, den Rollback-Tag
  den `peers init` setzt.
- Bei `--spec` ohne `/` und kein File: wird als Inline-Content
  behandelt. Mit `./spec.md` (oder beliebigem Pfad mit Separator)
  disambiguieren.
