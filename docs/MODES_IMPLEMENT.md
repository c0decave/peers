# `implement` mode — feature implementation from PLAN.md

**Languages:** EN / [DE](MODES_IMPLEMENT_DE.md)

End-to-end feature implementation driven from a markdown PLAN.md, with
reviewer-only check-offs, frozen acceptance contracts, blind-review
between peers, mandatory pessimism quota, a final HONESTY_AUDIT, and
cleanliness gates that refuse TODO/FIXME/stubs/skipped tests at
convergence. Standalone in v1 — **not composable** with
`audit`/`security`/`thorough`.

---

## TL;DR

```sh
peers-ctl new myfeature --container --modes=implement --plan ./PLAN.md
peers-ctl start myfeature --container --max-runtime 12h
peers-ctl tail myfeature                    # follow the run
peers-ctl ack-block myfeature STEP-7 --reason "API key not provisioned"
peers-ctl amend  myfeature --acceptance "pytest -k new_path" \
                            --reason "scope expanded; old acceptance subsumed"
```

`--plan FILE` is **required** with `--modes=implement` and is mutually
exclusive with `--spec`. The plan is validated, the declared acceptance
command is run as a preflight (it MUST currently fail — that's the
whole point), and PLAN.md / acceptance.sh / optional e2e.sh are frozen
under `.peers/` with sha256 pins.

---

## PLAN.md schema reference

PLAN.md is a tiny line-oriented markdown dialect (parsed by
`src/peers_ctl/plan_parser.py`). The parser is strict: anything
ambiguous raises `PlanValidationError` at `peers-ctl new` time, before
a single tick runs.

Required top-level sections: `## Meta` and `## Steps`. Optional:
`## Architecture`, `## Input Domains`.

### Full example

```markdown
# Feature: user login with refresh tokens

## Meta
surfaces: [cli, web]
acceptance: pytest tests/acceptance/test_login.py
e2e: playwright test e2e/login.spec.ts
convergence_n: 5
mutation_testing: false
honesty_audit_peer: gemini      # optional 3rd-peer honesty check

## Architecture
- AuthMiddleware: session cookie + refresh-token rotation
- SessionStore: redis-backed, 24h TTL, per-user revocation
- LoginRoute: POST /api/login, returns session + refresh in body

## Input Domains
- username: ascii [a-z0-9_-]{3,32}
- password: utf-8, 8..256 chars
- refresh_token: opaque uuid4

## Steps
- [ ] [STEP-1] Add SessionStore (in-memory backend)
  - touches: src/session/store.py, tests/test_session_store.py
  - rationale: STEP-2 + STEP-3 both depend on it
- [ ] [STEP-2] Add AuthMiddleware
  - touches: src/middleware/auth.py, tests/test_auth_middleware.py
  - depends: [STEP-1]
- [ ] [STEP-3] Add POST /api/login route
  - touches: src/routes/login.py, tests/test_login_route.py
  - depends: [STEP-1, STEP-2]
- [ ] [STEP-4] Wire e2e happy-path
  - touches: e2e/login.spec.ts
  - depends: [STEP-3]
  - trivial_step: true
```

### Meta keys

| Key | Required | Type | Notes |
|---|---|---|---|
| `surfaces` | yes | `[a, b, c]` list | `web` or `gui` triggers required `e2e:` |
| `acceptance` | yes | shell command | frozen as `.peers/contracts/acceptance.sh` |
| `e2e` | conditional | shell command | required iff `surfaces` includes `web`/`gui` |
| `convergence_n` | no (default 5) | int | N consecutive clean ticks to converge |
| `mutation_testing` | no (default false) | bool | opt into Phase-8.4 `mutation-sample` soft gate |
| `honesty_audit_peer` | no | peer name | force a 3rd peer (e.g. `gemini`) into HONESTY_AUDIT.md |
| `confidence_calibration` | no (default false) | bool | opt into per-step `confidence: N/5` annotation |

### Step lines

```text
- [<MARK>] [STEP-N] <free text>
  - touches: file1, file2, ...
  - depends: [STEP-X, STEP-Y]
  - rationale: <prose>
  - trivial_step: true        # exempt from min-impl-size warning
  - pure_refactor: true       # exempt from test-to-code-ratio warning
  - confidence: 4/5           # only when confidence_calibration: true
```

`<MARK>` is one of:

| Marker | Meaning |
|---|---|
| `[ ]` | open — not yet implemented |
| `[x]` / `[X]` | done — must carry a trailing `(SHA)` annotation and be checked off by the OTHER peer |
| `[PARTIAL]` | in-flight — fails `plan-checklist-empty` so it cannot converge |
| `[BLOCKED]` | hit an external blocker — fails `no-unresolved-blocks` until operator runs `peers-ctl ack-block` |
| `[BLOCKED-ACK]` | operator-acknowledged block (set by `ack-block`); passes `no-unresolved-blocks` |

`[STEP-N]` IDs must be **1-indexed and sequential** (`STEP-1`,
`STEP-2`, …). Gaps fail validation. `depends:` references must point at
declared step ids and must not form a cycle (Kahn's algorithm is run at
parse time).

A done step's `text` may carry a trailing `(commit-sha)` annotation,
e.g. `[x] [STEP-2] AuthMiddleware (a1b2c3d)`. The SHA is checked by
`plan-step-traceable` against `git log` and (if the step declared
`touches:`) against the commit's changed files.

---

## Worked example — a tiny feature, start to finish

**Goal:** add a `--count` flag to a `wordcount` CLI tool.

### 1. Write PLAN.md

```markdown
# Feature: --count flag for wordcount

## Meta
surfaces: [cli]
acceptance: pytest tests/test_count_flag.py -v

## Steps
- [ ] [STEP-1] Parse --count from argv
  - touches: src/wordcount/cli.py, tests/test_count_flag.py
- [ ] [STEP-2] Wire counter into output path
  - touches: src/wordcount/core.py, tests/test_count_flag.py
  - depends: [STEP-1]
- [ ] [STEP-3] Document --count in README
  - touches: README.md
  - depends: [STEP-2]
  - trivial_step: true
```

### 2. Scaffold + start

```sh
peers-ctl new wordcount-count --container \
  --modes=implement --plan ./wordcount-count-plan.md

# new validates the plan, runs the acceptance command (must fail —
# it does, because tests/test_count_flag.py does not exist yet),
# freezes acceptance.sh + PLAN.original.md, registers the project.

peers-ctl start wordcount-count --container --max-runtime 6h
peers-ctl tail wordcount-count
```

### 3. What the loop does

- **Tick 0 — recon**  → writes `RECON.md` (module map, conventions, deps).
- **Tick 1 — alignment** → writes `PLAN.aligned.md` (steps annotated against existing code).
- **Tick 2 — architecture intent** → writes `ARCHITECTURE.intended.md`; substrate freezes it (sha pin).
- **Ticks 3+ — implementer / reviewer alternation:**
  - Implementer commits code + updates `IMPLEMENTATION_NOTES.md` (does **not** read REVIEW_NOTES.md).
  - Reviewer reads the diff alone, writes `REVIEW_NOTES.md`, then compares to IMPLEMENTATION_NOTES.md and flips matching steps to `[x] (sha)` in PLAN.md. Any divergence → `[BLIND-REVIEW-MISMATCH]` entry in `CONCERNS.md`.
  - Pre-commit hook (`pre-commit-reviewer-checkoff`) blocks the implementer from checking off their own steps. `checkoff-by-other-peer` is the post-hoc backup gate.
- **Convergence tick** → both peers write three H3 subsections each in `HONESTY_AUDIT.md` (weakest part, likely uncaught bug, skipped/shortcut). All hard gates must pass; then `delivery-report-complete` requires a `DELIVERY.md` mapping every STEP-N to commit + tests + justification.

### 4. DELIVERY.md (expected output)

```markdown
# Delivery — wordcount --count flag

## [STEP-1] Parse --count from argv
- **Commit:** a1b2c3d
- **Tests:** tests/test_count_flag.py::test_count_short, ::test_count_long
- **Justification:** argparse subparser; happy/edge/sad covered (no flag, --count, --count=N).

## [STEP-2] Wire counter into output path
- **Commit:** d4e5f6a
- **Tests:** tests/test_count_flag.py::test_count_changes_output
- **Justification:** core.count() returns int; cli prints `count: N` line; reviewer confirmed both formats.

## [STEP-3] Document --count in README
- **Commit:** 7890abc
- **Tests:** N/A
- **Justification:** docs-only diff; trivial_step: true documented in PLAN.md.
```

Allowed Commit values: a hex SHA, `PENDING` (in-flight), or `BLOCKED`
(see escape valves). `plan-step-traceable` enforces SHA validity
separately; this gate only enforces structure.

---

## Honesty mechanisms

The mode's central bet is that **two peers reading the same diff
independently catch what one peer self-rationalising cannot**. Five
mechanisms hold the loop honest:

1. **Frozen contracts** (`src/peers_ctl/contracts.py`). PLAN.md is
   snapshotted to `.peers/PLAN.original.md` (mode 0444), acceptance.sh
   and optional e2e.sh are wrapped + frozen, and sha256 pins land in
   `.peers/contracts.sha`. The `contracts-unchanged` gate verifies
   them every tick. Tampering is detected immediately; legitimate
   changes go through `peers-ctl amend` which appends a hash-chained
   entry to `.peers/contracts.log`.

2. **Blind review** (`blind-review` gate +
   `prompts/blind_review_*.md`). The implementer writes
   `IMPLEMENTATION_NOTES.md`; the reviewer reads the **diff alone**
   and writes `REVIEW_NOTES.md` without peeking. Divergence between
   the two descriptions is filed as a `[BLIND-REVIEW-MISMATCH]` in
   CONCERNS.md and fails the gate.

3. **Reviewer-only checkoffs** (`pre-commit-reviewer-checkoff` hook +
   `checkoff-by-other-peer` gate). The git hook rejects commits where
   the same peer that wrote code is now flipping that step's box to
   `[x]`. The post-hoc gate catches violations from before the hook
   was installed.

4. **Mandatory pessimism** (`concerns-resolved` gate). `CONCERNS.md`
   is a structured pessimism log; in convergence mode an **empty**
   CONCERNS.md fails the gate ("zero filed concerns across a multi-tick
   run is rubber-stamping, not perfection"). Open or malformed status
   always fails.

5. **HONESTY_AUDIT** (`honesty-audit` gate). Final convergence anchor:
   every participating peer must write three H3 subsections — "Weakest
   part", "Likely uncaught bug", "Skipped or shortcut" — each with > 5
   words of substantive prose. Structural-only enforcement (we can't
   detect dishonesty from the filesystem), but the question itself
   resists boilerplate.

Plus the **cleanliness gates** (Schicht 5): `no-shortcut-markers`
(forbids TODO / FIXME / XXX / HACK / PLACEHOLDER / STUB and concrete
`NotImplementedError`), `no-empty-bodies` (AST-scan for pass/.../docstring-only
bodies), `no-skipped-tests` (forbids pytest.mark.skip / xit and
friends). Legitimate escapes require **both** a code-side
`# JUSTIFIED:` / `# SKIP-REASON:` annotation **and** a reviewer-signed
entry in `.peers/justifications.log` (hash-chained — see
`src/peers_ctl/justifications.py`).

---

## Escape valves cookbook

The loop will hit edge cases. Five escape valves keep it from grinding
forever or silently dropping scope:

### `[PARTIAL]` — in-flight on a single step

When a tick lands a checkpoint commit but the step isn't done yet:

```markdown
- [PARTIAL] [STEP-2] AuthMiddleware
  - touches: src/middleware/auth.py
  - rationale: rotation logic landed; revocation pending in next tick
```

`plan-checklist-empty` fails until the marker flips to `[x]` (or
`[BLOCKED]`). Use sparingly — the goal is steps that fit in one tick.

### `[BLOCKED]` — external blocker mid-run

When the loop cannot finish a step on its own (missing API key,
upstream dep unreleased, external service down):

```markdown
- [BLOCKED] [STEP-7] Wire OAuth callback
  - touches: src/auth/oauth.py
  - rationale: requires GOOGLE_OAUTH_CLIENT_ID secret; not in env
```

`no-unresolved-blocks` fails as long as the marker is bare `[BLOCKED]`.
Operator action required:

```sh
peers-ctl ack-block myfeature STEP-7 \
  --reason "OAuth integration deferred to next sprint; mock backend covers happy path"
```

`ack-block` validates the step is currently `[BLOCKED]`, rewrites the
PLAN.md marker to `[BLOCKED-ACK]`, and appends a hash-chained entry
to `.peers/blocks.log`. After acknowledgement,
`no-unresolved-blocks` passes.

### `peers-ctl amend` — legitimate acceptance change

The frozen acceptance command is intentionally tamper-evident; the
gate fires the moment its sha changes. When scope legitimately grows
or the original acceptance was wrong:

```sh
peers-ctl amend myfeature \
  --acceptance "pytest tests/acceptance/ -v" \
  --reason "scope expanded to include refresh-token rotation; old single-file acceptance subsumed"
```

This re-writes `.peers/contracts/acceptance.sh`, updates the sha pin,
and appends a hash-chained `<chain16> <iso8601> amend acceptance: ... | reason: ...`
line to `.peers/contracts.log`. The audit trail is preserved; the
gate goes green on the next tick.

### `# JUSTIFIED:` + signed entry — keep a TODO/FIXME

Some shortcuts are deliberate (deferred to a future PR, gated on
external work, performance tradeoff). Two-key escape:

1. Code-side annotation on the offending line:

   ```python
   def stub_for_now() -> None:  # JUSTIFIED: blocked on STEP-7 OAuth wiring
       raise NotImplementedError
   ```

2. Reviewer-signed entry in `.peers/justifications.log` (one line per
   `file:line`, hash-chained). The reviewer peer (or operator) appends
   via `peers_ctl.justifications.append_justification(...)`.

`no-shortcut-markers` only passes when **both** halves are present
for every match.

### `# SKIP-REASON:` — keep a `@pytest.mark.skip`

Same two-key principle, for tests:

```python
# SKIP-REASON: requires GPU runner; covered by tests/integration/test_gpu.py
@pytest.mark.skipif(not has_gpu(), reason="needs CUDA")
def test_kernel_fusion() -> None:
    ...
```

Plus a signed entry in `.peers/justifications.log` for that line.
`no-skipped-tests` honours both halves.

---

## FAQ

### When should I use `implement` vs `audit`?

| Situation | Mode |
|---|---|
| Greenfield feature with a clear PLAN.md | **`implement`** |
| Existing codebase you want hardened | `audit` (+`thorough`, +`security-*`) |
| Existing codebase that lacks docs | `describe` first, then `audit` |
| Implementing AND then auditing the result | run `implement`, converge, then re-init the same repo with `--modes=audit,thorough --force` |

`implement` is **not** composable with the audit/security/thorough
stack in v1 — its tick rhythm (Phase 0 ticks 0-2, then
implementer/reviewer alternation) and gate set are different. Plan one
project per mode; cherry-pick or fold artefacts across runs by hand.

### Budget tips

- Default `max_runtime_s` is **12h** (vs audit's 6h) — implement-mode
  has more surface per tick (commit → PLAN round-trip, frozen
  contracts, per-step coverage delta).
- Tune via `peers-ctl start ... --max-runtime 24h` to bump for one
  session.
- `--reset-budget` zeroes `spent_*` counters but preserves tick-log
  continuity — use when a tick storm exhausted budget but you want to
  continue.
- Phase 0 (ticks 0-2) is intentionally non-coding; budget at least
  3 × `idle_timeout_s` worth of slack at the front of the run.

### Language support

The implement-mode templates ship with Python-leaning gates
(`coverage_3class_delta` uses Python's KIND_RE vocabulary,
`no-empty-bodies` AST-walks .py files, `no-shortcut-markers` scans
`src/`). For JS / Rust / Go projects:

- The structural gates (plan-checklist-empty, plan-step-traceable,
  contracts-unchanged, delivery-report-complete, blind-review,
  concerns-resolved, honesty-audit, no-unresolved-blocks) are
  language-agnostic.
- `lint-clean` / `tests-pass` / `no-prior-regression` are reused from
  audit-mode and respect the project's `.peers/config.yaml` command
  overrides.
- The AST/regex-based cleanliness gates (`no-empty-bodies`,
  `no-shortcut-markers`, `no-skipped-tests`,
  `coverage-3class-delta`) are Python-specific in v1. Open an issue
  before running implement-mode on a non-Python project; we may need
  per-language check variants.

### What if a peer cheats?

- Implementer flips their own checkoff → `pre-commit-reviewer-checkoff`
  hook rejects the commit; `checkoff-by-other-peer` is the post-hoc
  backup.
- Implementer rewrites acceptance to make it trivially pass →
  `contracts-unchanged` fails on the next tick (sha mismatch).
- Either peer leaves stubs / TODOs / skipped tests behind →
  `no-shortcut-markers` / `no-empty-bodies` / `no-skipped-tests`
  block convergence.
- Reviewer rubber-stamps with empty CONCERNS.md → in convergence mode
  `concerns-resolved` fails (zero filed concerns ≠ perfection).
- Both peers write boilerplate HONESTY_AUDIT.md → structural gate
  catches < 5-word answers; behavioural quality is human-in-the-loop.

### What if the loop never converges?

Common patterns and remedies:

| Symptom | Likely cause | Fix |
|---|---|---|
| `plan-checklist-empty` failing for hours | Step too big for one tick | Split into smaller `STEP-N`s with explicit `depends:` |
| `blind-review` failing repeatedly | Reviewer peeking at IMPLEMENTATION_NOTES.md | Re-read `prompts/blind_review_reviewer.md`; consider `peers-ctl stop` + manual reset |
| `acceptance-pass` flaky | Test depends on environment | Pin env in `acceptance.sh` (run via `peers-ctl amend`) |
| `coverage-3class-delta` stuck | New code lacks edge/sad tests | Implementer needs to add edge + sad cases per step |
| Budget exhausted without convergence | Genuinely large feature | `--reset-budget --max-runtime 24h`, or split the plan into two PLAN.md files run sequentially |

### Where do the gates live in code?

All implement-mode-specific gates are under
`src/peers/templates/modes/implement/checks/X.py` (CLI scripts pattern,
mirrors `audit/coverage_3class.py`). The substrate auto-discovers them
via `python3 -m peers.cli run-check <name>`. See the implementation
plan note at the top of
`docs/plans/2026-05-26-implement-mode-implementation.md` for context
on this path (the plan's original design said `src/peers/goals/X.py`
but the actual location is the per-mode templates tree).

---

## See also

- [`docs/plans/2026-05-26-implement-mode-design.md`](plans/2026-05-26-implement-mode-design.md) — design rationale and Schicht layering
- [`docs/plans/2026-05-26-implement-mode-implementation.md`](plans/2026-05-26-implement-mode-implementation.md) — task-by-task implementation plan
- [`src/peers/templates/modes/implement/goals.yaml`](../src/peers/templates/modes/implement/goals.yaml) — full gate roster with per-gate prompts
- [`src/peers_ctl/plan_parser.py`](../src/peers_ctl/plan_parser.py) — PLAN.md grammar
- [`src/peers_ctl/contracts.py`](../src/peers_ctl/contracts.py) — frozen-contract layout + audit log
- [`src/peers_ctl/justifications.py`](../src/peers_ctl/justifications.py) — reviewer-signed shortcut escapes
- [`README.md`](../README.md) `## Modes` section — overview of all peers modes
