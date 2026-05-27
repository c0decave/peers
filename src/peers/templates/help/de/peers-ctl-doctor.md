# peers-ctl doctor — Pre-Flight Host- + Projekt-Check

## NAME
peers-ctl doctor — verifiziert, dass der Host hat, was `peers-ctl
start` braucht (peers, git, Peer-CLIs, optional podman + `peers:dev`-
Image), lädt dann jedes registrierte Projekt-Konfig + Goals und
reportet Status pro Projekt.

## SYNOPSIS
```
peers-ctl doctor
```

## BESCHREIBUNG
Zwei-Phasen-Health-Check:

**Host-Toolchain.**
- `peers` und `git` müssen auf PATH sein (Problems, Exit 1).
- `claude` und `codex` werden nur gewarnt (nicht errort) wenn
  fehlend — die brauchen nur Projekte die sie tatsächlich nutzen,
  und sie können im `peers:dev`-Container leben.
- `podman` wird gewarnt wenn fehlend (nur für `--container` nötig).
- Wenn podman da ist: prüft auch ob `peers:dev` gebaut ist; warnt
  mit `make build`-Hint wenn nicht.

**Pro-Projekt.**
Für jedes registrierte Projekt: assert `.peers/config.yaml` +
`.peers/goals.yaml` existieren, lade sie durch dieselben Validatoren
wie `peers run`, drucke `[ok] / [FAIL]` + kurzes Summary (Peer- +
Goal-Counts bei Success, Error-Text bei Failure).

Exit 0 bei sauberem Health-Check, 1 wenn irgendein *Problem* (kein
*Warning*) gefunden wurde.

## OPTIONS
Keine.

## BEISPIELE
```
peers-ctl doctor
peers-ctl doctor || echo 'erst Host-Setup fixen, dann peers-ctl start'
```

## DATEIEN
- Liest: Registry + jedes Projekt-`.peers/config.yaml` +
  `.peers/goals.yaml`.

## UMGEBUNGSVARIABLEN
- `PEERS_PROJECTS_ROOT` — wird in der Projects-Root-Zeile gezeigt.
- `PODMAN_CMD` — was `doctor` beim Podman-Check probt.

## SIEHE AUCH
- `peers info --help-man` — pro-Projekt-Konfig-Dump.
- `peers-ctl modes list` — Mode-Discovery sanity-checken.

## NOTES
- Der codex-Check probiert gängige VSCode-Extension-Pfade als
  Fallback und druckt einen konkreten „found at <pfad>; point
  config.yaml at it"-Hint wenn er eine Binary findet.
- Warnings ändern den Exit-Code nicht; nur gelistete `Problems`
  tun das.
