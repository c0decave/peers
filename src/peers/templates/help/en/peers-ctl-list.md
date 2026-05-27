# peers-ctl list — list all registered projects + state

## NAME
peers-ctl list — print a table of all registered projects with their
current state, PID (if running), and on-disk path.

## SYNOPSIS
```
peers-ctl list
```

## DESCRIPTION
Reconciles the registry first (drops PIDs of dead processes, marks
gone paths as `missing`), then prints a four-column table:

```
NAME                 STATE    PID      PATH
my-app               running  12345    /home/x/c0de/peers-c0de/my-app
older-proj           idle              /home/x/c0de/peers-c0de/older-proj
```

States are:

- `idle` — registered but no `peers run` process tracked.
- `running` — a `peers run` (host or container) is alive under the
  tracked PID.
- `missing` — the registered path no longer exists; remove it with
  `peers-ctl remove`.

## OPTIONS
None.

## EXAMPLES
```
peers-ctl list
peers-ctl --config-dir /tmp/ctl-test list
```

## FILES
- Reads: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## ENVIRONMENT
- `XDG_CONFIG_HOME` — registry location override.

## SEE ALSO
- `peers-ctl dashboard --help-man` — richer rollup including goal
  counts and last-tick timestamps.
- `peers-ctl status --help-man` — single-project deep status.

## NOTES
- The PID column is empty for projects that aren't running.
- `list` is read-only besides the reconciliation step, which only
  trims stale PIDs from the registry.
