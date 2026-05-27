# peers run — Peer-Loop bis zum Stop-Grund laufen lassen

## NAME
peers run — startet die orchestrierte Peer-Loop im Vordergrund und
läuft bis ein Stop-Grund erreicht ist (goals-complete, Budget durch,
max-ticks, halted, ...).

## SYNOPSIS
```
peers [-C <dir>] run [--max-ticks N] [--max-usd USD] [--dry-run] [-v]
```

## BESCHREIBUNG
Lädt `.peers/config.yaml` + `.peers/goals.yaml`, validiert, baut den
Orchestrator-Driver, fährt die Loop. Pro Tick:

1. Nächsten Peer via `turn_index` wählen.
2. Prompt bauen (peer-spezifisch + Goal-Status + Inbox).
3. Peer-CLI starten, Health-Guard supervised stdout/stderr.
4. Nach Peer-Commit (oder Fail): Hard-Goals + `verify.commands`
   evaluieren, `state.json` updaten, JSONL-Zeile an
   `.peers/log/runs.jsonl` anhängen.
5. Stop-Entscheidung (alle Hard-Goals grün / Budget durch /
   zu viele consecutive Fails / ...).

`.peers/run.lock` per `flock` — zwei `peers run` parallel auf dem
gleichen Projekt geht nicht (auch nach `kill -9` sauber, weil
flock-basiert).

## OPTIONS
- `--max-ticks N` — Tick-Limit für diese Invocation. Gut für
  Smoke-Tests und CI.
- `--max-usd USD` — `budget.max_usd` aus `config.yaml` für diesen
  Run überschreiben. Sicherheitsnetz.
- `--dry-run` — am Tick-Ende Peer-Commit reverten. Sehen was der
  Peer tun würde ohne dass das Repo Änderungen behält.
- `-v, --verbose` — nach jedem Tick die letzten 50 Zeilen Peer-stdout
  + 25 Zeilen Peer-stderr auf substrate-stderr echoen (Vollogs
  bleiben in `.peers/log/peers/tick-*`).

## BEISPIELE
```
# Bis Goals grün oder Budget alle.
peers run

# Smoke-Test: 5 Ticks, $1 hart gedeckelt.
peers run --max-ticks 5 --max-usd 1

# Peers beim Denken zugucken, Commits wegwerfen.
peers run --dry-run --max-ticks 3 -v
```

## DATEIEN
- Liest: `.peers/config.yaml`, `.peers/goals.yaml`, `.peers/state.json`.
- Schreibt: `.peers/state.json` (atomar), `.peers/log/runs.jsonl`,
  pro-Tick-Logs unter `.peers/log/peers/tick-NNNN-<peer>/`.
- Lock: `.peers/run.lock` (flock).
- Bei Halt: `.peers/HALTED.md` (menschen-lesbarer Grund).

## UMGEBUNGSVARIABLEN
- `PEERS_FORCE_DRIVER` — Test-Override; umgeht `config.yaml`-Driver.
- Peer-spezifische Env (z.B. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
  wird durchgereicht.

## SIEHE AUCH
- `peers tick --help-man` — Ein-Tick-Variante für Hook-Chains.
- `peers verify --help-man` — Hard-Gates standalone re-checken.
- `peers status --help-man` — aktuelle Iteration + Lock-Zustand.

## NOTES
- Idle-Timeout (default 30 min) gilt pro Peer; bei langen
  Testsuiten `health.idle_timeout_s` in `config.yaml` raufdrehen.
- `--max-usd` greift nur wenn der Peer per-Token-Cost meldet
  (claude/codex via API). OAuth-Subscriptions sind default `warn`
  (kein Hard-Cap).
