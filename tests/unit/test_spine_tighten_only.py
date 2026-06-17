"""STEP-5 — the tighten-only advisory for the §6.3 human-review seam.

Self-hosting runs are ALREADY forced to branch-pr (Tasks 2/4) -- a human reviews
them. ``tighten_only_advisory`` is a DETECTOR that flags when a self-hosting run's
diff WEAKENS/removes a gate registration in ``src/peers/spine/gates.py``, recorded
for that review so the reviewer sees "this diff removes a gate" without reading the
whole diff. Adding a gate is STRENGTHENING (no flag). It does NOT block
(self-hosting is branch-pr regardless); it is defense in depth for the reviewer.
Fail-safe: any undeterminable diff -> ``weakens=True`` (never a silent pass).
"""
from tests.unit._isolation_helpers import _git, _attested_repo

from peers.spine.auto_merge import tighten_only_advisory

_GATES_4 = '''def evaluate_spine_gates(rows, *, mode_run=None, dry_n=3, repo=None):
    return {
        "ModeRun-valid": True,
        "witness-ledgered": True,
        "authorship-attested": True,
        "stop-on-dry": True,
    }
'''
_GATES_3_REMOVED = _GATES_4.replace('        "witness-ledgered": True,\n', "")
_GATES_2_REMOVED = (_GATES_4.replace('        "witness-ledgered": True,\n', "")
                            .replace('        "stop-on-dry": True,\n', ""))
_GATES_5_ADDED = _GATES_4.replace(
    '        "stop-on-dry": True,\n',
    '        "stop-on-dry": True,\n        "new-gate": True,\n')


def _commit_gates(repo, body):
    p = repo / "src" / "peers" / "spine"
    p.mkdir(parents=True, exist_ok=True)
    (p / "gates.py").write_text(body)
    _git(repo, "add", "src/peers/spine/gates.py")
    _git(repo, "commit", "-q", "-m", "gates")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_adding_a_gate_is_allowed(tmp_path):
    # happy / strengthening: a self-hosting diff that ADDS a gate registration is
    # NOT a weakening -- the advisory clears it (weakens=False, nothing removed).
    _attested_repo(tmp_path)
    base = _commit_gates(tmp_path, _GATES_4)
    head = _commit_gates(tmp_path, _GATES_5_ADDED)
    adv = tighten_only_advisory(tmp_path, base=base, head=head)
    assert adv["weakens"] is False and adv["removed_gates"] == []


def test_no_gates_change_does_not_weaken(tmp_path):
    # nominal: a self-hosting diff that touches a NON-gate file leaves every gate
    # registration intact (gates.py identical at base..head) -> no flag.
    _attested_repo(tmp_path)
    base = _commit_gates(tmp_path, _GATES_4)
    (tmp_path / "README.md").write_text("a non-gate change\n")   # gates.py untouched
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "docs only")
    head = _git(tmp_path, "rev-parse", "HEAD").strip()
    adv = tighten_only_advisory(tmp_path, base=base, head=head)
    assert adv["weakens"] is False


def test_removing_a_gate_flags_weakens(tmp_path):
    # the core defect §6.3 names: a registration present at base is ABSENT at head.
    _attested_repo(tmp_path)
    base = _commit_gates(tmp_path, _GATES_4)
    head = _commit_gates(tmp_path, _GATES_3_REMOVED)
    adv = tighten_only_advisory(tmp_path, base=base, head=head)
    assert adv["weakens"] is True and "witness-ledgered" in adv["removed_gates"]


def test_removing_multiple_gates_flags_each(tmp_path):
    # edge: more than one removal -> EVERY removed registration is reported (sorted),
    # not just the first. Defends the reviewer against a multi-gate weakening.
    _attested_repo(tmp_path)
    base = _commit_gates(tmp_path, _GATES_4)
    head = _commit_gates(tmp_path, _GATES_2_REMOVED)
    adv = tighten_only_advisory(tmp_path, base=base, head=head)
    assert adv["weakens"] is True
    assert adv["removed_gates"] == ["stop-on-dry", "witness-ledgered"]   # sorted, both


def test_undeterminable_diff_fails_safe_to_weakens(tmp_path):
    # sad / fail-safe: bad shas -> the diff can't be read -> weakens=True (the
    # reviewer is told it could not be cleared, never a silent pass).
    _attested_repo(tmp_path)
    adv = tighten_only_advisory(tmp_path, base="deadbeef", head="cafe1234")  # bad shas
    assert adv["weakens"] is True and adv["reason"] == "undeterminable"
