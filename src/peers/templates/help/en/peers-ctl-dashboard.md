# peers-ctl dashboard — rollup view across all projects

## NAME
peers-ctl dashboard — read-only multi-project table with state, tick
count, open gates (hard / soft), blocking bug count, container name,
and last-tick timestamp.

## SYNOPSIS
```
peers-ctl dashboard
```

## DESCRIPTION
Reconciles the registry, then walks every project to produce a row:

```
NAME      STATE    TICKS  HARD_OPEN  SOFT_OPEN  BLOCKING  CONTAINER  LAST
my-app    running  47     2          1          3         peers-...  2026-...
older     idle     12     0          0          0         -          2026-...
```

- `TICKS` is taken from `.peers/log/runs.jsonl` (non-`exit` events).
- `HARD_OPEN` / `SOFT_OPEN` are counted via `peers.goals.load_goals`
  + per-goal status. Failed YAML → `?`; missing → `-`.
- `BLOCKING` calls `peers.bug_hunt.summarize` for the project's
  open blocking-bug count.
- `CONTAINER` shows the podman container name when known (parsed
  from the registry's `notes` field).
- `LAST` is the most recent `runs.jsonl` timestamp.

Columns are auto-sized for terminal-friendly width.

## OPTIONS
None.

## EXAMPLES
```
peers-ctl dashboard

# In a watch loop.
watch -n 5 peers-ctl dashboard
```

## FILES
- Reads: registry, each project's `.peers/log/runs.jsonl`,
  `.peers/goals.yaml`, `.peers/state.json`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers-ctl list --help-man` — minimal three-column form.
- `peers-ctl status --help-man` — single-project deep view.
- `peers-ctl report --help-man` — Markdown rollup with controller log
  paths.

## NOTES
- The dashboard call is read-only. Even if a project's YAML is
  broken, the row renders with `?` placeholders rather than failing
  the whole call.
- Bug-hunt summary failures degrade silently to `0` to keep the
  dashboard resilient.
