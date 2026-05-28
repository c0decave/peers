# peers-ctl dashboard — rollup view across all projects

## NAME
peers-ctl dashboard — read-only multi-project table with state, tick
count, open gates (hard / soft), blocking bug count, container name,
and last-tick timestamp.

## SYNOPSIS
```
peers-ctl dashboard [--live] [--refresh-s SECONDS] [--project NAME]
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
- In `--live`, `ALERT` surfaces `CRASHED`, `UNKNOWN`, `HALTED`,
  `BUDGET`, `DEGRADED`, or `WARN`; `EVENT` shows the latest decoded
  Claude session event when available.

Columns are auto-sized for terminal-friendly width.

With `--project NAME`, the dashboard switches from the multi-project
rollup to a single-project drilldown. The drilldown includes the
project row, recent `runs.jsonl` entries, and bug-report details. It
can also be combined with `--live` for continuous redraw.

## OPTIONS
- `--live` — redraw the dashboard continuously until Ctrl-C.
- `--refresh-s SECONDS` — refresh interval for `--live` (default:
  `2.0`). Must be greater than zero.
- `--project NAME` — show a single-project drilldown with recent runs
  and bug reports.

## EXAMPLES
```
peers-ctl dashboard

# Built-in live view.
peers-ctl dashboard --live --refresh-s 1

# Single-project drilldown.
peers-ctl dashboard --project my-app
peers-ctl dashboard --live --project my-app
```

## FILES
- Reads: registry, each project's `.peers/log/runs.jsonl`,
  `.peers/goals.yaml`, `.peers/state.json`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers-ctl list --help-man` — minimal three-column form.
- `peers-ctl status --help-man` — single-project deep view.
- `peers-ctl peek --help-man` — decoded live Claude session events.
- `peers-ctl report --help-man` — Markdown rollup with controller log
  paths.

## NOTES
- The dashboard call is read-only. Even if a project's YAML is
  broken, the row renders with `?` placeholders rather than failing
  the whole call.
- Bug-hunt summary failures degrade silently to `0` to keep the
  dashboard resilient.
