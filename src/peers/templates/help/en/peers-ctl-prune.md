# peers-ctl prune — delete old log files

## NAME
peers-ctl prune — remove controller-managed log files older than a
configurable threshold.

## SYNOPSIS
```
peers-ctl prune [--older-than-days N]
```

## DESCRIPTION
Walks the controller's `logs/` directory under `$XDG_CONFIG_HOME/peers-ctl/`
and unlinks any log file whose mtime is older than N days. Prints the
total number of files reaped. Reconciles the registry first so the
state column reflects reality.

Per-project `.peers/log/runs.jsonl` files inside each project are NOT
touched — `prune` only cleans the controller-side spill.

## OPTIONS
- `--older-than-days N` — threshold in days; default 7. Must be
  positive (the underlying helper raises ValueError on 0 or negative).

## EXAMPLES
```
# Default: anything older than a week.
peers-ctl prune

# More aggressive: only keep the last 24h.
peers-ctl prune --older-than-days 1
```

## FILES
- Removes: matching files under `$XDG_CONFIG_HOME/peers-ctl/logs/`.

## ENVIRONMENT
- `XDG_CONFIG_HOME` — config root override.

## SEE ALSO
- `peers-ctl logs --help-man`
- `peers-ctl tail --help-man`

## NOTES
- Active project logs (those of currently running loops) are NOT
  pruned regardless of age — the file is open by the running
  process and the registry knows about it.
- Run this from cron or systemd-timer if you want automatic
  housekeeping; the call is safe to run repeatedly.
