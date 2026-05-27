# peers tmux — tmux session wrappers for the sessions-driver

## NAME
peers tmux — create / kill / attach a tmux session that hosts one
window per peer plus a tiled watcher window for the comms inboxes.

## SYNOPSIS
```
peers [-C <dir>] tmux up
peers [-C <dir>] tmux down
peers [-C <dir>] tmux attach
```

## DESCRIPTION
For the sessions-driver, each peer runs as a long-lived process inside
its own tmux window (running e.g. `claude --continue` or `codex
resume`). The substrate doesn't tick them; messages flow through
`.peers/comms/`. The `peers tmux` subcommand wraps the tmux session
lifecycle:

- `up` creates session `peers-<basename>` with one window per peer
  (running the "continue an existing session" command), plus a
  `watch` window with tiled panes — one `peers watch <peer>` per
  receiver.
- `down` kills the session.
- `attach` runs `tmux attach -t peers-<basename>`.

`up` refuses if a session with the same name already exists; tear it
down first.

## OPTIONS
None on the parent. Each subcommand (`up`/`down`/`attach`) takes none
itself.

## EXAMPLES
```
peers tmux up         # create the session
peers tmux attach     # attach to it
peers tmux down       # tear it down
```

## FILES
- Reads: `.peers/config.yaml` to enumerate peers.
- The tmux session is purely in-memory; nothing under `.peers/` is
  written.

## ENVIRONMENT
- Requires `tmux` on PATH.
- Honours tmux's own env (`TMUX`, `TMUX_TMPDIR`, ...).

## SEE ALSO
- `peers watch --help-man` — the per-inbox tailer used in the
  `watch` window.
- `peers init --driver=sessions` — selects this driver at init time.

## NOTES
- Per-peer "continue" command falls back to a login shell for tools
  the substrate doesn't recognise (anything other than claude/codex).
  You can invoke the tool by hand inside that pane.
- Quote-safe: the target path is shlex-quoted before being embedded
  in tmux command strings.
