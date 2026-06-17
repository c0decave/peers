"""STEP-7 — direction-inference: the minimal "is there a trustworthy bar?" detector.

Stage 0 ships only the *detector*, not the characterization-baseline builder (P6,
Stage 4). It answers: does this tool expose a runnable test command whose baseline
is green-and-stable? Classification:
  - `present` — a runner was detected AND the injected baseline run exits 0,
  - `weak`    — a runner was detected but the baseline run is red,
  - `absent`  — no runner, or the baseline run produced no result.
The runner result is injected (no heavy suite runs in the unit test).

Covers happy (pytest green → present), edge (npm/go detection; pytest.ini; ledger
row), sad (no runner → absent; red → weak; runner present but None/garbage result
→ absent; package.json without a test script → absent).
"""
from peers.spine.direction import Bar, infer_bar
from peers.spine.ledger import RunLedger


def test_absent_when_no_runner(tmp_path):
    assert infer_bar(tmp_path, run_tests=lambda cmd: None).kind == "absent"


def test_present_when_green(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    bar = infer_bar(tmp_path, run_tests=lambda cmd: (0, "1 passed"))
    assert bar.kind == "present" and bar.command is not None


def test_weak_when_red(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert infer_bar(tmp_path, run_tests=lambda cmd: (1, "1 failed")).kind == "weak"


def test_pytest_ini_marker(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    bar = infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok"))
    assert bar.kind == "present" and "pytest" in bar.command


def test_npm_test_script_detected(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    bar = infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok"))
    assert bar.kind == "present" and "npm" in bar.command


def test_package_json_without_test_script_is_absent(tmp_path):
    # sad: a package.json with no `test` script is not a runner.
    (tmp_path / "package.json").write_text('{"name": "x", "scripts": {"build": "tsc"}}')
    assert infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok")).kind == "absent"


def test_go_mod_detected(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    bar = infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok"))
    assert bar.kind == "present" and "go test" in bar.command


def test_runner_present_but_none_result_is_absent(tmp_path):
    # edge: a detected runner whose baseline run yields nothing -> no bar.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    bar = infer_bar(tmp_path, run_tests=lambda cmd: None)
    assert bar.kind == "absent" and bar.command is not None


def test_garbage_result_is_absent(tmp_path):
    # sad: a malformed run_tests return is treated as "no trustworthy bar".
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert infer_bar(tmp_path, run_tests=lambda cmd: "weird").kind == "absent"


def test_pytest_wins_over_npm_when_both_present(tmp_path):
    # edge: deterministic priority — pytest is detected before npm.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    bar = infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok"))
    assert "pytest" in bar.command


def test_bar_inferred_row_is_ledgered(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    led = RunLedger(tmp_path / "run.jsonl")
    infer_bar(tmp_path, run_tests=lambda cmd: (0, "ok"), ledger=led, mode_run="r1")
    (row,) = [r for r in led.read() if r.event == "bar-inferred"]
    assert row.witness["bar"] == "present" and row.mode_run == "r1"


def test_bar_is_a_dataclass_with_fields(tmp_path):
    bar = infer_bar(tmp_path, run_tests=lambda cmd: None)
    assert isinstance(bar, Bar)
    assert bar.command is None and bar.exit_code is None
