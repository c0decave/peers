"""Wave-1a: the [tui] optional extra is declared and core stays pyyaml-only."""
import tomllib
from pathlib import Path


def _pyproject():
    root = Path(__file__).resolve().parents[2]
    return tomllib.loads((root / "pyproject.toml").read_text())


def test_tui_extra_declared():
    extras = _pyproject()["project"]["optional-dependencies"]
    assert "tui" in extras
    joined = " ".join(extras["tui"])
    assert "textual" in joined and "textual-window" in joined


def test_core_runtime_deps_stay_minimal():
    deps = _pyproject()["project"]["dependencies"]
    assert all("textual" not in d for d in deps)
