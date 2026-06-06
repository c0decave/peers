from __future__ import annotations

import os
import stat
import subprocess
import sys
from typing import Any

from peers.driver_helpers import _hash_goals_yaml
from peers.safe_io import read_text_no_symlink


def _driver_module() -> Any:
    import peers.driver_orchestrator as driver_orchestrator
    return driver_orchestrator


class DriverLifecycleMixin:
    def _capture_peer_dir_identity(self) -> tuple[int, int]:
        try:
            st = self.peer_dir.lstat()
        except OSError as e:
            raise RuntimeError(
                f"{self.peer_dir} is unavailable; refusing to operate: {e}"
            ) from e
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(
                f"{self.peer_dir} is not a directory; refusing to operate."
            )
        return (st.st_dev, st.st_ino)

    def _verify_peer_dir_identity(self) -> None:
        current = self._capture_peer_dir_identity()
        if self._peer_dir_identity is None:
            self._peer_dir_identity = current
            return
        if current != self._peer_dir_identity:
            raise RuntimeError(
                f"{self.peer_dir} changed while the loop was running; "
                "refusing control-plane IO. Restore the original .peers "
                "directory and restart."
            )

    def _save_state(self, state: dict[str, Any]) -> None:
        self._verify_peer_dir_identity()
        self.state_store.save(state)

    def _write_stop_reason(self, reason: str) -> None:
        """Write `.peers/last-stop-reason.txt` so `peers-ctl reconcile`
        can distinguish a clean self-termination ("stopped") from a
        hard process death ("crashed"). Pre-Phase-V, v6 and v7 both
        ran to convergence-complete but the controller marked them as
        crashed because there was no clean-exit sentinel.

        Best-effort: never let sentinel-write failure abort the exit
        path. Format: `<reason> <iso_utc_timestamp>\\n`.
        """
        try:
            import datetime as _dt
            sentinel = self.peer_dir / "last-stop-reason.txt"
            tmp = sentinel.with_suffix(sentinel.suffix + ".tmp")
            ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            tmp.write_text(f"{reason} {ts}\n")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, sentinel)
        except Exception as e:
            print(
                f"peers: warning, failed to write stop-reason sentinel: {e!r}",
                file=sys.stderr,
            )

    def _run_recon_step(self) -> None:
        """Substrate pre-tick recon. Runs once at the start of `run()`
        to write `.peers/recon.md` with a static project digest. The
        peers loop reads this file (via prompt-builder, future hookup)
        so tick 1 isn't burned on figure-out-what-is-this work.

        Recon is substrate-only — no LLM call, no token cost, no budget
        deduction. Errors are logged but do not abort the run; recon is
        a nice-to-have, not a hard prerequisite.
        """
        try:
            status = _driver_module()._run_recon(self.repo, self.peer_dir)
            print(f"peers: {status}", file=sys.stderr)
        except Exception as e:
            print(
                f"peers: warning: recon step failed: {e!r}; "
                "continuing without recon.md",
                file=sys.stderr,
            )

    def _run_codemap_step(self) -> None:
        """Substrate pre-tick structural CODEMAP. Writes .peers/CODEMAP.yaml
        + .peers/codemap.md (public API + signatures) so peers know the
        codebase's shape before tick 1. AST-only — no LLM, no budget cost.
        Errors are logged, never fatal."""
        try:
            status = _driver_module()._run_codemap(self.repo, self.peer_dir)
            print(f"peers: {status}", file=sys.stderr)
        except Exception as e:
            print(
                f"peers: warning: codemap step failed: {e!r}; "
                "continuing without codemap",
                file=sys.stderr,
            )

    def _run_document_seed_step(self) -> None:
        """`document`-mode only: seed the repo-root CODEMAP.yaml with the
        structural map (correct id/kind/file/line/signature, empty summaries)
        so the peers add summaries rather than invent structure. The three
        structural gates start green on the seed; summaries-complete starts red
        and drives the build. AST-only, idempotent (never clobbers an existing
        CODEMAP.yaml), errors logged-not-fatal."""
        try:
            status = _driver_module()._seed_repo_codemap(self.repo)
            print(f"peers: {status}", file=sys.stderr)
        except Exception as e:
            print(
                f"peers: warning: document seed step failed: {e!r}; "
                "continuing (peers can build CODEMAP.yaml themselves)",
                file=sys.stderr,
            )

    def _run_document_architecture_seed_step(self) -> None:
        """`document`-mode only: seed the repo-root ARCHITECTURE.md with the
        narrative outline so the `architecture-grounded` gate starts red and
        drives the human-docs build. AST-only, idempotent (never clobbers an
        existing ARCHITECTURE.md), errors logged-not-fatal."""
        try:
            status = _driver_module()._seed_repo_architecture(self.repo)
            print(f"peers: {status}", file=sys.stderr)
        except Exception as e:
            print(
                f"peers: warning: document architecture seed step failed: "
                f"{e!r}; continuing (peers can write ARCHITECTURE.md themselves)",
                file=sys.stderr,
            )

    def _verify_no_control_symlinks(self) -> None:
        """L1: refuse to operate on a .peers/ where any of the
        control files (or substrate-written log/report files) are
        symlinks.

        expanded the list. Previously only
        the static config/state files were checked, but the substrate
        ALSO writes to log/runs.jsonl, HALTED.md, REPORT.md, VERIFY.md
        and comms/*. A peer pre-creating those as symlinks could get
        the substrate to write to e.g. ~/.bashrc, ~/.ssh/authorized_keys
        with the substrate's own privileges. log/runs.jsonl is the
        worst — appended to every tick.
        """
        if self.peer_dir.is_symlink():
            raise RuntimeError(
                f"{self.peer_dir} is a symlink "
                f"({os.readlink(self.peer_dir)!r}); refusing to operate. "
                "Remove it manually to continue."
            )
        static_files = (
            "state.json", "goals.yaml", "goals.sha256",
            "state.json.tmp", "state.json.pre-migration",
            "run.lock", "config.yaml",
            # additions — substrate-managed write targets:
            "HALTED.md", "REPORT.md", "VERIFY.md",
            "log/runs.jsonl",
        )
        for rel in static_files:
            p = self.peer_dir / rel
            if p.is_symlink():
                raise RuntimeError(
                    f"{p} is a symlink ({os.readlink(p)!r}); "
                    "refusing to operate. Remove it manually to "
                    "continue."
                )
        static_dirs = ("log", "comms", "hooks", "checks", "queue")
        for rel in static_dirs:
            p = self.peer_dir / rel
            if p.is_symlink():
                raise RuntimeError(
                    f"{p} is a symlink ({os.readlink(p)!r}); "
                    "refusing to operate. Remove it manually to "
                    "continue."
                )
        # Recursively check the comms tree (sender-to-receiver dirs +
        # the archive) since hybrid comm-layer writes into them too.
        comms_root = self.peer_dir / "comms"
        if comms_root.exists():
            for sub in comms_root.rglob("*"):
                if sub.is_symlink():
                    raise RuntimeError(
                        f"{sub} is a symlink ({os.readlink(sub)!r}); "
                        "refusing to operate (hybrid comm files would "
                        "otherwise write through). Remove it manually."
                    )

    def _read_goal_hash_snapshot(self) -> str | None:
        """Read the goals.sha256 snapshot file ONCE at init time, or
        compute it from goals.yaml if the sha256 file is missing.
        Returns the hex digest, or None if no goals.yaml exists."""
        gfile = self.peer_dir / "goals.yaml"
        if not gfile.exists():
            return None
        snap = self.peer_dir / "goals.sha256"
        if snap.exists():
            try:
                return read_text_no_symlink(snap, max_bytes=129).strip().split()[0]
            except (OSError, IndexError):
                pass
        # Fall back to live hash — equivalent to "init now".
        return _hash_goals_yaml(gfile)

    def _sync_peer_order(self, state: dict[str, Any]) -> None:
        """If the loaded state's peer_order differs from the configured
        one (e.g. user reordered or renamed peers in config.yaml), trust
        the config and rebuild missing entries.

        Item 13: also (re)populate state['peer_roles'] from PeerSpec.role
        on every load so TurnManager.current() can skip recovery-role
        peers when default-role peers are healthy.
        """
        # Always refresh peer_roles — config can change PeerSpec.role
        # without otherwise touching peer_order.
        state["peer_roles"] = {p.name: p.role for p in self.peer_specs}
        if state.get("peer_order") != self.peer_names:
            old_order = state.get("peer_order", [])
            state["peer_order"] = list(self.peer_names)
            # Preserve health entries for peers still present; drop entries
            # for removed peers.
            old_peers = state.get("peers", {})
            new_peers: dict[str, Any] = {}
            for n in self.peer_names:
                new_peers[n] = old_peers.get(n) or {
                    "state": "healthy",
                    "consecutive_fails": 0,
                    "recent_fails": 0,
                    "recent_runs": [],
                }
            state["peers"] = new_peers
            # Try to preserve which peer was up next, otherwise reset.
            if 0 <= state.get("turn_index", -1) < len(old_order):
                old_active = old_order[state["turn_index"]]
                if old_active in self.peer_names:
                    state["turn_index"] = self.peer_names.index(old_active)
                else:
                    state["turn_index"] = 0
            else:
                state["turn_index"] = 0

    def _dirty_worktree(self, state: dict[str, Any] | None = None) -> bool:
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo, capture_output=True, text=True,
                check=True, encoding="utf-8", errors="replace",
            )
        except subprocess.CalledProcessError as e:
            # Fail-safe: a probe-failure is treated as DIRTY, not
            # clean. Otherwise a misbehaving git would mask the
            # tampering signal entirely.
            if state is not None:
                state.setdefault("warnings", []).append(
                    f"dirty-worktree probe failed: git status returned "
                    f"{e.returncode}; treating worktree as dirty"
                )
            return True
        return bool(r.stdout.strip())
