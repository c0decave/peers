# peers-ctl report — write a Markdown controller report

## NAME
peers-ctl report — render a controller-side Markdown report covering
one or all registered projects.

## SYNOPSIS
```
peers-ctl report [<name>]
```

## DESCRIPTION
Walks the registered projects (filtered to `<name>` if given),
collects per-project rollup data (state, ticks, blocking bug count,
last-tick timestamp, README status, controller log path), and writes
the result under `$XDG_CONFIG_HOME/peers-ctl/`:

- `REPORT.md` for the all-projects view.
- `REPORT-<name>.md` for a single-project run.

Exit code is 0 on a clean render, 1 if any per-project warning was
emitted (e.g. README missing / unsafe symlink, controller log
unsafe).

## OPTIONS
- `name` (positional, optional) — restrict to one project.

## EXAMPLES
```
# All projects.
peers-ctl report

# Just one.
peers-ctl report my-app
```

## FILES
- Writes: `$XDG_CONFIG_HOME/peers-ctl/REPORT.md` (or
  `REPORT-<name>.md`).
- Reads: registry, each project's `README.md`, `.peers/log/runs.jsonl`,
  `.peers/goals.yaml`.

## ENVIRONMENT
- `XDG_CONFIG_HOME` — config root override.

## SEE ALSO
- `peers report --help-man` — per-project report.
- `peers-ctl dashboard --help-man` — terminal table view.

## NOTES
- Use `peers-ctl report` for hand-off documents; use
  `peers-ctl dashboard` for interactive checks.
- Unknown `<name>` exits 1 with an explanatory stderr message.
