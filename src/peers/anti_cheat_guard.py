"""Anti-cheating checks and remediation for peer handoff commits."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


_TEST_ONLY_PATH_RE = re.compile(
    r"(^tests?/|.*/tests?/|(^|/)test_[^/]+\.py$|(^|/)[^/]+_test\.py$|"
    r".*_test\.go$|.*\.test\.[a-zA-Z]+$)"
)

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]+\.py$|.*_test\.go$|.*\.test\."
    r"[a-zA-Z]+$)"
)

MAX_RECENT_DIFF_STATS = 50


def is_test_only_commit(repo: Path, ref: str) -> bool:
    """True iff ``ref`` changes at least one file and all are tests."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff-tree", "--no-commit-id",
             "--name-only", "-r", ref],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    files = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    return bool(files) and all(_TEST_ONLY_PATH_RE.search(path) for path in files)


class AntiCheatGuard:
    """Classifies and handles test-only peer handoff attempts."""

    def __init__(
        self,
        repo: Path,
        head_before_invoke: str | None,
        head_sha: Callable[[], str],
    ) -> None:
        self.repo = repo
        self.head_before_invoke = head_before_invoke
        self.head_sha = head_sha

    def apply_outcome(
        self, state: dict[str, Any], peer: str, success: bool,
    ) -> bool:
        """Apply anti-cheating policy to a successful handoff result."""
        pinfo = state["peers"][peer]
        if not success:
            if pinfo.get("failed_cheating"):
                pinfo["failed_cheating"] = 0
            return False

        cheating = self.classify_cheating(state)
        if cheating is None:
            if pinfo.get("failed_cheating"):
                pinfo["failed_cheating"] = 0
            pinfo.pop("anti_cheat_prewarned", None)
            return True

        justification = self.test_only_justification()
        if justification is not None:
            pinfo.pop("anti_cheat_prewarned", None)
            state.setdefault("anti_cheat_justifications", []).append({
                "iter": state.get("iteration", 0),
                "peer": peer,
                "reason": justification[:500],
                "head": self.head_sha(),
            })
            state.setdefault("warnings", []).append(
                "anti-cheating: accepted test-only handoff because the "
                "commit included JUSTIFIED-TEST-ONLY. Audit the rationale: "
                f"{justification[:240]}"
            )
            return True

        if not pinfo.get("anti_cheat_prewarned"):
            pinfo["anti_cheat_prewarned"] = cheating
            state.setdefault("warnings", []).append(
                "anti-cheating pre-warning: your previous handoff changed "
                f"only tests ({cheating}). On the next tick, include a "
                "production-code change, add an explicit "
                "JUSTIFIED-TEST-ONLY rationale in the commit message, or "
                "the substrate will revert the test-only work."
            )
            return True

        reverted = self.revert_handoff(reason=cheating)
        pinfo.pop("anti_cheat_prewarned", None)
        pinfo["failed_cheating"] = pinfo.get("failed_cheating", 0) + 1
        pinfo.setdefault("last_run", {})["soft_fail_reason"] = (
            f"anti-cheating revert: {cheating}"
            + (" (revert failed)" if not reverted else "")
        )
        state.setdefault("warnings", []).append(
            "anti-cheating: your previous handoff was reverted because "
            f"{cheating}. Fix the underlying production code instead of "
            "relaxing tests / gaming metrics."
        )
        return False

    def classify_cheating(self, state: dict[str, Any]) -> str | None:
        """Return a human-readable reason for a clearly cheating diff."""
        _ = state
        stats = self.diff_stats_since_invoke()
        if stats is None:
            return None
        test_lines, src_lines = stats
        if test_lines > 0 and src_lines == 0:
            return (
                "the turn modified only test files "
                f"(+{test_lines} test lines, 0 source). Production "
                "behavior cannot have been the reason a gate flipped."
            )
        return None

    def diff_stats_since_invoke(self) -> tuple[int, int] | None:
        """Return (test_lines_delta, source_lines_delta) since invocation."""
        since = self.head_before_invoke
        if since is None or since == self.head_sha():
            return None
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", f"{since}..HEAD"],
                cwd=self.repo, capture_output=True, text=True,
                check=True, encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError:
            return None

        test_lines = 0
        src_lines = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, rem, path = parts
            try:
                delta = int(add) + int(rem)
            except ValueError:
                continue
            if _TEST_PATH_RE.search(path):
                test_lines += delta
            else:
                src_lines += delta
        return test_lines, src_lines

    def test_only_justification(self) -> str | None:
        """Return a sufficiently long JUSTIFIED-TEST-ONLY reason, if any."""
        since = self.head_before_invoke
        if since is None:
            return None
        try:
            result = subprocess.run(
                ["git", "log", "--format=%B%x00", f"{since}..HEAD"],
                cwd=self.repo, capture_output=True, text=True, check=True,
                encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError:
            return None
        marker = "JUSTIFIED-TEST-ONLY:"
        for line in result.stdout.splitlines():
            if marker not in line:
                continue
            _, _, reason = line.partition(marker)
            reason = reason.strip(" #\t")
            if len(reason) >= 20:
                return reason
        return None

    def revert_handoff(self, reason: str) -> bool:
        """Revert the commits created since invocation and commit the revert."""
        since = self.head_before_invoke
        if since is None:
            return False
        try:
            subprocess.run(
                ["git", "revert", "--no-commit", f"{since}..HEAD"],
                cwd=self.repo, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            print(
                "peers: anti-cheating revert via `git revert` failed "
                f"({self._stderr_text(exc)}); falling back to "
                "`git reset --hard`",
                file=sys.stderr,
            )
            try:
                subprocess.run(
                    ["git", "reset", "--hard", since],
                    cwd=self.repo, check=True, capture_output=True,
                )
                return True
            except subprocess.CalledProcessError as exc2:
                print(
                    "peers: CRITICAL: anti-cheating fallback reset ALSO "
                    "failed; cheating commit "
                    f"{self.head_sha()[:12]} is still in the tree. "
                    "Manual intervention required. "
                    f"({self._stderr_text(exc2)})",
                    file=sys.stderr,
                )
                return False
        try:
            subprocess.run(
                ["git",
                 "-c", "user.email=peers-substrate@local",
                 "-c", "user.name=peers-substrate",
                 "commit", "-m",
                 f"Anti-cheating revert: {reason}\n\n"
                 "Peer: peers-substrate\n"],
                cwd=self.repo, check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            print(
                "peers: anti-cheating revert staged but commit failed: "
                f"{self._stderr_text(exc)}. Leaving partial revert in "
                "index.",
                file=sys.stderr,
            )
            return False

    def detect_tampering(self, state: dict[str, Any]) -> None:
        """Append soft tampering warnings and diff stats after success."""
        stats = self.diff_stats_since_invoke()
        if stats is None:
            return
        test_lines, src_lines = stats
        if test_lines > 0 and src_lines == 0:
            state.setdefault("warnings", []).append(
                "test-tampering: turn modified only test files "
                f"(+{test_lines} lines, 0 src). Verify the tests still "
                "match the spec rather than being weakened."
            )
        self._record_recent_diff_stats(state, test_lines, src_lines)

    def _record_recent_diff_stats(
        self, state: dict[str, Any], test_lines: int, src_lines: int,
    ) -> None:
        """Record bounded per-handoff diff stats for dashboard hints."""
        stats_by_head = state.get("recent_diff_stats")
        if not isinstance(stats_by_head, dict):
            stats_by_head = {}
            state["recent_diff_stats"] = stats_by_head

        head = self.head_sha()
        stats_by_head.pop(head, None)
        stats_by_head[head] = {"test_lines": test_lines, "src_lines": src_lines}

        overflow = len(stats_by_head) - MAX_RECENT_DIFF_STATS
        if overflow <= 0:
            return
        for old_head in list(stats_by_head)[:overflow]:
            stats_by_head.pop(old_head, None)

    @staticmethod
    def _stderr_text(exc: subprocess.CalledProcessError) -> str:
        if exc.stderr is None:
            return str(exc)
        if isinstance(exc.stderr, bytes):
            return exc.stderr.decode("utf-8", errors="replace").strip()
        return str(exc.stderr).strip()
