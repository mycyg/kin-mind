"""The one file-writing helper the service keeps from the removed `.memory` stack (E1-05)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _fsync_directory(directory: Path) -> bool:
    """fsync the directory itself so the entry a rename made is on disk; False where the
    platform or file system does not support it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def atomic_write(path: Path, text: str) -> bool:
    """Write through a temporary file in the same directory and os.replace, so a reader never
    sees half a file. The content is fsynced before the rename and the directory after it, as
    adapters/atomic-json.mjs does. True only when both fsyncs succeeded; where the directory
    fsync is refused the write still completes and the rename is still atomic, and the answer
    is False, so only True may be reported as fully durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return _fsync_directory(path.parent)
