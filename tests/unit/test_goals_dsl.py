import json
import time
from pathlib import Path

import pytest

from peers.goals import evaluate_pass_when, load_goals


def test_exit_code_check_pass():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    assert evaluate_pass_when("exit_code == 0", ctx) is True


def test_exit_code_check_fail():
    ctx = {"exit_code": 1, "stdout": "", "stderr": "", "cwd": Path(".")}
    assert evaluate_pass_when("exit_code == 0", ctx) is False


def test_regex_absent_pass():
    ctx = {"exit_code": 0, "stdout": "all good", "stderr": "",
           "cwd": Path(".")}
    assert evaluate_pass_when("regex('FAIL', stdout) == None", ctx) is True


def test_regex_present_fail():
    ctx = {"exit_code": 0, "stdout": "test FAILED", "stderr": "",
           "cwd": Path(".")}
    assert evaluate_pass_when("regex('FAIL', stdout) == None", ctx) is False


def test_int_threshold():
    ctx = {"exit_code": 0, "stdout": "3", "stderr": "", "cwd": Path(".")}
    assert evaluate_pass_when("int(stdout.strip()) < 5", ctx) is True
    ctx["stdout"] = "9"
    assert evaluate_pass_when("int(stdout.strip()) < 5", ctx) is False


def test_json_path_threshold(tmp_path: Path):
    p = tmp_path / "cov.json"
    p.write_text(json.dumps({"totals": {"percent": 82.5}}))
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": tmp_path}
    expr = "json('cov.json').totals.percent >= 80"
    assert evaluate_pass_when(expr, ctx) is True


def test_json_dsl_rejects_file_over_size_cap(tmp_path: Path):
    from peers.goals import _MAX_DSL_JSON_BYTES
    p = tmp_path / "huge.json"
    p.write_text('{"blob":"' + ("x" * _MAX_DSL_JSON_BYTES) + '"}')
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": tmp_path}

    with pytest.raises(ValueError, match="json\\(\\) file too large"):
        evaluate_pass_when("json('huge.json').blob == 'x'", ctx)


def test_load_goals_parses_yaml(tmp_path: Path):
    yaml = """
goals:
  - id: tests-pass
    type: hard
    cmd: "pytest -q"
    pass_when: "exit_code == 0"
"""
    p = tmp_path / "goals.yaml"
    p.write_text(yaml)
    goals = load_goals(p)
    assert len(goals) == 1
    g = goals[0]
    assert g.id == "tests-pass"
    assert g.type == "hard"
    assert g.cmd == "pytest -q"
    assert g.pass_when == "exit_code == 0"


def test_load_goals_refuses_symlinked_goals_file(tmp_path: Path):
    target = tmp_path / "real-goals.yaml"
    target.write_text("goals: []\n")
    link = tmp_path / "goals.yaml"
    link.symlink_to(target)

    with pytest.raises(OSError):
        load_goals(link)


def test_dsl_rejects_arbitrary_python():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("__import__('os').system('echo pwned')", ctx)


def test_dsl_rejects_dunder_attribute():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("stdout.__class__", ctx)


def test_dsl_rejects_literal_rooted_attribute_chain():
    """Closes the `().__class__.__bases__[0].__subclasses__()` escape."""
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("().__class__", ctx)
    with pytest.raises(ValueError):
        evaluate_pass_when("''.__class__", ctx)
    with pytest.raises(ValueError):
        evaluate_pass_when("{}.__class__", ctx)


def test_dsl_rejects_subscript():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("stdout[0]", ctx)


def test_dsl_rejects_method_not_in_whitelist():
    ctx = {"exit_code": 0, "stdout": "x", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("stdout.encode()", ctx)


def test_dsl_still_accepts_stdout_strip():
    ctx = {"exit_code": 0, "stdout": "  3  ", "stderr": "", "cwd": Path(".")}
    assert evaluate_pass_when("int(stdout.strip()) == 3", ctx) is True


def test_dsl_rejects_lambda_and_comprehension():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError):
        evaluate_pass_when("(lambda: 1)()", ctx)
    with pytest.raises(ValueError):
        evaluate_pass_when("[x for x in [1]]", ctx)


def test_load_goals_rejects_hard_without_cmd(tmp_path: Path):
    """M4: a `type: hard` entry with no `cmd` would silently produce
    Goal(cmd=None), tripping an assertion deep in GoalEngine. Reject
    at load time."""
    p = tmp_path / "g.yaml"
    p.write_text(
        "goals:\n"
        " - id: x\n   type: hard\n   pass_when: 'exit_code == 0'\n"
    )
    import pytest as _pytest
    with _pytest.raises(ValueError, match="cmd"):
        from peers.goals import load_goals as _load
        _load(p)


def test_load_goals_rejects_unknown_type(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("goals:\n - id: x\n   type: weird\n")
    import pytest as _pytest
    with _pytest.raises(ValueError, match="type"):
        from peers.goals import load_goals as _load
        _load(p)


def test_load_goals_rejects_non_mapping_top_level(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("- not-a-mapping\n")
    with pytest.raises(ValueError, match="top-level"):
        load_goals(p)


def test_load_goals_wraps_yaml_parse_errors(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("goals:\n  - [unterminated\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_goals(p)


def test_load_goals_rejects_file_over_size_cap(tmp_path: Path):
    from peers.goals import _GOALS_YAML_MAX_BYTES

    p = tmp_path / "g.yaml"
    p.write_bytes(b"#" * (_GOALS_YAML_MAX_BYTES + 1))

    with pytest.raises(ValueError, match="file too large"):
        load_goals(p)


def test_load_goals_accepts_file_at_size_cap(tmp_path: Path):
    from peers.goals import _GOALS_YAML_MAX_BYTES

    p = tmp_path / "g.yaml"
    base = b"goals: []\n"
    filler = b"#" + b"x" * (_GOALS_YAML_MAX_BYTES - len(base) - 1)
    p.write_bytes(base + filler)

    assert load_goals(p) == []


def test_load_goals_wraps_pass_when_syntax_error(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text(
        "goals:\n"
        "  - id: broken\n"
        "    type: hard\n"
        "    cmd: 'true'\n"
        "    pass_when: 'exit_code =='\n"
    )

    with pytest.raises(ValueError, match="pass_when DSL invalid: syntax error"):
        load_goals(p)


def test_load_goals_rejects_non_list_goals(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("goals: {id: x}\n")
    with pytest.raises(ValueError, match="`goals` must be a list"):
        load_goals(p)


def test_load_goals_rejects_non_mapping_goal_entry(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("goals:\n  - plain-string\n")
    with pytest.raises(ValueError, match="goals\\[0\\] must be a mapping"):
        load_goals(p)


def test_load_goals_rejects_non_string_id(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("goals:\n  - id: 123\n    type: hard\n")
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        load_goals(p)


def test_load_goals_rejects_non_string_hard_cmd(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text(
        "goals:\n"
        "  - id: x\n"
        "    type: hard\n"
        "    cmd: 123\n"
        "    pass_when: 'exit_code == 0'\n"
    )
    with pytest.raises(ValueError, match="string `cmd`"):
        load_goals(p)


def test_load_goals_rejects_bool_consensus_needed(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text(
        "goals:\n"
        "  - id: x\n"
        "    type: soft\n"
        "    prompt: review\n"
        "    reviewer: other\n"
        "    consensus_needed: true\n"
    )
    with pytest.raises(ValueError, match="consensus_needed.*bool"):
        load_goals(p)


def test_dsl_rejects_bare_method_attribute_result():
    """M9: pass_when must return bool/numeric, not a bound method.
    `stdout.strip` (no parens) would otherwise be truthy and always pass."""
    ctx = {"exit_code": 0, "stdout": "x", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError, match="comparison"):
        evaluate_pass_when("stdout.strip", ctx)


def test_jsonview_supports_len(tmp_path: Path):
    """M5: len(json('x.json').items) should work for list/dict values."""
    import json as _json
    (tmp_path / "x.json").write_text(_json.dumps({"items": [1, 2, 3]}))
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": tmp_path}
    assert evaluate_pass_when("len(json('x.json').items) == 3", ctx) is True


def test_dsl_truncates_huge_stdout():
    """A pathological regex on a multi-GB stdout would hang the loop;
    truncation caps the input the DSL ever sees."""
    from peers.goals import _MAX_DSL_INPUT_BYTES
    huge = "a" * (_MAX_DSL_INPUT_BYTES + 10_000)
    ctx = {"exit_code": 0, "stdout": huge, "stderr": "",
           "cwd": Path(".")}
    # The DSL receives a truncated stdout; len(stdout) must equal cap.
    expr = f"len(stdout) == {_MAX_DSL_INPUT_BYTES}"
    assert evaluate_pass_when(expr, ctx) is True


def test_dsl_rejects_expensive_operators():
    ctx = {"exit_code": 0, "stdout": "", "stderr": "", "cwd": Path(".")}
    with pytest.raises(ValueError, match="operator not allowed"):
        evaluate_pass_when("10 ** 1000000 == 0", ctx)
    with pytest.raises(ValueError, match="operator not allowed"):
        evaluate_pass_when("'x' * 1000000 == ''", ctx)


def test_dsl_rejects_large_regex_pattern():
    ctx = {"exit_code": 0, "stdout": "x", "stderr": "", "cwd": Path(".")}
    pattern = "x" * 2000
    with pytest.raises(ValueError, match="pattern too large"):
        evaluate_pass_when(f"regex({pattern!r}, stdout) != None", ctx)


def test_dsl_regex_timeout_surfaces(monkeypatch):
    import peers.goals as goals_mod

    def slow_search(_pattern, _text):
        time.sleep(2)
        return None

    ctx = {"exit_code": 0, "stdout": "x", "stderr": "", "cwd": Path(".")}
    monkeypatch.setattr(goals_mod.re, "search", slow_search)

    with pytest.raises(ValueError) as exc:
        evaluate_pass_when("regex('x', stdout) != None", ctx)
    assert "timed out" in str(exc.value)


def test_safe_regex_search_rolls_back_handler_when_timer_setup_fails(
    monkeypatch,
):
    """BUG-105 reproducer (sad path): if signal.signal() succeeds but
    setitimer() then raises ValueError, the freshly installed
    ``_raise_timeout`` closure must NOT leak — the previous SIGALRM
    handler has to be restored before the fallback ``re.search`` runs.
    Parallel of BUG-101 in goals._safe_regex_search."""
    import peers.goals as goals_mod

    class FakeSignal:
        SIGALRM = "SIGALRM"
        ITIMER_REAL = "ITIMER_REAL"

        def __init__(self) -> None:
            self.handler = "previous-handler"
            self.signal_calls: list[object] = []

        def getsignal(self, signum):
            assert signum == self.SIGALRM
            return self.handler

        def signal(self, signum, handler):
            assert signum == self.SIGALRM
            self.signal_calls.append(handler)
            self.handler = handler

        def setitimer(self, which, seconds, interval=0):
            assert which == self.ITIMER_REAL
            raise ValueError("timer setup unavailable")

    fake = FakeSignal()
    monkeypatch.setattr(goals_mod, "signal", fake)

    match = goals_mod._safe_regex_search("needle", "needle in haystack")

    assert match is not None
    assert match.group(0) == "needle"
    # The fallback path must restore the original handler, not leave
    # the local ``_raise_timeout`` closure installed.
    assert fake.handler == "previous-handler"
    assert fake.signal_calls[-1] == "previous-handler"
