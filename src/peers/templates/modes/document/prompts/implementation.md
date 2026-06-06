# Document mode — build a verified CODEMAP

You are documenting this codebase by producing a **machine-readable, drift-gated
`CODEMAP.yaml`** at the repo root: one entry per public symbol, each with a
truthful one-line `summary`. The gates make the docs provably code-anchored —
this is not prose generation, it is verified documentation.

## What the substrate already did for you

Before tick 1 the substrate **seeded `CODEMAP.yaml`** with the full structural
skeleton — every public symbol with its correct `id`, `kind`, `file`, `line`,
and `signature`, derived directly from the AST. Those fields are **already
correct**. Each entry's `summary` is **empty** — filling them in is the job.

(If present, `.peers/CODEMAP.yaml` and `.peers/codemap.md` hold the same
structure as a read-only reference; the deliverable you edit and commit is
always the repo-root `CODEMAP.yaml`.)

## Your task each tick

Take ONE concrete step toward a fully-summarized CODEMAP:

1. Open `CODEMAP.yaml`, find entries whose `summary` is empty/missing.
2. For each, **read the real symbol at its `file:line`** and write a `summary`
   that is truthful, specific, and useful — *what it does and why it exists*.
3. Commit the change (handoff commit with `## Self-Review` + `Self-Review: pass`).

## Rules (the gates enforce these)

- **NEVER change `id` / `kind` / `file` / `line` / `signature`.** They are
  correct; editing them is *drift* and the `grounded` / `signature-match` /
  `complete` gates will fail the run. You only add/edit `summary`.
- **Every** public symbol must end up with a summary — the `complete` gate
  forbids dropping entries, and `summaries-complete` forbids leaving any empty.
- A summary must be **substantive**: no `TODO`/`TBD`/placeholders, not a
  restatement of the signature ("`run(state)` — runs with state"), not a weasel
  tautology true of any function. Say what is specific to *this* symbol.

## Reviewing your peer (soft goal `summaries-cross-review`)

Read the OTHER peer's summaries against the real code and reject any that are
inaccurate, vacuous, or just paraphrase the name/signature. Accuracy is the
whole point — a plausible-but-wrong summary is worse than none.

## Generate AGENTS.md (once the CODEMAP is complete)

AGENTS.md is a **deterministic render** of the verified CODEMAP — you do NOT
write it by hand. Once every entry has a summary, run **`peers agents-doc`** to
(re)generate `AGENTS.md` from `CODEMAP.yaml`, then commit it. The
`agents-in-sync` gate fails if AGENTS.md is missing or has drifted, so re-run
`peers agents-doc` after any later CODEMAP change.

## Write ARCHITECTURE.md (the human architecture guide)

The substrate seeded `ARCHITECTURE.md` with a 5-section outline (*Overview ·
Subsystem map · Data & control flow · Run lifecycle · Invariants*) and a
checklist of subsystems. Replace the outline with a real narrative — the
*connective* story AGENTS.md's flat list can't tell: how the subsystems fit,
the data/control flow, a run's lifecycle, the load-bearing invariants.

- **Anchor every factual claim** to a CODEMAP id with `[[id]]`, e.g. *"the tick
  loop `[[peers.tick_loop.TickLoop.run]]` drives each turn"*. The
  `architecture-grounded` gate fails on any anchor that does not resolve.
- **Cover every subsystem**: anchor at least one symbol from each public
  top-level module (the seed's checklist). A silently-omitted subsystem fails
  the gate.
- **Remove every placeholder.** The seed's `<!-- TODO -->` markers must be gone.
- This is prose you write — there is no generator. Accuracy is reviewed by your
  peer (`architecture-cross-review`): anchors resolving is not the same as the
  story being *true*.

## Done when

All six hard gates are green — `grounded`, `signature-match`, `complete`,
`summaries-complete`, `agents-in-sync`, `architecture-grounded` — both
cross-reviews reach consensus, and the skeptic re-audit stays clean. At that
point `CODEMAP.yaml` is a complete, verified, drift-proof map of the public
surface, `AGENTS.md` is its in-sync agent-facing rendering, and
`ARCHITECTURE.md` is a code-anchored human guide to how it all fits together.
