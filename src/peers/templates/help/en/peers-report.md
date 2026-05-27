# peers report — write a human-readable Markdown rollup

## NAME
peers report — write `.peers/REPORT.md` summarising state, recent
ticks, budgets, and warnings.

## SYNOPSIS
```
peers [-C <dir>] report
```

## DESCRIPTION
Reads `.peers/state.json` plus `.peers/log/runs.jsonl` and renders a
Markdown document covering:

- Iteration count, next-up peer, peer rotation order.
- HALTED warning if `.peers/HALTED.md` is present.
- Per-goal state table (id, state, diagnostic).
- Soft-goal consensus tracker.
- Budget consumption (iterations / runtime / tokens / USD).
- Per-peer state, consecutive failures, recent failures, cheating
  counter.
- Tick history (last 50 entries) with per-tick cost + classification.
- Run-termination events (`exit` records).
- Warnings history (last 20).

Skips malformed JSONL lines defensively (warns to stderr with a count)
so a single corrupt entry does not block reporting.

## OPTIONS
None.

## EXAMPLES
```
peers report
peers -C ~/c0de/peers-c0de/my-app report
```

## FILES
- Reads: `.peers/state.json`, `.peers/log/runs.jsonl`,
  `.peers/HALTED.md` (optional).
- Writes: `.peers/REPORT.md`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers verify --help-man` — re-run hard gates standalone.
- `peers replay --help-man` — drill into a specific tick.
- `peers-ctl report --help-man` — cross-project controller report.

## NOTES
- Tick history is capped at the last 50 entries to keep `REPORT.md`
  readable. For full history, parse `runs.jsonl` directly.
- USD per-tick is shown to 4 decimals; tokens are integer counts as
  reported by the peer's billing layer.
