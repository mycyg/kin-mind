"""记忆目录的路径解析。全包唯一允许拼 `.memory` 路径的地方。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 被管理项目内的记忆根目录名
MEMORY_DIRNAME = ".memory"


@dataclass(frozen=True)
class MemoryPaths:
    """一个被管理项目的记忆目录布局；不可变，所有路径属性由 root 派生。"""

    root: Path

    @classmethod
    def for_project(cls, project_dir: Path) -> "MemoryPaths":
        """由项目根目录得到记忆路径（<project>/.memory），路径展开为绝对路径。"""
        return cls(root=Path(project_dir).expanduser().resolve() / MEMORY_DIRNAME)

    # ---- 目录 ----

    @property
    def project_dir(self) -> Path:
        """被管理项目的根目录，用于把绝对路径规约成项目内相对路径。"""
        return self.root.parent

    @property
    def events_dir(self) -> Path:
        """L0 事件目录：一事件一文件。"""
        return self.root / "events"

    @property
    def raw_dir(self) -> Path:
        """预留的原文目录；V0.1 不写（对话原文由宿主保存）。"""
        return self.root / "raw"

    @property
    def index_dir(self) -> Path:
        """L1 索引目录：全部可重建。"""
        return self.root / "index"

    @property
    def log_dir(self) -> Path:
        """护栏日志与各类水位文件所在目录。"""
        return self.root / "log"

    @property
    def archive_dir(self) -> Path:
        """归档区：纪元摘要与 frozen 事件的 tar 包（SPEC §3.19）。

        不进 ensure()：按需在冻结时创建，没归档过的项目不该多一个空目录。
        """
        return self.root / "archive"

    # ---- 文件 ----

    @property
    def working_set(self) -> Path:
        """工作集文件：会话启动时注入上下文的文本本体。"""
        return self.index_dir / "working-set.md"

    @property
    def project_index(self) -> Path:
        """全量单行索引文件。"""
        return self.index_dir / "project.md"

    @property
    def anchors(self) -> Path:
        """锚点倒排索引文件（JSON）。"""
        return self.index_dir / "anchors.json"

    @property
    def lessons(self) -> Path:
        """lesson 表文件，含 candidate/promoted/retired 状态。"""
        return self.index_dir / "lessons.md"

    @property
    def log(self) -> Path:
        """护栏日志文件。"""
        return self.log_dir / "eventmem.log"

    @property
    def config(self) -> Path:
        """可缺省的参数覆盖文件。"""
        return self.root / "config.yml"

    @property
    def archive_index(self) -> Path:
        """冷事件的归档索引：每行 `id | epoch | intent`（SPEC §3.19）。"""
        return self.index_dir / "archive-index.md"

    def event_file(self, event_id: str) -> Path:
        """某个事件的 L0 文件路径。"""
        return self.events_dir / f"{event_id}.md"

    def epoch_summary(self, epoch: str) -> Path:
        """某纪元的摘要文件（一段时代总结 ＋ 成员清单）。"""
        return self.archive_dir / f"epoch-{epoch}.md"

    def epoch_pack(self, epoch: str, seq: int = 1) -> Path:
        """某纪元的事件包；seq≥2 为续包 epoch-<epoch>-<seq>.tar.gz。"""
        suffix = "" if seq <= 1 else f"-{seq}"
        return self.archive_dir / f"epoch-{epoch}{suffix}.tar.gz"

    def epoch_packs(self, epoch: str) -> list[Path]:
        """某纪元已存在的全部包（首包与续包），按文件名排序。"""
        if not self.archive_dir.is_dir():
            return []
        first = self.epoch_pack(epoch)
        found = [p for p in self.archive_dir.glob(f"epoch-{epoch}-*.tar.gz") if p.is_file()]
        if first.is_file():
            found.append(first)
        return sorted(found, key=lambda p: p.name)

    def all_packs(self) -> list[Path]:
        """归档区里的全部事件包，按文件名排序。"""
        if not self.archive_dir.is_dir():
            return []
        return sorted((p for p in self.archive_dir.glob("epoch-*.tar.gz") if p.is_file()), key=lambda p: p.name)

    def thaw_marker(self, event_id: str) -> Path:
        """解冻时间戳文件：存在即该事件的年龄从此刻重算（SPEC §3.19）。"""
        return self.log_dir / f"thaw-{event_id}"

    def seen_file(self, session_id: str) -> Path:
        """同会话浮现去重集合的落盘位置。"""
        return self.log_dir / f"seen-{session_id}.txt"

    def extract_watermark(self, session_id: str) -> Path:
        """某会话 transcript 的抽取水位文件（值为已处理行数）。"""
        return self.log_dir / f"extract-watermark-{session_id}"

    @property
    def deep_watermark(self) -> Path:
        """上次深整理的水位文件，用于计算脏量。"""
        return self.log_dir / "deep-watermark"

    def ensure(self) -> None:
        """创建全部目录（幂等）。"""
        for d in (self.root, self.events_dir, self.raw_dir, self.index_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path | str) -> str:
        """把路径规约为项目内的 POSIX 相对路径；项目外的路径原样返回。"""
        p = Path(path)
        if not p.is_absolute():
            return p.as_posix()
        try:
            return p.resolve().relative_to(self.project_dir).as_posix()
        except ValueError:
            return p.as_posix()


def _fsync_directory(directory: Path) -> bool:
    """fsync 目录本身，让 rename 后的目录项落盘；平台/文件系统不支持时返回 False。"""
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
    """同目录临时文件 ＋ os.replace 写入，读方永远看不到半截文件。

    落盘口径与 adapters/atomic-json.mjs 的 syncDirectory 一致：文件内容 fsync 后才
    rename，rename 后再 fsync 父目录，让目录项本身也 durable。返回 True 才表示两步
    fsync 都成功；目录 fsync 在不支持的平台上会被拒绝（OSError，如 ENOTSUP/EINVAL），
    此时写入照常完成、rename 依旧原子，但返回 False——调用方只有看到 True 才能宣称
    完全落盘。
    """
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
