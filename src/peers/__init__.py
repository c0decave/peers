"""peers: substrate that drives n ≥ 2 AI coding CLIs as cooperating peers."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
import tomllib


def _source_tree_version() -> str | None:
    """Prefer the checkout's pyproject version when running from source.

    Local editable installs can leave importlib.metadata briefly stale after
    a version bump. Reading pyproject in a source tree keeps `--version`
    honest without affecting wheel installs, where pyproject is absent.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return None
        project = data.get("project")
        if isinstance(project, dict) and project.get("name") == "peers":
            version = project.get("version")
            return version if isinstance(version, str) else None
    return None


__version__ = _source_tree_version()
if __version__ is None:
    try:
        __version__ = _pkg_version("peers")
    except PackageNotFoundError:
        # Source-tree run before the package is installed (e.g. `python -m
        # peers ...` from a fresh checkout). Fall through to a sentinel
        # rather than crash; both CLIs surface this verbatim via --version.
        __version__ = "0+unknown"

__all__ = ["__version__"]
