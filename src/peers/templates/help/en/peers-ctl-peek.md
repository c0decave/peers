# peers-ctl peek — decode live peer session events

## NAME
peers-ctl peek — print operator-readable Claude session jsonl events for a
registered project.

## SYNOPSIS
```
peers-ctl peek <name> [--session <id>] [--no-follow] [--last N]
```

## DESCRIPTION
Finds the newest Claude session jsonl for the project and decodes tool
calls, text emissions, and tool results into one-line events. This is
read-only and does not affect the running peers loop.

## OPTIONS
- `name` — registered project name.
- `--session <id>` — read a specific session id instead of the newest.
- `--no-follow` — print existing events and exit.
- `--last N` — show only the last N raw jsonl events before following.

## EXAMPLES
```
peers-ctl peek my-app
peers-ctl peek my-app --last 50 --no-follow
```

## FILES
- Reads: `~/.claude/projects/<encoded-cwd>/*.jsonl`.
- Reads: the peers-ctl project registry.

## ENVIRONMENT
- `HOME` — used to locate Claude session jsonl files.
- `XDG_CONFIG_HOME` — controller registry root override.

## SEE ALSO
- `peers-ctl status --help-man`
- `peers-ctl tail --help-man`
- `peers report --help-man`

## NOTES
- `peek` observes Claude session traffic only; it does not stop or signal
  the running peer process.
