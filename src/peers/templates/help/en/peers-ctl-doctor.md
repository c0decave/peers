# peers-ctl doctor — pre-flight host + project check

## NAME
peers-ctl doctor — verify the host has what `peers-ctl start` needs
(peers, git, peer CLIs, optionally podman + `peers:dev` image), then
load each registered project's config + goals and report status per
project.

## SYNOPSIS
```
peers-ctl doctor
```

## DESCRIPTION
Two-phase health check:

**Host toolchain.**
- `peers` and `git` must be on PATH (problems, exit 1).
- `claude` and `codex` are warned (not errored) if missing — they're
  only required by projects that actually use them, and may live
  inside the `peers:dev` container.
- `podman` is warned if missing (only needed for `--container`).
- If podman is present, also checks whether `peers:dev` is built;
  warns with a `make build` hint otherwise.

**Per-project.**
For each registered project: assert `.peers/config.yaml` +
`.peers/goals.yaml` exist, load them through the same validators
`peers run` uses, and print `[ok] / [FAIL]` plus a short summary
(peer + goal counts on success, error text on failure).

Exit 0 on a clean bill of health, 1 if any *problem* (not warning)
was found.

## OPTIONS
None.

## EXAMPLES
```
peers-ctl doctor
peers-ctl doctor || echo 'fix host setup before running peers-ctl start'
```

## FILES
- Reads: registry + each project's `.peers/config.yaml` +
  `.peers/goals.yaml`.

## ENVIRONMENT
- `PEERS_PROJECTS_ROOT` — shown in the projects-root line.
- `PODMAN_CMD` — what `doctor` probes when checking podman.

## SEE ALSO
- `peers info --help-man` — per-project config dump.
- `peers-ctl modes list` — sanity-check mode discovery.

## NOTES
- The codex check tries common VSCode extension paths as a fallback
  and prints a concrete "found at <path>; point config.yaml at it"
  hint if it locates a binary.
- Warnings don't change the exit code; only listed `Problems` do.
