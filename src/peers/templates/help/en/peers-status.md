# peers status — print iteration, lock, and goal status

## NAME
peers status — print a short human-readable summary of the loop's
current state without running anything.

## SYNOPSIS
```
peers [-C <dir>] status
```

## DESCRIPTION
Reads `.peers/state.json` (auto-migrating v1 → v2 in memory) and
prints:

- Current iteration count and whose turn is next.
- Lock-file state: held (PID), present-but-stale, present-but-empty.
- HALTED flag and dirty-worktree warning, if applicable.
- Budget rollup: iterations, runtime, tokens, USD.
- Per-goal state (pass / fail / pending) plus diagnostic.
- Per-peer state plus consecutive failure counter and last classification.
- Last warnings (up to 5) and total run-log entry count.

Reads only — never spawns peers, never mutates state.

## OPTIONS
None.

## EXAMPLES
```
peers status
peers -C ~/c0de/peers-c0de/my-app status
```

## FILES
- Reads: `.peers/state.json`, `.peers/run.lock`, `.peers/HALTED.md`,
  `.peers/log/runs.jsonl`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers report --help-man` — detailed Markdown rollup.
- `peers info --help-man` — config (peers/goals/budget) printout.
- `peers-ctl status --help-man` — for multi-project status.

## NOTES
- If `.peers/run.lock` shows "stale" (file present but flock not
  held), a previous run died ungracefully; re-running is safe.
- Schema v1 state files are upgraded in memory; the file on disk is
  rewritten the next time the orchestrator persists.
