# peers-ctl stop — stop a project loop cleanly

## NAME
peers-ctl stop — send SIGTERM to a running project loop, wait for it
to exit gracefully, escalate to SIGKILL after the grace period.

## SYNOPSIS
```
peers-ctl stop <name> [--grace-s SECONDS]
```

## DESCRIPTION
Looks up the tracked PID for `<name>` in the registry, signals it
with SIGTERM, waits up to `--grace-s` seconds for it to exit, and
escalates to SIGKILL if it's still alive. Container runs are signalled
via `podman stop` instead of POSIX kill.

The registry entry is updated (PID cleared, state → `idle`) once the
process is confirmed dead. If the project wasn't running, exits 1
with an explanatory stderr message.

## OPTIONS
- `name` (positional, required) — registered project name.
- `--grace-s SECONDS` — grace period before SIGKILL; default 10.0.

## EXAMPLES
```
peers-ctl stop my-app
peers-ctl stop my-app --grace-s 30
```

## FILES
- Writes: registry entry (PID cleared, state → idle).

## ENVIRONMENT
- `PODMAN_CMD` — override for container shutdown.

## SEE ALSO
- `peers-ctl start --help-man`
- `peers-ctl status --help-man`
- `peers-ctl list --help-man`

## NOTES
- A running peer subprocess inside the loop may inherit the SIGTERM
  and abort mid-tick. The next tick's idle-timeout / no-commit
  classification handles this gracefully on the next `start`.
- If the loop has already exited on its own, `stop` still cleans up
  the registry entry and prints a confirmation.
