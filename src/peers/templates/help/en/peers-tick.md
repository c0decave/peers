# peers tick — run exactly ONE tick and exit

## NAME
peers tick — execute a single tick of the peer loop and exit. Designed
for hook-driven mode where each peer's Stop-hook fires the next tick.

## SYNOPSIS
```
peers [-C <dir>] tick [--dry-run] [--after <peer-name>]
```

## DESCRIPTION
Functionally equivalent to `peers run --max-ticks 1`: load config +
goals, pick the next peer via `state.turn_index`, spawn it under the
health-guard, evaluate hard gates after commit, update `state.json`,
and exit. Returns the orchestrator's stop-reason class as the exit
code (0 for `complete`/`max_ticks`, non-zero otherwise).

This is the entry point installed in claude's `Stop` hook and codex's
`on_stop` hook when `peers init --driver=hooks` is used: each agent
finishing its turn triggers the next tick via `peers tick`.

## OPTIONS
- `--dry-run` — revert the peer's commit at the end of the tick.
- `--after <peer-name>` — informational tag identifying which peer
  just finished. The next tick still picks via `state.turn_index`;
  the flag is for log/debug clarity in hook chains.

## EXAMPLES
```
# Manual single tick.
peers tick

# Hook entrypoint after claude finishes a turn.
peers -C /path/to/project tick --after claude

# Dry-run probe.
peers tick --dry-run
```

## FILES
Same as `peers run`. Uses `.peers/run.lock` so two hooks firing at
once don't tick concurrently.

## ENVIRONMENT
Same as `peers run`.

## SEE ALSO
- `peers run --help-man`
- `peers init --help-man` (`--driver=hooks` section)
- `.peers/hooks/` — generated snippets explaining how to install
  the Stop-hooks.

## NOTES
- The `--after` flag is intentionally NOT enforced as a name match;
  if your hook fires for the "wrong" peer, the next tick still
  follows `turn_index`. Misaligned hooks are noisy but safe.
