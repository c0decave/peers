# peers watch — tail comms inboxes for a given receiver

## NAME
peers watch — poll `.peers/comms/<from>-to-<receiver>/` for new
messages and stream them to stdout. Used by the sessions-driver
where each peer is a long-lived tmux session.

## SYNOPSIS
```
peers [-C <dir>] watch <receiver> [--poll-s SECONDS]
```

## DESCRIPTION
For the sessions-driver, each peer runs in a long-lived process (e.g.
`claude --continue`) and the substrate doesn't spawn it per tick.
Inter-peer messages still arrive as files under
`.peers/comms/<from>-to-<receiver>/`. `peers watch` is a small
filesystem poller that prints new files as they appear, so a tmux pane
running `peers watch claude` shows whatever codex (or any other peer)
sends.

Files are matched against the `[0-9][0-9][0-9][0-9]-*.md` pattern
(four-digit sequence prefix). Already-seen files are remembered for
the lifetime of the process.

Runs forever; interrupt with Ctrl-C (returns exit 0).

## OPTIONS
- `receiver` (positional, required) — peer name to watch the inbox
  FOR (e.g. `claude` to see `codex-to-claude/` traffic).
- `--poll-s SECONDS` — filesystem poll interval; default 1.0 s.

## EXAMPLES
```
# In one tmux pane:
peers watch claude

# In another:
peers watch codex --poll-s 0.5
```

## FILES
- Reads: `.peers/comms/*-to-<receiver>/[0-9][0-9][0-9][0-9]-*.md`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers tmux up --help-man` — bring up a sessions-driver layout
  with watcher panes pre-wired.
- `peers run --help-man` — orchestrator-driver alternative (no
  long-lived sessions).

## NOTES
- Each message is capped at 64 KiB in the printed output; longer
  files are truncated with a `--- truncated ---` marker.
- The receiver name is validated; invalid names (containing path
  separators etc.) are rejected with exit code 2.
