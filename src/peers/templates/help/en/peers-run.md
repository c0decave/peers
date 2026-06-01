# peers run — drive the peer loop until a stop reason is reached

## NAME
peers run — run the orchestrated peer loop in the foreground until the
loop terminates (goal-complete, budget exhausted, max ticks, halted,
etc.).

## SYNOPSIS
```
peers [-C <dir>] run [--max-ticks N] [--max-usd USD] [--dry-run] [-v]
```

## DESCRIPTION
Loads `.peers/config.yaml` + `.peers/goals.yaml`, validates them,
instantiates the orchestrator driver, and runs the loop. For each
tick the substrate:

1. Picks the next peer via `turn_index`.
2. Builds the prompt (peer-specific + goal status + inbox messages).
3. Spawns the peer's CLI with the health-guard supervising stdout/stderr.
4. After the peer commits (or fails), runs hard goals and any
   `verify.commands`, updates `state.json`, and appends a JSONL line
   to `.peers/log/runs.jsonl`.
5. Decides whether to stop (all hard goals pass, budget exhausted,
   too many consecutive failures, ...).

A `run.lock` is taken on `.peers/` so concurrent `peers run`
invocations against the same project fail fast. The lock is
flock-based, so it's released cleanly even on `kill -9`.

## OPTIONS
- `--max-ticks N` — cap the number of ticks for this invocation.
  Useful for smoke tests and CI runs.
- `--max-usd USD` — override `budget.max_usd` from `config.yaml` for
  this run only. Surface-level safety belt.
- `--dry-run` — revert each peer's commit at the end of every tick.
  Lets you observe what peers would do without changing the repo.
- `-v, --verbose` — after each tick, echo the last 50 lines of peer
  stdout and 25 of stderr to the substrate's stderr (full logs still
  go to `.peers/log/peers/tick-*`).

## EXAMPLES
```
# Run until goals pass or the configured budget runs out.
peers run

# Smoke test: 5 ticks, $1 hard cap.
peers run --max-ticks 5 --max-usd 1

# Watch peers think but discard their commits.
peers run --dry-run --max-ticks 3 -v
```

## FILES
- Reads: `.peers/config.yaml`, `.peers/goals.yaml`, `.peers/state.json`.
- Writes: `.peers/state.json` (atomic), `.peers/log/runs.jsonl`,
  per-tick logs under `.peers/log/peers/tick-NNNN-<peer>/`.
- Acquires: `.peers/run.lock` (flock).
- Drops on halt: `.peers/HALTED.md` (human-readable reason).

## ENVIRONMENT
- `PEERS_FORCE_DRIVER` — testing override; bypasses `config.yaml`'s
  driver selection.
- Peer-specific env (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`) is passed through to the peer subprocess.
  `OPENROUTER_API_KEY` is required, and checked before launch, when a
  peer has `provider: openrouter`.

## SEE ALSO
- `peers tick --help-man` — one-tick variant for hook chains.
- `peers verify --help-man` — re-run hard gates standalone.
- `peers status --help-man` — current iteration + lock state.

## NOTES
- Idle-timeout (default 30 min) governs each peer; raise
  `health.idle_timeout_s` in `config.yaml` for long-running test
  suites.
- `--max-usd` is enforced only when the configured driver/peer
  reports per-token cost (claude/codex via API). OAuth subscriptions
  default to `warn`-mode (no hard cap).
