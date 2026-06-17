"""STEP-2..5 — ``DevelopFrontend``: the first concrete ModeFrontend.

One round of develop is: **AUDIT** (the injected :class:`Auditor`) →
**adversarial VERIFY** (each finding through
:func:`peers.spine.adversarial_verify.verify_claim`, k refuters) → **AUTHOR**
the survivors into a frozen-able implement contract (the injected
:class:`Author`) → **IMPLEMENT** that contract (the injected
:class:`Implementer`) → record a witnessed, attested ``confirmed-work`` and a
branch-PR ``landing``. The Stage-0 :func:`peers.spine.mode_run.drive` loop calls
``prepare`` once then ``run`` per round, terminating on stop-on-dry.

Load-bearing invariants (tighten-only — develop must not weaken the spine):

* A round records ``confirmed-work`` ONLY for a finding that was adversarially
  CONFIRMED, AUTHORED into a contract, and IMPLEMENTED to a result whose
  ``head_sha`` **resolves to a real 40-hex commit** in the target — written via
  :meth:`RunLedger.append_attested` with a ``git-sha`` witness that the spine
  ``witness-ledgered`` gate re-derives. An unattested-but-real commit is still
  written (so the negative path is auditable) but resolves to ``author=None`` →
  it is a *fake* confirm that does NOT reset the dry streak. A non-resolvable
  ``head_sha`` is a ``dry-round`` (no confirmed-work row at all).
* develop NEVER edits code freehand: every change flows through the AUTHOR seam.
  A ``None`` author result, no surviving finding, or a blocked run is a
  ``dry-round``.
* ``landing=branch-pr`` is the only landing Stage 1 performs.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from peers.spine.adversarial_verify import verify_claim
from peers.spine.baseline import BaselineAuthor, ensure_bar
from peers.spine.direction import Bar, infer_bar
from peers.spine.gates import resolves_to_commit
from peers.spine.landing import build_landing_contract
from peers.spine.mode_run import ModeRun
from peers.spine.self_hosting import is_self_hosting
from peers.develop.ports import Auditor, Author, Finding, Implementer


def _converged_changed_paths(repo, base_sha, head_sha):
    """The changed paths the run introduces vs its RECORDED BASE (the lease-time
    fork point on ``ModeRun.base_sha``) — the ACTUAL converged diff
    :func:`is_self_hosting` inspects. NEVER ``merge-base(HEAD, head_sha)``: in
    ``run_isolated`` the worktree HEAD IS the branch tip, so that base ==
    ``head_sha`` => an EMPTY diff (the path-glob layer would never fire on the real
    dogfood path). Uses ``--no-renames`` + ``-z``: a rename out of governance is
    surfaced as a delete of the OLD path (B1), and the NUL-delimited output is
    ``core.quotePath``-immune (B2). Returns ``None`` on any git error or a missing
    base (so ``is_self_hosting`` fails safe -> self-hosting)."""
    import subprocess
    if not base_sha:
        return None
    try:
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", "-z",
             f"{base_sha}..{head_sha}"],
            capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        # a hung git (TimeoutExpired -- a SubprocessError, NOT an OSError) or a missing/
        # unexecutable git (FileNotFoundError) MUST NOT propagate out of run()/drive() and
        # crash the run -- fail safe to None so is_self_hosting => self-hosting (the
        # docstring's promise; mirrors self_hosting._common_dir's except clause).
        return None
    if diff.returncode != 0:
        return None
    return [p for p in diff.stdout.split("\x00") if p]   # split on NUL, not newlines

#: A refuter factory that refutes every finding (fail-closed). The real auditor
#: adapter injects refuters that actually attempt refutation; absent one, no
#: finding can survive — uncertainty never helps a claim (mirrors verify_claim).
def _default_refuter_factory(finding: Finding) -> Callable[[int], bool]:
    return lambda i: True


class DevelopFrontend:
    """A :class:`peers.spine.mode_run.ModeFrontend` for develop mode.

    Capabilities are injected as ports so the orchestration is deterministic in
    tests: ``auditor`` (AUDIT), ``author`` (AUTHOR), ``implementer`` (IMPLEMENT).
    ``dimensions`` are passed to the auditor; ``run_tests`` feeds
    ``direction.infer_bar`` in :meth:`prepare`; ``k`` is the verify vote count;
    ``refuter_factory(finding)`` yields that finding's refuter.
    """

    def __init__(
        self,
        auditor: Auditor,
        author: Author,
        implementer: Implementer,
        *,
        dimensions: list[str],
        run_tests: Callable[[str], "tuple[int, str] | None"],
        k: int = 2,
        refuter_factory: Callable[[Finding], Callable[[int], bool]] | None = None,
        baseline_author: BaselineAuthor | None = None,
        run_tests_factory: "Callable[[Path], Callable[[str], tuple[int, str] | None]] | None" = None,
    ) -> None:
        self.auditor = auditor
        self.author = author
        self.implementer = implementer
        self.dimensions = dimensions
        self.run_tests = run_tests
        #: CB-4: when injected, ``prepare`` builds the bar runner against ``run.tool``
        #: at call time -- so a FLEET run infers/builds the bar inside the LEASED
        #: worktree, not a repo frozen at construction. ``None`` keeps the legacy
        #: behaviour: the construction-bound ``run_tests`` is used as-is (the
        #: deterministic test seam, and correct for single-repo ``peers develop``
        #: where ``repo == run.tool``).
        self.run_tests_factory = run_tests_factory
        self.k = k
        self.refuter_factory = refuter_factory or _default_refuter_factory
        #: Stage-4 opt-in port: when injected, ``prepare`` BUILDS a
        #: characterization baseline for a weak/absent bar (or stops honestly).
        #: ``None`` = the Stage-1 behaviour (an absent bar just blocks).
        self.baseline_author = baseline_author
        self.bar: Bar | None = None
        self._blocked = False

    # ---- ModeFrontend seam -------------------------------------------------
    def prepare(self, run: ModeRun) -> None:
        """Establish the quality bar BEFORE any change. With a ``baseline_author``
        injected (Stage-4), build-or-stop via ``baseline.ensure_bar``: a
        ``weak``/``absent`` bar is turned into a trustworthy ``present``/``built``
        bar by AUTHORING+greening characterization observations, or — when the
        tool is uncharacterizable — the bar stays ``absent``. With NO
        ``baseline_author`` (Stage-1 behaviour), just detect via ``infer_bar``.
        An *absent* bar blocks ALL work: ``run()`` then does nothing but emit dry
        rounds (an honest stop, never silent freehand work against a tool with no
        trustworthy bar)."""
        # CB-4: bind the bar runner to run.tool at CALL time when a factory is wired
        # (the production default) -- a fleet run leases a worktree whose path is
        # only known now, not at construction. Falls back to the construction-bound
        # runner (test seam / single-repo peers develop where repo == run.tool).
        run_tests = (self.run_tests_factory(run.tool)
                     if self.run_tests_factory is not None else self.run_tests)
        if self.baseline_author is not None:
            self.bar = ensure_bar(run.tool, run_tests,
                                  author=self.baseline_author, ledger=run.ledger,
                                  mode_run=run.mode_run)
        else:
            self.bar = infer_bar(run.tool, run_tests, ledger=run.ledger,
                                 mode_run=run.mode_run)
        self._blocked = self.bar.kind == "absent"

    def run(self, run: ModeRun) -> None:
        """Perform one develop round, appending its outcome row(s)."""
        if self._blocked:
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # AUDIT
        findings = self.auditor.audit(run.tool, self.dimensions)

        # adversarial VERIFY: every finding faces k refuters; only survivors pass.
        survivors: list[Finding] = []
        for f in findings:
            if verify_claim(f.id, refuter=self.refuter_factory(f), k=self.k,
                            ledger=run.ledger, mode_run=run.mode_run):
                survivors.append(f)
        if not survivors:
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # AUTHOR — develop never edits freehand; no contract → dry round.
        contract = self.author.author(survivors, run.tool)
        if contract is None:
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # IMPLEMENT — converge the contract to a real commit.
        result = self.implementer.implement(contract, run.tool)
        head_sha = result.head_sha
        if not (result.ok and head_sha is not None
                and resolves_to_commit(run.tool, head_sha)):
            run.ledger.append(event="dry-round", status="dry",
                              mode_run=run.mode_run)
            return

        # confirmed-work: attested (author derived from the substrate note —
        # an unattested commit resolves to author=None, a fake confirm that does
        # NOT reset stop-on-dry) + git-sha witnessed (the spine re-derives it).
        # guard: drive() does NOT catch IndexError, so never index an empty list.
        #
        # HS-05: independence=True is passed UNCONDITIONALLY and deliberately — do
        # NOT "harden" it to a value derived from whether the author resolved. It is
        # LOAD-BEARING for defense-in-depth: append_attested re-derives the real
        # author from refs/notes/peers-attest (None for an unattested/forged commit),
        # and the authorship-attested gate ONLY scrutinises independence=True rows
        # (it skips the rest). So independence=True forces the gate to RE-DERIVE and
        # REJECT an unattested confirm (author=None) as a second, independent layer
        # beyond the dry_streak `_is_real_confirmed_work` check. A derived False here
        # would make the gate skip the row and pass vacuously — silently removing a
        # tested layer (pinned by test_develop_end_to_end: unattested -> independence
        # True + authorship-attested gate False). Unlike find-bugs (which omits the
        # row entirely), develop keeps the auditable row AND the gate rejection.
        subj = contract.findings[0] if contract.findings else None
        run.ledger.append_attested(
            run.tool, head_sha, event="confirmed-work", subject=subj,
            status="pass",
            witness={"kind": "git-sha", "uri": result.head_sha,
                     "sha256": result.head_sha},
            independence=True,
            mode_run=run.mode_run,
        )
        # landing: a plain append (NOT a substrate-attested authorship event) —
        # branch-PR delivery. Stage-4 upgrades the thin row to carry the structured
        # LandingContract: mergeable + the per-gate map are DERIVED from the run's
        # own ledger rows (read AFTER the confirmed-work append above, so the
        # witness-ledgered gate sees it) — never from agent text. Witness kind
        # stays 'url' (advisory, recorded not gate-checked — the spine re-derives
        # only 'file'/'git-sha', so this append self-greens no gate).
        #
        # Stage 6 (§6.3): compute REAL self-hosting from the converged diff (the
        # run's changed files vs its RECORDED base — run.base_sha, the lease-time
        # fork point) — never the Stage-4 constant False, and never a
        # merge-base(HEAD, ...) base (EMPTY in run_isolated). A legacy single-HEAD
        # run (run.branch is None / no base_sha) has no isolated diff to inspect ->
        # trusted-by-shape (it also cannot auto-merge: land() refuses
        # no-isolated-branch). is_self_hosting fails safe to True on any
        # undeterminable diff (None changed_paths) AND when target_repo IS peers
        # (the dogfood: run.tool is the peers worktree -> identity layer fires).
        if run.branch is not None:
            changed = _converged_changed_paths(run.tool, run.base_sha,
                                               result.head_sha)
            self_hosting, _reason = is_self_hosting(run.tool, changed_paths=changed,
                                                    target_repo=run.tool)
        else:
            self_hosting = False
        lc = build_landing_contract(
            run.ledger.read(), repo=run.tool, mode_run=run.mode_run,
            branch=result.branch, head_sha=result.head_sha,
            landing_mode=run.op_config.landing, self_hosting=self_hosting)
        run.ledger.append(event="landing", status="ok", subject=result.branch,
                          witness=lc.to_witness(), mode_run=run.mode_run)
        # Stage 5: a propagation row distinct from landing -- a CONVERGED artifact
        # a dependent run may consume (attested + git-sha witnessed,
        # gate-re-derivable), vs `landing` (human-merge delivery, url-witnessed,
        # NOT gate-checked). Only when the run is isolated on its own branch
        # (legacy single-HEAD runs emit landing only). result.head_sha already
        # passed resolves_to_commit in the confirmed-work branch above, so this
        # git-sha witness re-derives by construction.
        if run.branch is not None:
            run.ledger.append_attested(
                run.tool, head_sha, event="propagation", subject=run.branch,
                status="ok",
                witness={"kind": "git-sha", "uri": result.head_sha,
                         "sha256": result.head_sha, "artifact": run.branch},
                independence=True, mode_run=run.mode_run)

    def interpret(self, run: ModeRun) -> dict:
        """Summarise the run: number of confirmed units, total rounds, the last
        landing target, and the Stage-4 ``mergeable``/``gates``/``baseline_provenance``.

        ``mergeable``/``gates`` are RE-DERIVED from the LIVE ledger via
        ``build_landing_contract`` — NEVER read off the stored landing-witness text
        (a ``url``-kind advisory record is not kernel-bound, so a forged second
        landing row claiming ``mergeable=True`` cannot fool this read path).
        Guards every empty case so no IndexError/KeyError/AttributeError escapes
        (``drive()`` does not catch those); a ``None`` head_sha makes the contract
        record ``mergeable=False`` rather than crash."""
        rows = run.ledger.read()
        confirmed = sum(1 for r in rows if r.event == "confirmed-work")
        rounds = sum(1 for r in rows if r.event in ("dry-round", "confirmed-work"))
        landings = [r for r in rows if r.event == "landing"]
        last_landing = landings[-1].subject if landings else None
        provenance = self.bar.provenance if self.bar is not None else "detected"

        if not landings:
            # No landing row -> nothing to re-derive; the stored contract (if any)
            # is display-only, NEVER the source of the mergeable verdict.
            return {"confirmed": confirmed, "rounds": rounds, "landing": None,
                    "mergeable": False, "gates": {},
                    "baseline_provenance": provenance}

        # Re-derive from the live ledger: branch = last landing subject; head =
        # the LAST confirmed-work's git-sha witness uri (None if absent).
        confirmed_rows = [r for r in rows if r.event == "confirmed-work"]
        head_sha = None
        if confirmed_rows:
            wit = confirmed_rows[-1].witness
            if isinstance(wit, dict):
                head_sha = wit.get("uri")
        lc = build_landing_contract(rows, repo=run.tool, mode_run=run.mode_run,
                                    branch=last_landing or "", head_sha=head_sha)
        return {"confirmed": confirmed, "rounds": rounds, "landing": last_landing,
                "mergeable": lc.mergeable, "gates": lc.gates,
                "baseline_provenance": provenance}
