# peers-ctl logs — print last N log lines

## NAME
peers-ctl logs — print the last N lines of a project's controller log,
non-streaming.

## SYNOPSIS
```
peers-ctl logs <name> [-n LINES | --lines LINES]
```

## DESCRIPTION
Resolves the log path for `<name>` via the registry (with safe-I/O
guards: refuses if the path leaves the controller's logs directory),
reads the file, and prints the last `LINES` lines. Useful when you
just want a post-mortem peek without committing to a streaming `tail`.

## OPTIONS
- `name` (positional, required) — registered project name.
- `-n LINES`, `--lines LINES` — number of trailing lines to print
  (default 50; must be positive).

## EXAMPLES
```
# Default 50 lines.
peers-ctl logs my-app

# Last 500 lines.
peers-ctl logs my-app -n 500
```

## FILES
- Reads: `$XDG_CONFIG_HOME/peers-ctl/logs/<name>.log` (or per-project
  path stored in the registry).

## ENVIRONMENT
- `XDG_CONFIG_HOME` — config root override.

## SEE ALSO
- `peers-ctl tail --help-man` — streaming follow-mode.
- `peers report --help-man` — structured per-project rollup.

## NOTES
- `--lines 0` (or negative) is rejected with exit 2.
- A missing log file (project never started) returns exit 1 with a
  "log not yet written" stderr message.
