# peers-ctl start — start a project loop

## NAME
peers-ctl start — spawn `peers run` for a registered project in the
background, optionally inside the `peers:dev` container.

## SYNOPSIS
```
peers-ctl start <name> [--max-ticks N] [--max-usd USD] [--container]
```

## DESCRIPTION
Resolves `<name>` against the registry, spawns `peers run` (with the
flags below threaded through) detached from the controller, records
the PID + log path in the registry, and prints a one-line
confirmation. The loop runs until it terminates on its own (goals
complete, budget exhausted, max ticks hit, halted) or until
`peers-ctl stop <name>`.

`--container` runs the loop inside a `peers:dev` podman container,
mounting:
- the project directory at `/work`,
- `~/.claude/` for the claude CLI's auth state,
- `~/.codex/` for the codex CLI's auth state.

The container path is preferred when the host doesn't have the peer
CLIs (claude/codex) installed but the image does.

stdout/stderr from the loop is teed into a controller-owned log file
under the config dir; tail it with `peers-ctl tail <name>`.

## OPTIONS
- `name` (positional, required) — registered project name.
- `--max-ticks N` — cap tick count for this run.
- `--max-usd USD` — override `budget.max_usd` for this run.
- `--container` — run inside the `peers:dev` container instead of
  on the host.

## EXAMPLES
```
# Host run with safety caps.
peers-ctl start my-app --max-ticks 50 --max-usd 5

# Container run with podman host networking.
PEERS_CTL_PODMAN_NETWORK=host \
  peers-ctl start my-app --container --max-ticks 100 --max-usd 50

# Open-ended (no cap on iterations) — be sure about your budgets.
peers-ctl start my-app --max-usd 20
```

## FILES
- Reads: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.
- Writes: registry entry (PID, log path) + opens the log file under
  the config dir's `logs/` subdir.

## ENVIRONMENT
- `PEERS_CTL_PODMAN_NETWORK` — podman network mode (`host`, `none`, ...).
- `PODMAN_CMD` — override `podman` binary.
- Peer-specific env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`) is passed through. In container mode,
  `OPENROUTER_API_KEY` is passed by name when a project uses
  `provider: openrouter` unless Codex config specifies a custom
  `model_providers.openrouter.env_key`; start fails early if the
  required key is missing.

## SEE ALSO
- `peers-ctl stop --help-man` — clean shutdown.
- `peers-ctl tail --help-man` — follow the log.
- `peers run --help-man` — what `start` is actually invoking.

## NOTES
- The image's major version must match the host `peers` package's
  major version, or container start refuses with a `make build` hint.
- For the OAuth subscription peers (claude / codex CLI), the
  per-token `--max-usd` is informational; an explicit hard-cap mode
  must be set in `config.yaml`.
