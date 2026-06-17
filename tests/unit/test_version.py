"""Both CLIs expose `--version`, both report the same version, and
both report the version declared in pyproject.toml. Catches:
  - someone adding a hardcoded version string back into __init__.py
  - peers and peers-ctl drifting apart (they MUST stay in lockstep —
    they are the same wheel)
  - someone bumping pyproject.toml but forgetting to verify
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "pyproject.toml has no version field"
    return m.group(1)


def test_peers_version_module_attr_matches_pyproject():
    import peers
    assert peers.__version__ == _pyproject_version()


def test_release_version_is_1_6_9():
    import peers
    assert peers.__version__ == "1.6.9"


def test_peers_ctl_version_module_attr_matches_pyproject():
    import peers_ctl
    assert peers_ctl.__version__ == _pyproject_version()


def test_peers_and_peers_ctl_share_version():
    """Same wheel → must report identical version. Drift would mean
    someone added a hardcoded literal instead of pulling from metadata."""
    import peers
    import peers_ctl
    assert peers.__version__ == peers_ctl.__version__


def test_peers_cli_version_flag():
    r = subprocess.run(
        [sys.executable, "-m", "peers.cli", "--version"],
        capture_output=True, text=True, check=False,
    )
    # argparse's `action="version"` prints to stdout and exits 0
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert out.startswith("peers "), f"unexpected: {out!r}"
    assert out == f"peers {_pyproject_version()}"


def test_peers_ctl_cli_version_flag():
    r = subprocess.run(
        [sys.executable, "-m", "peers_ctl.cli", "--version"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert out.startswith("peers-ctl "), f"unexpected: {out!r}"
    assert out == f"peers-ctl {_pyproject_version()}"


def test_version_string_is_semver_shaped():
    """No accidental `0+unknown` or empty in a real install."""
    import peers
    assert VERSION_RE.match(peers.__version__), (
        f"__version__ = {peers.__version__!r} is not SemVer-shaped — "
        f"is the package installed (importlib.metadata can resolve it)?"
    )
