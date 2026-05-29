# peers-ctl dashboard — rollup view across all projects

## NAME
peers-ctl dashboard — read-only multi-project table with state, tick
count, open gates (hard / soft), blocking bug count, container name,
and last-tick timestamp.

## SYNOPSIS
```
peers-ctl dashboard [--live] [--refresh-s SECONDS] [--frames N]
                    [--project NAME]
```

For most multi-project operators the **streaming view** (`--live`) is
the more useful mode: it redraws the table every `--refresh-s` seconds
and surfaces `ALERT` + `EVENT` columns that the one-shot snapshot
omits. The default no-flag invocation is a single rendered snapshot,
useful for scripting and quick checks.

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
- `--live` — redraw the dashboard continuously until Ctrl-C. Adds
  the `ALERT` and `EVENT` columns. This is the streaming view most
  operators want for day-to-day observability.
- `--refresh-s SECONDS` — refresh interval for `--live` (default:
  `2.0`). Must be greater than zero.
- `--frames N` — only valid with `--live`. Render `N` frames and
  exit (default: run until Ctrl-C). Useful for headless smoke tests
  and CI: `peers-ctl dashboard --live --frames 1` renders exactly
  one frame and exits 0.
- `--project NAME` — show a single-project drilldown with recent runs
  and bug reports.

## EXAMPLES
```
# Streaming view of all projects — the discoverable default for
# day-to-day observability (Ctrl-C to exit).
peers-ctl dashboard --live
peers-ctl dashboard --live --refresh-s 1

# One-shot snapshot — good for piping into other tools.
peers-ctl dashboard

# Non-interactive smoke test: render one frame and exit.
peers-ctl dashboard --live --frames 1

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
