# peers-ctl remove — unregister a project

## NAME
peers-ctl remove — remove a project entry from the controller registry.
Does NOT touch the project's files on disk.

## SYNOPSIS
```
peers-ctl remove <name>
```

## DESCRIPTION
Looks up `<name>` in the registry and deletes the entry. The
project's directory (and its `.peers/` control plane) is left intact —
re-register at any time with `peers-ctl add <path>`.

If a project loop is currently running under the removed name, the
process keeps going; the controller just won't track it anymore. Run
`peers-ctl stop <name>` before `remove` if you want a clean shutdown.

## OPTIONS
- `name` (positional, required) — registry name.

## EXAMPLES
```
peers-ctl remove old-project

# Stop first, then remove.
peers-ctl stop my-app
peers-ctl remove my-app
```

## FILES
- Writes: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## ENVIRONMENT
- `XDG_CONFIG_HOME` — registry location override.

## SEE ALSO
- `peers-ctl add --help-man`
- `peers-ctl stop --help-man`
- `peers-ctl list --help-man`

## NOTES
- Unknown names return exit 1 with an explanatory stderr message.
- Per-project log files under the controller's `logs/` directory are
  NOT deleted automatically; use `peers-ctl prune` if you want them
  reaped.
