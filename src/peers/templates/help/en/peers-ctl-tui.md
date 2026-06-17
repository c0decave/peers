# peers-ctl tui — host-side live cockpit

## NAME
peers-ctl tui — a dark, state-colored "mission control" terminal UI for a
peers fleet: start projects, watch the agents work, read what they say and
how they mutually check each other, and see gates / steps / tasks, bugs,
diffs, budget, consensus/attestation — plus a forward-looking view of the
agentic-os autonomy layer.

## SYNOPSIS
```
pip install -e .[tui]      # one-time: install the optional TUI extra
peers-ctl tui
```

## DESCRIPTION
A read-only, master-detail cockpit over the file-based signals a fleet
already writes (`projects.yaml`, per-run state, git trailers/attestation,
`bugs.jsonl`, `runs.jsonl`, the spine ledger).

**Optional extra.** The TUI is a Textual UI shipped behind the optional
`[tui]` extra (`pip install -e .[tui]` adds Textual + textual-window) so the
core install stays `pyyaml`-only. Running `peers-ctl tui` without the extra
prints a friendly install hint and exits cleanly — it never crashes.

**Read-only; acts only via the substrate.** The cockpit only *reads* the
signals. Every *action* (start/stop/resume, ack-block, amend, launch a new
project) shells out to the existing `peers-ctl` verbs, so the substrate's
guards and hash-chains stay authoritative — the TUI reimplements no write
logic and never writes into `.peers/`.

**Layout.** A Fleet sidebar plus movable / resizable / toggleable and
pop-out windows: Peers, Gates (with a history scrubber that steps through
past ticks), Tasks/Steps, Live-Stream, Tick-Verlauf, Budget, Bugs,
Konsens/Attestation (with a forgery badge), Log, and Diff — plus the
forward-looking autonomy windows (Autonomie-Ledger, Spine-Gates,
Propagations-DAG, Autonomie-Feed, Eskalations-Banner), which render an
honest empty-state until the spine is wired to an operator-launchable mode.

**Honest re-derivation.** CONVERGED / gate / integrity verdicts are always
RE-DERIVED from the substrate and never trust the agent-writable stored
`independence` flag.

**Launch wizard + interventions.** A doctor-gated, off-thread wizard
creates and starts projects. Intervention modals show the exact verb, then
run it; contract-touching ops (amend, ack-block) require type-to-confirm.

## OPTIONS
None (the cockpit is launched bare). Use `--help-man` for this page.

## EXAMPLES
```
pip install -e .[tui]
peers-ctl tui
# Step the Gates window through past ticks with [ and ]; press ? for help.
```

## FILES
- Reads (per project): `projects.yaml`, per-run state, `runs.jsonl`
  (now carrying a per-tick `gates` snapshot), `bugs.jsonl`, git
  trailers / `refs/notes/peers-attest`, and `.peers/spine-runs/*.json`.
- Live-Stream tails the per-peer `.peers/log/peers/tick-<N>-<peer>.stream.jsonl`
  files when the `observability.tee_stream` knob is on.
- Writes ONLY its own layout: `~/.config/peers-ctl/tui-layout.json`
  (or `$XDG_CONFIG_HOME/peers-ctl/tui-layout.json`).

## ENVIRONMENT
- `XDG_CONFIG_HOME` — base for the persisted `tui-layout.json`.
- `PEERS_TEE_STREAM=1` — enable the live tee (equivalent to setting
  `observability.tee_stream: true` in `.peers/config.yaml`) so codex /
  opencode are watchable live in the Live-Stream window (claude is always
  live via its session jsonl). Default off; fail-closed.
- `PEERS_PROJECTS_ROOT` — the projects registry root the cockpit reads.

## SEE ALSO
- `peers-ctl dashboard --help-man` — the non-interactive rollup.
- `peers-ctl start --help-man` / `peers-ctl new --help-man` — the write
  verbs the cockpit shells out to.
- `peers-ctl doctor --help-man` — the pre-flight the launch wizard gates on.

## NOTES
- Keys are vim + arrows + letters; press `?` for the in-app help screen.
  `[` / `]` step the Gates history scrubber; `o` / `space` pops a panel
  into a floating window, `x` closes it, `f1` switches floating windows.
- The three supporting observability changes are all additive and
  fail-closed: the live tee is default-off (a normal launch is
  byte-identical); the per-tick `gates` snapshot in `runs.jsonl` is
  backward-compatible (existing readers ignore the extra key); and the
  `.peers/spine-runs/<mode_run>.json` registry is observability-only.
