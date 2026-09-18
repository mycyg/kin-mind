"""eventmem 命令行（SPEC §3.10）。

`--project` 在每个子命令上都可用（默认当前目录），因为 hooks 层 spawn 出的
`eventmem extract` / `eventmem consolidate` 把 `--project` 放在子命令自己的参数
之后（例如 `extract --transcript P --session S --project DIR`）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from eventmem.consolidate import deep, dirty_count, light
from eventmem.extract import extract_events
from eventmem.index import (
    ArchiveRow,
    Budget,
    epoch_end,
    is_epoch,
    load_archive_index,
    rebuild_all,
    remove_archive_rows,
)
from eventmem.llm import ConfigError, LLMClient, LLMConfig
from eventmem.paths import MemoryPaths, atomic_write
from eventmem.recall import search as recall_search
from eventmem.recall import search_archive
from eventmem.schema import Event, SchemaError, id_to_datetime, to_markdown
from eventmem.store import EventNotFound, Store

__all__ = ["main", "build_parser"]

_CONFIG_TEMPLATE = """\
# eventmem 配置：缺省即使用默认值，按需取消注释覆盖。

# 工作集注入的 token 预算上限（估算：字符数 // 3）
# working_set_tokens: 1500

# 单次线索浮现返回的最多事件数
# surface_k: 3

# open 事件超过多少天视为 stale（深整理时在工作集里标注）
# stale_days: 14

# 深整理触发阈值：距上次深整理新增的事件数达到此值才跑深整理
# deep_threshold: 30

# 分级遗忘（archive）总开关，关掉则事件永远留在活跃层
# archive: true

# 冷却门槛：年龄超过多少天、且零引用零命中、显著性低于 salience_floor 才逐出索引
# cold_days: 90

# 冻结门槛：冷却后年龄再超过多少天，按季度打包移出 events/
# frozen_days: 365

# 冷却的显著性上限（低于它才可能被冷却）
# salience_floor: 0.2
"""


def _load_env(project_dir: Path) -> None:
    """加载 EVENTMEM_* 环境变量：先读 <project>/.env，再读 ~/.claude/eventmem.env。

    与 hooks/__init__.py 里的同名函数逻辑一致（两处独立实现，避免 cli 依赖 hooks
    子包）：跳过空行与 # 注释，KEY=VALUE 按首个 = 切分，值两侧的匹配引号剥掉；
    已存在的环境变量、以及先读到的文件里已设的值，都不被覆盖。
    """
    for path in (project_dir / ".env", Path.home() / ".claude" / "eventmem.env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _paths(args: argparse.Namespace) -> MemoryPaths:
    project_dir = Path(args.project) if getattr(args, "project", None) else Path.cwd()
    return MemoryPaths.for_project(project_dir)


def _load_budget(paths: MemoryPaths) -> Budget:
    """从 config.yml 覆盖 Budget 字段；文件缺失、损坏或字段缺失都退回默认值。

    deep_threshold 不在 Budget 里：consolidate.deep() 自己直接读 config.yml，
    这里不需要重复处理。
    """
    data: dict[str, Any] = {}
    if paths.config.is_file():
        try:
            loaded = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # noqa: BLE001 —— 配置损坏不阻断命令
            data = {}
    defaults = Budget()

    def _int(key: str, fallback: int) -> int:
        value = data.get(key)
        if value is None:
            return fallback
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    return Budget(
        working_set_tokens=_int("working_set_tokens", defaults.working_set_tokens),
        surface_k=_int("surface_k", defaults.surface_k),
        stale_days=_int("stale_days", defaults.stale_days),
    )


def _build_client() -> LLMClient | None:
    """构造 LLMClient；env 缺 key 时降级 client=None 并打印提示，不抛异常。"""
    try:
        return LLMClient(LLMConfig.from_env())
    except ConfigError as exc:
        print(f"[eventmem] LLM 不可用，降级为纯规则模式：{exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 埋点读取（SPEC §3.13）
#
# stats/log 两个命令只读，且都不依赖索引存在；salience.json／prefetch-outcome.jsonl
# 这类派生文件在 v0.2 的相邻增量里才会开始写入，运行时可能尚不存在——本节的读取
# 函数一律把「文件缺失」「JSON 损坏」「字段缺失」都当成「无证据」处理，返回 None
# 而不是抛异常，调用方据此显示 n/a（优雅降级是硬要求，见任务说明）。


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """宽容读取 jsonl 文件：整份缺失或某一行损坏都不报错，跳过继续。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _glob_jsonl(paths: MemoryPaths, pattern: str) -> list[dict[str, Any]]:
    """读 log/ 下某个通配符匹配到的全部 jsonl 文件，跨会话合并成一个列表。"""
    if not paths.log_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(paths.log_dir.glob(pattern)):
        records.extend(_read_jsonl(path))
    return records


def _as_int(value: Any) -> int:
    """宽容取整数；非数字取值一律按 0 处理，不让脏数据打断统计。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _adoption_evidence(paths: MemoryPaths) -> tuple[int, int] | None:
    """从 index/salience.json 的 evidence 汇总 hits/ignored；文件不存在或损坏返回 None。

    salience.json 由 consolidate.deep 派生写出（SPEC §3.11），本命令只读；在该增量
    落地前文件不存在是预期状态，不是错误。
    """
    try:
        raw = (paths.index_dir / "salience.json").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    hits = 0
    ignored = 0
    for record in data.values():
        if not isinstance(record, dict):
            continue
        evidence = record.get("evidence")
        if not isinstance(evidence, dict):
            continue
        hits += _as_int(evidence.get("hits"))
        ignored += _as_int(evidence.get("ignored"))
    return (hits, ignored)


def _prefetch_totals(paths: MemoryPaths) -> tuple[int, int] | None:
    """汇总 log/prefetch-outcome.jsonl 的 predicted/hit；文件不存在或为空返回 None。"""
    records = _read_jsonl(paths.log_dir / "prefetch-outcome.jsonl")
    if not records:
        return None
    predicted = sum(_as_int(r.get("predicted")) for r in records)
    hit = sum(_as_int(r.get("hit")) for r in records)
    return (predicted, hit)


def _repeated_pitfalls(events: list[Event]) -> int:
    """同一 error_sig 关联 ≥2 个 fix 事件的组数，直接扫 L0 得出（SPEC §3.13）。"""
    sig_to_events: dict[str, set[str]] = {}
    for e in events:
        if e.kind != "fix":
            continue
        for sig in e.anchors.error_sigs:
            sig_to_events.setdefault(sig, set()).add(e.id)
    return sum(1 for ids in sig_to_events.values() if len(ids) >= 2)


def _claude_md_suggestions_path(paths: MemoryPaths) -> Path:
    return paths.index_dir / "claude-md-suggestions.md"


def _claude_md_suggestions_count(paths: MemoryPaths) -> int:
    """粗略计数建议条数（SPEC §3.15）：按二级标题分块；无标题则按空行分段落。

    该文件由 consolidate.deep 在 lesson 晋升时生成／更新，本命令只读；文件不存在
    或全空白都返回 0，不视为错误。SPEC 只规定「每条一个区块」，未定死分隔符，
    因此这里采用 markdown 里最常见的两种分块方式，覆盖尚未固定的写出格式。
    """
    try:
        text = _claude_md_suggestions_path(paths).read_text(encoding="utf-8")
    except OSError:
        return 0
    if not text.strip():
        return 0
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    if headings:
        return len(headings)
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return len(blocks)


def _rate_text(numerator: int | None, denominator: int | None) -> str:
    """把 (分子, 分母) 格式化成百分比＋原始计数；分母缺失或为零时显示 n/a。"""
    if numerator is None or denominator is None or denominator <= 0:
        return "n/a"
    return f"{numerator / denominator:.1%} ({numerator}/{denominator})"


def _format_counter(counter: "Counter[str]") -> str:
    if not counter:
        return "(无)"
    return " ".join(f"{key}={value}" for key, value in sorted(counter.items()))


# ---------------------------------------------------------------- 分级遗忘（SPEC §3.19）
#
# thaw／purge 只动 archive/ 与 index/archive-index.md：purge 永不触碰 events/ 散文件，
# thaw 只往 events/ 放回包内原样的字节。

_PACK_SUFFIX = ".tar.gz"
_PACK_PREFIX = "epoch-"
_PACK_NAME_RE = re.compile(r"^(?P<epoch>\d{4}-Q[1-4])(?:-(?P<seq>\d+))?$")


def _pack_epoch(pack: Path) -> str:
    """从包文件名取纪元；不合命名约定时返回整个 stem，purge 据此保守跳过。"""
    stem = pack.name[len(_PACK_PREFIX) : -len(_PACK_SUFFIX)]
    match = _PACK_NAME_RE.match(stem)
    return match.group("epoch") if match else stem


def _pack_members(pack: Path) -> list[str]:
    """包内成员事件 id；包损坏返回空列表（只读操作，不因此报错）。"""
    try:
        with tarfile.open(pack, "r:gz") as archive:
            return sorted(Path(name).stem for name in archive.getnames() if name.endswith(".md"))
    except (OSError, tarfile.TarError):
        return []


def _member_bytes(pack: Path, event_id: str) -> bytes | None:
    """包内某个成员的原始字节；不存在或包损坏返回 None。"""
    try:
        with tarfile.open(pack, "r:gz") as archive:
            handle = archive.extractfile(f"{event_id}.md")
            return handle.read() if handle is not None else None
    except (OSError, tarfile.TarError, KeyError):
        return None


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """按字节原子写回事件文件：解冻要逐字节还原 L0，不经过任何文本再编码。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _frozen_row(paths: MemoryPaths, rows: dict[str, ArchiveRow], event_id: str) -> ArchiveRow | None:
    """事件是否处于 frozen（在归档索引里且散文件已不在）；是则返回它的归档行。"""
    row = rows.get(event_id)
    if row is None or paths.event_file(event_id).is_file():
        return None
    return row


def _thaw_one(paths: MemoryPaths, event_id: str, now: datetime) -> str:
    """解冻一个事件，返回结果说明。已在 events/ 的只需摘掉归档行。"""
    target = paths.event_file(event_id)
    if not target.is_file():
        data: bytes | None = None
        for pack in paths.all_packs():
            data = _member_bytes(pack, event_id)
            if data is not None:
                break
        if data is None:
            return "包内未找到"
        try:
            _write_bytes_atomic(target, data)
        except OSError as exc:
            return f"写回失败 {exc}"
    try:  # 解冻后按解冻时间重新计龄（SPEC §3.19），否则下一轮深整理立刻把它冻回去
        atomic_write(paths.thaw_marker(event_id), _naive_stamp(now) + "\n")
    except OSError:
        pass
    return "已解冻"


def _naive_stamp(now: datetime) -> str:
    return now.replace(tzinfo=None).isoformat(timespec="seconds")


def _archive_totals(paths: MemoryPaths, event_ids: set[str]) -> dict[str, Any]:
    """hot/cold/frozen 计数、包数与总体积、活跃层占比（stats 用）。"""
    rows = load_archive_index(paths)
    hot = len(event_ids - set(rows))
    cold = len(event_ids & set(rows))
    frozen = sum(1 for event_id in rows if event_id not in event_ids)
    packs = paths.all_packs()
    size = 0
    for pack in packs:
        try:
            size += pack.stat().st_size
        except OSError:
            continue
    total = hot + cold + frozen
    return {
        "hot": hot,
        "cold": cold,
        "frozen": frozen,
        "packs": len(packs),
        "bytes": size,
        "active_ratio": (hot / total) if total else None,
        "total": total,
    }


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


# ---------------------------------------------------------------- 子命令


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = Store(paths)
    events = list(store.iter_events())
    open_count = sum(1 for e in events if e.status == "open")

    print(f"事件总数: {len(events)}")
    print(f"open 数: {open_count}")
    print(f"脏量（距上次深整理的新增事件数）: {dirty_count(paths)}")
    print("索引文件:")
    for label, path in (
        ("working-set", paths.working_set),
        ("project", paths.project_index),
        ("anchors", paths.anchors),
        ("lessons", paths.lessons),
    ):
        if path.is_file():
            mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            print(f"  {label}: {mtime}")
        else:
            print(f"  {label}: 不存在")

    suggestions = _claude_md_suggestions_count(paths)
    if suggestions:
        print(f"未读 CLAUDE.md 建议: {suggestions} 条（见 {_claude_md_suggestions_path(paths)}）")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """默认只搜活跃层；--all 附带搜归档索引与纪元摘要（不解包，SPEC §3.19）。"""
    paths = _paths(args)
    store = Store(paths)
    hits = recall_search(args.query, store, paths, top=args.top)
    archived = search_archive(args.query, paths, top=args.top) if args.all else []
    if not hits and not archived:
        print("(无匹配)")
        return 0
    for hit in hits:
        print(hit.line)
    for hit in archived:
        print(hit.line)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = Store(paths)
    rows = load_archive_index(paths)
    try:
        event = store.read(args.id)
    except (EventNotFound, SchemaError) as exc:
        frozen = _frozen_row(paths, rows, args.id)
        if frozen is not None:  # 命中 frozen id：给纪元与解冻指令，而不是「不存在」
            print(f"[eventmem] 事件 {args.id} 已归档于 {frozen.epoch}：{frozen.intent}")
            print(f"[eventmem] 原文在归档包内，`eventmem thaw {args.id}` 可解冻后再读")
            return 1
        print(f"事件不存在或已损坏：{args.id}（{exc}）", file=sys.stderr)
        return 1
    print(to_markdown(event), end="")
    # 链接完整性：指向 frozen 事件的链在这里提示，store 不参与（SPEC §3.19）
    for label, target in (("superseded_by", event.superseded_by), ("parent", event.parent)):
        frozen = _frozen_row(paths, rows, str(target)) if target else None
        if frozen is not None:
            print(f"[eventmem] {label} {target} 已归档于 {frozen.epoch}，`eventmem thaw {target}` 可解冻")
    return 0


def cmd_thaw(args: argparse.Namespace) -> int:
    """解冻一个事件或整个纪元：包内原样写回 events/，摘掉归档行，年龄重新计起。"""
    paths = _paths(args)
    target = str(args.target).strip()
    rows = load_archive_index(paths)
    now = datetime.now()

    if is_epoch(target):
        ids = sorted({event_id for event_id, row in rows.items() if row.epoch == target})
        for pack in paths.epoch_packs(target):  # 归档行被手工删过时以包为准
            ids = sorted(set(ids) | set(_pack_members(pack)))
        if not ids:
            print(f"纪元 {target} 下没有可解冻的事件", file=sys.stderr)
            return 1
    else:
        ids = [target]
        if target not in rows:
            if paths.event_file(target).is_file():
                print(f"{target} 已在活跃层，无需解冻")
                return 0
            if not any(_member_bytes(pack, target) is not None for pack in paths.all_packs()):
                print(f"{target} 既不在归档索引里，也不在任何归档包内", file=sys.stderr)
                return 1

    thawed: list[str] = []
    for event_id in ids:
        result = _thaw_one(paths, event_id, now)
        print(f"  {event_id}: {result}")
        if result == "已解冻":
            thawed.append(event_id)
    if thawed:
        remove_archive_rows(paths, thawed)
    print(f"已解冻 {len(thawed)}/{len(ids)}；下次 rebuild／整理会自动收编进索引")
    return 0 if thawed else 1


def cmd_purge(args: argparse.Namespace) -> int:
    """删除指定日期之前的 frozen 包与对应摘要；默认 dry-run，--yes 才执行。"""
    paths = _paths(args)
    try:
        before = datetime.strptime(str(args.before).strip(), "%Y-%m-%d")
    except ValueError:
        print(f"--before 需要 YYYY-MM-DD 形态的日期，收到 {args.before}", file=sys.stderr)
        return 2

    groups: dict[str, list[Path]] = {}
    for pack in paths.all_packs():
        epoch = _pack_epoch(pack)
        end = epoch_end(epoch)
        if end is None or end >= before:  # 纪元未整季落在 before 之前就不动它
            continue
        groups.setdefault(epoch, []).append(pack)
    if not groups:
        print(f"{before.date()} 之前没有已冻结的纪元")
        return 0

    total_bytes = 0
    total_members = 0
    plan: list[tuple[str, list[Path], Path | None, list[str]]] = []
    for epoch in sorted(groups):
        packs = sorted(groups[epoch])
        members = sorted({mid for pack in packs for mid in _pack_members(pack)})
        summary = paths.epoch_summary(epoch)
        size = 0
        for pack in packs:
            try:
                size += pack.stat().st_size
            except OSError:
                continue
        total_bytes += size
        total_members += len(members)
        plan.append((epoch, packs, summary if summary.is_file() else None, members))
        print(f"{epoch}: 包 {len(packs)} 个 {_human_bytes(size)}，成员 {len(members)}")
        for pack in packs:
            print(f"  {pack}")
        if summary.is_file():
            print(f"  {summary}")
    print(f"合计 {len(plan)} 个纪元、{total_members} 个事件、{_human_bytes(total_bytes)}")

    if args.dry_run or not args.yes:  # 显式 --dry-run 压过 --yes：删除只在无歧义时发生
        print("dry-run：以上内容未被删除；确认无误后加 --yes 执行")
        return 0

    removed_rows = 0
    for epoch, packs, summary, members in plan:
        for pack in packs:
            try:
                pack.unlink()
            except OSError as exc:
                print(f"删除失败 {pack}：{exc}", file=sys.stderr)
        if summary is not None:
            try:
                summary.unlink()
            except OSError as exc:
                print(f"删除失败 {summary}：{exc}", file=sys.stderr)
        # 归档行一并摘掉：留着会指向一个再也 thaw 不出来的 id
        removed_rows += remove_archive_rows(paths, members)
    print(f"已删除 {len(plan)} 个纪元的包与摘要，归档索引移除 {removed_rows} 行；events/ 未被触碰")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = Store(paths)
    try:
        event = store.read(args.id)
    except (EventNotFound, SchemaError) as exc:
        print(f"事件不存在或已损坏：{args.id}（{exc}）", file=sys.stderr)
        return 1
    if not event.anchors.dialog:
        print("(无 dialog 指针)")
        return 0
    for pointer in event.anchors.dialog:
        print(pointer)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = Store(paths)
    client = _build_client()
    try:
        created = extract_events(
            Path(args.transcript), store, client, args.session, datetime.now()
        )
        print(f"新增事件: {len(created)}")
        for event_id in created:
            print(f"  {event_id}")
        if getattr(args, "then_light", False):
            # session_end 的串联通道：抽取（含 LLM 补漏层）完成后同进程跑整理，
            # 避免两个 detached 进程竞态、也避免机械 flush 抢先推水位让 LLM 层空转
            budget = _load_budget(paths)
            light(store, paths, budget, client, datetime.now())
            if dirty_count(paths) > 0:
                deep(store, paths, budget, client, datetime.now())
    finally:
        if client is not None:
            client.close()
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    if not (args.light or args.deep or args.deep_if_dirty):
        print("consolidate 需要至少一个 --light/--deep/--deep-if-dirty", file=sys.stderr)
        return 2

    paths = _paths(args)
    store = Store(paths)
    budget = _load_budget(paths)
    now = datetime.now()
    client = _build_client()
    try:
        if args.light:
            light(store, paths, budget, client, now)
            print("轻整理完成")
        if args.deep:
            deep(store, paths, budget, client, now)
            print("深整理完成（脏量未达阈值时为空操作）")
        elif args.deep_if_dirty:
            deep(store, paths, budget, client, now)
            print("深整理尝试完成（脏量未达阈值时为空操作）")
    finally:
        if client is not None:
            client.close()
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = Store(paths)
    budget = _load_budget(paths)
    rebuild_all(store, paths, budget, datetime.now())
    print("索引已重建")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    paths.ensure()
    if paths.config.exists():
        print(f"{paths.config} 已存在，跳过")
    else:
        atomic_write(paths.config, _CONFIG_TEMPLATE)
        print(f"已创建 {paths.config}")
    print(f"记忆目录就绪：{paths.root}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """事件统计与评估指标（SPEC §3.13）：kind/status 分布、浮现采纳率、注入量、
    重复踩坑数、预取命中率。全部只读，salience.json／prefetch-outcome.jsonl 缺失
    时对应指标降级为 n/a，不报错。
    """
    paths = _paths(args)
    store = Store(paths)
    events = list(store.iter_events())

    by_kind = Counter(e.kind for e in events)
    by_status = Counter(e.status for e in events)
    surfaced_total = len(_glob_jsonl(paths, "surfaced-*.jsonl*"))  # 含深整理处理后的 .done 文件
    injected_chars = sum(_as_int(r.get("chars")) for r in _glob_jsonl(paths, "injected-*.jsonl"))
    repeated = _repeated_pitfalls(events)
    suggestions = _claude_md_suggestions_count(paths)

    adoption = _adoption_evidence(paths)  # (hits, ignored) 或 None
    adoption_num = adoption[0] if adoption is not None else None
    adoption_den = (adoption[0] + adoption[1]) if adoption is not None else None
    adoption_rate = adoption_num / adoption_den if adoption_den else None

    prefetch = _prefetch_totals(paths)  # (predicted, hit) 或 None
    prefetch_den = prefetch[0] if prefetch is not None else None
    prefetch_num = prefetch[1] if prefetch is not None else None
    prefetch_rate = prefetch_num / prefetch_den if prefetch_den else None

    archive = _archive_totals(paths, {e.id for e in events})  # 分级遗忘各层（SPEC §3.19）

    if args.json:
        payload: dict[str, Any] = {
            "events_total": len(events),
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
            "surfaced_total": surfaced_total,
            "adoption_hits": adoption_num,
            "adoption_ignored": (adoption[1] if adoption is not None else None),
            "adoption_rate": round(adoption_rate, 4) if adoption_rate is not None else None,
            "injected_chars_total": injected_chars,
            "repeated_pitfalls": repeated,
            "prefetch_predicted": prefetch_den,
            "prefetch_hit": prefetch_num,
            "prefetch_hit_rate": round(prefetch_rate, 4) if prefetch_rate is not None else None,
            "claude_md_suggestions_pending": suggestions,
            "hot_events": archive["hot"],
            "cold_events": archive["cold"],
            "frozen_events": archive["frozen"],
            "events_all_layers": archive["total"],
            "archive_packs": archive["packs"],
            "archive_bytes": archive["bytes"],
            "active_ratio": (
                round(archive["active_ratio"], 4) if archive["active_ratio"] is not None else None
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"事件总数: {len(events)}")
    print(f"  kind 分布: {_format_counter(by_kind)}")
    print(f"  status 分布: {_format_counter(by_status)}")
    print(f"浮现次数: {surfaced_total}")
    print(f"采纳率: {_rate_text(adoption_num, adoption_den)}")
    print(f"注入字符量合计: {injected_chars}")
    print(f"重复踩坑数: {repeated}")
    print(f"预取命中率: {_rate_text(prefetch_num, prefetch_den)}")
    print(f"分级遗忘: hot={archive['hot']} cold={archive['cold']} frozen={archive['frozen']}")
    print(f"归档包: {archive['packs']} 个 {_human_bytes(archive['bytes'])}")
    print(f"活跃层占比: {_rate_text(archive['hot'], archive['total'])}")
    if suggestions:
        print(f"提示: 有 {suggestions} 条待处理的 CLAUDE.md 晋升建议，见 {_claude_md_suggestions_path(paths)}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """事件时间线（SPEC §3.13）：纯读 L0，不依赖任何索引文件存在。"""
    paths = _paths(args)
    store = Store(paths)
    all_events = list(store.iter_events())  # 升序；--tree 的缩进深度要在全量里算，避免过滤后断链
    by_id = {e.id: e for e in all_events}

    events = all_events
    if args.kind:
        events = [e for e in events if e.kind == args.kind]
    if args.since is not None:
        cutoff = datetime.now() - timedelta(days=args.since)
        events = [e for e in events if _before_cutoff(e, cutoff)]

    if not events:
        print("(无匹配事件)")
        return 0

    for e in events:
        indent = "  " * _event_depth(e, by_id) if args.tree else ""
        print(f"{indent}{e.id} {e.kind} {e.status} {_truncate(e.intent, 60)}")
    return 0


def _before_cutoff(e: Event, cutoff: datetime) -> bool:
    """事件 id 时间戳是否不早于 cutoff；id 无法解析时间戳时保守排除（宁漏勿胀）。"""
    moment = id_to_datetime(e.id)
    return moment is not None and moment >= cutoff


def _event_depth(event: Event, by_id: dict[str, Event]) -> int:
    """沿 parent 链计算 --tree 的缩进深度；环引用或异常深的链条在 100 步内截止兜底。"""
    depth = 0
    visited = {event.id}
    current = event
    while current.parent and current.parent in by_id and current.parent not in visited:
        visited.add(current.parent)
        current = by_id[current.parent]
        depth += 1
        if depth > 100:
            break
    return depth


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


_DISPATCH = {
    "status": cmd_status,
    "search": cmd_search,
    "read": cmd_read,
    "trace": cmd_trace,
    "extract": cmd_extract,
    "consolidate": cmd_consolidate,
    "rebuild": cmd_rebuild,
    "init": cmd_init,
    "stats": cmd_stats,
    "log": cmd_log,
    "thaw": cmd_thaw,
    "purge": cmd_purge,
}


# ---------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    # default=SUPPRESS：--project 同时挂在顶层解析器与每个子解析器上（好让它既能写
    # 在子命令前也能写在子命令后）。若用普通 default=None，未显式传参的那一层会用
    # 自己的默认值覆盖另一层已经解析出的值——SUPPRESS 让"没传"这件事完全不写入
    # 命名空间，两层谁真正解析到就是谁的值生效。
    project_parent = argparse.ArgumentParser(add_help=False)
    project_parent.add_argument(
        "--project", default=argparse.SUPPRESS, help="项目根目录，默认当前目录"
    )

    parser = argparse.ArgumentParser(
        prog="eventmem",
        description="基于事件闭环的 agent 记忆系统：不可变存储、可重建索引、线索触发的联想召回",
        parents=[project_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "status", parents=[project_parent], help="事件总数、open 数、脏量、索引文件年龄"
    )

    p_search = sub.add_parser(
        "search", parents=[project_parent], help="BM25 兜底检索，返回 outcome 单行"
    )
    p_search.add_argument("query")
    p_search.add_argument("--top", type=int, default=10)
    p_search.add_argument(
        "--all", action="store_true", help="附带搜归档索引与纪元摘要（不解包），结果标 [archived]"
    )

    p_read = sub.add_parser("read", parents=[project_parent], help="打印事件全文")
    p_read.add_argument("id")

    p_trace = sub.add_parser(
        "trace", parents=[project_parent], help="打印事件的 anchors.dialog 指针"
    )
    p_trace.add_argument("id")

    p_extract = sub.add_parser(
        "extract", parents=[project_parent], help="从 transcript 抽取事件"
    )
    p_extract.add_argument("--transcript", required=True)
    p_extract.add_argument("--session", required=True)
    p_extract.add_argument(
        "--then-light",
        action="store_true",
        dest="then_light",
        help="抽取后同进程跑轻整理（脏量达标时接深整理）；session_end hook 的串联通道",
    )

    p_consolidate = sub.add_parser(
        "consolidate", parents=[project_parent], help="轻整理／深整理"
    )
    p_consolidate.add_argument("--light", action="store_true", help="补 outcome、规则闭合、重建索引")
    p_consolidate.add_argument("--deep", action="store_true", help="lesson 蒸馏、晋升/退休（内部仍受脏量阈值门控）")
    p_consolidate.add_argument(
        "--deep-if-dirty", action="store_true", help="与 --deep 等价：脏量达标才真正执行"
    )

    sub.add_parser("rebuild", parents=[project_parent], help="重建全部 L1 索引")
    sub.add_parser("init", parents=[project_parent], help="创建 .memory/ 骨架与 config.yml 模板")

    p_stats = sub.add_parser(
        "stats", parents=[project_parent], help="kind/status 分布、浮现采纳率、注入量、预取命中率"
    )
    p_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    p_thaw = sub.add_parser(
        "thaw", parents=[project_parent], help="解冻归档事件或整个纪元，年龄重新计起"
    )
    p_thaw.add_argument("target", help="事件 id 或纪元（形如 2025-Q1）")

    p_purge = sub.add_parser(
        "purge", parents=[project_parent], help="删除指定日期前的归档包与摘要（默认 dry-run）"
    )
    p_purge.add_argument("--before", required=True, metavar="YYYY-MM-DD", help="早于该日期的整季纪元")
    p_purge.add_argument("--dry-run", action="store_true", help="默认行为：只打印将删清单")
    p_purge.add_argument("--yes", action="store_true", help="确认执行删除")

    p_log = sub.add_parser("log", parents=[project_parent], help="事件时间线，纯读不依赖索引")
    p_log.add_argument("--tree", action="store_true", help="按 parent 缩进两空格")
    p_log.add_argument("--since", type=int, default=None, metavar="N", help="只看最近 N 天的事件（按 id 时间戳）")
    p_log.add_argument("--kind", default=None, help="按 kind 过滤")

    return parser


def main(argv: list[str] | None = None) -> int:
    from eventmem.core.cli import COMMANDS, main as core_main
    from eventmem.core.integrity import enforce_source_root

    # A command line is pointed at whatever checkout its operator meant to use, so a
    # foreign source is said out loud here and never acted on: refusing would turn a
    # deliberate `--root` against another tree into a tool that cannot be run. The
    # services that must not start on the wrong code refuse for themselves. With no
    # KIN_SOURCE_ROOT in the environment there is nothing to compare against and this
    # costs nothing at all.
    enforce_source_root(warn_only=True)
    actual = argv if argv is not None else sys.argv[1:]
    if actual and actual[0] in COMMANDS:
        return core_main(actual)
    parser = build_parser()
    args = parser.parse_args(argv)

    project_dir = Path(args.project) if getattr(args, "project", None) else Path.cwd()
    _load_env(project_dir)

    handler = _DISPATCH[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
