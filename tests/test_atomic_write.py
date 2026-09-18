"""atomic_write 的落盘口径：文件 fsync → rename → 父目录 fsync。

父目录 fsync 是尽力而为：不支持（ENOTSUP/EINVAL 等 OSError）时写入照常完成，
但返回值必须是 False——任何"已完全落盘"的宣称只允许建立在 True 之上。
"""

from __future__ import annotations

import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from eventmem import paths as paths_mod
from eventmem.paths import atomic_write


def _tmp_leftovers(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.suffix == ".tmp"]


def test_write_is_durable_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "state.json"
    assert atomic_write(target, '{"a": 1}\n') is True
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert _tmp_leftovers(target.parent) == []


def test_temp_file_write_failure_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        raise OSError(errno.ENOSPC, "simulated write-path failure")

    monkeypatch.setattr(paths_mod.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        atomic_write(target, "data")
    assert not target.exists()
    assert _tmp_leftovers(tmp_path) == []
    monkeypatch.setattr(paths_mod.os, "fsync", real_fsync)


def test_replace_failure_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.json"

    def failing_replace(src: str, dst: str) -> None:
        raise OSError(errno.EACCES, "simulated rename failure")

    monkeypatch.setattr(paths_mod.os, "replace", failing_replace)
    with pytest.raises(OSError):
        atomic_write(target, "data")
    assert not target.exists()
    assert _tmp_leftovers(tmp_path) == []


def test_directory_fsync_unsupported_degrades_to_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录本身打不开/不让 fsync（ENOTSUP）：写入完成，但不得宣称完全落盘。"""
    target = tmp_path / "state.json"
    real_open = os.open

    def refusing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(path).is_dir():
            raise OSError(errno.ENOTSUP, "directory fsync unsupported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(paths_mod.os, "open", refusing_open)
    assert atomic_write(target, "data") is False
    assert target.read_text(encoding="utf-8") == "data"
    assert _tmp_leftovers(tmp_path) == []


def test_directory_fsync_failure_degrades_to_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录打开了但 fsync 被拒（EINVAL）：同样不得宣称完全落盘。"""
    target = tmp_path / "state.json"
    real_open, real_fsync = os.open, os.fsync
    directory_fds: list[int] = []

    def tracking_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fds.append(fd)
        return fd

    def selective_fsync(fd: int) -> None:
        if fd in directory_fds:
            raise OSError(errno.EINVAL, "simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(paths_mod.os, "open", tracking_open)
    monkeypatch.setattr(paths_mod.os, "fsync", selective_fsync)
    assert atomic_write(target, "data") is False
    assert target.read_text(encoding="utf-8") == "data"
    assert _tmp_leftovers(tmp_path) == []


def test_concurrent_writes_share_a_directory_without_collision(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    names = [f"file-{i}.txt" for i in range(16)]

    def write_one(name: str) -> bool:
        return atomic_write(directory / name, f"body of {name}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write_one, names))

    assert all(results)
    for name in names:
        assert (directory / name).read_text(encoding="utf-8") == f"body of {name}\n"
    assert _tmp_leftovers(directory) == []
