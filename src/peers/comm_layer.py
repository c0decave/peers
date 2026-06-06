"""Two CommLayer variants:

- GitCommLayer (default): commits + trailers ARE the message bus.
- HybridCommLayer: code travels via git (same), but free-form messages
  travel via files in .peers/comms/<from>-to-<to>/NNNN-<topic>.md.
"""
from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from peers.peer_spec import is_valid_peer_name
from peers.safe_io import (
    _ensure_private_dir,
    _open_dir_fd_no_symlink,
    _open_private_nested_dir_fd_no_symlink,
)

# Trailer keys must be at least 2 chars to avoid matching one-letter
# words at the start of body lines. They must also not be common
# scheme prefixes that look like trailers (`http`, `https`, `ftp`, ...).
_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,}):\s*(.*?)\s*$")
_URL_SCHEME_KEYS = {"http", "https", "ftp", "ftps", "ssh", "file", "ws", "wss"}


def parse_trailers(message: str) -> dict[str, str]:
    """Parse git-style trailers from the bottom of a commit message.

    Walks lines from the end upward, collecting `Key: value` lines until
    a blank line or a non-trailer line is hit. CRLF tolerated.

    Defensive against URL-like body lines (`https://...`) and other
    non-trailer content masquerading as trailers: keys must be ≥ 2
    chars, the value must not start with `//`, and we explicitly
    reject known URL scheme keys.

    L3: when the SAME key appears multiple times (e.g. a hedging
    `Self-Review: fail` followed by a flipped `Self-Review: pass`),
    the FIRST occurrence walking from the end wins — i.e. we honor
    the LATER-AUTHORED trailer-LINE, but ignore any earlier
    contradicting one. This collapses to "later value wins"
    deterministically rather than "last in dict insertion order".
    """
    lines = [line_.rstrip("\r") for line_ in message.rstrip().splitlines()]
    trailers: dict[str, str] = {}
    for line in reversed(lines):
        if line.strip() == "":
            break
        m = _TRAILER_RE.match(line)
        if not m:
            break
        key, value = m.group(1), m.group(2)
        if key.lower() in _URL_SCHEME_KEYS:
            break
        if value.startswith("//"):
            break
        # First-seen wins (which, walking from the END, means the
        # last-written trailer for that key in the source message
        # wins deterministically).
        if key not in trailers:
            trailers[key] = value
    return trailers


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    trailers: dict[str, str]


class GitCommLayer:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)

    def _git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self.repo, check=True,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return r.stdout

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def new_commits_by(self, peer: str, since: str | None) -> list[Commit]:
        # Bootstrap: with no cursor, return [] rather than walking all of
        # HEAD. Caller seeds the cursor at head_sha() on first read.
        if since is None:
            return []
        rev_range = f"{since}..HEAD"
        # Use `-z` to NUL-terminate records and a NUL-safe field
        # separator inside each record. This is robust against commit
        # bodies containing any byte (including the old \x1e / \x1f
        # separators we used to pick).
        fmt = "%H%x00%s%x00%B"
        try:
            out = self._git("log", "-z", rev_range,
                            f"--format={fmt}", "--reverse")
        except subprocess.CalledProcessError:
            return []
        commits: list[Commit] = []
        # `-z` makes commits NUL-terminated; each record itself contains
        # three NUL-separated fields (sha, subject, body).
        # `out` ends up structured as:  sha\0subject\0body\0  per commit.
        # We split on \0 with maxsplit, then walk in 3-field chunks.
        parts = out.split("\x00")
        # Trailing empty string after final \0.
        if parts and parts[-1] == "":
            parts.pop()
        i = 0
        while i + 2 < len(parts):
            sha = parts[i].lstrip("\n")
            subject = parts[i + 1]
            body = parts[i + 2]
            i += 3
            trailers = parse_trailers(body)
            if trailers.get("Peer") != peer:
                continue
            commits.append(Commit(
                sha=sha, subject=subject,
                body=body, trailers=trailers,
            ))
        return commits


class HybridCommLayer:
    """Code via Git (same as GitCommLayer), free-form messages via
    `.peers/comms/<from>-to-<to>/NNNN-<topic>.md`.

    Handoff and self-review trailers still live on commits — the file
    channel is purely for things that don't fit a commit: status notes,
    soft-review request bodies, longer review responses, etc.
    """

    def __init__(self, repo: Path, peer_dir: Path) -> None:
        self.git_layer = GitCommLayer(repo)
        self.peer_dir = Path(peer_dir)

    # Delegate code-side calls to the git layer.
    def head_sha(self) -> str:
        return self.git_layer.head_sha()

    def new_commits_by(self, peer: str, since: str | None) -> list[Commit]:
        return self.git_layer.new_commits_by(peer=peer, since=since)

    # File-channel API.
    def _inbox_dir(self, sender: str, receiver: str) -> Path:
        return self.peer_dir / "comms" / f"{sender}-to-{receiver}"

    def _archive_dir(self) -> Path:
        return self.peer_dir / "comms" / "archive"

    def send(self, sender: str, receiver: str, topic: str,
             body: str) -> Path:
        """Drop a message file. Returns the path written.

        Race-safe and atomic-publication: the payload is written to a
        dotfile first, then linked into its final NNNN name. Consumers only
        glob final `NNNN-*.md` files, so they never archive an empty or
        partially written message.

        BUG-186: every directory hop and every file open uses a no-follow
        ``dir_fd`` so a same-user race that swaps ``.peers``,
        ``.peers/comms``, or ``.peers/comms/<inbox>`` to a symlink between
        creation and write cannot redirect the message file outside the
        inbox.
        """
        # Defence against path traversal: peer names must be tame
        # tokens — they end up as directory components. Reject anything
        # that would let a sender redirect the message file out of the
        # inbox.
        for who, label in ((sender, "sender"), (receiver, "receiver")):
            if not is_valid_peer_name(who):
                raise ValueError(
                    f"hybrid comm: invalid {label} name: {who!r}"
                )
        import os as _os
        inbox_dirname = f"{sender}-to-{receiver}"
        # Anchor every file op to a freshly opened dir fd that walked
        # every component with O_NOFOLLOW. A later swap of any ancestor
        # cannot follow into an attacker dir.
        dir_fd = _open_private_nested_dir_fd_no_symlink(
            self.peer_dir, ("comms", inbox_dirname),
        )
        try:
            safe_topic = re.sub(r"[^a-zA-Z0-9_-]+", "-", topic).strip("-")[:40]
            safe_topic = safe_topic or "msg"

            # Seed the sequence at the highest existing+1 number using the
            # dir_fd-relative listing so a swapped inbox cannot lie to us.
            existing_names = sorted(
                n for n in _os.listdir(dir_fd)
                if (
                    len(n) >= 5
                    and n[:4].isdigit()
                    and n[4] == "-"
                    and n.endswith(".md")
                )
            )
            next_n = 1 + (int(existing_names[-1][:4]) if existing_names else 0)
            ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
            payload = (
                f"---\nfrom: {sender}\nto: {receiver}\nts: {ts}\n"
                f"topic: {topic}\n---\n\n{body.rstrip()}\n"
            )
            d_for_display = self._inbox_dir(sender, receiver)
            for attempt in range(10_000):  # generous upper bound on collisions
                final_name = f"{next_n:04d}-{safe_topic}.md"
                tmp_name = (
                    f".{next_n:04d}-{safe_topic}"
                    f".{_os.getpid()}.{attempt}.tmp"
                )
                try:
                    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL
                    flags |= getattr(_os, "O_NOFOLLOW", 0)
                    flags |= getattr(_os, "O_CLOEXEC", 0)
                    fd = _os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
                except FileExistsError:
                    next_n += 1
                    continue
                try:
                    with _os.fdopen(fd, "w") as f:
                        f.write(payload)
                        f.flush()
                        _os.fsync(f.fileno())
                    try:
                        _os.link(
                            tmp_name, final_name,
                            src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                        )
                    except FileExistsError:
                        next_n += 1
                        continue
                    return d_for_display / final_name
                finally:
                    try:
                        _os.unlink(tmp_name, dir_fd=dir_fd)
                    except OSError:
                        pass
            raise RuntimeError(
                f"hybrid comm: gave up after 10000 attempts to create a "
                f"unique inbox file in {d_for_display}"
            )
        finally:
            _os.close(dir_fd)

    def fetch_new(self, sender: str, receiver: str) -> list[Path]:
        """Returns sorted list of unarchived messages from sender→receiver.

        BUG-186: walk the chain with O_DIRECTORY|O_NOFOLLOW and list via
        ``os.listdir(dir_fd)`` so a same-user race that swaps the comms or
        inbox directory to a symlink between the BUG-175 check and the
        listing cannot import attacker-controlled files into the prompt.
        Does NOT create the inbox when missing (read-only operation).
        """
        import os as _os
        comms_path = self.peer_dir / "comms"
        try:
            comms_fd = _open_dir_fd_no_symlink(comms_path)
        except FileNotFoundError:
            return []
        try:
            inbox_dirname = f"{sender}-to-{receiver}"
            inbox_display = comms_path / inbox_dirname
            # Pre-flight lstat so we can give a specific symlinked-inbox
            # error rather than the generic ELOOP/ENOTDIR that O_NOFOLLOW
            # surfaces (callers and tests key off the message text).
            try:
                pre = _os.stat(
                    inbox_dirname, dir_fd=comms_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                return []
            import stat as _stat
            if _stat.S_ISLNK(pre.st_mode):
                raise OSError(
                    f"refusing symlinked inbox path: {inbox_display}"
                )
            flags = _os.O_RDONLY
            flags |= getattr(_os, "O_DIRECTORY", 0)
            flags |= getattr(_os, "O_NOFOLLOW", 0)
            flags |= getattr(_os, "O_CLOEXEC", 0)
            try:
                inbox_fd = _os.open(inbox_dirname, flags, dir_fd=comms_fd)
            except FileNotFoundError:
                return []
            except OSError as e:
                # ELOOP or ENOTDIR after our pre-flight passed means a
                # late swap raced us; re-stat to confirm and re-raise as
                # the canonical symlinked-inbox refusal.
                try:
                    post = _os.stat(
                        inbox_dirname, dir_fd=comms_fd, follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return []
                if _stat.S_ISLNK(post.st_mode):
                    raise OSError(
                        f"refusing symlinked inbox path: {inbox_display}"
                    ) from e
                raise
            try:
                inbox = self._inbox_dir(sender, receiver)
                return sorted(
                    inbox / n for n in _os.listdir(inbox_fd)
                    if (
                        len(n) >= 5
                        and n[:4].isdigit()
                        and n[4] == "-"
                        and n.endswith(".md")
                    )
                )
            finally:
                _os.close(inbox_fd)
        finally:
            _os.close(comms_fd)

    def archive(self, path: Path) -> None:
        """Move a consumed message to the archive.

        NNNN sequences are unique only
        WITHIN a sender-to-receiver inbox. Two pairs (e.g. claude→codex
        and codex→claude) both have 0001-... and a flat archive dir
        would clobber the second on rename. Namespace the archive by
        the source inbox directory name so cross-direction collisions
        are impossible.

        BUG-186: source and destination are both opened via no-follow
        dir-fd chains; the rename uses ``src_dir_fd``/``dst_dir_fd`` so
        a swapped inbox or archive directory cannot redirect the
        operation through a symlink.
        """
        import os as _os
        # refuse to archive out of a symlinked source inbox.
        if path.parent is not None and path.parent.is_symlink():
            raise OSError(
                f"refusing to archive from symlinked inbox: {path.parent}"
            )
        # Source inbox dirname (e.g. "claude-to-codex") becomes the
        # archive subdir; keep filename intact for forensics.
        inbox_name = path.parent.name if path.parent is not None else ""
        if not inbox_name:
            # Fall back to the legacy path-based rename for paths without
            # a parent name; safe_io still ensures private dirs.
            a = self._archive_dir()
            _ensure_private_dir(self.peer_dir)
            _ensure_private_dir(self.peer_dir / "comms")
            _ensure_private_dir(a)
            target = a / path.name
            try:
                path.rename(target)
            except FileNotFoundError:
                pass
            return
        # Open src inbox and dst archive subdir under no-follow chains.
        try:
            src_fd = _open_private_nested_dir_fd_no_symlink(
                self.peer_dir, ("comms", inbox_name),
            )
        except FileNotFoundError:
            return
        try:
            dst_fd = _open_private_nested_dir_fd_no_symlink(
                self.peer_dir, ("comms", "archive", inbox_name),
            )
            try:
                target_name = path.name
                try:
                    target_st = _os.stat(
                        target_name, dir_fd=dst_fd, follow_symlinks=False,
                    )
                except FileNotFoundError:
                    target_st = None
                if target_st is not None:
                    n = 1
                    stem, dot, suffix = path.name.rpartition(".")
                    if not dot:
                        stem, suffix = path.name, ""
                    while True:
                        candidate = (
                            f"{stem}.{n}.{suffix}" if suffix
                            else f"{stem}.{n}"
                        )
                        try:
                            _os.stat(
                                candidate, dir_fd=dst_fd, follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            target_name = candidate
                            break
                        n += 1
                try:
                    _os.rename(
                        path.name, target_name,
                        src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                    )
                except FileNotFoundError:
                    pass
            finally:
                _os.close(dst_fd)
        finally:
            _os.close(src_fd)
