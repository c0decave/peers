import pytest
from tests.unit._fleet_helpers import _spec, _program
from tests.unit._isolation_helpers import _init_repo

from peers.fleet.program import (ModeRunSpec, Program, validate_program,
                                 MODE_ARTIFACTS, artifact_of)


def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    return p


def test_valid_linear_dag_passes(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(
        _spec("a", tool=x, mode="find-bugs:reproduce"),
        _spec("b", tool=x, mode="develop", depends_on=["a"]))
    ok, errors = validate_program(prog)
    assert ok is True and errors == []


def test_cycle_is_rejected(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(
        _spec("a", tool=x, depends_on=["b"]),
        _spec("b", tool=x, depends_on=["a"]))          # a<->b cycle
    ok, errors = validate_program(prog)
    assert ok is False and any("cycle" in e for e in errors)


def test_self_dependency_is_a_cycle(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, depends_on=["a"]))
    ok, errors = validate_program(prog)
    assert ok is False and any("cycle" in e for e in errors)


def test_unknown_depends_on_is_rejected(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, depends_on=["ghost"]))
    ok, errors = validate_program(prog)
    assert ok is False and any("ghost" in e and "unknown" in e for e in errors)


def test_duplicate_run_id_is_rejected(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("a", tool=x, mode="research"))
    ok, errors = validate_program(prog)
    assert ok is False and any("duplicate" in e for e in errors)


@pytest.mark.parametrize("bad", ["", "a/b", "..", "."])
def test_empty_or_unsafe_run_id_is_rejected(tmp_path, bad):
    x = _repo(tmp_path, "x")
    # an unsafe run_id is rejected because the Stage-5 namer (which derives the
    # branch peers/run/<id>) is fail-closed on it -- the fleet validates eagerly.
    prog = _program(ModeRunSpec(tool=x, mode="develop",
                                op_config=_spec("z", tool=x).op_config,
                                run_id=bad, depends_on=[]))
    ok, errors = validate_program(prog)
    assert ok is False and any("run_id" in e for e in errors)


def test_missing_tool_root_is_rejected(tmp_path):
    prog = _program(_spec("a", tool=tmp_path / "does-not-exist"))
    ok, errors = validate_program(prog)
    assert ok is False and any("tool root" in e for e in errors)


def test_two_writable_runs_same_branch_no_isolation_is_rejected(tmp_path):
    # F1: two WRITABLE runs on the SAME tool whose run_ids collide to the SAME
    # branch peers/run/<id> with no distinct mode_run => they would share a
    # writable HEAD. The Stage-5 namer makes the branch a pure function of run_id,
    # so a collision is provable by name. (Here we force the collision by giving
    # both the same run_id family that maps to one branch -- the validator uses
    # workspace_names to detect it.)
    x = _repo(tmp_path, "x")
    # two specs that the namer maps to the SAME peers/run/<branch> on the same tool
    prog = Program(runs=[
        _spec("dup", tool=x, writable=True),
        _spec("dup", tool=x, mode="research", writable=True)])  # same run_id+tool+writable
    ok, errors = validate_program(prog)
    assert ok is False
    assert any("same branch" in e or "duplicate" in e for e in errors)


def test_two_readonly_runs_same_tool_is_allowed(tmp_path):
    # read-only runs do not own a writable HEAD -> no collision (e.g. two
    # find-bugs:reproduce audits on one repo). (distinct run_ids; writable=False.)
    x = _repo(tmp_path, "x")
    prog = _program(
        _spec("h1", tool=x, mode="find-bugs:reproduce", writable=False),
        _spec("h2", tool=x, mode="find-bugs:reproduce", writable=False))
    ok, errors = validate_program(prog)
    assert ok is True and errors == []


def test_dep_requires_artifact_producer_cannot_emit_is_rejected(tmp_path):
    # F1: B (cross-tool) depends on A's GIT-SHA BRANCH, but A is a `research` run,
    # which emits a FILE report, not a branch -> the producer mode cannot emit the
    # required artifact -> reject.
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("a", tool=x, mode="research"),
        # b declares it needs a's BRANCH artifact (git-sha), which research can't emit
        ModeRunSpec(tool=y, mode="develop", op_config=_spec("z", tool=y).op_config,
                    run_id="b", depends_on=["a"], requires_artifact="git-sha"))
    ok, errors = validate_program(prog)
    assert ok is False and any("cannot emit" in e and "a" in e for e in errors)


def test_mode_artifact_map_is_complete():
    # the map the validator + scheduler read -- every ALLOWED_MODE has an artifact.
    from peers.spine.op_config import ALLOWED_MODES
    for m in ALLOWED_MODES:
        assert m in MODE_ARTIFACTS, f"mode missing from artifact map: {m}"
    assert artifact_of("develop") == "git-sha"
    assert artifact_of("research") == "file"
    # a finding is a witnessed reproduced defect, NOT a propagatable git-sha branch
    # -- distinct kind so a cross-tool dep requiring a branch on a reproduce producer
    # is rejected (major F1-2). (The removed find-bugs:hunt 'hypothesis' kind used to
    # illustrate this; 'finding' carries the same non-propagatable contract.)
    assert artifact_of("find-bugs:reproduce") == "finding"
    # bring-up lands an attested fix as a git-sha branch (branch-PR) -> propagatable
    # like develop; a cross-tool dep on a bring-up producer can consume it.
    assert artifact_of("bring-up") == "git-sha"


def test_cross_tool_dep_on_file_only_producer_rejected_even_without_requires(tmp_path):
    # F1 (major): the producer-cannot-emit check is NOT opt-in. A CROSS-TOOL dep
    # (producer.tool != consumer.tool) on a `research` producer (emits a FILE, not a
    # git-sha branch the cross-tool propagation path can transfer) is rejected even
    # when requires_artifact is UNSET -- derived from the tool identity, not a client flag.
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("a", tool=x, mode="research"),
        _spec("b", tool=y, mode="develop", depends_on=["a"]))   # NO requires_artifact
    ok, errors = validate_program(prog)
    assert ok is False and any("cannot emit" in e and "a" in e for e in errors)


def test_cross_tool_dep_on_finding_producer_rejected(tmp_path):
    # F1 (major): a find-bugs:reproduce 'finding' is a witnessed defect, not a
    # propagatable git-sha branch -> a cross-tool dep on it is rejected (it can never
    # reach the CONVERGED+propagated gate). (Was the find-bugs:hunt 'hypothesis' case
    # before that label was removed; 'finding' carries the same contract.)
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("a", tool=x, mode="find-bugs:reproduce", writable=False),
        _spec("b", tool=y, mode="develop", depends_on=["a"]))
    ok, errors = validate_program(prog)
    assert ok is False and any("cannot emit" in e for e in errors)


def test_find_bugs_hunt_removed_from_artifact_map():
    # FB-06/SPEC-08: find-bugs:hunt is removed (no frontend/builder ever existed).
    # The 'hypothesis' artifact kind it was the sole carrier of goes with it; the
    # non-git-sha cross-tool rejection stays exercised by real modes (research=file,
    # find-bugs:reproduce=finding).
    assert artifact_of("find-bugs:hunt") is None
    assert "hypothesis" not in MODE_ARTIFACTS.values()


def test_cross_tool_dep_on_develop_producer_accepted(tmp_path):
    # the positive: a cross-tool dep on a `develop` producer (emits git-sha) passes.
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("a", tool=x, mode="develop"),
        _spec("b", tool=y, mode="develop", depends_on=["a"]))
    ok, errors = validate_program(prog)
    assert ok is True and errors == []


def test_non_git_tool_root_is_rejected(tmp_path):
    # F1 (minor): is_dir() alone is too weak -- a non-git directory passes is_dir but
    # every downstream git op fails far from the validator. Require a real git repo.
    plain = tmp_path / "plain"
    plain.mkdir()                                       # a dir, NOT a git repo
    prog = _program(_spec("a", tool=plain))
    ok, errors = validate_program(prog)
    assert ok is False and any("not a git repo" in e for e in errors)


def test_branch_is_a_pure_function_of_run_id(tmp_path):
    # the §7.2 collision check's PREMISE (the namer makes branch a pure function of
    # run_id) -- pinned so the defense-in-depth layer stays honest (info F1-5).
    x = _repo(tmp_path, "x")
    assert _spec("dup", tool=x).branch == "peers/run/dup"


def test_same_id_different_physical_repo_spellings_collide(tmp_path):
    # info F1-5: the (tool, branch) key is Path(tool).resolve(), so two spellings of
    # ONE physical repo with the same run_id are caught as a collision/duplicate.
    x = _repo(tmp_path, "x")
    prog = Program(runs=[
        _spec("dup", tool=x, writable=True),
        _spec("dup", tool=x / ".." / "x", writable=True)])   # same repo, other spelling
    ok, errors = validate_program(prog)
    assert ok is False
    assert any("same branch" in e or "duplicate" in e for e in errors)


def test_all_defects_reported_not_short_circuited(tmp_path):
    # a program with a cycle AND a missing tool root AND a duplicate id reports ALL.
    x = _repo(tmp_path, "x")
    prog = Program(runs=[
        _spec("a", tool=x, depends_on=["b"]),
        _spec("b", tool=x, depends_on=["a"]),          # cycle
        _spec("a", tool=tmp_path / "ghost")])          # duplicate id + missing root
    ok, errors = validate_program(prog)
    assert ok is False and len(errors) >= 2


# ---- BUG-118: ModeRunSpec.mode is public input and MUST be validated ----

def test_unknown_mode_is_rejected_even_with_no_deps(tmp_path):
    # BUG-118 sad-path repro: a local run with an unknown ModeRunSpec.mode but a
    # valid op_config (mode='develop') and NO cross-tool edge slipped through the
    # validator (returned (True, [])). ModeRunSpec.mode is public fleet input and
    # producer artifact checks read producer.mode, so an unknown label must be
    # rejected fail-closed before reaching the scheduler.
    x = _repo(tmp_path, "x")
    prog = _program(ModeRunSpec(
        tool=x, mode="not-a-mode",
        op_config=_spec("z", tool=x, mode="develop").op_config,
        run_id="a"))
    ok, errors = validate_program(prog)
    assert ok is False
    assert any("not-a-mode" in e and "mode" in e for e in errors)


def test_mode_disagreeing_with_op_config_is_rejected(tmp_path):
    # sad path (defense in depth): both labels are individually ALLOWED, but the
    # spec.mode the fleet schedules on disagrees with the op_config the spine
    # already validated -- a silent inconsistency that would mis-drive the run.
    x = _repo(tmp_path, "x")
    prog = _program(ModeRunSpec(
        tool=x, mode="research",
        op_config=_spec("z", tool=x, mode="develop").op_config,
        run_id="a"))
    ok, errors = validate_program(prog)
    assert ok is False
    assert any("disagree" in e or "op_config" in e for e in errors)


def test_empty_mode_is_rejected(tmp_path):
    # edge: an empty mode string is neither ALLOWED nor a real label.
    x = _repo(tmp_path, "x")
    prog = _program(ModeRunSpec(
        tool=x, mode="",
        op_config=_spec("z", tool=x, mode="develop").op_config,
        run_id="a"))
    ok, errors = validate_program(prog)
    assert ok is False and any("mode" in e for e in errors)


def test_none_op_config_fails_closed_not_raises(tmp_path):
    # sad path (hostile input): a ModeRunSpec built with op_config=None must make
    # the validator return (False, [...]) -- it must NOT raise AttributeError out
    # of the F1 boundary. getattr(spec.op_config, "mode", None) keeps it closed.
    x = _repo(tmp_path, "x")
    prog = _program(ModeRunSpec(
        tool=x, mode="develop", op_config=None, run_id="a"))
    ok, errors = validate_program(prog)
    assert ok is False and any("op_config" in e for e in errors)


def test_every_allowed_mode_matching_op_config_passes(tmp_path):
    # happy path: each ALLOWED mode, with a matching op_config, validates clean
    # (a read-only single-node program has no collision/dep defects).
    from peers.spine.op_config import ALLOWED_MODES
    x = _repo(tmp_path, "x")
    for m in ALLOWED_MODES:
        prog = _program(_spec("a", tool=x, mode=m, writable=False))
        ok, errors = validate_program(prog)
        assert ok is True and errors == [], f"mode {m!r} should validate: {errors}"
