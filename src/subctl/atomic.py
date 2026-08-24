from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    """Write text atomically, preserving ownership and mode when replacing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_stat = path.stat() if path.exists() else None
    if mode is None and existing_stat is not None:
        mode = stat.S_IMODE(existing_stat.st_mode)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        explicit_owner = owner_uid is not None or owner_gid is not None
        if explicit_owner:
            if owner_uid is None or owner_gid is None:
                raise ValueError("owner_uid and owner_gid must be provided together")
            if not hasattr(os, "chown"):
                raise OSError("explicit file ownership is not supported on this platform")
            os.chown(tmp_path, owner_uid, owner_gid)
        elif existing_stat is not None and hasattr(os, "chown"):
            os.chown(tmp_path, existing_stat.st_uid, existing_stat.st_gid)
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
