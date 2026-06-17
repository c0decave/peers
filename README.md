# peers

**Two AI coding agents are better than one — if you make them prove it.**

peers drives **n ≥ 2** AI coding CLIs (Claude Code, Codex, …) as cooperating
peers that don't just *agree* a task is done — they have to clear **hard,
measurable gates** first: tests pass, coverage holds, no regression, no
TODO/stub/skipped-test, secrets clean. One peer implements, the **other
blind-reviews** (without seeing the first's notes), and an **adversarial
skeptic** re-audits before any "done" is accepted. Runs **unattended**,
**budget-capped**, and **container-sandboxed**.

**Why it beats a single agent on a loop:**

- **Gated, not vibes-based.** "Looks done" never converges — *gates green +
  skeptic-clean* does. No convergence theater.
- **Blind peer review catches rubber-stamping** — an independent second pair
  of eyes, by construction.
- **An adversarial skeptic hunts the edge cases** your tests miss.
- **Unattended & safe:** idle-timeout supervision, USD/tick budget caps,
  rootless cap-dropped container, egress allow-listing.

In an instrumented diagnostic, peers built an expression-language interpreter
both greenfield and brownfield to **0 defects over 50,000 random test
programs** — catching planted regressions and self-finding edge-case bugs the
acceptance suite never probed.

> Deutsche Version: [README_DE.md](README_DE.md).

- **HOWTO: full audit + fix on an existing app**: [docs/HOWTO-audit-and-fix.md](docs/HOWTO-audit-and-fix.md) — [deutsche Anleitung](docs/HOWTO-audit-and-fix_DE.md)
- **`implement` mode (build a feature from PLAN.md)**: [docs/MODES_IMPLEMENT.md](docs/MODES_IMPLEMENT.md) — [DE](docs/MODES_IMPLEMENT_DE.md)
- Security model: [docs/SECURITY.md](docs/SECURITY.md) — [DE](docs/SECURITY_DE.md)

## Quickstart (unattended, via the controller)

### Path A — start from a fresh project (one shot)

```sh
peers-ctl new mything --modes=audit --spec ./mything-spec.md
$EDITOR ~/c0de/peers-c0de/mything/.peers/goals.yaml   # trim project-specific gates
peers-ctl start mything --max-ticks 20 --max-usd 5
```

Available modes: see `peers-ctl modes list`. Stack multiple with
`--modes=audit,thorough`. Current built-in modes:

| Mode | What it does |
|---|---|
| `audit` | bug-hunt + 3-class test coverage + secrets + deps + API stability + regression + diff-size + skip/xfail justification |
| `thorough` | anti-convergence-theater hard gate: N=3 consecutive clean ticks + skeptic-pass + aggressive-honesty soft goals |
| `describe` | iterative doc-writing mode — peers write SPEC.md/ARCHITECTURE.md/DESIGN.md until N consecutive non-substantive doc commits. Use BEFORE audit on a repo that lacks docs; not composable with audit modes |
| `document` | generate + maintain machine-readable docs: a `CODEMAP.yaml` drift-gated against the parsed AST (every entry maps to a real symbol with a matching signature), plus `AGENTS.md` and `ARCHITECTURE.md` kept in sync with it. Docs that can't silently rot; stackable, or run standalone before an audit |
| `implement` | end-to-end feature implementation from a markdown PLAN.md — frozen acceptance contract, blind-review between peers, reviewer-only checkoffs, HONESTY_AUDIT + cleanliness gates (no TODO/FIXME/stubs/skipped tests at convergence). Standalone; see [docs/MODES_IMPLEMENT.md](docs/MODES_IMPLEMENT.md) |

Typical multi-mode runs:

```sh
# audit + thorough (recommended default for an existing codebase):
peers-ctl new myapp --modes=audit,thorough

# bare audit:
peers-ctl new myapp --modes=audit

# write docs first, audit later (two separate runs):
peers-ctl new myapp --modes=describe                   # run 1
peers-ctl new myapp-audit --modes=audit,thorough       # run 2

# generate verified, drift-gated docs (CODEMAP + AGENTS.md + ARCHITECTURE.md):
peers-ctl new myapp --modes=document

# implement a feature from a PLAN.md (standalone — not composable):
peers-ctl new myfeature --container --modes=implement --plan ./PLAN.md
# see docs/MODES_IMPLEMENT.md for the PLAN.md schema + escape valves.
```

**Automatic hooks** (opt-out flags):
- **`recon` pre-tick** (default on): substrate scans the repo once before tick 1 and writes `.peers/recon.md` (detected languages, key docs, entry-point candidates, top-level tree). Free + fast — no LLM call. Eliminates the "blind tick 1" penalty. Opt out: `peers-ctl start <name> --without-recon`.
- **`codemap` pre-tick** (default on): substrate builds a structural CODEMAP from the AST and writes `.peers/CODEMAP.yaml` (machine-readable: every public symbol, its `file:line` and signature) plus `.peers/codemap.md` (a compact, byte-capped digest peers read as context). Free + fast — no LLM call. Primes peers with the codebase's public-API shape before tick 1, on top of recon's file-level view. Opt out: `peers-ctl start <name> --no-codemap`.
- **`auto-skeptic` post-convergence** (default on): when `consecutive_clean_ticks >= N` would fire `convergence-reached`, the orchestrator runs ONE extra tick with a critical re-audit prompt. If the skeptic-tick stays clean → really terminal. If it surfaces a new blocking bug → counter resets, loop continues. Opt out: `peers-ctl start <name> --without-post-convergence-skeptic`.

`peers-ctl new`:
- creates the directory if missing (refuses to scaffold into a
  non-empty dir unless `--force`);
- **bare name** (no `/`) lands under `$PEERS_PROJECTS_ROOT`, default
  `~/c0de/peers-c0de/<name>`. Path with `/` is taken verbatim;
- `git init` + initial scaffold commit;
- ensures a top-level `README.md` exists, even when `--force` is used
  against an existing Git repo;
- copies the `--spec` argument to `SPEC.md` (existing file paths are
  read; path-looking missing values such as `./typo.md` are rejected);
- runs `peers init` (which writes `.peers/`, tags `peers-baseline`,
  commits `.gitignore`, and creates `.peers/log/runs.jsonl`);
- with `--modes=audit`, installs six audit check scripts and an
  audit-ready `goals.yaml`; use `--lang=js`, `--lang=rust`, or
  `--lang=go` for stack-specific check entrypoints;
- registers the project with `peers-ctl` and creates the controller log
  under the peers-ctl config directory.

To use a different projects root (e.g. on a project-specific
disk): `export PEERS_PROJECTS_ROOT=/work/peers/` once, then bare
names land there. `peers-ctl doctor` prints the active root.

### Path B — bring your own existing project (first audit)

```sh
cd /path/to/your-target-project
peers init                              # writes .peers/ + commits .gitignore
$EDITOR .peers/goals.yaml               # delete `placeholder-replace-me`, write real gates
python3 - <<'PY'
import hashlib, pathlib
p = pathlib.Path(".peers")
(p / "goals.sha256").write_text(hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n")
PY
$EDITOR .peers/config.yaml              # only if codex needs a custom argv path
peers info                              # sanity-check: peers, goals, budget, health

peers-ctl add /path/to/your-target-project --name mything
peers-ctl doctor                        # confirms tooling + per-project config

peers-ctl start mything --max-ticks 20 --max-usd 5
```

### Path C — re-audit an existing project with different modes

Modes are baked into `.peers/goals.yaml` at scaffold-time. To re-run
the SAME project with a DIFFERENT mode set (e.g. you ran `audit` first
and now want `audit,thorough` on top):

```sh
# Variant 1: re-init in place (DESTRUCTIVE — overwrites goals.yaml + checks)
peers-ctl new mything /path/to/your-project \
  --modes=audit,thorough --force
# Then start as usual:
peers-ctl start mything --container --max-ticks 30

# Variant 2: separate worktree (NON-DESTRUCTIVE, recommended)
git -C /path/to/your-project worktree add \
  /path/to/your-project-thorough HEAD
peers-ctl new mything-thorough /path/to/your-project-thorough \
  --container --modes=audit,thorough
peers-ctl start --container mything-thorough
# Cherry-pick the substantive fixes back to your main worktree when done.
```

**Variant 2 is the recommended pattern for iterative audits.** Each
run audits a worktree clone; fixes are cherry-picked back via merge
with `--no-ff` after review. The worktree pattern keeps your existing
audit history (`.peers/state.json`, `.peers/log/runs.jsonl`) intact.

### While it runs

```sh
peers-ctl status mything                # snapshot
peers-ctl dashboard                     # all registered projects at once
peers-ctl dashboard --live              # continuous redraw with alerts/events
peers-ctl dashboard --project mything   # drilldown: recent runs + bugs
peers-ctl tail mything                  # live tail (Ctrl-C to detach)
tail -f /path/to/your-target-project/.peers/log/runs.jsonl   # rich per-tick audit
peers -C /path/to/your-target-project replay 3               # inspect tick 3
```

### When it's done (or you want to stop)

```sh
peers-ctl stop mything                  # graceful SIGTERM → 10s → SIGKILL
peers -C /path/to/your-target-project report   # writes .peers/REPORT.md
peers-ctl report mything                # writes controller REPORT-mything.md
peers-ctl review mything                # latest handoff self-review
```

CI guardrails are available as `.gitea/workflows/test.yml` plus
`scripts/pre-push.sh`; install the local hook with `make hooks-install`.

The controller is stateless; the project's own `.peers/state.json`
and `runs.jsonl` are the durable record. If the host reboots
mid-run, `peers-ctl list` will mark the project `crashed`; you can
`start` it again and the loop resumes from the saved iteration.

**Project states shown by `peers-ctl list`:**

| State | Meaning |
|---|---|
| `fresh` | scaffolded by `peers-ctl new/add` but never started |
| `running` | active loop, container/PID alive |
| `stopped` | exited cleanly — wrote `.peers/last-stop-reason.txt` with `complete`, `max_ticks`, `max_iterations`, or `budget:*` reason. **A run that reached `convergence-reached` is `stopped`, not `crashed`.** |
| `crashed` | process died without a sentinel — segfault, OOM, halt-pattern, goal-mutation, host reboot mid-run |

---

## Modes — detailed reference

A **mode** is a reusable bundle of audit goals + check scripts that
`peers-ctl new --modes=…` lays down in `.peers/`. Modes are
**stackable** (comma-separated list) — except `describe`, which is
mutually exclusive with audit/security modes (it writes docs, not
audits code).

### `audit` (foundation — almost always required)

Hard gates: `self-review-on-handoff`, `tests-pass`,
`tests-cover-happy-edge-sad`, **`tests-no-unjustified-skip-or-fail`
(peers must justify every `@pytest.mark.skip`/`xfail`)**,
`lint-clean`, `type-clean`, `bug-hunt-clean`, `tdd-reproduces-bug`,
`no-secrets-committed`, `deps-justified`, `api-stable`,
`no-prior-regression`, `diff-size-per-resolve`.

Soft goals: `bug-hunt-round-1-deep`, `bug-hunt-round-2-cross-review`,
`tests-3-class-review`.

**Use it always.** Other modes assume `audit`'s hard-gates are active
and tighten what „clean" means.

### `thorough` (stacks ON TOP of audit)

Adds:
- `convergence-reached` (hard, N=3 default): N consecutive clean
  ticks without new crit/high/med bug-reports — the substrate refuses
  to declare success without N proofs of stillness.
- `all-peers-healthy` (hard): refuses to declare success while any
  peer is in `unavailable` state (halt-pattern hit).
- `skeptic-pass` (soft, both peers, interval 1): every tick re-audits
  with extra suspicion; refuses to pass without documenting 5+
  failure modes excluded per file/module.
- `aggressive-honesty` (soft, both peers, interval 3): per src
  top-level path: 3+ failure modes checked, 2+ security categories,
  1 test-coverage gap explicitly named.

**`thorough` alone (without `audit`) is incomplete** — `convergence-
reached` depends on `bug-hunt-clean` (from audit) to know what
„clean" means. Always stack with audit: `--modes=audit,thorough`.

### `describe` (write docs, don't audit)

Peers WRITE the project's spec docs (SPEC.md + ARCHITECTURE.md +
DESIGN.md) iteratively until N=2 consecutive non-substantive doc
commits. Hard gates:
- `description-files-present`: all 3 files exist, ≥500 chars each
- `description-sections-present`: SPEC has `## Threat Model` +
  `## Invariants` + `## API`; ARCH has `## Components` + `## Data
  Flow`; DESIGN has `## Decisions` + `## Tradeoffs`; each section
  body ≥50 chars
- `description-converged`: last N commits to the 3 files are non-
  substantive (no new `##` section, <100 lines added, <50% deletion)

**Not composable** with audit modes — describe writes, audit attacks.
Run `--modes=describe` FIRST on a repo that lacks docs, cherry-pick
the produced files into a follow-up `--modes=audit,…` run.

### `document` (generate + drift-gate machine-readable docs)

Peers build a verified, machine-readable **`CODEMAP.yaml`** of the
codebase, then keep **`AGENTS.md`** and **`ARCHITECTURE.md`** in sync
with it. Unlike `describe` (free-form prose), every artifact is gated
against the parsed AST, so the docs cannot silently rot. Hard gates:
- `codemap-grounded` / `codemap-signature-match` / `codemap-complete`:
  every CODEMAP entry maps to a real symbol, signatures match the parsed
  AST, and the public API is fully covered (no missing or phantom nodes)
- `codemap-summaries-complete`: every entry carries a human summary
- `agents-in-sync`: `AGENTS.md` matches the CODEMAP it derives from
- `architecture-grounded`: every anchor in `ARCHITECTURE.md` resolves to
  a real CODEMAP node

Soft goals: `summaries-cross-review` + `architecture-cross-review` — the
other peer reviews the generated prose for accuracy.

**Stackable**, but commonly run on its own to lay down docs:
`--modes=document`. A substrate-only structural CODEMAP also runs as a
free pre-tick step in every mode (opt out with `--no-codemap`).

### `implement` (build a feature from PLAN.md)

End-to-end feature implementation from a markdown PLAN.md.
**Standalone — not composable with audit/thorough/describe.**
See [docs/MODES_IMPLEMENT.md](docs/MODES_IMPLEMENT.md) for the
full operator reference: PLAN.md schema, frozen acceptance contracts,
reviewer-only checkoffs, escape valves (`[PARTIAL]` / `[BLOCKED]` /
`peers-ctl amend` / `peers-ctl ack-block`).

### Choosing modes — quick decision tree

| Project type | Recommended modes |
|---|---|
| First touch on undocumented repo | `--modes=describe` (alone, run-1) then `--modes=audit,thorough` (run-2) |
| Existing Python lib / CLI tool | `audit,thorough` |
| Want living, drift-gated docs (CODEMAP/AGENTS/ARCHITECTURE) | `--modes=document` |
| Implement a planned feature | `--modes=implement --plan ./PLAN.md` |

`peers-ctl modes list` always shows the current built-in set.

---

## CLI reference — `peers` and `peers-ctl`

Two CLIs:

- **`peers`** runs the loop INSIDE one repo. The inner driver.
- **`peers-ctl`** registers + supervises one or more peers projects
  from outside. The outer controller. Spawns `peers run` (host or
  container) and tracks PID/container liveness.

### Common `peers-ctl` operations

```sh
# Lifecycle
peers-ctl modes list                       # available modes
peers-ctl new <name> [path] --modes=…      # scaffold + register
peers-ctl add <path> --name <n>            # register an EXISTING .peers/
peers-ctl start [<name>] --container       # start (--container = podman)
peers-ctl status [<name>]                  # one or all
peers-ctl stop [<name>] [--grace-s 10]     # SIGTERM → wait → SIGKILL
peers-ctl remove <name>                    # unregister (does NOT delete .peers/)
peers-ctl list                             # all projects + state

# Observe
peers-ctl dashboard                        # rollup across all projects
peers-ctl dashboard --live --refresh-s 1   # live rollup with alerts/events
peers-ctl dashboard --project <name>        # recent runs + bug drilldown
peers-ctl tail [<name>]                    # follow controller log
peers-ctl logs <name> [-n 100]             # print last N lines
peers-ctl report [<name>]                  # write controller REPORT-<n>.md
peers-ctl review <name>                    # latest handoff's self-review block

# Maintenance
peers-ctl doctor                           # pre-flight: peers + git + peer CLIs + image
peers-ctl prune <name>                     # delete old per-project log files
```

### `peers-ctl tui` — live cockpit

```sh
pip install -e .[tui]                      # one-time: install the optional TUI extra
peers-ctl tui                              # launch the host-side live cockpit
```

A dark, state-colored master-detail "mission control" for a peers fleet: start
projects, watch the agents work, read what they say and how they mutually check
each other, and see the gates / steps / tasks-done, the bugs they find, and the
diffs they produce — plus a forward-looking view of the agentic-os autonomy
layer.

- **Optional extra.** The TUI is a Textual UI shipped behind the optional
  `[tui]` extra (`pip install -e .[tui]` adds Textual + textual-window) so the
  core install stays `pyyaml`-only. Running `peers-ctl tui` without the extra
  prints a friendly install hint and exits — it never crashes.
- **Read-only over the signals; acts via the substrate.** The cockpit only
  *reads* the file-based signals (`projects.yaml`, per-run state, git
  trailers/attestation, `bugs.jsonl`, `runs.jsonl`, the spine ledger). Every
  *action* shells out to the existing `peers-ctl` verbs, so the substrate's
  guards and hash-chains stay authoritative — the TUI reimplements no write
  logic, never writes into `.peers/`, and adds no new trust surface. CONVERGED /
  gate / integrity verdicts are always **re-derived** from the substrate, never
  trusting the agent-writable stored `independence` flag.
- **Windows.** A Fleet sidebar plus movable / resizable / toggleable + pop-out
  windows — Peers, Gates (with a history scrubber: step `[` / `]` through past
  ticks with absolute + relative time), Tasks/Steps, Live-Stream, Tick-Verlauf,
  Budget, Bugs, Konsens/Attestation (with a forgery badge), Log, Diff — plus
  forward-looking autonomy windows (Autonomie-Ledger, Spine-Gates,
  Propagations-DAG, Autonomie-Feed, Eskalations-Banner) that render an honest
  empty-state until the spine is wired to an operator-launchable mode.
- **Acting safely.** A doctor-gated, off-thread launch wizard creates + starts
  projects; intervention modals (stop / resume / ack-block / amend) show the
  exact verb and use type-to-confirm for contract-touching ops.
- **Keys + layout.** vim + arrows + letters (`?` for the in-app help); layout
  persists to `~/.config/peers-ctl/tui-layout.json`.
  Full design: [docs/plans/2026-06-11-peers-tui-design.md](docs/plans/2026-06-11-peers-tui-design.md).

#### Observability knobs (host-side; all additive + fail-closed)

The TUI is fed by three substrate additions, all opt-in-safe and backward
compatible:

- **Live tee — opt-in, default-off.** Set `observability.tee_stream: true` in
  `.peers/config.yaml` (or `PEERS_TEE_STREAM=1`) to mirror each peer's live
  stdout to a tail-able `.peers/log/peers/tick-<N>-<peer>.stream.jsonl`, so
  **codex / opencode are watchable live** in the Live-Stream window just like
  claude (which is always live via its session jsonl). A normal launch with the
  knob off is byte-identical; a tee error can never disturb the loop or
  liveness (fail-closed), and the stream files are log-rotated like the other
  per-tick logs.
- **Per-tick `gates` snapshot — always-on, backward-compatible.** Each
  `runs.jsonl` tick line now carries a compact `gates` map (gate-id → state,
  soft-consensus n/m). It powers the Gates window's **history scrubber** (what
  the gates stood at a past tick + when it happened). Every existing
  `runs.jsonl` reader ignores the extra key.
- **`.peers/spine-runs/<mode_run>.json` registry — observability-only.**
  Written fail-closed by the spine's `worktree.lease()` so spine mode-runs are
  host-discoverable; the autonomy windows light up once the spine becomes
  operator-runnable. Prune re-derives liveness at reap time (never reaps a live
  record).

### Common `peers` operations (inside a target repo)

```sh
peers -C /path/to/target init              # write .peers/
peers -C /path/to/target run               # start the loop in current shell
peers -C /path/to/target run --max-ticks 5 # cap ticks
peers -C /path/to/target run --max-usd 1   # cap budget (API-key billing only)
peers -C /path/to/target status            # iteration / next peer / lock
peers -C /path/to/target info              # config + goals snapshot
peers -C /path/to/target verify            # one-shot goal evaluation
peers -C /path/to/target report            # write .peers/REPORT.md
peers -C /path/to/target replay <iter>     # reconstruct any past tick
peers -C /path/to/target tick --after claude  # hooks-driver: trigger after a peer
peers -C /path/to/target watch             # follow runs.jsonl
```

### Opt-out flags (defaults are on)

```sh
peers-ctl start <name> --without-recon
# Skip the substrate-only pre-tick recon step (no LLM call, free).
# Only opt out if .peers/recon.md was hand-prepared.

peers-ctl start <name> --no-codemap
# Skip the substrate-only pre-tick structural CODEMAP step (no LLM call, free).

peers-ctl start <name> --without-post-convergence-skeptic
# Skip the auto-skeptic re-audit tick that fires when consecutive_clean_
# ticks ≥ N would declare terminal. Default on for higher confidence;
# opt out for CI runs where false-convergence is acceptable.

peers-ctl start <name> --max-ticks 50 --max-usd 1
# Same flags work on both peers-ctl and `peers run` directly.
```

`peers run --help` and `peers-ctl start --help-man` show the full
flag set with descriptions.

### Config-file options (`.peers/config.yaml`)

A few capabilities are opt-in via the project's `.peers/config.yaml` (the
generated file is annotated; the highlights):

- `graphify_mcp: true` — give the peers an opt-in, supply-chain-caged code
  knowledge graph they query over MCP instead of `grep` (callers /
  blast-radius / shortest-path / "who uses X / how does A reach B"), so code
  navigation is cheaper and more precise. Off by default; **fail-open** (any
  failure just continues with no graph, byte-identical to off). Needs
  `podman` + the `graphify-sandbox` image; `PEERS_CTL_NO_GRAPHIFY=1` forces
  it off fleet-wide. In `--container` runs it shares the egress/auth-proxy
  network at a private loopback port.
- `egress_allow: ['^host\.example$', ...]` — extra hosts the `--container`
  peers may reach (tinyproxy host-regexes appended to the egress allow-list,
  on top of the LLM API hosts), e.g. to let a peer fetch a spec or a research
  source. Off by default (no extra egress); anchor each pattern.

---

## Troubleshooting

### `peers-ctl start` fails with `pasta: Failed to open() /dev/net/tun`

Rootless podman's default networking needs the `tun` kernel module.
Bypass with host networking:

```sh
PEERS_CTL_PODMAN_NETWORK=host peers-ctl start --container <name>
```

For permanent: `echo 'export PEERS_CTL_PODMAN_NETWORK=host' >>
~/.bashrc`, then `source ~/.bashrc`. Alternatively load the module:
`sudo modprobe tun` (persist via `/etc/modules-load.d/tun.conf`).

### Project shows `crashed` after convergence-complete

The orchestrator writes `.peers/last-stop-reason.txt` and reconcile
maps clean reasons to `stopped`. If you still see `crashed`
post-convergence:
1. `cat .peers/last-stop-reason.txt` — should contain `complete <ts>`.
2. `make build` to ensure the container image matches the host code.

### tick 1 process-fail or idle-timeout

- `process-fail` after ~4min usually = peer CLI returned 5xx
  (Anthropic Overloaded, Codex rate-limit) and idle-timeout kicked.
  Run produced no commit. Next tick retries the OTHER peer; the
  problematic peer auto-recovers if rate-limit was transient.
- `idle-timeout` after exactly `health.idle_timeout_s` (default
  900s) = peer wrote stdout below the silence threshold for too long.
  Increase `idle_timeout_s` in `.peers/config.yaml` for heavy DA
  mode runs (peer spends more time thinking before each commit).

### `peer-unavailable:<name>` exit_event

A halt-class pattern matched (`authentication failed`, `quota
exhausted`, `invalid API key`, `usage limit` per
`templates/config.yaml`). Operator action required:
1. Re-login or top-up the OAuth account
2. Restart: `peers-ctl start <name> --container`
3. The loop resumes from the saved iteration

This is intentional — the substrate refuses to silently degrade
peers on operator-action failures.

### `peers-ctl list` shows `fresh` instead of `stopped`

`fresh` means the project was registered but NEVER started. After
the first successful `peers-ctl start`, state moves to `running`,
then `stopped`/`crashed` on exit. If you intended to start it:
`peers-ctl start <name> --container`.

---

## Container-mode (`--container`)

If codex (or any other peer CLI) isn't on the host but is available
in the `peers:dev` image, run the loop inside the container:

```sh
make build                              # one-time main image
make proxy-build                        # egress sidecar
make auth-proxy-build                   # Claude OAuth sidecar
peers-ctl doctor                        # confirms podman + image exist
peers-ctl start mything --container --max-ticks 20 --max-usd 5
```

This spawns `podman run -d --rm --name ... --userns=keep-id ... peers:dev run …`
and tracks the running container by name via `podman ps`. The displayed
PID is only the host-side `podman logs -f` streamer. `peers-ctl stop
--grace-s N` uses `podman stop -t N`, then reaps the log streamer.

Container mode bind-mounts the target repo, `~/.claude`, `~/.codex`,
and optional read-only `~/.gitconfig`. When `~/.claude.json` exists,
it is mounted into the per-project `peers-auth-proxy_<name>` sidecar
instead of the workspace container; the workspace talks to
`ANTHROPIC_BASE_URL=http://127.0.0.1:8080`.
Before launch, `peers-ctl` compares the host package version with
`peers --version` inside the image: minor/patch drift warns, major
drift refuses start until you rebuild (`make build`).

Override the image name with `PEERS_CTL_IMAGE=name:tag` if you've
tagged your build differently.

## Install (local development)

```sh
pip install -e .[dev]
pytest          # the full suite should pass
```

## Single project — drive one repo

```sh
cd /path/to/your-project
peers init
$EDITOR .peers/goals.yaml            # delete the placeholder, write your gates
python3 - <<'PY'
import hashlib, pathlib
p = pathlib.Path(".peers")
(p / "goals.sha256").write_text(hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n")
PY
peers run --max-ticks 20
peers status
tail -f .peers/log/runs.jsonl        # rich per-tick audit log
peers replay <iter>                  # reconstruct any iteration
```

`peers init` writes `.peers/` into the target, tags the current HEAD
as `peers-baseline` (rollback anchor), snapshots the goals hash
(`goals.sha256`), and adds `.peers/` to the target's `.gitignore`.
If you edit `.peers/goals.yaml` manually before starting a run, refresh
`goals.sha256`; the loop intentionally halts on unacknowledged goal
changes or if `goals.yaml` disappears mid-run.

### Selecting a driver

```sh
peers init --driver=hooks            # scaffold Stop-hook snippets
peers init --driver=hooks --install  # ALSO merge into your host config (with backup)
peers tmux up                        # sessions driver: tmux up/down/attach
```

`--driver=hooks` drops ready-to-paste fragments in `.peers/hooks/`
for your `~/.claude/settings.json` and `~/.codex/config.toml`.

`--install` (only valid with `--driver=hooks`) goes one step further:
it merges the Stop-hook entry directly into your host configs and
writes timestamped backups (`settings.json.bak.peers-<ts>`,
`config.toml.bak.peers-<ts>`). Behavior:

- **idempotent** — re-running prints `noop` and does not duplicate
  entries. Each entry is tagged with `# peers:<absolute-target-path>`
  so the installer recognises its own work.
- **drift-aware** — if the target path changed (e.g. the project
  moved), the existing entry is rewritten in place and the old file
  is backed up.
- **conservative on TOML** — if your `~/.codex/config.toml` already
  has a non-peers `[hooks]` section with an `on_stop`, the installer
  refuses to touch it and prints a notice (codex has no general TOML
  merge logic in stdlib; we will not clobber a custom config).
- **Independent failure** — patching claude vs codex is independent.
  Whichever side succeeded is reported on stdout; the other is
  reported on stderr with the path of the snippet you can merge
  manually.

Smoke-test after install:

```sh
peers status                         # nothing yet (no run)
peers tick                           # one manual tick — should run cleanly
```

## Multiple projects — `peers-ctl`

`peers-ctl` is a host-side controller that supervises many peers loops
without a daemon. Each project is a detached background process; the
controller stores PIDs (with a `/proc`-based starttime fingerprint to
guard against PID recycle) under `~/.config/peers-ctl/`.

```sh
peers-ctl doctor                     # pre-flight: peers/git/peer-CLIs + per-project config sanity
peers-ctl add  /path/to/project-a   --name a
peers-ctl add  /path/to/project-b   --name b
peers-ctl list

peers-ctl start a --max-ticks 20 --max-usd 3
peers-ctl status a
peers-ctl tail a                     # follow log via tail -f
peers-ctl report a                   # write Markdown controller report
peers-ctl review a                   # show latest handoff self-review
peers-ctl stop a                     # graceful: SIGTERM -> 10s grace -> SIGKILL; state.json persisted
peers-ctl prune                      # delete old log files
```

`peers-ctl report` writes a clean Markdown summary to
`~/.config/peers-ctl/REPORT.md` (or `REPORT-<name>.md` when scoped to
one project). The report includes controller log paths, per-project
tick counts, blocking bug counts, last activity, and README status so a
handoff can spot missing operator docs before the next run.
`peers-ctl dashboard` is the fast terminal view: state, ticks, open
hard/soft goals, blocking bug count, running container name, and last
tick timestamp for every registered project. Add `--live` for a
periodic redraw that also shows alert state and the newest decoded
Claude session event when available. Add `--project <name>` for a
single-project drilldown with recent runs and bug reports; combine it
with `--live` to redraw that detail view.

Example `peers-ctl doctor` output:

```
peers-ctl doctor — 3 project(s) registered, config dir ~/.config/peers-ctl

  [ok] snake                ~/code/snake
           2 peer(s), 5 goal(s)
  [ok] cpu-emu              /tmp/peers-dogfood-r2/cpu-emu
           2 peer(s), 8 goal(s)
  [FAIL] freshproject       ~/code/freshproject
           missing ~/code/freshproject/.peers/config.yaml

Warnings:
  - `codex` is not on PATH. If any project uses it, either add it to PATH
    or set the full path in that project's .peers/config.yaml.
```

`doctor` surfaces three classes of problem up front: missing tooling,
missing or unparseable per-project config, and per-project ambiguity
(unknown peer name, no goals, etc.). Use it before kicking off a
long autonomous run.

## n-peer configurations

`config.yaml` accepts an ordered `peers:` list. The substrate is
neutral about names; pick what you want.

```yaml
peers:
  - name: claude
    tool: claude
    model: opus        # optional; omit to use CLI default
    reasoning: high    # claude: low|medium|high|xhigh|max
    argv: ["claude", "-p", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute

  - name: codex
    tool: codex
    model: gpt-5.1-codex-max
    reasoning: xhigh   # codex: minimal|low|medium|high|xhigh
    provider: openai   # openai|openrouter
    argv: ["codex", "exec", "{PROMPT}"]
    prompt_mode: argv-substitute

  # Third peer is fine — anything in [A-Za-z0-9][A-Za-z0-9_-]{0,31}:
  - name: claude-2
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute
```

The legacy `tools: {claude: …, codex: …}` mapping is still loaded for
back-compat and auto-promoted to the new shape.

`model`, `reasoning`, and `provider` are optional convenience fields.
Explicit `argv` switches still win. To scaffold them without editing
YAML:

```sh
peers-ctl new myapp --modes=audit \
  --peer-model claude=opus \
  --peer-provider codex=openrouter \
  --peer-model codex=~openai/gpt-latest \
  --peer-reasoning codex=xhigh
```

For OpenRouter, export `OPENROUTER_API_KEY` before `peers run`,
`peers tick`, `peers tmux up`, or `peers-ctl start`; these commands fail
early if the key is missing. Container mode passes the key name through
and opens only `openrouter.ai` in the egress proxy allow-list for projects
that opt in.

### opencode peers + local models (ollama / vllm / llama.cpp)

`opencode` is a first-class tool alongside `claude` and `codex`. Run it with
`--format json` so the substrate gets the same structured channel it uses for
the others — token + USD accounting (from `step-finish` events) and
echo-immune auth/quota halt detection (from `error` events):

```yaml
peers:
  - name: opencode
    tool: opencode
    model: ollama/qwen2.5      # opencode's <provider>/<model> (NOT a separate provider:)
    reasoning: high            # → --variant high
    argv: ["opencode", "run", "--format", "json", "--dangerously-skip-permissions", "{PROMPT}"]
    prompt_mode: argv-substitute
```

opencode is also the simplest path to **local models**. It is a universal
gateway: configure the backend once in opencode's own config
(`opencode providers`, or an `opencode.json` custom provider) — ollama, vllm,
llama.cpp, LM Studio, or any OpenAI-compatible `/v1` endpoint — then point a
peer's `model` at `<provider>/<model>`:

```yaml
    model: ollama/qwen2.5            # local via ollama
    model: openai-compatible/<name> # local vllm / llama.cpp server
    model: anthropic/claude-...      # cloud, routed through opencode
```

The substrate needs no local-model-specific config; opencode resolves the
provider. Notes:

- `provider:` is **not** used for opencode — encode the provider in `model`
  (`provider/model`). Setting `provider:` on an opencode peer is rejected.
- Billing for opencode is treated as **warn**, never a hard `max_usd` kill
  (local = free, opencode-hosted = subscription, BYOK cloud = metered — the
  tool name alone can't tell which, so the conservative default applies).
- `codex` can also reach local models, but only `ollama`/`lmstudio` via
  `codex exec --oss --local-provider …`, or a custom provider that speaks the
  OpenAI **Responses** API (`wire_api=responses`) — codex dropped chat-API
  support, so chat-only servers (llama.cpp, vanilla ollama OpenAI-compat) go
  through opencode instead.

## Reviewer modes (soft goals)

Soft goals get one of these `reviewer:` modes:

- `other` — any non-active peer can submit a review on their turn.
- `both` — every peer must submit `consensus_needed` pass:true reviews.
- `alternating` — review duty rotates one slot per recorded review.
- `quorum` — together with `quorum: "N/M"`, pass when ≥N of the
  most recent M reviews were pass:true.

## Container (Podman)

```sh
make build
make init-target TARGET=/path/to/your-target
make run         TARGET=/path/to/your-target
make status      TARGET=/path/to/your-target
```

On some hosts the default `pasta` network backend fails with
`/dev/net/tun: No such device`; `make build` therefore uses
`BUILD_NETWORK=host` by default. Use `make run NETWORK=host TARGET=...`
to bypass runtime networking issues too. Plain `podman` works without
the Makefile:

```sh
podman build --network=host -f Containerfile -t peers:dev .
podman run --rm -it --userns=keep-id --cap-drop=ALL \
    --security-opt=no-new-privileges \
    -v $PWD:/work \
    -v $HOME/.claude:~/.claude \
    -v $HOME/.codex:~/.codex \
    peers:dev run
```

`podman compose` works too (see `compose.yaml`) but its
`docker-compose` provider needs the podman daemon socket.

Host-side requirement: `podman`, `git`, `python3`. The container
brings its own Node.js and the Claude/Codex CLIs.

## What the controller protects against

The `peers-ctl` flow is the recommended way to run unattended:

- **PID-recycle defence.** Each start records the process's
  kernel-issued starttime via `/proc/<pid>/stat`; `stop` verifies it
  matches before signalling, so a recycled PID owned by an unrelated
  process is never killed.
- **Graceful stop.** `peers-ctl stop` sends SIGTERM, which routes
  inside the loop into the substrate's KeyboardInterrupt path (state
  persisted, run.lock released) before falling through to SIGKILL.
- **Lock status clarity.** `run.lock` is intentionally left on disk
  after unlock so all contenders use the same inode; `peers status`
  probes `flock` and distinguishes an active lock from a stale file.
- **Pre-flight check.** `peers-ctl doctor` flags missing tooling and
  per-project misconfiguration in one shot — no surprises 20
  minutes into a run.
- **Crash detection.** `peers-ctl reconcile` (run automatically by
  `list`/`status`/`start`) sees that a recorded PID is dead, marks
  the project `crashed`, and clears the PID so a fresh `start` is
  unambiguous.
- **No daemon.** Each project's loop is a setsid'd background
  process. `peers-ctl` is a stateless CLI; the registry on disk is
  the source of truth, accessed under `fcntl.flock` so concurrent
  invocations serialise their mutations.

## Pick the right `idle_timeout_s`

The substrate's health model is **output-driven**: a peer is "stuck"
when its child process has written nothing to stdout/stderr for
`idle_timeout_s` seconds. This works great for chatty peers
(codex by default streams progress) but **claude in `-p` (print)
mode is silent until the response is ready**. A claude tick that
sets up a non-trivial project from scratch can take 5–20+ minutes
of silent thought before any output appears.

Rule of thumb:

| Task scale | `idle_timeout_s` |
|---|---|
| Small fixes / single-file edits | 600 (10 min) |
| Multi-file feature work | 1800 (30 min) |
| From-scratch project scaffolding | 3600 (60 min) |
| Heavy refactors of large codebases | 5400 (90 min) |

If you see runs.jsonl entries with `classification: idle-timeout`,
your value is too low. Edit `.peers/config.yaml`:

```yaml
health:
  idle_timeout_s: 3600
```

`absolute_max_runtime_s` is a separate paranoid ceiling — set it
larger than `idle_timeout_s` (e.g. 2× to 4×).

## Enable `max_usd` budget tracking with claude

`claude -p` in its default text-output mode is silent about token
usage, so `budget.max_usd` and `budget.max_tokens` are effectively
off — the substrate sees `(tokens, usd) = (0, 0)` after every tick.

Fix: switch claude to JSON output. The substrate auto-detects the
envelope and pulls `usage.input_tokens + cache_creation +
cache_read + output_tokens` and `total_cost_usd`.

Edit `.peers/config.yaml` once:

```yaml
peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "json", "{PROMPT}"]
    prompt_mode: argv-substitute
```

For incremental output (so a long tick is not silent and `idle_timeout_s`
sees progress) use `stream-json`:

```yaml
    argv: ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose", "{PROMPT}"]
```

### `max_usd_mode` — OAuth vs API-key billing

`claude` (Claude Code) and `codex` (ChatGPT-bundled) authenticate via
**OAuth → flat subscription**. Their `total_cost_usd` field reports
the *API-equivalent* price; the user pays $0 incrementally. A *hard*
budget cap is meaningless there — it kills a perfectly-paid run.

`max_usd_mode` controls the policy:

| mode  | behavior                                                       |
|-------|-----------------------------------------------------------------|
| `auto` (default) | inspect `~/.claude/.credentials.json` + `~/.codex/auth.json` (`auth_mode`). All peers OAuth → `warn`; any peer using an API key → `hard`. |
| `hard` | exit on cap (pre-Phase-3i behavior). Use this if you set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| `warn` | log a one-time warning at the threshold; do NOT exit. |
| `off`  | ignore `max_usd` entirely. |

`peers info` shows the *resolved* mode and the reason it picked, e.g.:

```
budget:  iterations≤20, runtime≤10800s, USD≤$25.0
  max_usd_mode=warn (auto: all peers OAuth-billed)
```

## Bug-hunt protocol

Every `peers init` ships five default goals plus the intentional
`placeholder-replace-me` hard fail. The default set forces self-review
and mutual bug-hunting before claiming convergence:

| Gate | Type | Pass when |
|---|---|---|
| `self-review-on-handoff` | hard | every handoff commit has `## Self-Review` and `Self-Review: pass` |
| `bug-hunt-clean` | hard | zero unresolved bugs at severity `crit`/`high`/`med` |
| `bug-hunt-round-1` | soft (`consensus_needed: 2`) | each peer says "round 1 done" |
| `bug-hunt-round-2` | soft (`consensus_needed: 2`) | each peer says "round 2 done" after round-1 fixes landed |
| `test-coverage-3-class` | soft (`consensus_needed: 2`) | each peer reviewed the other's tests for happy/edge/sad coverage |

A peer files a bug as a standalone commit:

```
BUG-007: null deref in parser

## Bug-Report
{"id":"BUG-007","severity":"high","fix_by":"codex",
 "location":"src/parser.py:42",
 "description":"Crashes on empty input; expected: return None."}

Peer: claude
Bug-Report: BUG-007
```

The `fix_by` peer resolves it with another commit:

```
Resolve BUG-007

## Bug-Resolution
{"resolves":"BUG-007","status":"fixed","note":"guarded with if not s: return"}

Peer: codex
Bug-Resolves: BUG-007
```

Inspect anytime:

```sh
python3 -m peers.bug_hunt summary           # human rollup
python3 -m peers.bug_hunt gate /path/to/repo  # exit 0 iff clean
peers verify                                # re-runs every hard gate, includes bug-hunt-clean
```

Severity ladder: `crit` (data loss / RCE) > `high` (broken feature)
> `med` (degraded UX) > `low` (nit) > `info` (note). Only the top
three block completion. A `wontfix` resolution keeps the bug in the
counter — use only with the other peer's agreement.

The full protocol (when to file vs fix, severity guidance, what NOT to
bug-report) ships in the per-tick prompt as `BUG_HUNT_BLOCK`; peers
see it on every turn.

## `api-error` diagnostics

When a peer process exits with `classification: "api-error"`, the
`runs.jsonl` entry includes:

```json
"matched_error_pattern": "Authentication failed",
"matched_error_snippet": "Authentication failed: token expired ..."
```

so you can see *which* `health.error_patterns` regex fired without
grepping the raw container log. Any non-success tick also records
`stderr_tail` and `stdout_tail`; soft-review ticks include
`soft_reviews_seen`, `soft_reviews_ingested`, and
`soft_reviews_rejected`.

The substrate's handoff detection reads git commits, not claude's
stdout content, so the format change is safe — only your
per-tick `runs.jsonl` console snippet becomes JSON instead of plain
text. `peers report` summarizes that for you.

codex emits its own `tokens used` line by default; no config change
needed there.

## `peers verify` — re-run the gates without a peer

After `peers run` completes (or on any later check-out of the finished
project) you can re-run every hard goal against the current files,
without spinning up any peer process:

```sh
peers verify           # exits 0 iff every gate passes; writes .peers/VERIFY.md
```

Use it to:

- Confirm `tests-pass`, `ruff-clean`, `smoke-import` (and whatever
  else is in `goals.yaml`) on a different machine.
- Validate a hand-edit didn't break a gate.
- Smoke-test a UI build with `verify.commands`:

```yaml
# .peers/config.yaml
verify:
  timeout_s: 60
  commands:
    - name: cli-help
      cmd: "PYTHONPATH=src python -m mything --help"
    - name: ui-screenshot
      cmd: "xvfb-run -a python tools/screenshot.py out.png"
      timeout_s: 30
```

`peers verify` uses `goals.timeout_s` for hard goals unless
`verify.timeout_s` overrides it. `verify.commands` exit code 0 = pass;
non-zero or timeout = fail.
Combined hard-goals + verify.commands result is rendered as a markdown
table at `.peers/VERIFY.md`.

## What the substrate guarantees

- **State durability.** `state.json` is atomically written tmp+fsync+rename
  with a parent-directory fsync, and v1 → v2 schema migration writes a
  `state.json.pre-migration` backup once.
- **Self-review on handoff.** The `self-review-on-handoff` hard gate
  ships on every `peers init`. Every handoff commit must include a
  `## Self-Review` body section and `Self-Review: pass` trailer. The
  default gate runs the trusted package checker, not a mutable
  project-local copy.
- **Anti-cheating hard-block.** A turn that modifies only test files
  is reverted (`git revert --no-commit` + commit), success is demoted
  to fail, the peer keeps the turn, and the warning lands in the next
  prompt. Two reverts in a row mark the peer `degraded`.
- **Sandboxed `pass_when` DSL.** `regex(...)` and `json('path')` are
  available; `json()` is restricted to relative paths inside the target
  repo, refuses symlinks/hardlinks via the safe readers, and has a
  2 MiB read cap. `stdout`/`stderr` exposed to the DSL are capped at
  1 MiB, string literals and regex patterns are bounded, and `regex()`
  has a timeout.
- **Goal-mutation lock.** `goals.yaml`'s sha256 is verified before
  every tick using no-follow reads; in-loop changes halt the loop with
  a clear reason, and deletion of `goals.yaml` is treated as mutation.
- **Control-plane file hardening.** State, logs, reports, verify output,
  controller registry files, and controller logs refuse symlinks,
  non-regular files, and hardlinks. Log appends open the parent
  directory with no-follow semantics to block late parent-symlink swaps.
  State, goals, project config, and controller registry reads are
  size-capped before JSON/YAML parsing; `health.error_patterns` also has
  count and per-pattern size limits before regex compilation.
- **PID-recycle defence.** `peers-ctl` records each loop's
  `/proc/<pid>/stat` starttime and refuses to signal a PID whose
  fingerprint no longer matches.
- **File-channel race-safe.** Hybrid-comm `send()` uses temp-file +
  atomic link publication so consumers never see partial messages, and
  avoids two concurrent senders colliding on the same NNNN.
- **Audit trail.** `runs.jsonl` records `soft_fail_reason`, tokens
  & USD per tick, head_before/after, peer_state_after,
  warnings_emitted, and the `truncated` flag from HealthGuard.
  `peers init` creates the file up front, and `peers-ctl add/new`
  creates the controller-side log up front, so there is always a
  stable place to write or inspect run evidence.

## Project layout

```
src/
├── peers/                  # the substrate
│   ├── cli.py              # peers init / run / status / tick / replay / watch / tmux
│   ├── driver_orchestrator.py      # public facade
│   ├── _driver_orchestrator_impl.py # thin runtime coordinator
│   ├── driver_*.py          # decomposed lifecycle / observability / health hooks
│   ├── state_store.py      # schema v2 + v1 migration
│   ├── turn_manager.py     # round-robin over n peers
│   ├── goal_engine.py
│   ├── goals.py            # YAML loader + pass_when DSL
│   ├── peer_spec.py        # PeerSpec + load_peer_specs
│   ├── comm_layer.py       # GitCommLayer + HybridCommLayer
│   ├── health_guard.py     # streaming reader + idle-timeout + truncation
│   ├── prompt_builder.py
│   └── templates/
├── peers_ctl/              # the controller
    ├── cli.py              # add / remove / list / start / stop / status / review / logs / tail / prune
    ├── store.py            # registry on disk, fcntl-locked
    └── runner.py           # detached spawn + PID-recycle defence
└── auth_proxy/             # OAuth sidecar server

tests/
├── unit/                   # unit tests
└── integration/            # smoke + adversarial peer fixtures
```

## Further reading

- [docs/HOWTO-audit-and-fix.md](docs/HOWTO-audit-and-fix.md) — end-to-end recipe to audit + fix an existing application
- [docs/MODES_IMPLEMENT.md](docs/MODES_IMPLEMENT.md) — `implement` mode operator reference
- [docs/SECURITY.md](docs/SECURITY.md) — threat model + per-layer mitigations
