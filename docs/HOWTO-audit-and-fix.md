# HOWTO: full audit + fix on an existing app with peers

> Goal: have an existing app inspected end-to-end by two or more
> LLM-peers (claude + codex) — every bug found, critically triaged,
> then honestly fixed, with happy/edge/sad tests, all committed and
> pushed. No shortcuts.

This guide is the result of multiple real-world dogfood runs on
Python and JS projects.

> The German edition lives at
> [HOWTO-audit-and-fix_DE.md](HOWTO-audit-and-fix_DE.md).

---

## 0) Prerequisites (one-time per host)

```sh
cd <path-to-your-peers-checkout>
pip install -e .[dev]                 # peers + peers-ctl on PATH
podman build --network=host -f Containerfile -t peers:dev .
peers-ctl doctor                      # sanity-check
```

`peers-ctl doctor` must show:
- `claude` found (or your VSCode-extension path)
- `codex` found
- `peers:dev` image built
- `podman` ≥ 4.0
- Auth files present: `~/.claude/`, `~/.claude.json`, `~/.codex/`

If auth is missing: run `claude login` and `codex auth login`.

---

## 1) Register the project

```sh
# Bare name lands under $PEERS_PROJECTS_ROOT (default ~/c0de/peers-c0de/).
# Full paths are taken verbatim.
peers-ctl new myapp --container --modes=audit --spec ./myapp-spec.md
```

`myapp-spec.md` is a Markdown description of:
- **What the app does** (1–2 paragraphs)
- **What "done" means** (concrete acceptance criteria, not "roughly")
- **What is explicitly OUT of scope** (negative scope)
- **Known weaknesses peers should investigate**
- **Performance hot-paths**: paths that `perf-no-regression` should
  benchmark (e.g. "the `parse_message` function in `src/wire.py`")
- **Public API**: what must NOT accidentally break — list of exported
  functions/classes/CLI flags (this list is what makes `api-stable`
  snapshots meaningful)

The more precise SPEC.md is, the more focused the audit. Vague SPECs
produce cosmetic bug reports.

For JavaScript/TypeScript projects:

```sh
peers-ctl new myapp --container --modes=audit --lang=js --spec ./myapp-spec.md
```

Currently supported: `python` (default) and `js`. Other values warn
and fall back to Python templates so scaffolding never fails on a
typo.

What gets created:
```
~/c0de/peers-c0de/myapp/
├── .peers/
│   ├── config.yaml         # peer setup + budget + health
│   ├── goals.yaml          # audit hard + soft goals (adjust freely)
│   ├── SPEC.md             # copy of your spec
│   ├── checks/             # audit check scripts from the template
│   └── log/runs.jsonl      # tick-by-tick JSON, populated as it runs
```

---

## 2) goals.yaml — the actual audit program

Replace the default scaffold with the following (adapt to your tech
stack). The individual goals are **intentionally hard-configured** —
peers have no escape hatch via "well, this one's just complex".

```yaml
goals:
  # ===== HARD GATES — all must be green for "complete" =====

  - id: self-review-on-handoff
    type: hard
    description: "Every handoff commit carries a self-review."
    cmd: "python3 -m peers.templates.modes.audit.checks.verify_self_review"
    pass_when: "exit_code == 0"

  - id: tests-pass
    type: hard
    description: "The full test suite is green."
    cmd: "python3 -m pytest -q 2>&1 || true"      # adapt to your tool
    pass_when: |
      regex('failed', stdout) == None
        and regex('passed', stdout + stderr) != None

  - id: tests-cover-happy-edge-sad
    type: hard
    description: "Every non-trivial source file in src/ has at least
      one happy + edge + sad test (via .peers/checks/coverage_3class.py)."
    cmd: "python3 .peers/checks/coverage_3class.py src tests"
    pass_when: "exit_code == 0"

  - id: lint-clean
    type: hard
    cmd: "ruff check . 2>&1 || true"              # or eslint/clippy/...
    pass_when: "regex('error', stdout + stderr) == None"

  - id: type-clean
    type: hard
    cmd: "mypy src/ 2>&1 || true"                 # only if your project is typed
    pass_when: "regex('error', stdout + stderr) == None"

  - id: bug-hunt-clean
    type: hard
    description: "0 open bugs at severity crit/high/med.
      `Bug-Defer:`-with-rationale counts as closed."
    cmd: "python3 -m peers.bug_hunt gate ."
    pass_when: "exit_code == 0"

  - id: tdd-reproduces-bug
    type: hard
    description: "Every Bug-Resolves at blocking severity has a
      PRECEDING Bug-Reproduce commit (failing test first)."
    cmd: "python3 -m peers.bug_hunt gate-tdd ."
    pass_when: "exit_code == 0"

  - id: no-secrets-committed
    type: hard
    description: "No credentials/secrets in the working tree.
      trufflehog is just one example; any scanner that exits 1 on find works."
    cmd: |
      python3 .peers/checks/scan_secrets.py .
    pass_when: "exit_code == 0"

  - id: deps-justified
    type: hard
    description: "Every newly added runtime dependency carries a
      `Dependency-Justification:` trailer in a Bug-Report commit."
    cmd: |
      python3 .peers/checks/deps_justified.py .
    pass_when: "exit_code == 0"

  - id: api-stable
    type: hard
    description: "The public API listed in SPEC.md is unchanged OR
      the commit carries an explicit Breaking-API: trailer."
    cmd: |
      python3 .peers/checks/api_stable.py .
    pass_when: "exit_code == 0"

  - id: no-prior-regression
    type: hard
    description: "No test that was green BEFORE this audit is now red.
      (Prevents a fix from silently taking other features with it.)"
    cmd: |
      python3 .peers/checks/no_regression.py .
    pass_when: "exit_code == 0"

  - id: diff-size-per-resolve
    type: hard
    description: "Every Bug-Resolves commit changes ≤ 200 lines net.
      Huge bundled-fix commits are unreviewable."
    cmd: |
      python3 .peers/checks/diff_size_per_resolve.py .
    pass_when: "exit_code == 0"

  # ===== SOFT GOALS — peers review each other =====

  - id: bug-hunt-round-1-deep
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 1
    prompt: |
      Round 1 deep audit. NO SHORTCUTS. Read EVERY file in src/ and
      tests/ in full — do not skim.

      Look for (all categories, aim for 5+ findings each):
        - Logic errors (off-by-one, wrong conditional ordering)
        - Race conditions / TOCTOU
        - Error-handling gaps (silently-swallowed exceptions)
        - Resource leaks (file handles, sockets, subprocess, threads)
        - Unbounded growth (lists, dicts, caches)
        - Missing input validation at system boundaries
        - Security holes (cmd injection, path traversal, SSRF, …)
        - API contracts not honoured
        - Spec violations (re-read SPEC.md, check every feature)
        - Confabulation risk: code where YOU are uncertain how it gets
          called or which inputs it receives. Such uncertain spots
          MUST be filed as `Bug-Report:investigate-<X>` (severity
          info) — never guess.

      File every defect as a Bug-Report commit per the BUG_HUNT_BLOCK
      schema, with honest severity. Severity inflation and deflation
      both hurt; justify the chosen severity in `## Bug-Report`.

      GUESSING IS A SHORTCUT. If you do not 100% understand what is
      happening, file investigate-X instead of a wrong Bug-Report.

      "Nothing found" is a substantive statement — only reply with
      {"pass": true, "notes": "round 1: N filed (M crit/high)"} if
      you have really read EVERY file AND can justify that the
      remaining N are not severity-misclassified.

  - id: bug-hunt-round-2-cross-review
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 1
    prompt: |
      Round 2: read the OTHER peer's diff since peers-baseline, file
      by file. Did they ship a bug fix? Critique it:
        - Does the fix address the ROOT CAUSE or just the symptom?
        - Ordering: is there a `Bug-Reproduce:` commit landing BEFORE
          the `Bug-Resolves:` (failing test first)? Without that
          it's not a TDD fix.
        - Were tests written for the fix (happy + edge + sad)?
        - Does the fix create new problems (perf regression,
          readability, unclear naming, new race conditions)?
        - Is the fix the smallest possible change, or a drive-by
          refactor bundled in that doesn't belong to the bug?
        - Did the fix turn previously-green tests red? If so:
          Bug-Report.

      Only sign off on Bug-Resolves if you are convinced after this
      review. Otherwise: file a new Bug-Report `## Bug-Report`
      explaining WHY the fix is insufficient (with concrete
      edge-case or failing test). Alternative: if the fix is
      fundamentally wrong AND too large to redo this session, file
      a `Bug-Defer:` commit with honest rationale.

      Reply {"pass": true, "notes": "round 2: F new / R confirmed / U unconvinced / D deferred"}.

  - id: bug-hunt-round-3-spec-conformance
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 2
    prompt: |
      Round 3 FINAL: re-read SPEC.md paragraph by paragraph. For
      EVERY sentence that asserts a behaviour: look up the matching
      test file and verify the behaviour is tested. If the test is
      missing: file a Bug-Report `missing-test:<feature>` at
      severity med, write happy + edge + sad tests directly in the
      same commit, and resolve it.

      NO shortcuts. If you think "this is obvious, doesn't need a
      test" — write the test anyway.

      Reply {"pass": true, "notes": "round 3 done: N missing-tests added"}.

  - id: tests-3-class-review
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 2
    prompt: |
      Read EVERY new or changed test in the current audit. For each
      test verify:
        - happy: nominal input, expected output
        - edge: boundary (empty, max, off-by-one, unicode, very long)
        - sad: invalid input, malformed data, exceptions, timeouts,
               disk-full, network-fail, partial-state-rollback

      Reject `assert True`. Reject "the function returns something".
      Reject tests that only cover the happy path and drop edge/sad.

      For every rejected test: file Bug-Report
      `weak-test:<file>:<name>` with a concrete suggestion of which
      test class is missing.

      Reply {"pass": bool, "notes": "...", "weak": [list of test names]}.

  - id: critical-fix-review
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 3
    prompt: |
      Critical review of fixes landed so far. For every commit with
      a `Bug-Resolves:` trailer:
        1. Was the original finding's severity correct? Was a crit
           bug downgraded to med or vice versa?
        2. Was the bug fixed at its ROOT CAUSE, or at a symptom
           (e.g. catch-all exception instead of fixing the caller)?
        3. Honest tests included?
        4. Would the fix have caught the bug under the original
           audit's stricter rules?

      If a fix looks like it was waved through, file a
      `weak-resolution:BUG-NNN` Bug-Report. Be explicit
      anti-cargo-cult.

      Reply {"pass": bool, "notes": "N resolves audited, M flagged"}.

  - id: honesty-self-check
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Honest internal testing. Answer EVERY question:
        - Did you NOT file at least one bug in this round because
          you thought "ah, too small"? If yes: file it NOW with
          severity info.
        - Did you "simplify" a test because edge-cases were
          inconvenient? If yes: add the missing cases NOW.
        - Did you `git revert` a fix because a test failed,
          instead of fixing the test?
        - Is there code you _assumed_ rather than verified? If
          yes: name it + add a spec-conformance verification in
          the next tick.
        - Did you add a new dependency (pip/npm/cargo)? If yes:
          is there a `Dependency-Justification:` trailer with
          rationale? If not, add it NOW.
        - Did you guess at any external API's existence? Did you
          actually consult it? If unsure: file an investigate Bug.

      Reply {"pass": true, "notes": "honest list: ..."} — be concrete.
      Blanket "yes, all honest" is rejected by the other peer.

  - id: perf-no-regression
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Performance pass. Look in SPEC.md under "Performance hot-paths"
      and run their benchmark NOW (e.g. `python -m mything
      --bench=1000`). Compare against the last value in
      `.peers/perf.log` (format: ISO-ts | sha | hotpath | metric).
      If no perf.log exists: create one with this run as baseline.

      If the current measurement is > 20% worse than the previous:
      file a Bug-Report `perf-regression:<hotpath>` at severity med
      (or higher if the hotpath is user-facing) with concrete
      before/after numbers in the `## Bug-Report` JSON block.

      Reply {"pass": bool, "notes": "perf: hotpath=<v> (baseline=<v>, delta=<%>)"}.

  - id: api-stability-check
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 3
    prompt: |
      Public API stability. SPEC.md lists the public API
      (functions/classes/CLI flags). Generate an API snapshot via
      `python3 .peers/checks/api_stable.py --dump > /tmp/api.now`
      and diff against `.peers/api-baseline.txt`.

      Every change to this list is suspicious: peers tend toward
      drive-by refactor. For each change:
        - If intentional + spec-conformant: commit must carry
          `Breaking-API: <funcname>: <exactly how>` as trailer
          AND a migration note in `## Bug-Resolution`.
        - Otherwise: revert or file Bug-Report
          `unintended-api-break:<symbol>`.

      Reply {"pass": bool, "notes": "api: N added / M removed / K signature-changed"}.

  - id: defer-discipline
    type: soft
    reviewer: both
    consensus_needed: 2
    review_interval: 5
    prompt: |
      Review of all `Bug-Defer:` commits. For each defer check:
        - Is there a `reason`/`note` in the `## Bug-Defer` JSON
          block? (Without rationale it was not an honest defer.)
        - Is the defer reason plausible ("too large" / "needs new
          dependency" / "needs production data to reproduce")
          or obviously a "can't be bothered"?
        - Is there a next-step hint in the defer commit for the
          next session (what preparation would make the fix
          possible)?

      If a defer is questionable, file Bug-Report
      `weak-defer:BUG-NNN` with a concrete suggestion of how the
      bug could still be tackled this session.

      Reply {"pass": bool, "notes": "defers reviewed: N total, M flagged"}.

  - id: docs-sync
    type: soft
    reviewer: other
    consensus_needed: 2
    review_interval: 4
    prompt: |
      Doc-drift check. For every Bug-Resolves commit:
        - Was the behaviour described in a user-facing file
          (README.md, docs/, docstring on the public function)?
        - If yes: is the description still accurate after the fix?
          If wrong: update it in THIS audit (a `Bug-Resolves:`
          commit is OK if the doc was the bug; otherwise a regular
          docs-update commit).
        - Is there a CHANGELOG.md? If yes, is the fix entered there?

      Reply {"pass": bool, "notes": "docs: N updates needed, M done"}.
```

If you edit `.peers/goals.yaml` manually after scaffolding, refresh
the integrity hash:

```sh
python3 - <<'PY'
import hashlib
from pathlib import Path

p = Path(".peers")
(p / "goals.sha256").write_text(
    hashlib.sha256((p / "goals.yaml").read_bytes()).hexdigest() + "\n"
)
PY
```

During a running `peers-ctl start`, `goals.yaml` is intentionally
protected: edits or deletion trigger a halt with a clear reason.

### Why these goals are written this way

| Goal | What it prevents |
|------|------------------|
| `tests-cover-happy-edge-sad` as HARD | "complete" with only happy tests per src file |
| `bug-hunt-clean` as HARD | 0 open crit/high/med is non-negotiable |
| `tdd-reproduces-bug` as HARD | Tests built up after the fix (they only pass for the fix, not for the bug) |
| `no-secrets-committed` as HARD | Accidental commits of `.env`, credentials, tokens |
| `deps-justified` as HARD | Drive-by `pip install foo` without rationale |
| `api-stable` as HARD | Unannounced breaking changes to the public API |
| `no-prior-regression` as HARD | A fix for bug X silently breaks feature Y |
| `diff-size-per-resolve` as HARD | Unreviewable 800-line bundled commits |
| `round-2-cross-review` + "enforce TDD order" | Test-with-fix instead of test-before-fix; mutual wave-throughs |
| `critical-fix-review` (separate) | Root-cause vs. symptom; severity re-triage |
| `perf-no-regression` | An O(n²) fix that passes all tests but is 10× slower |
| `defer-discipline` | `Bug-Defer:` without rationale gets caught |
| `docs-sync` | README/docstring/CHANGELOG drift relative to fixed behaviour |
| `honesty-self-check` | Self-audit on shortcuts, confabulation, unjustified deps |

### Bug-hunt trailers — quick reference

| Trailer | Meaning | Gate status |
|---------|-----------|----------------|
| `Bug-Report: BUG-NNN` | Finding filed | bug is OPEN |
| `Bug-Resolves: BUG-NNN` + JSON `"status":"fixed"` | Fix landed | bug is CLOSED |
| `Bug-Resolves: BUG-NNN` + `"status":"wontfix"` | Deliberately not fixed | bug stays OPEN (human must explicitly re-triage) |
| `Bug-Defer: BUG-NNN` + `## Bug-Defer {reason}` | Too large for this session, documented | bug is CLOSED (for gate) + visible in summary |
| `Bug-Reproduce: BUG-NNN` | Commit adds failing test for the bug | evaluated by `gate-tdd` |
| `Dependency-Justification: <package>: <why>` | New dep with rationale | checked by `deps-justified` |
| `Breaking-API: <symbol>: <how>` | Intentional API change | accepted by `api-stable` as legitimate |

`gate-tdd` evaluates git history linearly. If you use merge commits
or side-branches, make sure the `Bug-Reproduce` commit semantically
lands before its `Bug-Resolves`; otherwise a historically-later
merged reproducer looks like a missing TDD proof.

---

## 3) Check scripts (reference for `.peers/checks/`)

`peers-ctl new --modes=audit` copies these 6 scripts automatically
to `.peers/checks/` and wires `goals.yaml` directly at them. The
bodies below stay here as a reference and for customisation; for
JavaScript/TypeScript use `--lang=js`, for Rust `--lang=rust`,
for Go `--lang=go`. Unknown languages deliberately fall back to
Python.

### `coverage_3class.py`

```python
#!/usr/bin/env python3
"""Exit 1 if any non-trivial src/ file lacks happy + edge + sad tests."""
import re, sys
from pathlib import Path

KIND_RE = {
    "happy": re.compile(r"(?i)(happy|ok|success|nominal|baseline)"),
    "edge":  re.compile(r"(?i)(edge|boundary|empty|max|min|long|unicode)"),
    "sad":   re.compile(r"(?i)(sad|fail|error|invalid|exception|timeout|broken)"),
}

def kinds_in(testfile: Path) -> set[str]:
    text = testfile.read_text(errors="ignore")
    return {k for k, rx in KIND_RE.items()
            for m in re.finditer(r"def\s+(test_\w+)", text)
            if rx.search(m.group(1))}

def main(srcdir="src", testdir="tests"):
    missing: list[str] = []
    for src in Path(srcdir).rglob("*.py"):
        if src.name.startswith("_") or src.name == "__init__.py":
            continue
        if sum(1 for _ in src.open()) < 50:                # tiny module
            continue
        stem = src.stem
        candidates = list(Path(testdir).rglob(f"test_{stem}*.py"))
        if not candidates:
            missing.append(f"{src}: no test_{stem}* anywhere in {testdir}/")
            continue
        kinds = set().union(*(kinds_in(c) for c in candidates))
        gap = {"happy", "edge", "sad"} - kinds
        if gap:
            missing.append(f"{src}: missing {sorted(gap)} test class(es)")
    if missing:
        print("coverage_3class FAIL:\n  " + "\n  ".join(missing))
        return 1
    print(f"coverage_3class: clean ({srcdir} ↔ {testdir})")
    return 0

if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3] if len(sys.argv) >= 3 else ()))
```

### `scan_secrets.py`

The template scans git-tracked files plus untracked, non-ignored
files (`git ls-files --cached --others --exclude-standard`). Files
deliberately ignored (such as `.env`) stay a git-policy concern; if
you want to audit them too, add a real filesystem scanner like
trufflehog to your project gates.

```python
#!/usr/bin/env python3
"""Exit 1 if a credential-shaped string sneaks into the working tree.

Production-grade option: shell out to `trufflehog filesystem .` or
`git-secrets --scan`. Below is a minimal stdlib version that catches
the most common patterns (good enough as a first gate; supplement
with a real scanner for SOC2-grade audits)."""
import re, subprocess, sys
from pathlib import Path

PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"),            "AWS access key id"),
    (re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{6,}"), "hard-coded password"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][a-z0-9_\-]{16,}"), "API key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"),        "GitHub PAT"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"),         "OpenAI-style secret"),
]
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules",
        ".venv", "venv", ".peers"}

def main(root: str = "."):
    findings = []
    tree = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True
    )
    files = [Path(root) / f for f in tree.stdout.splitlines() if f]
    for f in files:
        if any(s in f.parts for s in SKIP):
            continue
        try:
            text = f.read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        for rx, label in PATTERNS:
            for m in rx.finditer(text):
                findings.append(f"{f}:{text[:m.start()].count(chr(10))+1}: {label}")
    if findings:
        print("secrets FAIL:\n  " + "\n  ".join(findings))
        return 1
    print(f"secrets: clean ({len(files)} files scanned)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `deps_justified.py`

```python
#!/usr/bin/env python3
"""Exit 1 if a runtime dependency line was added without an explicit
Dependency-Justification: trailer in any commit that touched the
dependency file. Adjust DEP_FILES to your stack."""
import subprocess, sys
from pathlib import Path

DEP_FILES = ["pyproject.toml", "requirements.txt", "package.json",
             "Cargo.toml", "go.mod"]

def changed_dep_lines(repo: str) -> list[str]:
    # Compare HEAD against peers-baseline (or initial commit fallback).
    base = subprocess.run(
        ["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True
    ).stdout.strip().splitlines()[-1]
    baseline_ref = "peers-baseline" if subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "peers-baseline"],
        capture_output=True
    ).returncode == 0 else base
    out = subprocess.run(
        ["git", "-C", repo, "diff", f"{baseline_ref}..HEAD", "--unified=0",
         "--", *DEP_FILES],
        capture_output=True, text=True
    ).stdout
    added: list[str] = []
    for line in out.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
    return [a for a in added if a and not a.startswith("#")]

def has_justifications(repo: str) -> set[str]:
    log = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD",
         "--grep=^Dependency-Justification:", "--format=%B"],
        capture_output=True, text=True
    ).stdout
    out = set()
    for line in log.splitlines():
        if line.startswith("Dependency-Justification:"):
            pkg = line.split(":", 2)[1].strip().split()[0]
            out.add(pkg.lower().rstrip(","))
    return out

def main(repo: str = "."):
    added = changed_dep_lines(repo)
    justified = has_justifications(repo)
    missing = []
    for line in added:
        pkg = line.split()[0].split("=")[0].split(">")[0].split("<")[0]\
                  .strip("\"',").lower()
        if pkg and pkg not in justified:
            missing.append(f"{pkg}  (added in deps but no Dependency-Justification: trailer)")
    if missing:
        print("deps_justified FAIL:\n  " + "\n  ".join(missing))
        return 1
    print(f"deps_justified: clean ({len(added)} added, all justified)")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `api_stable.py`

```python
#!/usr/bin/env python3
"""Snapshot the public API surface; fail if it drifted without an
explicit Breaking-API: trailer in the corresponding commit.

Usage:
  api_stable.py --dump > .peers/api-baseline.txt   # once at audit start
  api_stable.py                                    # gate mode
"""
import ast, subprocess, sys
from pathlib import Path

def public_symbols(srcdir: str = "src") -> list[str]:
    out = []
    for f in sorted(Path(srcdir).rglob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        mod = ".".join(f.with_suffix("").parts[1:])
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    out.append(f"{mod}.{node.name}")
    return out

def main():
    if "--dump" in sys.argv:
        for s in public_symbols():
            print(s)
        return 0
    baseline = Path(".peers/api-baseline.txt")
    if not baseline.exists():
        print("api_stable: no baseline — run with --dump first to capture")
        return 1
    declared = set(baseline.read_text().splitlines())
    actual = set(public_symbols())
    added = actual - declared
    removed = declared - actual
    log = subprocess.run(
        ["git", "log", "peers-baseline..HEAD", "--grep=^Breaking-API:",
         "--format=%B"],
        capture_output=True, text=True
    ).stdout
    allowed = {line.split(":", 1)[1].strip().split(":")[0].strip()
               for line in log.splitlines()
               if line.startswith("Breaking-API:")}
    unannounced = (added | removed) - allowed
    if unannounced:
        print("api_stable FAIL: unannounced API changes:")
        for s in sorted(unannounced):
            kind = "added" if s in added else "removed"
            print(f"  {kind}: {s}")
        return 1
    print(f"api_stable: clean (+{len(added)}/-{len(removed)} all "
          f"covered by Breaking-API: trailers)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### `no_regression.py`

```python
#!/usr/bin/env python3
"""Exit 1 if a test that was green at peers-baseline is now red.

Records the set of passing test nodeids at baseline (one-time) into
.peers/passing-baseline.txt via pytest's --junitxml output. On
gate-run, runs the suite NOW and compares: any baseline-passing test
that isn't in the new passing set is a regression."""
import subprocess, sys, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

BASELINE = Path(".peers/passing-baseline.txt")

def collect_passing() -> set[str]:
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml_path = tf.name
    subprocess.run(
        ["python3", "-m", "pytest", "-q", "--no-header",
         f"--junitxml={xml_path}", "--tb=no"],
        capture_output=True, text=True
    )
    tree = ET.parse(xml_path)
    Path(xml_path).unlink(missing_ok=True)
    out: set[str] = set()
    for tc in tree.getroot().iter("testcase"):
        # Failed/errored tests carry <failure>/<error> children; passing
        # ones have neither (skipped → <skipped/>, treat as not-passing).
        if any(child.tag in ("failure", "error", "skipped") for child in tc):
            continue
        cls = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        out.add(f"{cls}::{name}" if cls else name)
    return out

def main():
    if "--snapshot" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        passing = collect_passing()
        BASELINE.write_text("\n".join(sorted(passing)) + "\n")
        print(f"no_regression: snapshot saved to {BASELINE} ({len(passing)} tests)")
        return 0
    if not BASELINE.exists():
        print(f"no_regression: missing {BASELINE}; "
              "run once with --snapshot at audit start")
        return 1
    expected = set(BASELINE.read_text().splitlines()) - {""}
    actual = collect_passing()
    regressed = expected - actual
    if regressed:
        print(f"no_regression FAIL: {len(regressed)} previously-green tests are red:")
        for t in sorted(regressed)[:30]:
            print(f"  {t}")
        return 1
    print(f"no_regression: clean ({len(expected)} baseline-green still green)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Note: uses pytest's `--junitxml` output rather than parsing stdout —
robust against format changes in pytest's terminal reporter (e.g.
between `-q`/`-v`/colored). The Python template is pytest-specific;
the `--lang=js|rust|go` scaffold variants lay down stack-specific
`no_regression.sh` entry-points that you can harden against Jest,
Cargo, or Go JSON reporters as needed.

### `diff_size_per_resolve.py`

```python
#!/usr/bin/env python3
"""Exit 1 if any Bug-Resolves commit exceeds NET_LINES of diff."""
import subprocess, sys

LIMIT = 200
def main(repo: str = "."):
    log = subprocess.run(
        ["git", "-C", repo, "log", "peers-baseline..HEAD",
         "--grep=^Bug-Resolves:", "--format=%H"],
        capture_output=True, text=True
    ).stdout.splitlines()
    over: list[str] = []
    for sha in log:
        ns = subprocess.run(
            ["git", "-C", repo, "show", "--stat", "--format=", sha],
            capture_output=True, text=True
        ).stdout.strip().splitlines()
        if not ns:
            continue
        last = ns[-1]
        # "N files changed, X insertions(+), Y deletions(-)"
        ins = del_ = 0
        for tok in last.split(","):
            tok = tok.strip()
            if "insertion" in tok:
                ins = int(tok.split()[0])
            elif "deletion" in tok:
                del_ = int(tok.split()[0])
        net = ins + del_
        if net > LIMIT:
            over.append(f"  {sha[:8]}: {net} lines (limit {LIMIT})")
    if over:
        print("diff_size_per_resolve FAIL:\n" + "\n".join(over))
        return 1
    print(f"diff_size_per_resolve: clean ({len(log)} resolves, all ≤ {LIMIT})")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
```

### `verify_self_review.py` (for `self-review-on-handoff`)

The default uses the trusted package checker:

```sh
python3 -m peers.templates.modes.audit.checks.verify_self_review
```

`peers init` additionally copies a compatible file to
`.peers/checks/verify_self_review.py` so existing projects and
local special-case checks keep working. For new goals the package
path is more robust because it doesn't depend on an editable target
repo.

---

## 3.5) Stacking modes — when you want more than just bug-audit

`--modes` is a comma-separated list. Built-in:
- `audit` — bug audit (everything from §3)
- `thorough` — anti-convergence-theater: HARD gate on N=3
  consecutive clean ticks + skeptic-pass + aggressive-honesty
- `describe` — peers write SPEC/ARCH/DESIGN docs, don't audit
- `implement` — feature implementation from PLAN.md (standalone,
  not stackable)

```sh
peers-ctl new myapp --modes=audit,thorough --spec ./myapp-spec.md
```

Custom scopes (e.g. `security-crypto`, `security-mobile`) live under
user modes at `~/.config/peers/modes/<name>/`. See `peers-ctl modes list`.

### External tools as user modes

`~/.config/peers/modes/cloc-baseline/`:
- `mode.yaml`: `{name: cloc-baseline, version: 1, description: ...}`
- `goals.yaml`: a `cmd:` that calls the external binary (e.g. `cloc`).

Then:
```sh
peers-ctl new myapp --modes=audit,cloc-baseline
```

`peers-ctl modes list` shows all available modes (built-in + user).

### Deep audit: `--modes=audit,thorough`

When you really want "run until nothing's left":

```sh
peers-ctl new myapp --modes=audit,thorough --spec ./spec.md
```

What `thorough` adds:
- **HARD `convergence-reached`**: requires N=3 (default, override via
  `goals.convergence_n` in config.yaml) consecutive ticks without
  new crit/high/med Bug-Reports + without new weak-fix/shallow-fix
  findings. Info findings don't count for reset — otherwise the loop
  runs forever on "info: missing docstring".
- **SOFT `skeptic-pass`** every tick: peers must concretely justify
  and rule out 5 failure modes per file, otherwise rejected.
- **SOFT `aggressive-honesty`** every 3 ticks: per top-level path
  peers must name 3 failure modes + 2 security categories + 1
  coverage gap concretely.

Recommended `config.yaml` tweaks when you stack thorough:

```yaml
budget:
  max_iterations: 500       # thorough needs 20-50 more ticks
  max_runtime_s: 86400      # 24h emergency brake
  max_consecutive_failures: 10
goals:
  convergence_n: 3          # 3 clean ticks; bump to 5 for stricter
```

---

## 4) One-time setup at audit start

`peers init` (via `peers-ctl new`) has already tagged HEAD with
`peers-baseline` — that's the anchor reference for all check scripts
that work against `peers-baseline..HEAD`.

In addition, before the first `peers-ctl start`:

```sh
cd ~/c0de/peers-c0de/myapp

# Freeze audit env (for reproducibility)
{
  echo "audit-started: $(date -Is)"
  echo "peers: $(peers --version)"
  echo "peers-ctl: $(peers-ctl --version)"
  echo "claude: $(claude --version 2>/dev/null || echo n/a)"
  echo "codex: $(codex --version 2>/dev/null || echo n/a)"
  echo "podman: $(podman --version)"
  echo "git: $(git rev-parse HEAD)"
} > .peers/audit-env.txt

# Snapshots for no_regression + api_stable
python3 .peers/checks/no_regression.py --snapshot
python3 .peers/checks/api_stable.py --dump > .peers/api-baseline.txt

git add .peers/audit-env.txt .peers/passing-baseline.txt .peers/api-baseline.txt
git commit -m "audit: capture env + baseline snapshots"
git tag -f peers-baseline HEAD     # move the anchor to THIS commit
```

The final `git tag -f` moves `peers-baseline` so the baseline
snapshots themselves are not counted as "audit diff".

---

## 5) config.yaml — audit-grade

```yaml
driver: orchestrator
comm: hybrid                          # peers also talk via files, not only git

peers:
  - name: claude
    tool: claude
    argv: ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "json", "{PROMPT}"]   # json → USD tracking
    prompt_mode: argv-substitute
  - name: codex
    tool: codex
    argv: ["codex", "exec",
           "--skip-git-repo-check",
           "--sandbox", "workspace-write",
           "--dangerously-bypass-approvals-and-sandbox", "{PROMPT}"]
    prompt_mode: argv-substitute

budget:
  max_iterations: 50                  # audit needs many ticks
  max_runtime_s: 28800                # 8 h emergency brake
  max_consecutive_failures: 5
  max_usd_mode: auto                  # OAuth setup → warn (no hard kill)

health:
  idle_timeout_s: 1800                # 30 min — peers think long on big repos
  absolute_max_runtime_s: 7200
  error_patterns:
    # Use defaults from template/config.yaml (ERROR/FATAL anchored)

# Bump this when pytest takes > 120s
goals:
  timeout_s: 600                      # 10 min — covers most suites
```

---

## 6) Starting + observing

```sh
PEERS_CTL_PODMAN_NETWORK=host \
    peers-ctl start myapp --container --max-ticks 50 --max-usd 100

# Three terminals to watch live (or use tmux):
peers-ctl tail myapp                  # container log live
peers-ctl status myapp                # current goal status + peer health
watch -n 30 'python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp'
```

What a "real" audit session looks like:
- Ticks 1–3: peers read the whole src/ tree, file first wave of bugs
- Ticks 4–8: round-2 cross-review runs; half the round-1 findings get
  re-triaged or marked as "weak resolution"
- Ticks 9–15: code fixes land, tests are written
- Tick 16+: round-3-spec-conformance finds missing-tests;
  honesty-self-check triggers small further findings
- Convergence: bug-hunt-clean exit 0 + all hard goals pass + soft
  consensus ≥2/2

---

## 7) Manual stop when needed

```sh
peers-ctl stop myapp                  # SIGTERM → 10s grace → SIGKILL
```

The substrate persists state cleanly via the SIGTERM handler. You
can later `peers-ctl start myapp --container --max-ticks 50` and
it resumes: `state.json` is atomically written and `goals.yaml` is
protected against the start snapshot. If you want to change goals,
stop the run, edit `goals.yaml`, refresh `goals.sha256`, and start
again.

---

## 8) Acceptance — no shortcuts

```sh
# Full re-validation of all hard gates
peers -C ~/c0de/peers-c0de/myapp verify
cat ~/c0de/peers-c0de/myapp/.peers/VERIFY.md

# Bug ledger: what was filed, resolved, deferred
python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp

# TDD discipline: did every blocking fix have a failing test FIRST?
python3 -m peers.bug_hunt gate-tdd ~/c0de/peers-c0de/myapp

# Read REPORT.md — the substrate's own summary
cat ~/c0de/peers-c0de/myapp/.peers/REPORT.md

# Read runs.jsonl — tick-by-tick of what happened
jq -s '.' ~/c0de/peers-c0de/myapp/.peers/log/runs.jsonl | less
```

**Critical review of the findings — mandatory:**

1. **Severity sanity check.** Walk through `bug_hunt summary` and
   ask, for every crit/high: "would an experienced engineer really
   classify it that way?". Peers tend toward severity inflation early
   and deflation late. On discrepancy: check manually, optionally
   kick off another tick with a re-triage prompt.

2. **Fix-quality spot-check.** Pick 5 random `Bug-Resolves:` commits.
   Read the diff. Ask yourself: would I have fixed the bug this way?
   If not, check whether there's a good reason or whether it's
   cargo-cult.

3. **Test-quality spot-check.** Pick 5 random new test functions.
   Read them. Are happy + edge + sad really covered, or did the peer
   write three test functions with the same happy case and only
   suggest the "edge" / "sad" class through the name?

4. **Spec-conformance spot-check.** Read SPEC.md paragraph by
   paragraph. For every asserted behaviour: does a test exist? If
   not, round-3 didn't complete properly — schedule another tick.

5. **Honesty check.** Read the `honesty-self-check` review replies.
   "All good, nothing found" across multiple runs in a row is
   suspicious.

---

## 9) Commit + push

The substrate already commits during the loop; every commit carries
`Peer: <name>` + a Self-Review trailer. But you must still
double-check and push to your own branch:

```sh
cd ~/c0de/peers-c0de/myapp

# What did the audit change?
git log --oneline peers-baseline..HEAD | head -50
git diff --stat peers-baseline..HEAD

# Final sanity check before push — EVERY line must exit 0
python3 -m pytest -q                                # tests green?
ruff check .                                        # lint clean?
python3 -m peers.bug_hunt gate .                    # 0 crit/high/med?
python3 -m peers.bug_hunt gate-tdd .                # TDD discipline?
python3 .peers/checks/scan_secrets.py .             # no secrets?
python3 .peers/checks/deps_justified.py .           # new deps justified?
python3 .peers/checks/api_stable.py .               # API stable?
python3 .peers/checks/no_regression.py .            # no previously-green now red?
python3 .peers/checks/diff_size_per_resolve.py .    # all resolves ≤ 200 LOC?
peers verify                                        # all hard goals green?

# If EVERYTHING is green:
git remote -v                                       # which origin do you push to?
git push origin <your-branch>
```

If your workflow uses pull requests:

```sh
gh pr create --title "Audit + fix run on myapp" --body "$(cat <<'EOF'
## Summary
- Full peers audit + fix (claude + codex, $N ticks, $M USD)
- $K bug reports filed, $J resolved (severity distribution in body)
- Tests: $B → $A passing (+$delta new tests)
- Lint/type: clean

## Bug ledger
$(python3 -m peers.bug_hunt summary . | head -40)

## Test plan
- [ ] `pytest -q` green locally on a clean clone
- [ ] `peers verify` exit 0
- [ ] Manual smoke test: <project-specific>
- [ ] Spec-conformance spot check on 3 random SPEC sentences

🤖 Generated by peers-substrate via Claude Code
EOF
)"
```

**Before you merge the PR:** read the bug ledger yourself, not just
the summary. Verify spot-wise. If something looks off: another audit
tick with a targeted prompt is better than merging now and fixing
later.

---

## 10) Common pitfalls

- **`/tmp` is tmpfs**: audit projects do NOT belong under `/tmp/`.
  Use `$PEERS_PROJECTS_ROOT` (default `~/c0de/peers-c0de/`) — otherwise
  projects get lost after a reboot.

- **PID-1 assumptions in your tests:** if your test suite does
  `os.kill(1, …)` or `os.killpg(0, …)` assuming PID 1 is init/systemd:
  in the peers:dev container PID 1 is the substrate (uid 1000). Mock
  it instead of actually killing.

- **`idle_timeout_s` too small**: the most common failure mode. claude
  `-p` is completely silent during work (no streaming). Rule of
  thumb: 600 s only for small fixes; 1800–3600 s for multi-file;
  3600+ s for large audits.

- **pasta network bug** on some hosts: `PEERS_CTL_PODMAN_NETWORK=host`
  before `peers-ctl start ...`.

- **Goal-mutation halt**: a peer edited or deleted `goals.yaml`. The
  loop halts — by design. Stop, manually decide whether to accept
  the edit, refresh `goals.sha256`, restart. The start-snapshot
  protects the running pass even if `goals.yaml` and `goals.sha256`
  are modified together.

- **api-error in runs.jsonl**: the substrate logs `matched_error_pattern`
  + `stderr_tail`. Use them to tell real rate-limit from config issue
  (e.g. missing `--dangerously-bypass-approvals-and-sandbox` on
  codex).

- **Convergence takes long**: auditing a 5k-LOC project realistically
  takes 20–40 ticks at ~5–15 min = 2–10 h wallclock + $30–100 USD on
  API billing (OAuth: free). Budget accordingly, or lower
  `max_iterations` for a quick pass.

---

## 11) Honesty — meta-reminder

This guide does not guarantee a perfect audit. What it does guarantee:

- **Two independent peer eyes** on every bug report (`reviewer: both`,
  `consensus_needed: 2`)
- **Structurally prevented "complete"** without 0-crit/high/med
- **Structurally enforced** happy/edge/sad test-class coverage
- **Self-audit** ("honesty-self-check") as a recurring prompt
- **Human in the loop** for severity sanity, fix quality, spec
  conformance

What it does NOT replace:

- Your own critical reading of findings + fixes
- Your domain knowledge of the app
- Penetration testing for security-critical apps (peers find
  classical OWASP patterns, not state-of-the-art exploit chaining)
- Performance profiling (peers review code, not latency profiles)

If the audit says "all green" and your gut says "that was too
quick" — trust the gut. Another tick with a targeted skeptic prompt:

```yaml
- id: skeptic-pass
  type: soft
  reviewer: both
  consensus_needed: 2
  review_interval: 1
  prompt: |
    The previous audit ended on "all green". That's suspicious.
    Walk every src file again and find at least 1 bug the prior
    rounds missed. If after a conscientious search you really find
    nothing, document CONCRETELY which 5 failure modes you checked
    and ruled out. Blanket "clean" gets rejected by the other peer.
```

---

## TL;DR (the rushed version)

```sh
peers-ctl new myapp --container --modes=audit --spec ./spec.md
cd ~/c0de/peers-c0de/myapp
$EDITOR .peers/{goals,config}.yaml SPEC.md          # trim goals/config/SPEC
python3 .peers/checks/no_regression.py --snapshot
python3 .peers/checks/api_stable.py --dump > .peers/api-baseline.txt
git add .peers && git commit -m "audit: baseline" && git tag -f peers-baseline
cd -

PEERS_CTL_PODMAN_NETWORK=host \
    peers-ctl start myapp --container --max-ticks 50 --max-usd 100
peers-ctl tail myapp                  # watch
# wait for "complete" or stop manually

peers -C ~/c0de/peers-c0de/myapp verify
python3 -m peers.bug_hunt summary ~/c0de/peers-c0de/myapp
python3 -m peers.bug_hunt gate-tdd ~/c0de/peers-c0de/myapp
# read critically, spot-check, optionally re-run with skeptic-pass
git push origin <branch>
```
