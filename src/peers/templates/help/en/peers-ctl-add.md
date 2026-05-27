# peers-ctl add — register an existing project

## NAME
peers-ctl add — register an existing directory as a peers-ctl project
so it can be addressed by short name afterwards.

## SYNOPSIS
```
peers-ctl add <path> [--name NAME]
```

## DESCRIPTION
Resolves `<path>` (bare name → `$PEERS_PROJECTS_ROOT/<name>`; full
path → verbatim), validates that the resulting directory exists,
checks the name against the project-name policy, and writes the entry
into the registry under `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

The path is NOT modified — `peers init` is NOT run. If
`<path>/.peers/config.yaml` is missing, a warning is printed telling
the operator to run `peers -C <path> init` before
`peers-ctl start <name>`.

For one-shot "scaffold + register" use `peers-ctl new` instead.

## OPTIONS
- `path` (positional, required) — directory to register.
- `--name NAME` — override registry name (default: directory basename).

## EXAMPLES
```
# Bare name → ~/c0de/peers-c0de/existing-app/
peers-ctl add existing-app

# Full path verbatim.
peers-ctl add /opt/work/big-project --name big

# Add then init manually.
peers-ctl add ~/Code/old-project
peers -C ~/Code/old-project init --modes=audit
```

## FILES
- Writes: `$XDG_CONFIG_HOME/peers-ctl/projects.json`.

## ENVIRONMENT
- `PEERS_PROJECTS_ROOT` — base for bare-name resolution.
- `XDG_CONFIG_HOME` — registry location override.

## SEE ALSO
- `peers-ctl new --help-man` — scaffold + init + register in one go.
- `peers init --help-man` — bootstrap a `.peers/` plane manually.
- `peers-ctl remove --help-man` — unregister.

## NOTES
- Project names follow the same validation as `peers-ctl new` (no
  path separators; safe for filenames). Invalid names exit 2.
- Re-adding the same name is rejected — `peers-ctl remove` first.
