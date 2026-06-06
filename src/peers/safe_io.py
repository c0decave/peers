"""Small no-follow file helpers for control-plane writes.

The peers substrate writes files that can be influenced by project code
(`.peers/run.lock`, `.peers/log/runs.jsonl`, state temp files, controller
logs). A pre-write `Path.is_symlink()` check is useful for diagnostics but
not enough against a same-user race. These helpers open the final path with
O_NOFOLLOW where the platform supports it, so an attacker cannot swap in a
symlink between the check and the write. They also refuse non-regular files
and pre-existing hardlinks before truncating or appending.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO, Sequence


_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700


def _check_existing_write_target(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"refusing to open symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"refusing to open non-regular file: {path}")
    if st.st_nlink != 1:
        raise OSError(f"refusing to open hard-linked file: {path}")


def _tighten_to_private(fd: int) -> None:
    """Drop group/other bits if any are set; preserve owner bits.

    BUG-106 defense: control-plane files (state, prompts, peer logs,
    runs.jsonl) must not be readable by another local user. The threat
    model in SPEC.md treats other local users as untrusted. We narrow
    on every open so a file pre-created at 0o644 by an older substrate
    version (or with a permissive umask) becomes private on first use.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        return
    if st.st_mode & 0o077:
        os.fchmod(fd, st.st_mode & ~0o077)


def _ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) with mode 0o700, refusing symlinks.

    The mkdir mode= is subject to umask, so we explicitly chmod after
    creation. If the directory already exists with broader perms we
    do NOT widen it back; we only narrow if g/o bits are set.
    """
    if path.is_symlink():
        raise OSError(f"refusing symlinked dir: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        lst = path.lstat()
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(f"refusing symlinked dir: {path}")
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"refusing non-directory: {path}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped dir: {path}")
        if st.st_mode & 0o077:
            try:
                os.fchmod(fd, st.st_mode & ~0o077)
            except OSError:
                pass
    finally:
        os.close(fd)


def _validate_single_path_component(name: str, what: str) -> None:
    if name in ("", ".", "..") or Path(name).name != name:
        raise ValueError(f"{what} must be a single path component: {name!r}")


def _open_dir_fd_no_symlink(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        lst = path.lstat()
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(f"refusing symlinked dir: {path}")
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"refusing non-directory: {path}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped dir: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _ensure_private_child_dir_fd(
    parent_fd: int, dirname: str, display_path: Path,
) -> int:
    _validate_single_path_component(dirname, "dirname")
    try:
        os.mkdir(dirname, _PRIVATE_DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    lst = os.stat(dirname, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(lst.st_mode):
        raise OSError(f"refusing symlinked dir: {display_path}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(dirname, flags, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"refusing non-directory: {display_path}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped dir: {display_path}")
        if st.st_mode & 0o077:
            try:
                os.fchmod(fd, st.st_mode & ~0o077)
            except OSError:
                pass
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_nested_dir_fd_no_symlink(
    root: Path, dirnames: Sequence[str],
) -> int:
    """Create/open nested private dirs without following any component.

    Returns a directory fd for ``root/dirnames...``. The caller owns it.
    """
    fds: list[int] = []
    try:
        root_fd = _open_dir_fd_no_symlink(root)
        fds.append(root_fd)
        parent_fd = root_fd
        display_path = root
        for dirname in dirnames:
            display_path = display_path / dirname
            child_fd = _ensure_private_child_dir_fd(
                parent_fd, dirname, display_path,
            )
            fds.append(child_fd)
            parent_fd = child_fd
        return os.dup(parent_fd)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _write_text_in_private_nested_dir_no_symlink(
    root: Path, dirnames: Sequence[str], filename: str, text: str,
) -> None:
    dir_fd = _open_private_nested_dir_fd_no_symlink(root, dirnames)
    fd = -1
    display_dir = root.joinpath(*dirnames)
    _validate_single_path_component(filename, "filename")
    display_path = display_dir / filename
    try:
        try:
            st_pre = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(st_pre.st_mode):
                raise OSError(f"refusing to open symlink: {display_path}")
            if not stat.S_ISREG(st_pre.st_mode):
                raise OSError(
                    f"refusing to open non-regular file: {display_path}"
                )
            if st_pre.st_nlink != 1:
                raise OSError(
                    f"refusing to open hard-linked file: {display_path}"
                )
        # BUG-120 (v15 internal testing): NO O_TRUNC here. A hardlink raced in
        # between the pre-stat and this open would otherwise be truncated
        # before the nlink check below rejects it, clobbering the victim.
        # Truncate only AFTER all the post-open checks pass (the same
        # delayed-truncate pattern open_text_no_symlink uses).
        flags = os.O_WRONLY | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(filename, flags, _PRIVATE_FILE_MODE, dir_fd=dir_fd)
        st = os.fstat(fd)
        lst = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(f"refusing to open symlink: {display_path}")
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing to open non-regular file: {display_path}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped file: {display_path}")
        if st.st_nlink != 1:
            raise OSError(f"refusing to open hard-linked file: {display_path}")
        _tighten_to_private(fd)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


def open_text_no_symlink(path: Path, mode: str = "w") -> IO[str]:
    """Open `path` for text writing/appending without following symlinks.

    Supports `"w"` and `"a"` modes, both UTF-8. The parent directory is not
    created here; callers keep ownership of directory creation and fsync.
    """
    if mode not in ("w", "a"):
        raise ValueError(f"unsupported no-follow text mode: {mode!r}")
    _check_existing_write_target(path)
    flags = os.O_WRONLY | os.O_CREAT
    if mode == "a":
        flags |= os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags, _PRIVATE_FILE_MODE)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError(f"refusing to open non-regular file: {path}")
    if st.st_nlink != 1:
        os.close(fd)
        raise OSError(f"refusing to open hard-linked file: {path}")
    _tighten_to_private(fd)
    try:
        if mode == "w":
            os.ftruncate(fd, 0)
        return os.fdopen(fd, mode, encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def open_text_read_no_symlink(path: Path) -> IO[str]:
    """Open `path` for UTF-8 text reading without following symlinks."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError(f"refusing to read non-regular file: {path}")
    if st.st_nlink != 1:
        os.close(fd)
        raise OSError(f"refusing to read hard-linked file: {path}")
    try:
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except Exception:
        os.close(fd)
        raise


def read_bytes_no_symlink(path: Path, max_bytes: int | None = None) -> bytes:
    """Read bytes from a regular, non-linked file."""
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing to read non-regular file: {path}")
        if st.st_nlink != 1:
            raise OSError(f"refusing to read hard-linked file: {path}")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read() if max_bytes is None else f.read(max_bytes)
    finally:
        if fd >= 0:
            os.close(fd)


def read_text_no_symlink(path: Path, max_bytes: int | None = None) -> str:
    """Read UTF-8 text from a regular, non-linked file.

    When `max_bytes` is provided, reads at most that many bytes from the
    already-validated file descriptor.
    """
    data = read_bytes_no_symlink(path, max_bytes=max_bytes)
    return data.decode("utf-8", errors="replace")


def write_text_no_symlink(path: Path, text: str) -> None:
    with open_text_no_symlink(path, "w") as f:
        f.write(text)


def append_text_no_symlink(path: Path, text: str) -> None:
    with open_text_no_symlink(path, "a") as f:
        f.write(text)


def open_text_in_dir_no_symlink(
    parent: Path, filename: str, mode: str = "a"
) -> IO[str]:
    """Open ``parent/filename`` without following parent or leaf symlinks.

    ``open_text_no_symlink(parent / filename, "a")`` protects the final path
    but the kernel still resolves every parent component first. This helper
    opens the directory itself with O_NOFOLLOW and then opens the file via
    ``dir_fd`` so a late swap of e.g. ``.peers/log`` or controller ``logs``
    to a symlink cannot redirect the write.
    """
    if mode not in ("w", "a"):
        raise ValueError(f"unsupported no-follow text mode: {mode!r}")
    if Path(filename).name != filename:
        raise ValueError(f"filename must be a single path component: {filename!r}")
    dir_flags = os.O_RDONLY
    dir_flags |= getattr(os, "O_DIRECTORY", 0)
    dir_flags |= getattr(os, "O_NOFOLLOW", 0)
    dir_flags |= getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(str(parent), dir_flags)
    fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT
        if mode == "a":
            flags |= os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(filename, flags, _PRIVATE_FILE_MODE, dir_fd=dir_fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing to open non-regular file: {parent / filename}")
        if st.st_nlink != 1:
            raise OSError(f"refusing to open hard-linked file: {parent / filename}")
        _tighten_to_private(fd)
        if mode == "w":
            os.ftruncate(fd, 0)
        f = os.fdopen(fd, mode, encoding="utf-8")
        fd = -1
        return f
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


def append_text_in_dir_no_symlink(parent: Path, filename: str, text: str) -> None:
    """Append to ``parent/filename`` without following parent or leaf symlinks."""
    with open_text_in_dir_no_symlink(parent, filename, "a") as f:
        f.write(text)


def atomic_write_text_in_dir_no_symlink(path: Path, text: str) -> None:
    """Atomically and durably write ``text`` to ``path`` without following a
    symlinked PARENT or leaf.

    Writes to ``<path>.tmp`` opened relative to a no-follow (dev/ino-
    rechecked) parent ``dir_fd``, fsyncs it, ``os.replace()``s it into place
    using ``src_dir_fd``/``dst_dir_fd``, then fsyncs the directory. Three
    guarantees, all relative to the verified parent fd:

    - BUG-118: a symlinked/swapped parent is refused (``_open_dir_fd_no_symlink``)
      before any state bytes are written — the leaf no-follow guard alone
      cannot stop the kernel resolving a symlinked parent.
    - BUG-119: the temp open carries no ``O_TRUNC``; truncation is delayed
      until after the nlink/swap checks, so a hardlink raced onto the temp
      between the pre-stat and the open is rejected, not clobbered.
    - BUG-114: the durability dir-fsync uses the same no-follow ``dir_fd`` and
      cannot follow a symlinked parent into an attacker directory.

    The parent directory must already exist. ``path``'s basename must be a
    single path component.
    """
    parent = path.parent
    name = path.name
    _validate_single_path_component(name, "filename")
    tmp_name = name + ".tmp"
    display_tmp = parent / tmp_name
    dir_fd = _open_dir_fd_no_symlink(parent)
    fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(tmp_name, flags, _PRIVATE_FILE_MODE, dir_fd=dir_fd)
        st = os.fstat(fd)
        lst = os.stat(tmp_name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(lst.st_mode):
            raise OSError(f"refusing to open symlink: {display_tmp}")
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"refusing to open non-regular file: {display_tmp}")
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise OSError(f"refusing swapped file: {display_tmp}")
        if st.st_nlink != 1:
            raise OSError(f"refusing to open hard-linked file: {display_tmp}")
        _tighten_to_private(fd)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        # Durability: the rename's metadata only hits disk after the parent
        # dir is fsync'd. Some filesystems (FAT, NFS) don't support it — skip
        # rather than fail, same as the leaf write above.
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)
