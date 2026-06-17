from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from peers.driver_helpers import _hash_goals_yaml
from peers.driver_host import _DriverHost
from peers.safe_io import (
    atomic_write_text_in_dir_no_symlink,
    read_bytes_no_symlink,
    read_text_no_symlink,
)


def _driver_module() -> Any:
    import peers.driver_orchestrator as driver_orchestrator
    return driver_orchestrator


class DriverLifecycleMixin(_DriverHost):
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

    def _open_peer_dir_identity_fd(self, expected: tuple[int, int]) -> int:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self.peer_dir), flags)
        except OSError as e:
            raise RuntimeError(
                f"{self.peer_dir} is unavailable; refusing to operate: {e}"
            ) from e
        st = os.fstat(fd)
        current = (st.st_dev, st.st_ino)
        if current != expected:
            os.close(fd)
            raise RuntimeError(
                f"{self.peer_dir} changed while the loop was running; "
                "refusing control-plane IO. Restore the original .peers "
                "directory and restart."
            )
        return fd

    def _close_peer_dir_identity_fd(self) -> None:
        fd = getattr(self, "_peer_dir_identity_fd", None)
        if fd is None:
            return
        self._peer_dir_identity_fd = None
        try:
            os.close(fd)
        except OSError:
            pass

    def _verify_peer_dir_identity(self) -> None:
        current = self._capture_peer_dir_identity()
        if self._peer_dir_identity is None:
            self._peer_dir_identity = current
            if getattr(self, "_peer_dir_identity_fd", None) is None:
                self._peer_dir_identity_fd = self._open_peer_dir_identity_fd(
                    current
                )
            return
        expected = self._peer_dir_identity
        fd = getattr(self, "_peer_dir_identity_fd", None)
        if fd is None:
            self._peer_dir_identity_fd = self._open_peer_dir_identity_fd(
                expected
            )
            return
        try:
            st = os.fstat(fd)
        except OSError as e:
            raise RuntimeError(
                f"{self.peer_dir} identity handle is unavailable; "
                "refusing control-plane IO."
            ) from e
        expected = (st.st_dev, st.st_ino)
        if current != expected:
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
            ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            atomic_write_text_in_dir_no_symlink(
                sentinel, f"{reason} {ts}\n",
            )
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

    # CAP-14 FINDING-1 DE-SCOPE: the STEP-5 convergence-time regenerator hooks
    # (`_run_regenerate_architecture_actual_step` and
    # `_maybe_regenerate_architecture_actual_on_convergence`) were REMOVED. They
    # wrote ARCHITECTURE.actual.md from the live tree with STRUCTURAL module-H2
    # headings, but a production ARCHITECTURE.intended.md is PEER-AUTHORED PROSE
    # with SEMANTIC H2s (disjoint heading sets) — so the soft architecture-
    # coherent gate WARNed permanently on every converged implement run
    # regardless of real drift. The gate is now back to its prior honest
    # behaviour (actual_missing -> soft no-op). A REAL arch-coherent check is a
    # tracked follow-up requiring a design decision (AST-vs-AST drift or a
    # normalized comparison), NOT delivered here.

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
        self._verify_checks_manifest()
        self._verify_checks_template_version()

    def _verify_checks_template_version(self) -> None:
        """CAP-14 (defense-in-depth, STACKED on `_verify_checks_manifest`):
        fail CLOSED when a deployed gate script has drifted from the CURRENT
        template source it was provisioned from.

        ``_verify_checks_manifest`` only proves the deployed copy still matches
        ``checks.sha256`` — a digest written from the SAME bytes at provision
        time (cli.py). So a snapshot deployed BEFORE a template fix (e.g. the
        BUG-011 whole-word `xit`/`xdescribe` fix in no_skipped_tests.py) matches
        its own manifest and passes silently. That stale gate forced two false
        product-convergence calls.

        This second layer reads the provision-time companion
        ``.peers/checks.template.sha256`` (written by ``peers init``), and for
        every name it records compares the DEPLOYED ``.peers/checks/<name>``
        bytes against the CURRENT template source bytes (resolved live from
        ``peers.modes``). Any mismatch raises — re-run ``peers init`` to
        re-deploy the corrected gate.

        Inert by design until provisioned: a missing ``.peers/checks/`` dir, a
        missing companion (legacy trees), or a name with no resolvable template
        source all return cleanly. A PRESENT-but-stale companion is a hard stop.
        Never fabricates a digest; on uncertainty it errs toward inert, NOT
        toward a false pass (the deployed-vs-template byte compare is exact).
        """
        checks_dir = self.peer_dir / "checks"
        if not checks_dir.is_dir():
            return
        companion = self.peer_dir / "checks.template.sha256"
        if not companion.exists():
            # Legacy tree provisioned before CAP-14 — nothing to enforce.
            return
        try:
            companion_text = read_text_no_symlink(
                companion, max_bytes=256 * 1024,
            )
        except OSError as e:
            raise RuntimeError(
                f"failed to read {companion}: {e}; refusing to operate."
            ) from e

        tracked: dict[str, str] = {}
        for lineno, raw_line in enumerate(companion_text.splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise RuntimeError(
                    f"{companion}:{lineno}: malformed "
                    "checks.template.sha256 line"
                )
            digest, name = parts[0].lower(), parts[1].strip()
            if (
                len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise RuntimeError(
                    f"{companion}:{lineno}: malformed sha256 digest"
                )
            if name in ("", ".", "..") or os.path.basename(name) != name:
                raise RuntimeError(
                    f"{companion}:{lineno}: check name must be a single path "
                    f"component, got {name!r}"
                )
            tracked[name] = digest

        if not tracked:
            return

        template_sources = self._resolve_template_check_sources()

        for name in sorted(tracked):
            tmpl_path = template_sources.get(name)
            if tmpl_path is None:
                # No current template ships this name (the mode set changed, or
                # a user-supplied check). Stay inert rather than guess.
                continue
            try:
                tmpl_bytes = read_bytes_no_symlink(tmpl_path)
            except OSError:
                # Template unreadable — cannot prove drift, do not fabricate.
                continue
            deployed_path = checks_dir / name
            try:
                deployed_bytes = read_bytes_no_symlink(deployed_path)
            except OSError as e:
                raise RuntimeError(
                    f"failed to read deployed check {deployed_path}: {e}; "
                    "refusing to operate."
                ) from e
            tmpl_digest = hashlib.sha256(tmpl_bytes).hexdigest()
            dep_digest = hashlib.sha256(deployed_bytes).hexdigest()
            if tmpl_digest != dep_digest:
                raise RuntimeError(
                    f"{name}: deployed gate is STALE vs template "
                    f"(expected {tmpl_digest}, got {dep_digest}); "
                    "re-run `peers init` to re-deploy the corrected gate."
                )

    @staticmethod
    def _resolve_template_check_sources() -> dict[str, Path]:
        """Map check-script basename -> current template source Path.

        Resolved live from ``peers.modes.discover()`` so a template fix
        (re-vendored gate) changes the bytes this guard enforces. Scans each
        discovered mode's ``checks/`` dir AND its ``checks/lang_*/`` subdirs so
        language-specific gates resolve too. A user mode overriding a builtin
        name wins (last writer), matching ``merge``'s override semantics.
        """
        from peers import modes as _modes

        sources: dict[str, Path] = {}
        for mode in _modes.discover().values():
            cdir = mode.path / "checks"
            if not cdir.is_dir():
                continue
            for entry in sorted(cdir.iterdir()):
                if entry.is_file() and entry.name.endswith(".py"):
                    sources[entry.name] = entry
                elif entry.is_dir() and entry.name.startswith("lang_"):
                    for sub in sorted(entry.iterdir()):
                        if sub.is_file() and sub.name.endswith(".py"):
                            sources[sub.name] = sub
        return sources

    def _verify_checks_manifest(self) -> None:
        """Fail closed when installed gate scripts drift from checks.sha256.

        ``peers init`` writes the manifest after hardening ``.peers/checks``.
        The chmod is only advisory for same-UID peers, so the live driver must
        re-hash the scripts before trusting any hard-gate evaluation.
        """
        checks_dir = self.peer_dir / "checks"
        if not checks_dir.exists():
            return
        if not checks_dir.is_dir():
            raise RuntimeError(
                f"{checks_dir} is not a directory; refusing to operate."
            )
        manifest_path = self.peer_dir / "checks.sha256"
        if not manifest_path.exists():
            raise RuntimeError(
                f"{manifest_path} is missing while {checks_dir} exists; "
                "refusing to run unverifiable gate scripts."
            )
        try:
            manifest_text = read_text_no_symlink(
                manifest_path, max_bytes=256 * 1024,
            )
        except OSError as e:
            raise RuntimeError(
                f"failed to read {manifest_path}: {e}; refusing to operate."
            ) from e

        expected: dict[str, str] = {}
        for lineno, raw_line in enumerate(manifest_text.splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                raise RuntimeError(
                    f"{manifest_path}:{lineno}: malformed checks.sha256 line"
                )
            digest, name = parts[0].lower(), parts[1].strip()
            if (
                len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise RuntimeError(
                    f"{manifest_path}:{lineno}: malformed sha256 digest"
                )
            if name in ("", ".", "..") or os.path.basename(name) != name:
                raise RuntimeError(
                    f"{manifest_path}:{lineno}: check name must be a "
                    f"single path component, got {name!r}"
                )
            if not name.endswith(".py"):
                raise RuntimeError(
                    f"{manifest_path}:{lineno}: check manifest only supports "
                    f"Python check scripts, got {name!r}"
                )
            if name in expected:
                raise RuntimeError(
                    f"{manifest_path}:{lineno}: duplicate check entry {name!r}"
                )
            expected[name] = digest

        actual_names = {
            p.name for p in checks_dir.iterdir()
            if p.name.endswith(".py")
        }
        expected_names = set(expected)
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        if missing:
            raise RuntimeError(
                f"{manifest_path} lists missing check script(s): "
                f"{', '.join(missing)}"
            )
        if extra:
            raise RuntimeError(
                f"{manifest_path} does not list installed check script(s): "
                f"{', '.join(extra)}"
            )

        for name, expected_digest in expected.items():
            check_path = checks_dir / name
            try:
                data = read_bytes_no_symlink(check_path)
            except OSError as e:
                raise RuntimeError(
                    f"failed to read check script {check_path}: {e}; "
                    "refusing to operate."
                ) from e
            actual_digest = hashlib.sha256(data).hexdigest()
            if actual_digest != expected_digest:
                raise RuntimeError(
                    f"{manifest_path} mismatch for {name}: expected "
                    f"{expected_digest}, got {actual_digest}; refusing to "
                    "run tampered gate scripts."
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
