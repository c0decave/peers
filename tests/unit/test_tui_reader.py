"""Wave-1a: fail-soft read-only readers for the TUI (state.json + runs.jsonl)."""
from __future__ import annotations

import json
import os

from peers_ctl.tui import reader as R


# --------------------------------------------------------------------------- #
# Task 4: read_state — happy / sad / edge                                      #
# --------------------------------------------------------------------------- #
def test_read_state_happy(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"iteration": 7, "peer_order": ["claude"], "turn_index": 0}))
    st = R.read_state(p)
    assert st["iteration"] == 7


def test_read_state_missing_returns_empty(tmp_path):
    assert R.read_state(tmp_path / "nope.json") == {}


def test_read_state_corrupt_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert R.read_state(p) == {}


def test_read_state_oversized_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{" + " " * (6 * 1024 * 1024) + "}")
    assert R.read_state(p, max_bytes=5 * 1024 * 1024) == {}


def test_read_state_symlink_returns_empty(tmp_path):
    # edge: a symlinked state.json is refused by safe_io -> fail-soft {}.
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"iteration": 1}))
    link = tmp_path / "state.json"
    os.symlink(real, link)
    assert R.read_state(link) == {}


def test_read_state_non_object_json_returns_empty(tmp_path):
    # edge: top-level JSON array/scalar is not a state dict -> {}.
    p = tmp_path / "state.json"
    p.write_text("[1, 2, 3]")
    assert R.read_state(p) == {}


def test_read_state_oversize_enforced_before_parse(tmp_path):
    # M1 edge: a SMALL valid-JSON prefix that on its own parses, followed by a
    # huge run of trailing bytes. The size cap must be ENFORCED (length check)
    # BEFORE parsing so this degrades to {} rather than parsing the prefix.
    cap = 32
    p = tmp_path / "state.json"
    p.write_text('{"a":1}' + " " * cap)
    assert R.read_state(p, max_bytes=cap) == {}


# --------------------------------------------------------------------------- #
# Task 5: gate_views — happy / sad / edge                                      #
# --------------------------------------------------------------------------- #
def test_gate_views_hard_soft_and_stuck():
    state = {
        "goals_status": {
            "tests-pass": {"state": "pass", "diagnostic": "", "duration_ms": 0},
            "no-shortcut": {"state": "fail", "diagnostic": "3 hits", "duration_ms": 12},
        },
        "soft_status": {"code-quality": {"consensus_count": 1}},
        "stuck_counter": {"no-shortcut": 3},
    }
    gv = {g.id: g for g in R.gate_views(state, soft_needed={"code-quality": 2})}
    assert gv["tests-pass"].kind == "hard" and gv["tests-pass"].cached is True
    assert gv["no-shortcut"].state == "fail" and gv["no-shortcut"].stuck == 3
    assert gv["no-shortcut"].cached is False  # fail is never cached
    assert gv["code-quality"].kind == "soft" and gv["code-quality"].consensus == (1, 2)
    assert gv["code-quality"].state == "pending"  # 1 < 2


def test_gate_views_empty_state():
    # sad: no gate data at all -> [].
    assert R.gate_views({}) == []


def test_gate_views_non_dict_state_returns_empty():
    # I1 sad: a non-dict state must degrade to [] (never raise AttributeError),
    # matching the module's "never raise" contract.
    assert R.gate_views(None) == []
    assert R.gate_views([]) == []
    assert R.gate_views("x") == []
    assert R.gate_views(5) == []


def test_gate_views_soft_reached_default_need_and_malformed_skipped():
    # edge: soft need defaults to 2 when not supplied; a reached count flips to
    # "reached"; non-dict goal/soft entries are skipped without crashing; a
    # passing hard gate with non-zero duration is NOT cached.
    state = {
        "goals_status": {
            "tests-pass": {"state": "pass", "duration_ms": 42},  # ran, not cached
            "bogus": "not-a-dict",                                # skipped
        },
        "soft_status": {
            "consensus-gate": {"consensus_count": 2},            # 2 >= default 2
            "junk": ["nope"],                                    # skipped
        },
    }
    gv = {g.id: g for g in R.gate_views(state)}
    assert "bogus" not in gv and "junk" not in gv
    assert gv["tests-pass"].cached is False and gv["tests-pass"].duration_ms == 42
    assert gv["consensus-gate"].state == "reached" and gv["consensus-gate"].consensus == (2, 2)


def test_gate_views_non_finite_numbers_degrade_to_defaults():
    # BUG-705 edge: Python json accepts Infinity; fail-soft gate views must not
    # crash while coercing duration/stuck/consensus counters.
    state = {
        "goals_status": {
            "tests-pass": {
                "state": "pass",
                "duration_ms": float("inf"),
            },
        },
        "soft_status": {"review": {"consensus_count": float("inf")}},
        "stuck_counter": {"tests-pass": float("inf")},
    }

    gv = {g.id: g for g in R.gate_views(
        state, soft_needed={"review": float("inf")}
    )}

    assert gv["tests-pass"].duration_ms == 0
    assert gv["tests-pass"].stuck == 0
    assert gv["review"].consensus == (0, 2)
    assert gv["review"].state == "pending"


# --------------------------------------------------------------------------- #
# Task 6: convergence_view — happy / sad / edge                                #
# --------------------------------------------------------------------------- #
def test_convergence_view_non_implement_has_no_phase():
    # happy (non-implement run): phase fields are ABSENT -> None.
    cv = R.convergence_view({"consecutive_clean_ticks": 2})
    assert cv.consecutive_clean_ticks == 2 and cv.convergence_phase is None
    assert cv.phase_b_extra_ticks is None


def test_convergence_view_implement_mode():
    # happy (implement run): phase fields present and surfaced.
    cv = R.convergence_view({"consecutive_clean_ticks": 0,
                             "convergence_phase": "B", "phase_b_extra_ticks": 1})
    assert cv.convergence_phase == "B" and cv.phase_b_extra_ticks == 1


def test_convergence_view_empty_state():
    # sad/edge: empty/garbage state -> zeroed, no phase, no crash.
    cv = R.convergence_view({})
    assert cv.consecutive_clean_ticks == 0
    assert cv.convergence_phase is None and cv.phase_b_extra_ticks is None


def test_convergence_view_non_dict_state_returns_default():
    # I1 sad: a non-dict state must degrade to the zero/None default (never
    # raise AttributeError), matching the module's "never raise" contract.
    for bad in (None, [], "x", 5):
        cv = R.convergence_view(bad)
        assert cv.consecutive_clean_ticks == 0
        assert cv.convergence_phase is None and cv.phase_b_extra_ticks is None


def test_convergence_view_coerces_display_field_types():
    # M1 sad: malformed state leaks a wrong type onto the public view-model
    # contract (convergence_phase is str|None, phase_b_extra_ticks is int|None).
    # A non-str phase must be coerced to str (never leak an int); a
    # non-coercible extra-ticks must degrade to None (never leak a str).
    cv = R.convergence_view({"convergence_phase": 5, "phase_b_extra_ticks": "x"})
    assert cv.convergence_phase is None or isinstance(cv.convergence_phase, str)
    assert not isinstance(cv.convergence_phase, bool)
    assert not isinstance(cv.convergence_phase, int)
    assert cv.convergence_phase == "5"
    assert cv.phase_b_extra_ticks is None or isinstance(cv.phase_b_extra_ticks, int)
    assert cv.phase_b_extra_ticks is None  # "x" is not coercible


def test_convergence_view_non_finite_numbers_degrade_to_defaults():
    # BUG-705 sad: Python's json parser accepts Infinity, and int(inf)
    # raises OverflowError. The fail-soft TUI reader must still degrade.
    cv = R.convergence_view({
        "consecutive_clean_ticks": float("inf"),
        "phase_b_extra_ticks": float("inf"),
    })
    assert cv.consecutive_clean_ticks == 0
    assert cv.phase_b_extra_ticks is None


# --------------------------------------------------------------------------- #
# Task 7: peer_views + current_peer — happy / sad / edge                       #
# --------------------------------------------------------------------------- #
def test_peer_views_four_states_and_float_runs():
    # happy: 4 states incl. unavailable; recent_runs keeps the 0.5 float credit;
    # current peer = peer_order[turn_index].
    state = {
        "peer_order": ["claude", "codex"], "turn_index": 1,
        "peers": {
            "claude": {"state": "healthy", "consecutive_fails": 0.0,
                       "recent_runs": [True, 0.5, False]},
            "codex": {"state": "unavailable", "consecutive_fails": 2.0,
                      "recent_runs": [False], "unavailable_reason": "auth"},
        },
    }
    pv = {p.name: p for p in R.peer_views(state)}
    assert pv["codex"].state == "unavailable"
    assert 0.5 in pv["claude"].recent_runs  # NOT coerced to bool
    assert pv["claude"].recent_runs == [True, 0.5, False]
    assert pv["codex"].consecutive_fails == 2.0  # float, not int
    assert R.current_peer(state) == "codex"


def test_peer_views_missing_peers_returns_empty():
    # sad: no peers map at all -> [] and current_peer tolerates the gap.
    assert R.peer_views({}) == []
    assert R.current_peer({}) is None


def test_peer_views_sparse_last_run_and_bad_index():
    # edge: last_run is sparse (read with .get()); a turn_index out of bounds /
    # malformed peer_order -> current_peer None, no crash; an unknown state
    # string is passed through for display (not dropped).
    state = {
        "peer_order": ["claude"], "turn_index": 5,  # out of bounds
        "peers": {
            "claude": {"state": "weird-future-state", "consecutive_fails": 1.0,
                       "recent_runs": [], "last_run": {"classification": "success"}},
        },
    }
    pv = {p.name: p for p in R.peer_views(state)}
    assert pv["claude"].state == "weird-future-state"      # passed through
    assert pv["claude"].last_run.get("classification") == "success"
    assert pv["claude"].last_run.get("missing-key") is None  # sparse-safe
    assert R.current_peer(state) is None                    # bad index guarded


# --------------------------------------------------------------------------- #
# Task 8: budget_view — happy / sad / edge                                     #
# --------------------------------------------------------------------------- #
def test_budget_view_happy_all_fields():
    state = {
        "budget": {
            "spent_runtime_s": 120, "max_runtime_s": 21600,
            "spent_tokens": 5000, "max_tokens": 1000000,
            "spent_usd": 1.25, "max_usd": 10.0,
            "max_usd_mode": "warn", "max_usd_mode_reason": "oauth-subscription",
            "consecutive_failures": 1,
            "wasted_runtime_per_tick": [
                {"iteration": 3, "peer": "codex", "duration_s": 42},
            ],
        },
    }
    b = R.budget_view(state)
    assert b.spent_runtime_s == 120 and b.max_runtime_s == 21600
    assert b.spent_tokens == 5000 and b.max_tokens == 1000000
    assert b.spent_usd == 1.25 and b.max_usd == 10.0
    assert b.max_usd_mode == "warn" and b.max_usd_mode_reason == "oauth-subscription"
    assert b.consecutive_failures == 1
    assert b.wasted_runtime == [{"iteration": 3, "peer": "codex", "duration_s": 42}]


def test_budget_view_missing_budget_zeros_and_none():
    # sad: no budget block -> zeroed spent, None caps, [] wasted, no crash.
    b = R.budget_view({})
    assert b.spent_runtime_s == 0 and b.spent_tokens == 0 and b.spent_usd == 0.0
    assert b.max_runtime_s is None and b.max_tokens is None and b.max_usd is None
    assert b.max_usd_mode is None and b.max_usd_mode_reason is None
    assert b.consecutive_failures == 0 and b.wasted_runtime == []


def test_budget_view_wasted_absent_or_malformed_is_empty():
    # edge: wasted_runtime_per_tick absent -> []; a non-list value -> [].
    assert R.budget_view({"budget": {"spent_usd": 0.5}}).wasted_runtime == []
    assert R.budget_view({"budget": {"wasted_runtime_per_tick": "nope"}}).wasted_runtime == []


def test_budget_view_coerces_mode_field_types():
    # M1 sad: max_usd_mode / max_usd_mode_reason are str|None on the public
    # view-model contract. A malformed budget that carries a non-str (e.g. an
    # int) must be coerced to str (or None) — never leak an int onto a str|None
    # field that Plan 1b will trust.
    b = R.budget_view({"budget": {"max_usd_mode": 42, "max_usd_mode_reason": 7}})
    for field_val in (b.max_usd_mode, b.max_usd_mode_reason):
        assert field_val is None or isinstance(field_val, str)
        assert not isinstance(field_val, bool)
        assert not isinstance(field_val, int)
    assert b.max_usd_mode == "42" and b.max_usd_mode_reason == "7"


# --------------------------------------------------------------------------- #
# Task 9: tick_entries (runs.jsonl) — happy / sad / edge                       #
# --------------------------------------------------------------------------- #
def test_tick_entries_tolerates_exit_and_torn_lines(tmp_path):
    # happy + edge: a normal tick, the synthetic exit line (no iteration/peer/
    # classification), and a torn final line that must be skipped.
    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"ts":"t1","iteration":1,"peer":"claude","classification":"success",'
        '"success":true,"tokens_this_tick":10,"usd_this_tick":0.1,'
        '"head_before":"a","head_after":"b","warnings_emitted":[]}\n'
        '{"event":"exit","reason":"complete","ticks_in_run":1,"ts":"t2"}\n'
        '{"ts":"t3","iteration":2,"peer":"codex","classific'  # torn last line
    )
    ents = R.tick_entries(p)
    assert len(ents) == 2
    assert ents[0].iteration == 1 and ents[0].is_exit is False
    assert ents[0].peer == "claude" and ents[0].success is True
    assert ents[0].tokens == 10 and ents[0].usd == 0.1
    assert ents[0].head_before == "a" and ents[0].head_after == "b"
    assert ents[1].is_exit is True and ents[1].exit_reason == "complete"
    assert ents[1].iteration is None and ents[1].peer is None  # exit line is sparse


def test_tick_entries_missing_file_returns_empty(tmp_path):
    # sad: no runs.jsonl -> [].
    assert R.tick_entries(tmp_path / "nope.jsonl") == []


def test_tick_entries_coerces_str_optional_fields(tmp_path):
    # M2 edge: a malformed line where a str|None field carries a non-str (e.g.
    # "peer": 99) must be coerced to str (or None) — never leak a non-str type
    # onto a str|None field.
    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"ts":"t1","iteration":1,"peer":99,"classification":7,'
        '"head_before":1,"head_after":2,"success":true}\n'
    )
    ents = R.tick_entries(p)
    assert len(ents) == 1
    e = ents[0]
    assert e.peer == "99" and isinstance(e.peer, str)
    for field_val in (e.peer, e.classification, e.head_before, e.head_after,
                      e.exit_reason):
        assert field_val is None or isinstance(field_val, str)
        assert not isinstance(field_val, bool)
        assert not isinstance(field_val, int)


def test_tick_entries_coerces_exit_reason(tmp_path):
    # M2 edge: the synthetic exit line's reason is also str|None coerced.
    p = tmp_path / "runs.jsonl"
    p.write_text('{"event":"exit","reason":42,"ts":"t2"}\n')
    ents = R.tick_entries(p)
    assert len(ents) == 1
    assert ents[0].exit_reason == "42" and isinstance(ents[0].exit_reason, str)


def test_tick_entries_blank_lines_and_warnings(tmp_path):
    # edge: blank/whitespace lines are skipped; warnings_emitted surfaces as a list;
    # a fully malformed (non-torn) middle line is skipped too.
    p = tmp_path / "runs.jsonl"
    p.write_text(
        "\n"
        '   \n'
        '{"ts":"t1","iteration":1,"peer":"claude","classification":"success",'
        '"success":true,"warnings_emitted":["w1","w2"]}\n'
        'totally not json\n'
        '{"ts":"t2","iteration":2,"peer":"codex","classification":"no_handoff",'
        '"success":false}\n'
    )
    ents = R.tick_entries(p)
    assert len(ents) == 2
    assert ents[0].warnings == ["w1", "w2"]
    assert ents[0].tokens == 0 and ents[0].usd == 0.0  # missing -> defaults
    assert ents[1].classification == "no_handoff" and ents[1].success is False
    assert ents[1].warnings == []  # absent warnings_emitted -> []


# --------------------------------------------------------------------------- #
# Task 10: commit_review_view — happy / sad / edge                            #
# --------------------------------------------------------------------------- #
import subprocess  # noqa: E402


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def test_commit_review_view_parses_trailer(tmp_path):
    # happy: a Peer:/Self-Review: trailer is parsed; with no peers-attest note
    # attest_match is False (absence is not a forgery alarm).
    repo = tmp_path
    _init_repo(repo)
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "do thing\n\nPeer: claude\nSelf-Review: pass")
    rows = R.commit_review_view(repo, limit=5)
    assert len(rows) == 1
    assert rows[0].subject == "do thing"
    assert rows[0].trailers.get("Peer") == "claude"
    assert rows[0].trailers.get("Self-Review") == "pass"
    assert rows[0].trailer_peer == "claude"
    assert rows[0].attested_peer is None
    assert rows[0].attest_match is False  # no peers-attest note in this fixture


def test_commit_review_view_attest_match_and_mismatch(tmp_path):
    # edge: a real peers-attest note. A matching note -> attest_match True; a
    # note that disagrees with the trailer -> attest_match False (forgery signal).
    from peers import attest
    repo = tmp_path
    _init_repo(repo)
    (repo / "a").write_text("a")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (repo / "b").write_text("b")
    _git(repo, "add", "b")
    _git(repo, "commit", "-q", "-m", "real work\n\nPeer: claude")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Substrate attests HEAD to claude (matches trailer).
    attest.attest_commits(repo, "claude", base, head)
    rows = {r.sha: r for r in R.commit_review_view(repo, limit=5)}
    assert rows[head].attested_peer == "claude"
    assert rows[head].trailer_peer == "claude"
    assert rows[head].attest_match is True
    # Now re-attest HEAD to a DIFFERENT peer -> forgery signal (no match).
    attest.attest_commits(repo, "codex", base, head)
    rows2 = {r.sha: r for r in R.commit_review_view(repo, limit=5)}
    assert rows2[head].attested_peer == "codex"
    assert rows2[head].trailer_peer == "claude"
    assert rows2[head].attest_match is False  # attested present but != trailer


def test_commit_review_view_separator_in_subject_does_not_forge_attestation(tmp_path):
    # I1: the commit SUBJECT (%s) is attacker-controlled and CAN contain the
    # field-separator \x1f. With the old field order (...%s%x1f%N...) the embedded
    # separator shifts fields so subject text bleeds into the NOTE slot -> a forged
    # attested_peer / attest_match=True with NO real peers-attest note. After the
    # reorder (note BEFORE subject) a \x1f in the subject can only bleed into the
    # body, never the note. Here: subject "innocent\x1fclaude", trailer Peer: claude,
    # and NO peers-attest note -> attested_peer must be None, attest_match False.
    repo = tmp_path
    _init_repo(repo)
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "innocent\x1fclaude\n\nPeer: claude")
    rows = R.commit_review_view(repo, limit=5)
    assert len(rows) == 1
    assert rows[0].trailer_peer == "claude"
    assert rows[0].attested_peer is None, "separator-in-subject forged an attestation!"
    assert rows[0].attest_match is False


def test_commit_review_view_non_repo_returns_empty(tmp_path):
    # sad: a non-repo directory -> [] (no crash).
    assert R.commit_review_view(tmp_path / "not-a-repo") == []
    assert R.commit_review_view(tmp_path) == []  # exists but not a git repo


def test_commit_review_view_no_trailers(tmp_path):
    # edge: a commit with NO trailers -> empty trailers dict, no crash.
    repo = tmp_path
    _init_repo(repo)
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "just a subject")
    rows = R.commit_review_view(repo, limit=5)
    assert len(rows) == 1
    assert rows[0].trailers == {}
    assert rows[0].trailer_peer is None
    assert rows[0].attest_match is False


# --------------------------------------------------------------------------- #
# Task 11: bug_views + blocking_open — happy / sad / edge                     #
# --------------------------------------------------------------------------- #
def test_bug_views_happy_mix(tmp_path):
    # happy: a mix of open + resolved bugs; fields parsed; blocking_open counts
    # only OPEN crit/high/med.
    p = tmp_path / "bugs.jsonl"
    p.write_text(
        json.dumps({"id": "BUG-1", "severity": "high", "title": "leak",
                    "status": "open", "filed_tick": 3, "author": "claude"}) + "\n"
        + json.dumps({"id": "BUG-2", "severity": "med", "title": "race",
                      "status": "resolved", "filed_tick": 1, "resolved_tick": 5,
                      "author": "codex"}) + "\n"
        + json.dumps({"id": "BUG-3", "severity": "crit", "title": "rce",
                      "status": "open", "filed_tick": 2, "author": "claude"}) + "\n"
    )
    bugs = R.bug_views(p)
    assert len(bugs) == 3
    by_id = {b.id: b for b in bugs}
    assert by_id["BUG-1"].severity == "high" and by_id["BUG-1"].status == "open"
    assert by_id["BUG-1"].filed_tick == 3 and by_id["BUG-1"].resolved_tick is None
    assert by_id["BUG-2"].resolved_tick == 5 and by_id["BUG-2"].author == "codex"
    assert by_id["BUG-3"].severity == "crit"
    # open crit + open high = 2 (resolved med does NOT count)
    assert R.blocking_open(bugs) == 2


def test_bug_views_missing_file_returns_empty(tmp_path):
    # sad: a missing bugs.jsonl -> [] (no crash).
    assert R.bug_views(tmp_path / "nope.jsonl") == []
    assert R.blocking_open(R.bug_views(tmp_path / "nope.jsonl")) == 0


def test_bug_views_malformed_line_and_low_severity(tmp_path):
    # edge: a malformed line is skipped; a non-blocking severity (low) and a
    # non-dict line do not count toward blocking_open and do not crash.
    p = tmp_path / "bugs.jsonl"
    p.write_text(
        json.dumps({"id": "BUG-1", "severity": "low", "title": "nit",
                    "status": "open"}) + "\n"
        + "{ not valid json\n"
        + "[1, 2, 3]\n"  # valid JSON but not an object -> skipped
        + json.dumps({"id": "BUG-2", "severity": "high", "title": "x",
                      "status": "open"}) + "\n"
    )
    bugs = R.bug_views(p)
    assert {b.id for b in bugs} == {"BUG-1", "BUG-2"}
    # low does NOT count; only the open high does.
    assert R.blocking_open(bugs) == 1


# --------------------------------------------------------------------------- #
# Task 12: fleet_entries (registry, NO reconcile) — happy / sad / edge        #
# --------------------------------------------------------------------------- #
import yaml  # noqa: E402


def _write_projects_yaml(config_dir, projects):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "projects.yaml").write_text(
        yaml.safe_dump({"projects": projects}, sort_keys=False)
    )


def test_fleet_entries_happy_two_projects(tmp_path):
    # happy: two registered projects; one has a .peers/state.json with an
    # iteration + gates; state/pid come straight from the registry (no reconcile).
    cfg = tmp_path / "config"
    proj_a = tmp_path / "proj-a"
    (proj_a / ".peers").mkdir(parents=True)
    (proj_a / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 12,
        "goals_status": {
            "tests-pass": {"state": "pass", "duration_ms": 5},
            "no-shortcut": {"state": "fail", "duration_ms": 1},
        },
    }))
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir()
    _write_projects_yaml(cfg, [
        {"name": "proj-a", "path": str(proj_a), "state": "running", "pid": 4242},
        {"name": "proj-b", "path": str(proj_b), "state": "fresh", "pid": None},
    ])
    entries = R.fleet_entries(config_dir=cfg)
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"proj-a", "proj-b"}
    assert by_name["proj-a"].state == "running" and by_name["proj-a"].pid == 4242
    assert by_name["proj-a"].iteration == 12
    assert by_name["proj-a"].gates_total == 2 and by_name["proj-a"].gates_green == 1
    assert by_name["proj-a"].alert is False
    # proj-b has no state.json -> safe fallbacks, no crash.
    assert by_name["proj-b"].iteration is None
    assert by_name["proj-b"].gates_total is None and by_name["proj-b"].gates_green is None
    assert by_name["proj-b"].pid is None


def test_fleet_entries_missing_registry_returns_empty(tmp_path):
    # sad: no projects.yaml in the config dir -> [] (NO Store side effect / write).
    cfg = tmp_path / "empty-config"
    assert R.fleet_entries(config_dir=cfg) == []
    # confirm we did NOT create/seed the registry (pure read).
    assert not (cfg / "projects.yaml").exists()


def test_fleet_entries_gone_path_and_alert(tmp_path):
    # edge: a registered project whose path is gone -> safe fallbacks, no crash;
    # and a project with HALTED.md / pending warnings -> alert True.
    cfg = tmp_path / "config"
    halted = tmp_path / "halted-proj"
    (halted / ".peers").mkdir(parents=True)
    (halted / ".peers" / "state.json").write_text(json.dumps({
        "iteration": 3, "warnings": ["degraded peer"],
    }))
    (halted / ".peers" / "HALTED.md").write_text("all peers degraded")
    _write_projects_yaml(cfg, [
        {"name": "gone", "path": str(tmp_path / "does-not-exist"),
         "state": "crashed", "pid": None},
        {"name": "halted", "path": str(halted), "state": "running", "pid": 9},
    ])
    by_name = {e.name: e for e in R.fleet_entries(config_dir=cfg)}
    # vanished path -> safe fallbacks (no state), no crash.
    assert by_name["gone"].iteration is None
    assert by_name["gone"].gates_total is None
    assert by_name["gone"].alert is False
    assert by_name["gone"].state == "crashed"
    # HALTED.md present + pending warning -> alert True.
    assert by_name["halted"].alert is True
    assert by_name["halted"].iteration == 3


# --------------------------------------------------------------------------- #
# Task 13: run_snapshot — happy / sad / edge                                  #
# --------------------------------------------------------------------------- #
def test_run_snapshot_composes_state(tmp_path):
    # happy: a present state.json -> RunSnapshot composed from the Unit-B readers;
    # mode taken from modes-applied.txt; current_peer from peer_order/turn_index.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "state.json").write_text(json.dumps({
        "iteration": 5,
        "peer_order": ["claude", "codex"], "turn_index": 1,
        "goals_status": {"tests-pass": {"state": "pass", "duration_ms": 3}},
        "peers": {"claude": {"state": "healthy", "recent_runs": [True]},
                  "codex": {"state": "degraded", "recent_runs": [False]}},
        "budget": {"spent_tokens": 100},
        "consecutive_clean_ticks": 2,
    }))
    (peer_dir / "modes-applied.txt").write_text(
        "2026-06-11T00:00:00+00:00  audit   v1  sha256=abc\n"
    )
    snap = R.run_snapshot(tmp_path, "myrun")
    assert snap.name == "myrun"
    assert snap.state_present is True
    assert snap.iteration == 5
    assert snap.mode == "audit"
    assert snap.current_peer == "codex"
    assert len(snap.gates) == 1 and snap.gates[0].id == "tests-pass"
    assert len(snap.peers) == 2
    assert snap.budget is not None and snap.budget.spent_tokens == 100
    assert snap.convergence is not None and snap.convergence.consecutive_clean_ticks == 2


def test_run_snapshot_no_state(tmp_path):
    # sad: no state.json -> RunSnapshot with state_present False, safe defaults.
    snap = R.run_snapshot(tmp_path, "ghost")
    assert snap.name == "ghost"
    assert snap.state_present is False
    assert snap.iteration == 0
    assert snap.current_peer is None
    assert snap.gates == [] and snap.peers == []


def test_run_snapshot_mode_from_state_when_no_trail(tmp_path):
    # edge: no modes-applied.txt -> mode falls back to state["mode"].
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "state.json").write_text(json.dumps({
        "iteration": 1, "mode": "develop",
    }))
    snap = R.run_snapshot(tmp_path, "r")
    assert snap.state_present is True
    assert snap.mode == "develop"


def test_run_snapshot_non_finite_iteration_degrades_without_crash(tmp_path):
    # BUG-705 sad: state.json with JSON Infinity parses to float("inf");
    # run_snapshot must not crash while coercing the iteration counter.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "state.json").write_text(
        '{"iteration": Infinity, "phase_b_extra_ticks": Infinity}'
    )

    snap = R.run_snapshot(tmp_path, "r")

    assert snap.state_present is True
    assert snap.iteration == 0
    assert snap.convergence is not None
    assert snap.convergence.phase_b_extra_ticks is None


# --------------------------------------------------------------------------- #
# Task 13: spine_runs (empty-state-aware) — happy / sad                        #
# --------------------------------------------------------------------------- #
def test_spine_runs_absent_dir_returns_empty(tmp_path):
    # sad/empty-state: the Wave-2 registry dir does not exist -> [] (honest empty).
    assert R.spine_runs(tmp_path) == []


def test_spine_runs_one_registry_file(tmp_path):
    # happy: one fake registry json -> one record.
    d = tmp_path / ".peers" / "spine-runs"
    d.mkdir(parents=True)
    (d / "r1.json").write_text(json.dumps({
        "mode_run": "r1", "worktree_path": "/wt/r1", "branch": "feat/x",
        "ledger_path": "/wt/r1/.peers/run.jsonl", "pid": 1234,
        "started_at": "2026-06-11T00:00:00+00:00",
    }))
    runs = R.spine_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].mode_run == "r1" and runs[0].branch == "feat/x"
    assert runs[0].pid == 1234 and runs[0].worktree_path == "/wt/r1"


def test_spine_runs_skips_malformed_json(tmp_path):
    # edge: a malformed registry file is skipped, a valid one survives.
    d = tmp_path / ".peers" / "spine-runs"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{ not json")
    (d / "ok.json").write_text(json.dumps({"mode_run": "r2"}))
    runs = R.spine_runs(tmp_path)
    assert len(runs) == 1 and runs[0].mode_run == "r2"
    assert runs[0].pid is None  # missing fields -> safe None


# --------------------------------------------------------------------------- #
# Task 13: autonomy_ledger_view (re-derived, never trust stored flag)         #
# --------------------------------------------------------------------------- #
import hashlib  # noqa: E402


def _file_witness(d, content="ok"):
    p = d / "evidence.txt"
    p.write_text(content)
    return {"kind": "file", "uri": str(p),
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def _attested_repo(p, peer="claude"):
    """init repo + two commits + attest HEAD-delta -> return HEAD sha (sanctioned)."""
    from peers import attest
    _init_repo(p)
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    attest.attest_commits(p, peer, base, sha)
    return sha


def test_autonomy_ledger_view_missing_returns_empty(tmp_path):
    # sad: a missing ledger -> empty view (verified None, gates {}, not converged).
    v = R.autonomy_ledger_view(tmp_path / "run.jsonl")
    assert v.verified is None
    assert v.gates == {}
    assert v.converged is False
    assert v.dry_streak == 0
    assert v.events == []


def test_autonomy_ledger_view_converged_rederived(tmp_path):
    # happy: a minimal VALID attested ledger -> verify() True, gates re-derive,
    # converged True. Convergence/independence are RE-DERIVED, never read from a
    # stored flag.
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config
    sha = _attested_repo(tmp_path, "claude")
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="bar-inferred", status="pass")
    led.append_attested(tmp_path, sha, event="confirmed-work", subject="u1",
                        status="pass", witness=_file_witness(tmp_path),
                        independence=True)
    v = R.autonomy_ledger_view(tmp_path / "run.jsonl", mode_run="r1", repo=tmp_path)
    assert v.verified is True
    assert v.gates.get("authorship-attested") is True
    assert v.gates.get("witness-ledgered") is True
    assert v.converged is True
    assert v.dry_streak == 0
    assert any(e.get("event") == "confirmed-work" for e in v.events)


def test_autonomy_ledger_view_forged_independence_not_converged(tmp_path):
    # HONESTY SEAM (belt-and-suspenders): a hand-forged raw ledger line that
    # bypasses RunLedger.append — it claims independence:true with author:null
    # and a bogus entry_sha (broken hash chain). The TUI must NOT show
    # convergence: verify() is fail-closed (bad entry_sha -> False) and
    # is_converged RE-DERIVES (never trusts the stored independence flag).
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    forged = {
        "v": 1, "prev": None, "event": "confirmed-work", "mode_run": "r1",
        "author": None, "subject": "forged",
        "status": "pass", "witness": None,
        "independence": True,          # forged stored flag
        "entry_sha": "deadbeef" * 8,   # bogus digest -> chain does not re-derive
    }
    (peer_dir / "run.jsonl").write_text(json.dumps(forged) + "\n")
    v = R.autonomy_ledger_view(peer_dir / "run.jsonl", mode_run="r1", repo=tmp_path)
    assert not v.verified              # falsy: tamper detected (fail-closed)
    assert v.converged is False        # re-derived, never trusts stored flag


def test_autonomy_ledger_view_present_but_unconverged(tmp_path):
    # edge: a present, hash-valid ledger that does NOT meet convergence (no
    # attested confirmed-work) -> verified True, converged False. We never fake it.
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    # a dry round, no attested confirmed-work -> not converged
    led.append(event="dry-round", status="pass")
    v = R.autonomy_ledger_view(tmp_path / "run.jsonl", mode_run="r1", repo=tmp_path)
    assert v.verified is True
    assert v.converged is False
    assert v.dry_streak >= 1


# --------------------------------------------------------------------------- #
# Wave-2 §5.3: registry -> reader -> ledger end-to-end (the autonomy path)     #
# --------------------------------------------------------------------------- #
def test_spine_run_registry_reader_ledger_round_trip(tmp_path):
    # Prove the WHOLE path the autonomy windows use: a real spine-runs registry
    # record (as lease() writes it) points at a worktree that holds a real
    # CONVERGED run.jsonl; spine_runs() enumerates it; autonomy_ledger_view()
    # re-derives gates/convergence from the ledger it points to (honesty held).
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config

    # A leased worktree dir holding a real attested ledger (the producer side).
    wt = tmp_path / "wt-r1"
    (wt / ".peers").mkdir(parents=True)
    sha = _attested_repo(wt, "claude")
    led = RunLedger(wt / ".peers" / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="bar-inferred", status="pass")
    led.append_attested(wt, sha, event="confirmed-work", subject="u1",
                        status="pass", witness=_file_witness(wt),
                        independence=True)

    # The host-side repo whose .peers/spine-runs/ the registry record lives under
    # (exactly the shape lease() writes).
    repo = tmp_path / "repo"
    reg_dir = repo / ".peers" / "spine-runs"
    reg_dir.mkdir(parents=True)
    (reg_dir / "r1.json").write_text(json.dumps({
        "mode_run": "r1",
        "worktree_path": str(wt),
        "branch": "peers/run/r1",
        "ledger_path": str(wt / ".peers" / "run.jsonl"),
        "pid": 4321,
        "started_at": "2026-06-11T00:00:00+00:00",
    }))

    # 1. reader enumerates the registry into a populated SpineRunEntry.
    entries = R.spine_runs(repo)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.mode_run == "r1"
    assert entry.ledger_path == str(wt / ".peers" / "run.jsonl")

    # 2. the ledger the entry points at re-derives gates/convergence (honesty
    #    rule: re-derived, never read off a stored flag).
    v = R.autonomy_ledger_view(entry.ledger_path, mode_run=entry.mode_run, repo=wt)
    assert v.verified is True
    assert v.gates.get("authorship-attested") is True
    assert v.gates.get("witness-ledgered") is True
    assert v.converged is True
    assert any(e.get("event") == "confirmed-work" for e in v.events)


# --------------------------------------------------------------------------- #
# Unit G: plan_progress — happy / sad / edge                                   #
# --------------------------------------------------------------------------- #
def _seed_plan(project_path, text, *, original=False):
    peer_dir = project_path / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    name = "PLAN.original.md" if original else "PLAN.md"
    (peer_dir / name).write_text(text)


def test_plan_progress_happy(tmp_path):
    _seed_plan(tmp_path, (
        "# Plan\n"
        "- [x] [STEP-1] scaffold (abc123)\n"
        "- [ ] [STEP-2] implement\n"
        "- [X] [STEP-3] test\n"
        "prose with an inline - [x] that must NOT count\n"
    ))
    done, total, steps = R.plan_progress(tmp_path)
    assert (done, total) == (2, 3)
    assert [s.done for s in steps] == [True, False, True]
    assert "scaffold" in steps[0].text
    assert steps[1].done is False


def test_plan_progress_missing_returns_zero(tmp_path):
    # sad: no PLAN.md / no .peers dir at all -> (0, 0, []).
    done, total, steps = R.plan_progress(tmp_path)
    assert (done, total, steps) == (0, 0, [])


def test_plan_progress_prefers_original(tmp_path):
    # edge: a frozen PLAN.original.md is preferred over the peer-editable live one.
    _seed_plan(tmp_path, "- [x] live-only\n- [ ] live-two\n", original=False)
    _seed_plan(tmp_path, "- [x] frozen-one\n- [x] frozen-two\n- [ ] frozen-three\n",
               original=True)
    done, total, steps = R.plan_progress(tmp_path)
    assert (done, total) == (2, 3)
    assert "frozen-one" in steps[0].text


def test_plan_progress_empty_checklist(tmp_path):
    # edge: a PLAN.md with prose but no checklist items -> (0, 0, []).
    _seed_plan(tmp_path, "# Plan\n\nJust some prose, no checkboxes.\n")
    done, total, steps = R.plan_progress(tmp_path)
    assert (done, total, steps) == (0, 0, [])


def test_plan_progress_oversized_fail_soft(tmp_path):
    # edge: an oversized PLAN.md is refused -> (0, 0, []) (never raises).
    big = "- [ ] x\n" * 5
    _seed_plan(tmp_path, big + (" " * (2 * 1024 * 1024)))
    done, total, steps = R.plan_progress(tmp_path, max_bytes=1024)
    assert (done, total, steps) == (0, 0, [])


# --------------------------------------------------------------------------- #
# Unit G: commit_diff — happy / sad / edge                                     #
# --------------------------------------------------------------------------- #
import subprocess as _sp  # noqa: E402


def _dgit(repo, *args):
    _sp.run(["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True)


def _init_diff_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _dgit(repo, "init", "-q")
    _dgit(repo, "config", "user.email", "t@t")
    _dgit(repo, "config", "user.name", "t")
    _dgit(repo, "config", "commit.gpgsign", "false")
    _dgit(repo, "commit", "--allow-empty", "-q", "-m", "root")
    return _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                   capture_output=True, text=True).stdout.strip()


def test_commit_diff_happy(tmp_path):
    repo = tmp_path / "repo"
    _init_diff_repo(repo)
    (repo / "f.txt").write_text("hello\nworld\n")
    _dgit(repo, "add", "f.txt")
    _dgit(repo, "commit", "-q", "-m", "add f")
    sha = _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip()
    out = R.commit_diff(repo, sha)
    assert "f.txt" in out
    assert "+hello" in out


def test_commit_diff_bad_sha_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    _init_diff_repo(repo)
    assert R.commit_diff(repo, "deadbeefdeadbeef") == ""


def test_commit_diff_non_repo_returns_empty(tmp_path):
    # sad: a directory that is not a git repo -> "" (fail-soft, never raises).
    assert R.commit_diff(tmp_path, "HEAD") == ""


def test_commit_diff_empty_sha_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    _init_diff_repo(repo)
    assert R.commit_diff(repo, "") == ""
    assert R.commit_diff(repo, None) == ""  # type: ignore[arg-type]


def test_commit_diff_capped(tmp_path):
    # edge: a huge diff is size-capped (output never exceeds the cap).
    repo = tmp_path / "repo"
    _init_diff_repo(repo)
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)) + "\n")
    _dgit(repo, "add", "big.txt")
    _dgit(repo, "commit", "-q", "-m", "big")
    sha = _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip()
    out = R.commit_diff(repo, sha, max_bytes=512)
    assert len(out) <= 512


def test_commit_diff_rejects_option_like_sha(tmp_path):
    # C1 (CRITICAL): the sha arrives from agent-writable runs.jsonl (.peers/) with
    # no hex validation. A hostile option-like sha such as
    # ``--output=<path>`` makes ``git show --output=...`` WRITE A FILE (arbitrary
    # file write). The reader must fail-CLOSED: validate the sha (hex / HEAD) and
    # pin everything after --end-of-options as non-options. Result: "" and NO file
    # is ever written to the attacker-chosen path.
    from pathlib import Path
    repo = tmp_path / "repo"
    _init_diff_repo(repo)
    target = tmp_path / "should_not_exist_xyz"
    try:
        assert R.commit_diff(repo, f"--output={target}") == ""
        assert not Path(target).exists(), "git option-injection WROTE a file!"
        # other option-like shas are likewise rejected before shelling.
        assert R.commit_diff(repo, "--all") == ""
        assert R.commit_diff(repo, "--help") == ""
        assert R.commit_diff(repo, "-q") == ""
        # a non-hex garbage sha is rejected too (sad path preserved).
        assert R.commit_diff(repo, "not-a-sha!!") == ""
    finally:
        # belt-and-suspenders cleanup in case a regression wrote the file.
        if Path(target).exists():
            Path(target).unlink()


# --------------------------------------------------------------------------- #
# Unit G: log_lines — happy / sad / edge                                       #
# --------------------------------------------------------------------------- #
def test_log_lines_happy(tmp_path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "state.json").write_text(json.dumps({
        "warnings_history": [
            {"ts": "2026-06-11T10:00:00+00:00", "iter": 3, "w": "no-shortcut markers"},
            {"ts": "2026-06-11T10:01:00+00:00", "iter": 4, "w": "stuck:tests-pass"},
        ],
    }))
    (peer_dir / "last-stop-reason.txt").write_text(
        "converged 2026-06-11T10:05:00+00:00\n")
    rows = R.log_lines(tmp_path)
    texts = [r.text for r in rows]
    assert any("no-shortcut markers" in t for t in texts)
    assert any("converged" in t for t in texts)
    # each row carries its iter where known + a kind tag.
    warn = [r for r in rows if "stuck:tests-pass" in r.text][0]
    assert warn.iteration == 4
    assert warn.kind == "warning"


def test_log_lines_missing_returns_empty(tmp_path):
    # sad: no .peers dir / no state.json / no stop-reason -> [].
    assert R.log_lines(tmp_path) == []


def test_log_lines_limit_caps(tmp_path):
    # edge: more than `limit` warnings -> only the most recent `limit` returned.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    hist = [{"ts": "t", "iter": i, "w": f"w{i}"} for i in range(50)]
    (peer_dir / "state.json").write_text(json.dumps({"warnings_history": hist}))
    rows = R.log_lines(tmp_path, limit=10)
    # warnings + (no stop-reason) -> at most `limit` rows.
    assert len(rows) <= 10
    # the most recent (highest iter) survives the cap.
    assert any(r.iteration == 49 for r in rows)


def test_log_lines_malformed_state_fail_soft(tmp_path):
    # edge: a corrupt state.json must not raise -> [] (stop-reason still read).
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "state.json").write_text("{ not json")
    (peer_dir / "last-stop-reason.txt").write_text("crashed 2026-06-11T11:00:00+00:00\n")
    rows = R.log_lines(tmp_path)
    assert any("crashed" in r.text for r in rows)
    assert all(r.kind in ("warning", "stop") for r in rows)


# --------------------------------------------------------------------------- #
# Unit H: peer_tool — which CLI tool backs a peer (claude|codex|opencode)      #
# --------------------------------------------------------------------------- #
def _write_config(tmp_path, body: str):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "config.yaml").write_text(body)


def test_peer_tool_happy_new_peers_shape(tmp_path):
    _write_config(tmp_path, (
        "peers:\n"
        "  - name: claude\n"
        "    tool: claude\n"
        "  - name: gpt\n"
        "    tool: codex\n"
    ))
    assert R.peer_tool(tmp_path, "claude") == "claude"
    assert R.peer_tool(tmp_path, "gpt") == "codex"


def test_peer_tool_happy_legacy_tools_shape(tmp_path):
    # legacy `tools: {name: {...}}` shape -> name == tool.
    _write_config(tmp_path, (
        "tools:\n"
        "  claude:\n"
        "    argv: [\"claude\", \"-p\", \"{PROMPT}\"]\n"
        "  codex:\n"
        "    argv: [\"codex\"]\n"
    ))
    assert R.peer_tool(tmp_path, "claude") == "claude"
    assert R.peer_tool(tmp_path, "codex") == "codex"


def test_peer_tool_sad_missing_config_defaults_claude(tmp_path):
    # no config.yaml at all -> default to claude (the genuinely-live peer).
    assert R.peer_tool(tmp_path, "whoever") == "claude"


def test_peer_tool_sad_corrupt_config_defaults_claude(tmp_path):
    _write_config(tmp_path, "{ not: valid: yaml: [[[")
    assert R.peer_tool(tmp_path, "claude") == "claude"


def test_peer_tool_edge_unknown_peer_name_defaults_claude(tmp_path):
    _write_config(tmp_path, "peers:\n  - name: claude\n    tool: claude\n")
    # a peer name not in the config -> default claude, never raise.
    assert R.peer_tool(tmp_path, "ghost") == "claude"


def test_peer_tool_edge_unknown_tool_value_falls_back_claude(tmp_path):
    # a tool value outside KNOWN_TOOLS is not trusted -> default claude.
    _write_config(tmp_path, "peers:\n  - name: p\n    tool: bogus-tool\n")
    assert R.peer_tool(tmp_path, "p") == "claude"


def test_peer_tool_edge_none_peer_defaults_claude(tmp_path):
    _write_config(tmp_path, "peers:\n  - name: claude\n    tool: claude\n")
    assert R.peer_tool(tmp_path, None) == "claude"


# --------------------------------------------------------------------------- #
# Unit J: escalation_state — happy / sad / edge                                #
# --------------------------------------------------------------------------- #
def test_escalation_state_quiet_when_absent(tmp_path):
    # sad/empty-state: no HALTED.md and no CONCERNS.md -> both False, no excerpt.
    st = R.escalation_state(tmp_path)
    assert st == {"halted": False, "concerns": False, "halted_excerpt": ""}


def test_escalation_state_halted_present_with_excerpt(tmp_path):
    # happy: a HALTED.md is present -> halted True + a (capped) excerpt.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "HALTED.md").write_text("# HALTED\nthe loop stopped: max ticks reached\n")
    st = R.escalation_state(tmp_path)
    assert st["halted"] is True
    assert st["concerns"] is False
    assert "HALTED" in st["halted_excerpt"]


def test_escalation_state_concerns_present(tmp_path):
    # happy: a CONCERNS.md is present (no HALTED) -> concerns True, halted False.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "CONCERNS.md").write_text("the peers raised concerns\n")
    st = R.escalation_state(tmp_path)
    assert st["halted"] is False
    assert st["concerns"] is True
    assert st["halted_excerpt"] == ""


def test_escalation_state_excerpt_capped(tmp_path):
    # edge: a huge HALTED.md -> the excerpt is capped (never the whole file),
    # and the reader never raises.
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    (peer_dir / "HALTED.md").write_text("X" * (5 * 1024 * 1024))
    st = R.escalation_state(tmp_path)
    assert st["halted"] is True
    assert 0 < len(st["halted_excerpt"]) <= 4096


def test_escalation_state_symlinked_halted_fails_soft(tmp_path):
    # edge/security: a symlinked HALTED.md is refused by safe_io for the excerpt
    # read, but presence is still reported (it exists) with an empty excerpt —
    # never raises.
    real = tmp_path / "real.md"
    real.write_text("secret elsewhere")
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir(parents=True)
    link = peer_dir / "HALTED.md"
    try:
        os.symlink(real, link)
    except OSError:
        import pytest
        pytest.skip("symlinks unsupported here")
    st = R.escalation_state(tmp_path)
    # presence is honest (the marker is there); the excerpt fails soft to "".
    assert st["halted"] is True
    assert st["halted_excerpt"] == ""


def test_escalation_state_bad_project_path_fails_soft(tmp_path):
    # sad: a path that errors on access -> safe default, never raises.
    st = R.escalation_state(tmp_path / "does-not-exist")
    assert st == {"halted": False, "concerns": False, "halted_excerpt": ""}


# --------------------------------------------------------------------------- #
# Live-Stream source selection (Wave-2 unified tee §5.1)                       #
# --------------------------------------------------------------------------- #
def _peers_log(tmp_path):
    d = tmp_path / ".peers" / "log" / "peers"
    d.mkdir(parents=True)
    return d


def test_newest_tee_stream_picks_newest(tmp_path):
    # happy: with multiple tee files for a peer, the newest (by mtime) wins.
    d = _peers_log(tmp_path)
    old = d / "tick-00001-claude.stream.jsonl"
    new = d / "tick-00002-claude.stream.jsonl"
    old.write_text("old\n")
    new.write_text("new\n")
    os.utime(old, (1, 1))
    os.utime(new, (10, 10))
    assert R.newest_tee_stream(tmp_path, "claude") == new


def test_newest_tee_stream_none_when_absent(tmp_path):
    # sad: no .stream.jsonl (tee off) -> None.
    _peers_log(tmp_path)
    assert R.newest_tee_stream(tmp_path, "claude") is None


def test_newest_tee_stream_missing_dir_fails_soft(tmp_path):
    # edge: no .peers dir at all -> None, never raises.
    assert R.newest_tee_stream(tmp_path, "claude") is None


def test_newest_tee_stream_is_peer_scoped(tmp_path):
    # edge: another peer's tee must not be returned for this peer.
    d = _peers_log(tmp_path)
    (d / "tick-00001-codex.stream.jsonl").write_text("x\n")
    assert R.newest_tee_stream(tmp_path, "claude") is None
    assert R.newest_tee_stream(tmp_path, "codex").name.endswith(
        "tick-00001-codex.stream.jsonl")


def test_newest_tee_stream_skips_symlinked_candidate(tmp_path):
    # sad: a peer-writable log symlink must not become the host TUI's tail path.
    d = _peers_log(tmp_path)
    old = d / "tick-00001-codex.stream.jsonl"
    old.write_text("old\n")
    outside = tmp_path / "outside.stream.jsonl"
    outside.write_text("outside\n")
    linked = d / "tick-00002-codex.stream.jsonl"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation unavailable for this platform: {exc}")
    os.utime(old, (1, 1))

    assert R.newest_tee_stream(tmp_path, "codex") == old


def test_newest_tick_log_skips_symlinked_candidate(tmp_path):
    # sad: codex/opencode fallback logs have the same tail -F sink as tee logs.
    d = _peers_log(tmp_path)
    old = d / "tick-00001-codex.stdout.log"
    old.write_text("old\n")
    outside = tmp_path / "outside.stdout.log"
    outside.write_text("outside\n")
    linked = d / "tick-00002-codex.stdout.log"
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation unavailable for this platform: {exc}")
    os.utime(old, (1, 1))

    assert R.newest_tick_log(tmp_path, "codex") == old


def test_newest_live_logs_refuse_symlinked_log_parent_BUG_759(tmp_path):
    # sad: a symlinked .peers/log/peers parent must not redirect the host TUI
    # to tail an off-tree file.
    log_root = tmp_path / ".peers" / "log"
    log_root.mkdir(parents=True)
    outside = tmp_path / "outside-logs"
    outside.mkdir()
    (outside / "tick-00099-codex.stream.jsonl").write_text("outside tee\n")
    (outside / "tick-00099-codex.stdout.log").write_text("outside stdout\n")
    try:
        (log_root / "peers").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    assert R.newest_tee_stream(tmp_path, "codex") is None
    assert R.newest_tick_log(tmp_path, "codex") is None


def test_live_stream_kind_prefers_tee_for_all_peers(tmp_path):
    # happy: a tee file makes EVERY peer (incl. codex) use the live tee.
    d = _peers_log(tmp_path)
    (d / "tick-00003-codex.stream.jsonl").write_text("{}\n")
    assert R.live_stream_kind(tmp_path, "codex", "codex") == "tee"
    (d / "tick-00003-claude.stream.jsonl").write_text("{}\n")
    assert R.live_stream_kind(tmp_path, "claude", "claude") == "tee"


def test_live_stream_kind_claude_peek_fallback(tmp_path):
    # sad/fallback: no tee + claude -> legacy peek.
    _peers_log(tmp_path)
    assert R.live_stream_kind(tmp_path, "claude", "claude") == "peek"


def test_live_stream_kind_codex_ticklog_fallback(tmp_path):
    # fallback: no tee + codex + a completed stdout log -> tick-level log.
    d = _peers_log(tmp_path)
    (d / "tick-00004-codex.stdout.log").write_text("done\n")
    assert R.live_stream_kind(tmp_path, "codex", "codex") == "ticklog"


def test_live_stream_kind_none_when_nothing(tmp_path):
    # edge: no tee, codex, no stdout log -> nothing to follow.
    _peers_log(tmp_path)
    assert R.live_stream_kind(tmp_path, "codex", "codex") == "none"


# --------------------------------------------------------------------------- #
# Wave-2 §5.2: gate_history (runs.jsonl per-tick `gates` snapshots)            #
# --------------------------------------------------------------------------- #
def test_gate_history_happy_parses_snapshots_and_gap(tmp_path):
    # happy: two ticks each carry a `gates` snapshot -> two rows, with green/
    # total pre-counted and gap_s computed from the ts delta.
    from peers_ctl.tui.snapshots import GateSnapshotRow

    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"ts":"2026-06-11T00:00:00+00:00","iteration":1,"peer":"claude",'
        '"classification":"success","gates":{"hard":{"tests":"fail",'
        '"lint":"pass"},"soft":{"review":"0/2"}}}\n'
        '{"ts":"2026-06-11T00:00:30+00:00","iteration":2,"peer":"codex",'
        '"classification":"success","gates":{"hard":{"tests":"pass",'
        '"lint":"pass"},"soft":{"review":"2/2"}}}\n'
    )
    rows = R.gate_history(p)
    assert len(rows) == 2
    assert all(isinstance(r, GateSnapshotRow) for r in rows)
    # Row 0: tests fail, lint pass, review 0/2 -> green = lint = 1, total = 3.
    assert rows[0].iteration == 1
    assert rows[0].gates["hard"]["tests"] == "fail"
    assert rows[0].green == 1 and rows[0].total == 3
    assert rows[0].gap_s is None  # first row -> no previous
    # Row 1: tests pass, lint pass, review 2/2 reached -> green = 3, total = 3.
    assert rows[1].iteration == 2
    assert rows[1].green == 3 and rows[1].total == 3
    assert rows[1].gap_s == 30.0  # 30s after row 0


def test_gate_history_skips_exit_and_missing_field(tmp_path):
    # sad/edge: a line WITHOUT `gates`, plus the synthetic exit line, are both
    # skipped — only the snapshot-bearing tick becomes a row.
    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"ts":"t1","iteration":1,"peer":"claude","classification":"success"}\n'
        '{"ts":"t2","iteration":2,"peer":"codex","classification":"success",'
        '"gates":{"hard":{"tests":"pass"}}}\n'
        '{"event":"exit","reason":"complete","ticks_in_run":2,"ts":"t3",'
        '"gates":{"hard":{"bogus":"pass"}}}\n'
    )
    rows = R.gate_history(p)
    assert len(rows) == 1
    assert rows[0].iteration == 2
    assert rows[0].gates["hard"]["tests"] == "pass"
    assert rows[0].green == 1 and rows[0].total == 1


def test_gate_history_missing_file_returns_empty(tmp_path):
    # sad: no runs.jsonl -> [].
    assert R.gate_history(tmp_path / "nope.jsonl") == []


def test_gate_history_tolerates_torn_and_garbage_gates(tmp_path):
    # edge: a torn final line is skipped; a line whose `gates` is NOT a dict is
    # skipped (never raises); a bad ts -> gap_s None but row still produced.
    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"ts":"not-a-ts","iteration":1,"gates":{"hard":{"a":"pass"}}}\n'
        '{"ts":"t2","iteration":2,"gates":"not-a-dict"}\n'
        '{"ts":"t3","iteration":3,"gates":{"hard":{"a":"fail"}}}\n'
        '{"ts":"t4","iteration":4,"gates":{"hard":{"a":"pas'  # torn last line
    )
    rows = R.gate_history(p)
    # Rows with a dict `gates`: iteration 1 and 3 (2 is non-dict gates -> skip;
    # 4 is torn -> skip).
    assert [r.iteration for r in rows] == [1, 3]
    assert rows[0].gap_s is None  # bad ts


def test_gate_snapshot_views_maps_hard_and_soft():
    # gate_snapshot_views turns a GateSnapshotRow's compact gates map into
    # GateView rows the panel can color/render (textual-free).
    from peers_ctl.tui.snapshots import GateSnapshotRow, GateView

    row = GateSnapshotRow(
        iteration=5, ts="2026-06-11T00:00:00+00:00",
        gates={"hard": {"tests": "fail", "lint": "pass"},
               "soft": {"review": "1/2", "design": "2/2"}},
        green=2, total=4, gap_s=None,
    )
    views = R.gate_snapshot_views(row)
    assert all(isinstance(v, GateView) for v in views)
    by_id = {v.id: v for v in views}
    assert by_id["tests"].kind == "hard" and by_id["tests"].state == "fail"
    assert by_id["lint"].kind == "hard" and by_id["lint"].state == "pass"
    # soft "1/2" -> pending, consensus (1,2); "2/2" -> reached.
    assert by_id["review"].kind == "soft" and by_id["review"].state == "pending"
    assert by_id["review"].consensus == (1, 2)
    assert by_id["design"].state == "reached" and by_id["design"].consensus == (2, 2)


def test_gate_snapshot_views_skips_garbage_soft():
    # edge: a soft value that is not "n/m" is rendered as pending with no
    # consensus tuple rather than raising.
    from peers_ctl.tui.snapshots import GateSnapshotRow

    row = GateSnapshotRow(
        iteration=1, ts="t", gates={"soft": {"x": "garbage"}},
        green=0, total=1, gap_s=None,
    )
    views = R.gate_snapshot_views(row)
    assert len(views) == 1
    assert views[0].id == "x" and views[0].state == "pending"
    assert views[0].consensus is None


def test_reader_ignores_truncated_marker_in_gates_snapshot(tmp_path):
    """Fix #4 (reader side of the ``_truncated`` invariant): the substrate flags
    a truncated per-tick snapshot with a top-level ``"_truncated": True`` marker.
    The TUI readers MUST treat that marker as bookkeeping, NEVER as a gate — it
    must not become a phantom row and must not perturb the green/total tally."""
    from peers_ctl.tui.snapshots import GateSnapshotRow

    # A snapshot that was truncated by the substrate: real gates + the marker.
    gates = {
        "hard": {"tests": "pass", "lint": "fail"},
        "soft": {"review": "2/2"},
        "_truncated": True,
    }

    # (a) _count_gates: the marker is NOT counted toward green or total. Without
    # the marker the tally is green=2 (tests + review), total=3; the marker must
    # leave that unchanged (no phantom 4th gate).
    green, total = R._count_gates(gates)
    assert (green, total) == (2, 3), (green, total)

    # (b) gate_snapshot_views: no GateView is emitted for "_truncated" — only the
    # three real gates render, so there is no phantom row in the panel.
    row = GateSnapshotRow(
        iteration=7, ts="2026-06-11T00:00:00+00:00", gates=gates,
        green=green, total=total, gap_s=None,
    )
    views = R.gate_snapshot_views(row)
    ids = sorted(v.id for v in views)
    assert ids == ["lint", "review", "tests"], ids
    assert "_truncated" not in ids

    # (c) end-to-end via gate_history: a truncated snapshot line parses to one
    # row whose green/total ignore the marker (no phantom gate in the count).
    p = tmp_path / "runs.jsonl"
    p.write_text(json.dumps({
        "ts": "2026-06-11T00:00:00+00:00", "iteration": 7, "peer": "claude",
        "classification": "success", "gates": gates,
    }) + "\n")
    rows = R.gate_history(p)
    assert len(rows) == 1
    assert (rows[0].green, rows[0].total) == (2, 3), rows[0]
