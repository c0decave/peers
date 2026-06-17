"""Stage-4 — the real :class:`BaselineAuthor` adapter (thin).

``CharacterizationAuthor`` wires a characterization-test GENERATOR (the live
LLM/heuristic that reads the tool and emits candidate observation tests) to the
``CandidateBaseline`` contract the builder consumes. The renderer is INJECTED so
the unit test stays deterministic — it does NOT run a live LLM (that is
integration validation, NOT a unit acceptance). The QUALITY of the generated
characterization tests (do they truly pin behaviour?) is integration validation.

The adapter is the SOLE writer of the candidate test file, via the no-follow
primitive ``safe_io.atomic_write_text_in_dir_no_symlink`` — the builder re-hashes
that file from disk for the ``built`` ``file`` witness, so a later normaliser
swapping it would invalidate the hash (fail-closed), exactly as intended.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from peers import safe_io
from peers.spine.baseline import CandidateBaseline
from peers.spine.direction import Bar

#: The leaf file every characterization baseline is materialised to.
_CANDIDATE_NAME = "test_characterization.py"
_CANDIDATE_COMMAND = "python3 -m pytest test_characterization.py"


class CharacterizationAuthor:
    """A :class:`peers.spine.baseline.BaselineAuthor` whose test-body renderer is
    injected. ``render(repo, bar) -> str | None`` returns the candidate test body
    or ``None`` (the honest-stop input — the builder maps it to
    ``uncharacterizable``)."""

    def __init__(self, *, render: Callable[[Path, Bar], "str | None"]) -> None:
        self.render = render

    def author(self, repo: Path, bar: Bar) -> CandidateBaseline | None:
        body = self.render(Path(repo), bar)
        if body is None:
            return None
        path = Path(repo) / _CANDIDATE_NAME
        # The real 2-arg no-follow primitive (parent + basename derived from path
        # internally; the 3-arg append_text_in_dir_no_symlink is a DIFFERENT
        # function and would raise TypeError here).
        safe_io.atomic_write_text_in_dir_no_symlink(path, body)
        return CandidateBaseline(path=str(path), command=_CANDIDATE_COMMAND)
