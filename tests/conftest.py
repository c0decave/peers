import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(scope="session")
def _isolated_git_global_config(tmp_path_factory):
    """A throwaway global gitconfig carrying only an identity.

    Tests spin up real git repos and assert on hooks / staged content.
    If the operator running the suite has a global `core.hooksPath`
    (e.g. a personal commit-gate) or a global `core.excludesFile` (e.g.
    one that ignores `.env`/secrets), those leak into every test repo:
    hooks install outside the tmp repo, and `git add leak.env` is
    silently refused. Both produce spurious failures — and the hook
    install can even pollute the operator's real global hook.
    """
    cfg = tmp_path_factory.mktemp("gitconfig") / "config"
    cfg.write_text(
        "[user]\n\tname = peers-tests\n\temail = tests@peers.local\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch, _isolated_git_global_config):
    """Point every git subprocess at the throwaway global config and
    disable system config, so the operator's global git environment
    (hooksPath, excludesFile, …) cannot influence the suite."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_isolated_git_global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
