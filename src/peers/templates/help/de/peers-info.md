# peers info — Peers, Goals, Budget und Health drucken

## NAME
peers info — dumpt aufgelöste `.peers/config.yaml` + `.peers/goals.yaml`
ohne irgendwas zu starten. Sanity-Check für fresh inits oder Diff
zwischen Projekten.

## SYNOPSIS
```
peers [-C <dir>] info
```

## BESCHREIBUNG
Lädt und validiert Konfig + Goals, druckt dann:

- Aufgelöstes Target-Pfad.
- Driver (`orchestrator`/`hooks`/`sessions`).
- Comm-Channel (`git`/`hybrid`).
- Peers: Anzahl + pro-Peer-`(tool, prompt_mode)`-Zeile.
- Budget: Iterations, Runtime, optional Tokens + USD-Caps. Bei
  gesetztem `max_usd` zusätzlich effektiver `max_usd_mode`
  (`auto`/`hard`/`warn`/`off`) plus Begründung der Wahl.
- Health: Idle-Timeout, Absolut-Max-Runtime, Buffer-Cap.
- Goals: total / hard / soft + pro-Goal-ID, Reviewer-Mode,
  Consensus-needed, Quorum (wo zutreffend).

Exit nicht-null bei jedem Validierungsfehler (kaputtes YAML,
fehlende Health-Keys, ungültiger Regex in `error_patterns`, ...).

## OPTIONS
Keine.

## BEISPIELE
```
peers info
peers -C ~/c0de/peers-c0de/meine-app info
```

## DATEIEN
- Liest: `.peers/config.yaml`, `.peers/goals.yaml`.

## UMGEBUNGSVARIABLEN
Keine.

## SIEHE AUCH
- `peers status --help-man` — Runtime-State statt Konfig.
- `peers verify --help-man` — Hard-Gates re-checken.
- `peers-ctl modes show <name>` — Mode-Konfig inspizieren.

## NOTES
- `info` validiert dasselbe Schema wie `peers run` — wenn `info`
  nicht-null exitet, exitet `run` auch. Schneller Sanity-Check.
- Für OAuth-Subscription-Peers (claude / codex CLI) ist
  `max_usd_mode` default `warn` (kein Hard-Cap), weil per-Token-
  Billing dort nicht aussagekräftig ist.
