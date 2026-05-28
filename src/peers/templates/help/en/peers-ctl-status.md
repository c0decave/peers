# peers-ctl status — status of one or all projects

## NAME
peers-ctl status — print detailed status for a single project (JSON +
embedded `peers status`), or fall back to `peers-ctl list` when
called with no name.

## SYNOPSIS
```
peers-ctl status [<name>] [--no-reconcile]
```

## DESCRIPTION
With no argument, behaves exactly like `peers-ctl list` (table of all
projects + state).

With a `<name>`, reconciles the registry, looks up the project, and
prints:

1. The registry entry serialised as indented JSON.
2. (If `.peers/state.json` exists) The output of
   `peers -C <path> status` so the in-loop iteration / lock / goal
   status surfaces too.

## OPTIONS
- `name` (positional, optional) — registered project name. If omitted,
  prints the list view.
- `--no-reconcile` — print registry state without probing PID/container
  liveness first.

## EXAMPLES
```
# Multi-project view.
peers-ctl status

# Deep view of one project.
peers-ctl status my-app
```

## FILES
- Reads: `$XDG_CONFIG_HOME/peers-ctl/projects.json`,
  `<project>/.peers/state.json`.

## ENVIRONMENT
None directly.

## SEE ALSO
- `peers-ctl list --help-man`
- `peers-ctl dashboard --help-man`
- `peers status --help-man` (the per-project command embedded here).

## NOTES
- The embedded `peers status` call uses the same lock-probe logic as
  the standalone command — if the loop is currently running, you'll
  see the live PID; if it died ungracefully, you'll see "stale".
