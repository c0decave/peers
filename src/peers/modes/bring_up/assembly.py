"""Assembly factory + fail-closed guards for the operator-runnable bring-up CLI.

Wires a manifest into a ready-to-drive :class:`BringUpFrontend` and guards the
two crash classes the next-steps plan §3 flagged: an empty (no-commit) git repo
and duplicate corpus case-ids. The CLI (``peers bring-up``) layers an honest
terminal ledger row on top of these guards.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .corpus import make_corpus_adapter
from .frontend import BringUpFrontend, EscalateOnlyFixer, Fixer
from .manifest import BringUpManifest, OracleSpec
from .memory import BringUpMemory
from .models import require_unique_case_ids
from .oracle import RuntimeOracle, TestSuiteOracle
from .runner import ToolRunner


def validate_git_repo(repo: Path) -> str | None:
    """Return an error message if ``repo`` is not a git repo with >=1 commit.

    Returns ``None`` when the repo is usable. Fail-closed guard: a directory
    with ``.git`` but NO commits passes a naive existence check yet raises
    inside ``git worktree add ... HEAD`` and yields no HEAD sha for memory /
    landing — so an empty repo is almost always an operator mistake. Catch it
    up front with an actionable message rather than an uncaught crash.
    """
    repo = Path(repo)
    if not repo.is_dir():
        return f"target repo does not exist: {repo}"
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f"git not runnable on {repo}: {e}"
    if r.returncode != 0:
        detail = r.stderr.strip() or "git rev-parse HEAD failed"
        return (f"target repo has no commits (or is not a git repo): {repo} "
                f"({detail})")
    return None


def _build_oracle(spec: OracleSpec, root: Path | None = None):
    """Construct one oracle from its manifest spec, or fail closed.

    ``differential`` is wired from generic config-driven readers (a corpus-field
    ground-truth + a sqlite tool-verdict, e.g. a ``findings.sqlite3``), so
    it is now CLI-constructible from a manifest. ``root`` anchors a relative DB
    path (the per-case run dir takes precedence at read time).
    """
    if spec.kind == "runtime":
        return RuntimeOracle(spec.config)
    if spec.kind == "test-suite":
        return TestSuiteOracle(spec.config)
    if spec.kind == "differential":
        from .oracle import DifferentialOracle
        from .readers import (
            make_corpus_expected_reader,
            make_sqlite_tool_verdict_reader,
        )
        return DifferentialOracle(
            spec.config,
            tool_verdict=make_sqlite_tool_verdict_reader(
                spec.config, root=Path(root) if root is not None else Path(".")),
            expected_verdict=make_corpus_expected_reader(spec.config))
    raise ValueError(
        f"oracle kind {spec.kind!r} cannot be built from a manifest; "
        "the CLI supports 'runtime', 'test-suite', and 'differential'")


def make_bring_up_frontend(
    manifest: BringUpManifest, repo: Path, *, fixer: Fixer | None = None,
) -> BringUpFrontend:
    """Wire a complete :class:`BringUpFrontend` from a validated manifest + repo.

    Fails CLOSED on an un-CLI-constructible oracle, a corpus-adapter error, or
    duplicate case-ids. The default ``fixer`` is :class:`EscalateOnlyFixer`
    (observe-and-report); pass a landing Fixer to opt into live fixing.
    """
    repo = Path(repo)
    if fixer is None:
        fixer = EscalateOnlyFixer()
    oracles = [_build_oracle(s, root=repo) for s in manifest.oracle]
    adapter = make_corpus_adapter(manifest.corpus, root=repo)
    cases = adapter.cases()
    require_unique_case_ids(cases)
    runner = ToolRunner(manifest.driver, target=repo)
    memory = None
    if manifest.memory.mode != "off":
        memory = BringUpMemory(
            repo / ".peers" / "bringup-memory.jsonl",
            mode=manifest.memory.mode, reverify=manifest.memory.reverify,
            hint_budget=manifest.memory.hint_budget)
    return BringUpFrontend(
        manifest, cases=cases, runner=runner, oracles=oracles, fixer=fixer,
        memory=memory, is_escalate_only=isinstance(fixer, EscalateOnlyFixer))


def make_landing_fixer(
    *,
    diagnose,
    implement,
    refuter_factory,
    k: int = 3,
):
    """Wire a :class:`~peers.modes.bring_up.fixer.LandingFixer` whose
    adversarial-verify collaborator is the REAL
    :func:`peers.spine.adversarial_verify.verify_claim` — ``k`` independent
    refuters, the majority survival threshold, and a ledger ``gate`` row, exactly
    as develop's verify seam does.

    The three irreducibly-LLM steps stay INJECTED (this is the live-driver seam,
    mirroring bring-up's runner/oracle drivers and develop's Auditor/Author):

    * ``diagnose(case, judgment, run) -> Diagnosis`` — root-cause (tool-bug vs
      corpus-error) + the fix brief;
    * ``refuter_factory(case, diagnosis) -> (refuter(i) -> bool)`` — builds the
      k skeptics that try to REFUTE the tool-bug diagnosis (only an explicit
      ``False`` clears a vote; erroring/uncertain refuters are fail-closed);
    * ``implement(case, diagnosis, run) -> FixAttempt`` — the n>=2/TDD fix that
      lands + attests and reports the real ``head_sha``.

    ``verify_claim`` runs BEFORE ``implement`` (a refuted diagnosis is never
    written into the tool), and the frontend re-validates the returned sha
    (``resolves_to_commit`` + ``resolve_author``) as a second, independent gate.
    """
    from peers.spine.adversarial_verify import verify_claim

    from .fixer import LandingFixer

    # Fail FAST at wiring time, not as a mid-run crash inside verify_claim.
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"make_landing_fixer: k must be an int >= 1 (got {k!r})")

    def verify(case, diagnosis, run) -> bool:
        # Fail-CLOSED, symmetric with verify_claim's per-vote containment: a
        # refuter-factory that blows up is a non-survival, never a crashed run.
        try:
            refuter = refuter_factory(case, diagnosis)
        except Exception:
            return False
        return verify_claim(
            case.id,
            refuter=refuter,
            k=k,
            ledger=getattr(run, "ledger", None),
            mode_run=getattr(run, "mode_run", None),
        )

    return LandingFixer(diagnose=diagnose, verify=verify, implement=implement)
