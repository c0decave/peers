# Operator workflows: `peers develop` and `peers research`

> Two **one-shot, operator-runnable** workflows driven off the inner `peers`
> CLI. Unlike the stackable `--modes=…` audit loop (run via `peers-ctl`), these
> run once against a single repo, drive its configured peer, and leave their
> result on your **current branch** — no controller, no long-lived run dir.
>
> README summary: [README.md](../README.md#operator-runnable-workflows--develop-and-research).
> The German edition lives at [WORKFLOWS_DEVELOP_RESEARCH_DE.md](WORKFLOWS_DEVELOP_RESEARCH_DE.md).

---

## Prerequisites (both)

Both workflows operate on a git repo that already carries a configured peer in
`.peers/config.yaml`:

```sh
cd /path/to/your-repo
peers init                 # writes .peers/ (once); commits .gitignore
$EDITOR .peers/config.yaml # name at least one peer (e.g. claude / codex)
```

`peers-ctl doctor` verifies the peer CLIs + git are usable. Both workflows
**fail CLOSED** (non-zero exit, honest message) on a non-git repo, a missing
`.peers/config.yaml`, or invalid input — they never fabricate a result.

---

## `peers develop` — autonomously improve this repo

```sh
peers develop . --dimensions correctness,security,perf
```

Drives the real **AUDIT → VERIFY → AUTHOR → IMPLEMENT** seam over one repo:

1. **AUDIT** — the configured peer audits the repo along each named
   `--dimension` and surfaces candidate findings.
2. **VERIFY** — each finding is **adversarially verified** (the spine's
   `verify_claim`) before it is allowed to drive a change, so a mislabelled or
   refuted "bug" is never fixed into the tool.
3. **AUTHOR** — the surviving findings are frozen into an **implement
   contract** (the same frozen-acceptance-contract machinery as `implement`
   mode), generated from the audit instead of a hand-written `PLAN.md`.
4. **IMPLEMENT** — the contract is converged to an **attested commit** via the
   blind-review + acceptance gates, up to `--convergence-budget` attempts.

### Flags

| Argument | Default | Meaning |
|---|---|---|
| `repo` (positional) | — | path to the target git repository |
| `--dimensions` | **required** | comma-separated audit dimensions, e.g. `correctness,security,perf` |
| `--peer <name>` | first peer in `.peers/config.yaml` | which configured peer drives the agent |
| `--convergence-budget <N>` | `5` | max implement attempts per contract before giving up |

### Output

- Attested commit(s) landed on your **current branch** (each carries the
  substrate's own authorship attestation — trust the attestation, not the
  trailer).
- A run ledger at `.peers/run.jsonl` (tick-by-tick record).
- Exit `0` on a converged, attested result; non-zero on a fail-closed stop.

**Use it when** you want the substrate to *find AND fix*: pick the dimensions,
walk away, then review the commit it lands.

---

## `peers research` — synthesize a cited report from a `TOPIC.md`

```sh
cat > TOPIC.md <<'MD'
## Scope
What I want answered, and the boundaries of the question.

## Questions
- First concrete question?
- Second concrete question?
MD
peers research . --modalities codebase,web
```

Reads an operator-authored **`TOPIC.md`** at the repo root and runs
**INTAKE → DECOMPOSE → SWEEP → SYNTHESIZE**:

1. **INTAKE** — `require_topic` validates `TOPIC.md`: it needs a non-vacuous
   `## Scope` and `## Questions` (a `## Frameworks` section is **not** required —
   research is a generic KNOWLEDGE workflow, so a non-security topic like
   "cloning plants in Alaska" is fine). Fail-closed + symlink-refusing, with a
   2 MiB cap and a per-section character floor.
2. **DECOMPOSE** — the topic is broken into concrete sub-questions.
3. **SWEEP** — each enabled modality is swept for corroborating evidence.
4. **SYNTHESIZE** — a **cited `RESEARCH.md`** is written from the claims the
   run could confirm.

### Flags

| Argument | Default | Meaning |
|---|---|---|
| `repo` (positional) | — | path to the git repository (must hold `TOPIC.md`) |
| `--modalities <list>` | `codebase` | comma-separated evidence modalities: `codebase` and/or `web` |
| `--peer <name>` | first peer in `.peers/config.yaml` | which configured peer drives the agent |

`codebase` corroborates claims from the repo itself; add `web` so the agent can
cite primary-source URLs.

### Honesty contract (what the report is gated on)

`RESEARCH.md` is **citation-gated**, not free prose:

- **CITED** — load-bearing claims need at least `MIN_CITATIONS` (2) distinct
  primary-source URLs; uncorroborated claims are **dropped, never guessed**.
- **GAPS** — a non-trivial `## Gaps` section is required (what the run could
  *not* answer is reported, not hidden).
- **No completeness theater** — "comprehensive"/"exhaustive"-style claims are
  banned; the report states what it actually established.

### Output

- `RESEARCH.md` written onto your **current branch**.
- Exit `0` on a synthesized report; non-zero (with a reported stop-reason) on a
  missing/invalid `TOPIC.md` or an empty evidence sweep.

**Use it when** you want a checkable, source-cited answer to a written brief —
without the model padding gaps with guesses.
