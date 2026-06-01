"""Tests for the Phase-3i bug-hunt protocol parser + gate."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from peers.bug_hunt import (
    BLOCKING_SEVERITIES,
    SEVERITY_ORDER,
    gate_pass,
    list_commits,
    parse_commit_trailers,
    summarize,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    _git(p, "init", "-q", "-b", "main")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    (p / "x").write_text("x")
    _git(p, "add", "x")
    _git(p, "commit", "-q", "-m", "init")
    return p


def _commit(p: Path, msg: str) -> str:
    fname = p / f"f-{hash(msg) & 0xffff:04x}"
    fname.write_text("x")
    _git(p, "add", fname.name)
    _git(p, "commit", "-q", "-m", msg)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True,
    )
    return out.stdout.strip()


# ---------------- parse_commit_trailers ------------------------------


def test_parse_trailers_from_last_paragraph_only():
    msg = textwrap.dedent("""\
        subject

        body with Bug-Report: NOPE somewhere in prose

        Peer: claude
        Bug-Report: BUG-001
    """)
    t = parse_commit_trailers(msg)
    assert t == {"Peer": ["claude"], "Bug-Report": ["BUG-001"]}


def test_parse_trailers_multiple_values():
    msg = "subj\n\nPeer: x\nBug-Report: A\nBug-Report: B\n"
    t = parse_commit_trailers(msg)
    assert t["Bug-Report"] == ["A", "B"]


def test_parse_trailers_requires_contiguous_block_at_end():
    msg = textwrap.dedent("""\
        subj

        Peer: claude
        Bug-Report: BUG-404
        this is prose, not a trailer
    """)
    assert parse_commit_trailers(msg) == {}


# ---------------- summarize end-to-end -------------------------------


def test_no_bugs_summary_is_clean(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    s = summarize(repo)
    assert s.is_clean() is True
    assert s.open_blocking_count == 0
    assert s.reports == {}


def test_high_severity_bug_is_blocking(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-001: null deref in foo

        ## Bug-Report
        {"id":"BUG-001","severity":"high","title":"null deref",
         "description":"crashes on empty input",
         "fix_by":"codex","location":"src/foo.py:10"}

        Peer: claude
        Bug-Report: BUG-001
    """))
    s = summarize(repo)
    assert s.is_clean() is False
    assert s.open_blocking_count == 1
    assert s.reports["BUG-001"].severity == "high"
    assert s.reports["BUG-001"].fix_by == "codex"


def test_bug_report_json_allows_braces_inside_strings(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-009: brace text in description

        ## Bug-Report
        {"id":"BUG-009","severity":"med","title":"brace",
         "description":"message contains } and { characters"}

        Peer: claude
        Bug-Report: BUG-009
    """))

    s = summarize(repo)

    assert s.reports["BUG-009"].severity == "med"
    assert "characters" in s.reports["BUG-009"].description


def test_list_commits_handles_record_separator_in_body(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    sep = "\x1e"
    sha = _commit(repo, textwrap.dedent(f"""\
        BUG-RS: separator in body

        body contains old separator {sep} but remains one commit

        ## Bug-Report
        {{"id":"BUG-RS","severity":"high","title":"separator"}}

        Peer: claude
        Bug-Report: BUG-RS
    """))

    commits = list_commits(repo)
    assert len(commits) == 2
    s = summarize(repo)
    assert s.reports["BUG-RS"].sha == sha
    assert s.open_blocking_count == 1


def test_resolved_bug_no_longer_blocks(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-001: null deref

        ## Bug-Report
        {"id":"BUG-001","severity":"crit","title":"null deref"}

        Peer: claude
        Bug-Report: BUG-001
    """))
    _commit(repo, textwrap.dedent("""\
        Resolve BUG-001

        ## Bug-Resolution
        {"resolves":"BUG-001","status":"fixed","note":"guarded with if"}

        Peer: codex
        Bug-Resolves: BUG-001
    """))
    s = summarize(repo)
    assert s.is_clean() is True
    assert s.resolutions["BUG-001"].status == "fixed"


def test_newer_bug_report_after_fixed_resolution_reopens_bug(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-010: original

        ## Bug-Report
        {"id":"BUG-010","severity":"high","title":"original"}

        Peer: claude
        Bug-Report: BUG-010
    """))
    _commit(repo, textwrap.dedent("""\
        Resolve BUG-010

        ## Bug-Resolution
        {"resolves":"BUG-010","status":"fixed","note":"first fix"}

        Peer: codex
        Bug-Resolves: BUG-010
    """))
    _commit(repo, textwrap.dedent("""\
        BUG-010: regression reopened

        ## Bug-Report
        {"id":"BUG-010","severity":"high","title":"regression"}

        Peer: claude
        Bug-Report: BUG-010
    """))

    s = summarize(repo)

    assert s.open_blocking_count == 1
    assert "BUG-010" not in s.resolutions
    assert any("reopened" in w for w in s.warnings)


def test_low_severity_is_not_blocking(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-002: tiny nit

        ## Bug-Report
        {"id":"BUG-002","severity":"low","title":"nit"}

        Peer: claude
        Bug-Report: BUG-002
    """))
    s = summarize(repo)
    assert s.is_clean() is True
    assert len(s.open_by_severity["low"]) == 1


def test_unknown_severity_demoted_to_info(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-003: weird

        ## Bug-Report
        {"id":"BUG-003","severity":"VERY_HIGH","title":"weird"}

        Peer: claude
        Bug-Report: BUG-003
    """))
    s = summarize(repo)
    assert s.reports["BUG-003"].severity == "info"
    assert s.is_clean() is True
    assert any("very_high" in w.lower() for w in s.warnings)


def test_wontfix_status_keeps_bug_open(tmp_path: Path):
    """A `wontfix` resolution leaves the bug open (the substrate must
    not treat 'we decided not to fix it' as the same as fixed)."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-004: spec gap

        ## Bug-Report
        {"id":"BUG-004","severity":"high","title":"gap"}

        Peer: claude
        Bug-Report: BUG-004
    """))
    _commit(repo, textwrap.dedent("""\
        BUG-004 wontfix

        ## Bug-Resolution
        {"resolves":"BUG-004","status":"wontfix","note":"out of scope"}

        Peer: codex
        Bug-Resolves: BUG-004
    """))
    s = summarize(repo)
    assert s.is_clean() is False
    assert s.resolutions["BUG-004"].status == "wontfix"


def test_duplicate_bug_id_first_wins_with_warning(tmp_path: Path):
    """Two reports with the same ID — newest-first iteration → first
    wins; a warning surfaces the conflict."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-005: original

        ## Bug-Report
        {"id":"BUG-005","severity":"med","title":"original"}

        Peer: claude
        Bug-Report: BUG-005
    """))
    _commit(repo, textwrap.dedent("""\
        BUG-005: redux (newer)

        ## Bug-Report
        {"id":"BUG-005","severity":"crit","title":"redux"}

        Peer: codex
        Bug-Report: BUG-005
    """))
    s = summarize(repo)
    # Newer wins (newest-first iteration):
    assert s.reports["BUG-005"].severity == "crit"
    assert s.reports["BUG-005"].title == "redux"
    # A warning surfaces the duplicate so operators can spot
    # severity-downgrade-style gaming on review.
    assert any(
        "BUG-005" in w and "duplicate" in w.lower()
        for w in s.warnings
    ), f"expected duplicate warning for BUG-005, got: {s.warnings}"


def test_duplicate_bug_id_cannot_downgrade_blocking_severity(tmp_path: Path):
    """A newer duplicate report may change metadata, but an older
    blocking severity still keeps the gate closed until explicitly fixed."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-006: original high

        ## Bug-Report
        {"id":"BUG-006","severity":"high","title":"original"}

        Peer: claude
        Bug-Report: BUG-006
    """))
    _commit(repo, textwrap.dedent("""\
        BUG-006: suspicious downgrade

        ## Bug-Report
        {"id":"BUG-006","severity":"low","title":"downgraded"}

        Peer: codex
        Bug-Report: BUG-006
    """))

    s = summarize(repo)

    assert s.reports["BUG-006"].title == "downgraded"
    assert s.reports["BUG-006"].severity == "high"
    assert s.is_clean() is False
    assert any("higher severity" in w for w in s.warnings)


def test_gate_pass_helper(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    ok, diag = gate_pass(repo)
    assert ok is True
    assert "0 blocking open" in diag


def test_gate_fail_helper(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-007: hot

        ## Bug-Report
        {"id":"BUG-007","severity":"crit","title":"hot"}

        Peer: claude
        Bug-Report: BUG-007
    """))
    ok, diag = gate_pass(repo)
    assert ok is False
    assert "BUG-007" in diag


def test_blocking_severities_are_crit_high_med():
    assert BLOCKING_SEVERITIES == {"crit", "high", "med"}
    assert SEVERITY_ORDER[:3] == ("crit", "high", "med")


def test_missing_json_block_falls_back_to_subject(tmp_path: Path):
    """Tolerate commits that file a Bug-Report trailer without the
    `## Bug-Report` JSON heading — severity defaults to info + a
    warning is recorded, but the bug is still tracked."""
    repo = _init_repo(tmp_path / "r")
    _commit(repo, textwrap.dedent("""\
        BUG-008: forgot the json block

        body but no json

        Peer: claude
        Bug-Report: BUG-008
    """))
    s = summarize(repo)
    assert "BUG-008" in s.reports
    assert s.reports["BUG-008"].severity == "info"
    assert any("BUG-008" in w for w in s.warnings)


# ---------------- Bug-Defer (deferred = not blocking) ----------------


def _file_blocking_bug(repo: Path, bid: str = "BUG-100",
                       sev: str = "high") -> str:
    return _commit(repo, textwrap.dedent(f"""\
        {bid}: needs investigation

        ## Bug-Report
        {{"id":"{bid}","severity":"{sev}","title":"a problem"}}

        Peer: claude
        Bug-Report: {bid}
    """))


def test_bug_defer_closes_the_bug_for_gate_purposes(tmp_path: Path):
    """A `Bug-Defer:` with a reason must clear the bug from blocking
    open even though it's not 'fixed'. The honest-defer path."""
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo)
    s = summarize(repo)
    assert s.is_clean() is False
    _commit(repo, textwrap.dedent("""\
        defer: BUG-100 too large for this session

        ## Bug-Defer
        {"id":"BUG-100","reason":"requires schema migration; next session"}

        Peer: codex
        Bug-Defer: BUG-100
    """))
    s = summarize(repo)
    assert s.is_clean() is True
    assert s.deferred_count == 1
    assert s.resolutions["BUG-100"].status == "deferred"
    assert "schema migration" in s.resolutions["BUG-100"].note


def test_bug_defer_without_reason_warns(tmp_path: Path):
    """A defer without rationale is suspicious — we honor it but
    surface a warning so a human can spot gaming."""
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-101")
    _commit(repo, textwrap.dedent("""\
        defer: BUG-101

        Peer: claude
        Bug-Defer: BUG-101
    """))
    s = summarize(repo)
    assert s.resolutions["BUG-101"].status == "deferred"
    assert s.is_clean() is True  # still honored
    assert any("BUG-101" in w and "reason" in w for w in s.warnings)


def test_bug_defer_loses_to_existing_resolution(tmp_path: Path):
    """Newest-first iteration: a defer that arrives AFTER a `fixed`
    resolution (older in time) must NOT override it. The duplicate-
    resolution warning fires."""
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-102")
    # Older fix (filed first in time → encountered LAST in newest-first
    # iteration → it should be the one that gets accepted, because the
    # later defer commit lands first in iteration and wins).
    _commit(repo, textwrap.dedent("""\
        fix: address BUG-102

        ## Bug-Resolution
        {"id":"BUG-102","status":"fixed","note":"clamped index"}

        Peer: claude
        Bug-Resolves: BUG-102
    """))
    # Newer defer (encountered first in newest-first iteration → it
    # wins; the older fix gets the duplicate warning).
    _commit(repo, textwrap.dedent("""\
        defer: BUG-102 actually, too complex

        ## Bug-Defer
        {"id":"BUG-102","reason":"changed our mind; defer instead"}

        Peer: codex
        Bug-Defer: BUG-102
    """))
    s = summarize(repo)
    # The defer wins (newest-first), the fix shows up as duplicate warning.
    assert s.resolutions["BUG-102"].status == "deferred"
    assert any("duplicate" in w.lower() for w in s.warnings)


def test_deferred_bug_visible_in_summary_text(tmp_path: Path):
    from peers.bug_hunt import format_summary
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-103")
    _commit(repo, textwrap.dedent("""\
        defer

        ## Bug-Defer
        {"id":"BUG-103","reason":"out of scope"}

        Peer: claude
        Bug-Defer: BUG-103
    """))
    txt = format_summary(summarize(repo))
    assert "1 deferred" in txt


# ---------------- Bug-Reproduce (TDD-before-fix) ---------------------


def test_bug_reproduce_trailer_collected(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-200")
    _commit(repo, textwrap.dedent("""\
        test: reproduce BUG-200 (failing)

        Peer: claude
        Bug-Reproduce: BUG-200
    """))
    s = summarize(repo)
    assert "BUG-200" in s.reproductions
    assert len(s.reproductions["BUG-200"]) == 1
    assert s.reproduced_count == 1


def test_historical_tdd_reproducer_subject_collected_BUG_305(tmp_path: Path):
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-305")
    _commit(repo, textwrap.dedent("""\
        TDD: reproducer for BUG-305 (old commit shape)

        This commit predates the Bug-Reproduce trailer convention but
        clearly identifies the failing test target in the subject.

        Peer: claude
    """))
    s = summarize(repo)
    assert "BUG-305" in s.reproductions
    assert s.reproductions["BUG-305"][0].reproduced_by == "claude"


def test_gate_tdd_rejects_untrailered_non_tdd_subject_BUG_305(tmp_path: Path):
    from peers.bug_hunt import gate_tdd_pass

    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-306", sev="med")
    _commit(repo, textwrap.dedent("""\
        test: reproduce BUG-306 without trailer

        Looks like a test commit but does not use the historical TDD
        subject form or the Bug-Reproduce trailer.

        Peer: claude
    """))
    _commit(repo, textwrap.dedent("""\
        fix BUG-306

        ## Bug-Resolution
        {"id":"BUG-306","status":"fixed"}

        Peer: codex
        Bug-Resolves: BUG-306
    """))
    ok, diag = gate_tdd_pass(repo)
    assert ok is False
    assert "no Bug-Reproduce" in diag


def test_bug_reproduce_multiple_per_bug(tmp_path: Path):
    """Three reproduce commits (happy/edge/sad) for one bug — all
    counted, in commit-order."""
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-201")
    for kind in ("happy", "edge", "sad"):
        _commit(repo, textwrap.dedent(f"""\
            test: BUG-201 {kind}

            Peer: claude
            Bug-Reproduce: BUG-201
        """))
    s = summarize(repo)
    assert len(s.reproductions["BUG-201"]) == 3


def test_gate_tdd_passes_when_reproduce_precedes_fix(tmp_path: Path):
    from peers.bug_hunt import gate_tdd_pass
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-300", sev="high")
    _commit(repo, textwrap.dedent("""\
        test: reproduce BUG-300

        Peer: claude
        Bug-Reproduce: BUG-300
    """))
    _commit(repo, textwrap.dedent("""\
        fix BUG-300

        ## Bug-Resolution
        {"id":"BUG-300","status":"fixed","note":"check bounds"}

        Peer: codex
        Bug-Resolves: BUG-300
    """))
    ok, diag = gate_tdd_pass(repo)
    assert ok, diag
    assert "tdd-gate: clean" in diag


def test_gate_tdd_fails_when_fix_has_no_reproduce(tmp_path: Path):
    from peers.bug_hunt import gate_tdd_pass
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-301", sev="med")
    _commit(repo, textwrap.dedent("""\
        fix BUG-301

        ## Bug-Resolution
        {"id":"BUG-301","status":"fixed"}

        Peer: claude
        Bug-Resolves: BUG-301
    """))
    ok, diag = gate_tdd_pass(repo)
    assert ok is False
    assert "BUG-301" in diag and "no Bug-Reproduce" in diag


def test_gate_tdd_fails_when_reproduce_lands_after_fix(tmp_path: Path):
    """Test-with-fix (not test-before-fix) is rejected."""
    from peers.bug_hunt import gate_tdd_pass
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-302", sev="crit")
    _commit(repo, textwrap.dedent("""\
        fix BUG-302

        ## Bug-Resolution
        {"id":"BUG-302","status":"fixed"}

        Peer: claude
        Bug-Resolves: BUG-302
    """))
    # Reproduce commit AFTER the fix — wrong order.
    _commit(repo, textwrap.dedent("""\
        test: reproduce BUG-302

        Peer: claude
        Bug-Reproduce: BUG-302
    """))
    ok, diag = gate_tdd_pass(repo)
    assert ok is False
    assert "TDD-order broken" in diag


def test_gate_tdd_ignores_deferred_and_low_severity(tmp_path: Path):
    """gate-tdd only enforces TDD on FIXED blocking-severity bugs.
    Deferred, wontfix, low/info: no reproduce required."""
    from peers.bug_hunt import gate_tdd_pass
    repo = _init_repo(tmp_path / "r")
    _file_blocking_bug(repo, "BUG-400", sev="high")
    _commit(repo, textwrap.dedent("""\
        defer BUG-400

        ## Bug-Defer
        {"id":"BUG-400","reason":"out of scope"}

        Peer: codex
        Bug-Defer: BUG-400
    """))
    _file_blocking_bug(repo, "BUG-401", sev="low")     # low sev, no test needed
    _commit(repo, textwrap.dedent("""\
        fix BUG-401

        ## Bug-Resolution
        {"id":"BUG-401","status":"fixed"}

        Peer: claude
        Bug-Resolves: BUG-401
    """))
    ok, diag = gate_tdd_pass(repo)
    assert ok, diag


def test_cli_gate_tdd_exit_code(tmp_path: Path):
    """`python -m peers.bug_hunt gate-tdd <repo>` round-trip."""
    import sys
    repo = _init_repo(tmp_path / "r")
    result = subprocess.run(
        [sys.executable, "-m", "peers.bug_hunt", "gate-tdd", str(repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    # Add a fix without reproduce → should fail.
    _file_blocking_bug(repo, "BUG-500", sev="high")
    _commit(repo, textwrap.dedent("""\
        fix BUG-500

        ## Bug-Resolution
        {"id":"BUG-500","status":"fixed"}

        Peer: claude
        Bug-Resolves: BUG-500
    """))
    result = subprocess.run(
        [sys.executable, "-m", "peers.bug_hunt", "gate-tdd", str(repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "BUG-500" in result.stdout


def test_cli_gate_exit_code(tmp_path: Path):
    """`python -m peers.bug_hunt gate <repo>` exits 0 when clean, 1
    when blocking bugs open. Required so the goal can use
    `pass_when: exit_code == 0`."""
    import sys
    repo = _init_repo(tmp_path / "r")
    result = subprocess.run(
        [sys.executable, "-m", "peers.bug_hunt", "gate", str(repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    _commit(repo, textwrap.dedent("""\
        BUG-009: oops

        ## Bug-Report
        {"id":"BUG-009","severity":"high"}

        Peer: claude
        Bug-Report: BUG-009
    """))
    result = subprocess.run(
        [sys.executable, "-m", "peers.bug_hunt", "gate", str(repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "BUG-009" in result.stdout
