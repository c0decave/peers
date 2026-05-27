# peers-ctl tail — follow a project's controller log

## NAME
peers-ctl tail — tail-and-follow the controller-side log file for a
project, printing existing content first, then streaming new lines
as they arrive. Like `tail -f`, but with the safe-I/O guards.

## SYNOPSIS
```
peers-ctl tail <name>
```

## DESCRIPTION
Resolves the log path for `<name>` via the registry, opens it with
no-symlink-follow guards, prints the last 20 lines, and then loops:
read any new content; print; sleep 0.5s; repeat. Interrupt with
Ctrl-C (returns 0).

This is the controller-side stdout/stderr stream of the `peers run`
process — peer subprocess output, substrate prints, exceptions. For
structured per-tick history, use `peers replay <N>` against the
project directly.

## OPTIONS
- `name` (positional, required) — registered project name.

## EXAMPLES
```
# Watch live while peers-ctl start runs in another shell.
peers-ctl tail my-app

# Bonus: pipe to grep.
peers-ctl tail my-app | grep -i 'error\|halt'
```

## FILES
- Reads: `$XDG_CONFIG_HOME/peers-ctl/logs/<name>.log` (or
  per-project path stored in the registry entry).

## ENVIRONMENT
- `XDG_CONFIG_HOME` — config root override.

## SEE ALSO
- `peers-ctl logs --help-man` — non-streaming last-N variant.
- `peers replay --help-man` — structured per-tick JSONL.

## NOTES
- The file must already exist when `tail` is started. If you call it
  before `peers-ctl start`, you'll get a "log not yet written" stderr
  message.
- Sleep cadence is fixed at 0.5s — fine for any human-facing tailing.
