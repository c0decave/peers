# peers init — bootstrap a .peers/ control plane

## NAME
peers init — initialize a `.peers/` directory in a target project so
the loop can be started against it.

## SYNOPSIS
```
peers [-C <dir>] init [--force] [--driver {orchestrator,hooks,sessions}]
                      [--install] [--modes <list>] [--lang <lang>]
                      [--peer-model VALUE] [--peer-reasoning VALUE]
                      [--peer-provider VALUE]
```

## DESCRIPTION
`peers init` lays down a fresh control plane in `<target>/.peers/`:
`config.yaml` (peer roster + health/budget), `goals.yaml` (gates),
an empty `log/runs.jsonl`, and `checks/verify_self_review.py`. The
target's `.gitignore` is extended with `.peers/` if missing, the
current HEAD is tagged `peers-baseline` (rollback anchor), and a
`goals.sha256` snapshot is taken so future ticks can detect goal
file mutation.

When `--modes=<a,b,c>` is passed, the named modes are resolved (with
conflict/cycle detection) BEFORE any file is written. On success, the
default `goals.yaml` is overwritten with the merged-modes content,
their check scripts are copied into `.peers/checks/`, and a
`modes-applied.txt` audit trail is written.

`--driver=hooks` additionally writes ready-to-paste Stop-hook snippets
under `.peers/hooks/` for claude (`settings.json`) and codex
(`config.toml`). With `--install` the snippets are merged directly
into the user's host config with timestamped backups.

The command refuses to operate on `/` or `$HOME`, refuses to follow
symlinks named `.peers/` or `.gitignore`, and refuses to overwrite an
existing `.peers/` without `--force`.

## OPTIONS
- `--force` — overwrite an existing `.peers/` directory.
- `--driver {orchestrator,hooks,sessions}` — default `orchestrator`.
  `hooks` scaffolds Stop-hook snippets; `sessions` selects the tmux
  long-running session driver.
- `--install` — (with `--driver=hooks`) merge Stop-hooks directly into
  `~/.claude/settings.json` and `~/.codex/config.toml`.
- `--modes <list>` — comma-separated mode names (`audit`, `security`,
  `thorough`, ...). See `peers-ctl modes list`.
- `--lang <lang>` — `python` (default), `js`, `rust`, `go`. Controls
  which language-specific check scripts the audit mode installs.
- `--audit-templates` — DEPRECATED alias for `--modes=audit`.
- `--peer-model VALUE` — set `model` in generated config. Repeatable.
  `VALUE` applies to all peers; `NAME=VALUE` or `TOOL=VALUE` targets.
- `--peer-reasoning VALUE` — set `reasoning` in generated config.
- `--peer-provider VALUE` — set `provider` (`anthropic`, `openai`,
  `openrouter`) in generated config.

## EXAMPLES
```
# Default scaffold in the current directory.
peers init

# Audit + security modes for a JavaScript project.
peers -C ./my-app init --modes=audit,security --lang=js

# Re-init after editing modes (overwrites .peers/).
peers init --force --modes=audit,thorough

# Hook-driven mode with auto-install into host config.
peers init --driver=hooks --install

# Pin Codex to OpenRouter at scaffold time.
peers init --peer-provider codex=openrouter \
           --peer-model codex=~openai/gpt-latest \
           --peer-reasoning codex=xhigh
```

## FILES
Created under `<target>/.peers/`:
- `config.yaml` — peer roster, comm channel, health, budget.
- `goals.yaml` — hard + soft gates (merged-modes content when applicable).
- `checks/verify_self_review.py` — default `self-review-on-handoff` gate.
- `checks/*.py` — mode-supplied check scripts.
- `log/runs.jsonl` — empty, ready for the first tick.
- `goals.sha256` — anti-tamper snapshot.
- `modes-applied.txt` — audit trail (only with `--modes`).
- `hooks/` — Stop-hook snippets (only with `--driver=hooks`).

Also touched:
- `<target>/.gitignore` — `.peers/` entry added + committed if needed.
- Git tag `peers-baseline` on the current HEAD.

## ENVIRONMENT
- `PEERS_MODES_DIR` — extra directory scanned by `peers.modes.discover()`
  when resolving `--modes` (in addition to bundled + `~/.config/peers/modes/`).
- `OPENROUTER_API_KEY` — required at runtime when a peer has
  `provider: openrouter`.
- `HOME` — used to refuse `init` against `$HOME`.

## SEE ALSO
- `peers run --help-man`
- `peers-ctl modes list` / `peers-ctl modes show <name>`
- `docs/HOWTO-audit-and-fix.md` — full audit workflow.

## NOTES
- The `peers-baseline` tag is only set if `<target>` is a git repo with
  at least one commit. Otherwise a notice is printed; consider
  `git init && git commit --allow-empty` before `peers init`.
- The `.gitignore` mutation is committed with trailer
  `Peer: peers-init` so `dirty_worktree` detection isn't tripped on
  tick 0.
