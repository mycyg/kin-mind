"""轻整理／深整理（SPEC §3.8，DESIGN §4.3）。

安全纪律：两级整理只写 L1 派生文件、store.set_lesson、store.set_salience_prior、
规则闭合（store.close）与 stale 标注；不修改任何事件的 intent／body。

v0.2 增量：采纳判定与埋点消费（§3.13）、显著性后验重算（§3.11）、预取（§3.12）、
粒度自适应（§3.16）、CLAUDE.md 建议（§3.15）、跨项目 lesson 晋升（§3.18）。
每个 pass 的 LLM 调用失败只让该 pass 降级或跳过，其余 pass 照常执行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from eventmem.extract import load_todo_state, log_line, memory_log_dir
from eventmem.index import (
    GLOBAL_LESSONS_FILE,
    GLOBAL_STATE_FILE,
    ArchiveRow,
    anchor_key,
    append_archive_rows,
    claude_md_suggestions_file,
    epoch_of,
    global_dir,
    global_lessons_enabled,
    granularity_file,
    iter_anchor_keys,
    load_archive_index,
    load_granularity,
    load_prefetch,
    load_salience,
    prefetch_file,
    rebuild_all,
    salience_file,
)
from eventmem.index import salience_scores as index_salience_scores
from eventmem.llm import LLMError
from eventmem.paths import atomic_write
from eventmem.schema import id_to_datetime

if TYPE_CHECKING:  # 仅注解用
    from eventmem.index import Budget
    from eventmem.llm import LLMClient
    from eventmem.paths import MemoryPaths
    from eventmem.schema import Event
    from eventmem.store import Store

__all__ = [
    "light",
    "deep",
    "dirty_count",
    "salience_score",
    "kind_default_prior",
    "OUTCOME_SYSTEM",
    "LESSON_SYSTEM",
    "PRIOR_SYSTEM",
    "PREFETCH_SYSTEM",
    "MERGE_SYSTEM",
    "SEGMENT_SYSTEM",
    "PORTABILITY_SYSTEM",
    "EPOCH_SYSTEM",
    "archive_pass",
]

# ---------------------------------------------------------------- 常量

DEFAULT_DEEP_THRESHOLD: Final[int] = 30
DEFAULT_STALE_DAYS: Final[int] = 14

LIGHT_BATCH: Final[int] = 12  # 轻整理要求秒级，单批规模小、批数有上限
LIGHT_MAX_BATCHES: Final[int] = 3
DEEP_LESSON_BATCH: Final[int] = 8
DEEP_MAX_LESSON_EVENTS: Final[int] = 48

NGRAM_N: Final[int] = 8
NEAR_DUP_THRESHOLD: Final[float] = 0.6
PROMOTE_MIN_REPEATS: Final[int] = 2
RETIRE_AFTER_RUNS: Final[int] = 3

DEEP_WATERMARK_FILE: Final[str] = "deep-watermark"
LESSON_STATE_FILE: Final[str] = "lesson-state.json"
SURFACED_GLOB: Final[str] = "surfaced-*.jsonl"
SURFACED_PREFIX: Final[str] = "surfaced-"
DONE_SUFFIX: Final[str] = ".done"
PREFETCH_OUTCOME_FILE: Final[str] = "prefetch-outcome.jsonl"
TRANSCRIPT_DIR_ENV: Final[str] = "EVENTMEM_TRANSCRIPT_DIR"

# ---- 显著性（SPEC §3.11）：权重与 clamp 规则的模块级常量 ----

SALIENCE_W_PRIOR: Final[float] = 0.35
SALIENCE_W_REFS: Final[float] = 0.25
SALIENCE_W_HITS: Final[float] = 0.30
SALIENCE_W_IGNORED: Final[float] = 0.10
SALIENCE_W_SUPERSEDE: Final[float] = 0.20
PRIOR_VALUES: Final[dict[str, float]] = {"low": 0.2, "medium": 0.5, "high": 0.8}
REFS_CAP: Final[int] = 4
IGNORED_CAP: Final[int] = 4
DECISION_FLOOR: Final[float] = 0.4
# 0.25：让钳位在零证据时真实可达（prior 项最大 0.35*0.8=0.28 > 0.25），高自评的
# 顺利 build 被压到中性线附近；证据抬升后失效。0.5 的旧值在当前权重下是死分支。
SMOOTH_BUILD_CAP: Final[float] = 0.25
# 单个锚点桶参与 refs 计数的事件数上限，避免热点文件把计数拖成 O(N²)
REFS_BUCKET_CAP: Final[int] = 40

# ---- 采纳判定（SPEC §3.13）----

ADOPTION_WINDOW_TOOLS: Final[int] = 10
_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_FILE_INPUT_KEYS: Final[tuple[str, ...]] = ("file_path", "notebook_path", "path")

# ---- 预取（SPEC §3.12）----

PREFETCH_MAX_ITEMS: Final[int] = 12
PREFETCH_MODEL_MAX: Final[int] = 3
PREFETCH_RECENT_EVENTS: Final[int] = 10

# ---- 粒度自适应（SPEC §3.16）----

MERGE_MIN_GROUP: Final[int] = 3
MERGE_MAX_GAP_MINUTES: Final[int] = 30
MERGE_MAX_GROUPS: Final[int] = 8
COARSE_MIN_FILES: Final[int] = 8
COARSE_MAX_DIALOG_LINES: Final[int] = 400
COARSE_MAX_SEGMENTS: Final[int] = 4
COARSE_MAX_EVENTS: Final[int] = 6

# ---- 分级遗忘（SPEC §3.19）----

DEFAULT_COLD_DAYS: Final[int] = 90
DEFAULT_FROZEN_DAYS: Final[int] = 365
DEFAULT_SALIENCE_FLOOR: Final[float] = 0.2
# 单次冻结进 prompt 的成员上限；超出部分只进成员清单，不进摘要输入
EPOCH_PROMPT_MEMBERS: Final[int] = 60
EPOCH_SUMMARY_CHARS: Final[int] = 600
# 一个季度最多的续包序号，防止异常情况下无限找位
MAX_PACK_SEQ: Final[int] = 99

# ---- 跨项目晋升（SPEC §3.18）----

GLOBAL_NEAR_DUP_THRESHOLD: Final[float] = 0.5
GLOBAL_MIN_PROJECTS: Final[int] = 2
GLOBAL_MAX_JUDGED: Final[int] = 12

_LESSON_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<prefix>\s*-\s*\[(?P<id>[^\]]+)\]\s*)"
    r"(?:\((?P<tag>candidate|promoted|retired|adopted)\)\s*)?"
    r"(?P<rest>.*)$"
)
_WS_ID_RE: Final[re.Pattern[str]] = re.compile(r"\[(?P<id>[^\]]+)\]")
_DIALOG_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"#L(?P<a>\d+)-L(?P<b>\d+)$")

# ---------------------------------------------------------------- prompt

OUTCOME_SYSTEM: Final[str] = """为已经结束或标记完成、但缺少 outcome 的事件补写实际结果。只依据给出的 intent、kind、anchors 和正文，不编造结果、原因或数字。材料不足时明确尚无法从记录确认结果，不猜测或给建议。每项用一句准确简洁的中文，不在这里写经验教训。只返回以事件 id 为键、结果句为值的 JSON，每项输入对应一个键。"""

LESSON_SYSTEM: Final[str] = """从放弃方案或修复故障的事件中判断是否形成适用于今后不同任务的方法。只在材料已经说明原因、情境会重现、且能写清条件和行动时提炼；否则返回 null，全部为 null 也可以。不要重复目标或结果，不写“仔细测试”等泛泛建议，不把猜测原因当事实。用一两句准确简洁的中文，输出以事件 id 为键、方法句或 null 为值的 JSON。"""

PRIOR_SYSTEM: Final[str] = """评估已结束事件对本项目后续工作的潜在价值。它是当时的初步判断，之后仍可依据新证据修正，不预测未来。high：方案取舍、有理由放弃的假设、会复现的重要原因，遗失会导致重复劳动；medium：有具体可迁移原因的修复或调查；low：代码已记录的常规建设或缺少复用价值的步骤。依据不足默认 low。用简洁中文说明依据，只返回 {"事件id":{"prior":"low|medium|high","reason":"依据"}}，每项输入一个键。"""

PREFETCH_SYSTEM: Final[str] = """根据尚未完成的事件、明确的后续约定与最近事件锚点，推测下次从哪里继续，以便预加载相关旧事。最多三个入口，不明确时返回空列表。anchors 只用材料中原样出现的标识，不重复锚点，不猜结果。text 用一句简洁中文。只返回 {"predictions":[{"text":"继续的事情","anchors":["已有锚点"]}]}，无合适内容则 {"predictions":[]}。"""

MERGE_SYSTEM: Final[str] = """判断同一父项、共享锚点且发生时间接近的一组事件是否属于同一件工作。属于时用一句简洁中文概括全组，覆盖中间进展，不只总结首尾；只依据材料，不编结果。不属于同一工作则返回 null。只返回以组 id 为键、概括句或 null 为值的 JSON，每组一个键。"""

SEGMENT_SYSTEM: Final[str] = """按文件锚点为过大的单一事件分段，每项最多四段。每个文件只能来自该事件原锚点列表，且最多归入一段，不合适的文件可省略。确为同一件工作时返回空数组。label 用简洁准确的中文，输出 {"事件id":[{"label":"分段名","files":["原锚点"]}]}，每项输入一个键。"""

PORTABILITY_SYSTEM: Final[str] = """检查一条方法是否无需修改即可用于其他团队的不同项目。通过项会进入跨项目共享文件，因此不得含文件路径、模块名、项目名、服务名、内部术语，也不能依赖本项目架构、数据或惯例。去掉背景后仍须有可执行的条件和动作。不确定则 false，全部 false 也可以。只返回以方法 id 为键、true 或 false 为值的 JSON，每项一个键。"""

EPOCH_SYSTEM: Final[str] = """为一个日历季度中即将归档的事件写总览。这些输入是逐项意图；原文件打包后不再位于活动索引。概括这个季度主要做什么、涉及哪些部分和反复出现的主题，逐句依据材料，不编结果、原因或数字。成员列表另行保存，不逐项列举。用一段简洁准确的中文，只返回 {"summary":"季度总览"}。"""


# ---------------------------------------------------------------- 小工具


def _naive(moment: datetime) -> datetime:
    """统一为 naive，避免与 id 解析出的时间比较时报错。"""
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _ngrams(text: str) -> set[str]:
    """去空白小写后的 8-gram 集合。"""
    flat = "".join((text or "").split()).lower()
    if not flat:
        return set()
    if len(flat) <= NGRAM_N:
        return {flat}
    return {flat[i : i + NGRAM_N] for i in range(len(flat) - NGRAM_N + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def near_duplicate(left: str, right: str, threshold: float = NEAR_DUP_THRESHOLD) -> bool:
    """文本近似重复判定：8-gram Jaccard ≥ 阈值。"""
    return _jaccard(_ngrams(left), _ngrams(right)) >= threshold


def _read_int(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except Exception:  # noqa: BLE001
        return 0


def _load_config(paths: "MemoryPaths") -> dict[str, Any]:
    """config.yml 缺省即默认值（SPEC §3.10）。"""
    config = getattr(paths, "config", None)
    if not isinstance(config, Path) or not config.exists():
        return {}
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 配置损坏不阻断整理
        return {}
    return data if isinstance(data, dict) else {}


def _deep_threshold(paths: "MemoryPaths", budget: "Budget") -> int:
    fallback = getattr(budget, "deep_threshold", DEFAULT_DEEP_THRESHOLD)
    try:
        return max(1, int(_load_config(paths).get("deep_threshold", fallback)))
    except (TypeError, ValueError):
        return DEFAULT_DEEP_THRESHOLD


def _safe_events(store: "Store", paths: "MemoryPaths") -> list["Event"]:
    try:
        return list(store.iter_events())
    except Exception as exc:  # noqa: BLE001 —— 空库或读失败按空处理
        log_line(paths, f"consolidate: 事件库读取失败 {exc}")
        return []


def _has_outcome(event: "Event") -> bool:
    return bool((getattr(event, "outcome", None) or "").strip())


def _deep_watermark(paths: "MemoryPaths") -> Path:
    value = getattr(paths, "deep_watermark", None)
    return value if isinstance(value, Path) else memory_log_dir(paths) / DEEP_WATERMARK_FILE


def _lesson_state_path(paths: "MemoryPaths") -> Path:
    return memory_log_dir(paths) / LESSON_STATE_FILE


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读一个 jsonl 埋点文件；单行损坏跳过，文件缺失返回空列表。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """追加一条埋点记录；失败静默（埋点不得影响整理）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _write_json(paths: "MemoryPaths", path: Path, payload: Any, label: str) -> None:
    """原子写一个派生 JSON；失败记日志不抛（派生文件缺失只是退回 v0.1 行为）。"""
    try:
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        log_line(paths, f"deep: {label} 写入失败 {exc}")


def _parse_ts(value: Any) -> datetime | None:
    """解析埋点／transcript 的时间戳；带时区的换算成本地 naive，便于统一比较。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _non_dialog_anchors(event: "Event") -> set[str]:
    """事件的可比对锚点集合（dialog 指针是会话坐标，不参与交集判定）。"""
    anchors = getattr(event, "anchors", None)
    if anchors is None:
        return set()
    values: set[str] = set()
    for attr in ("files", "commits", "tests", "error_sigs"):
        values.update(str(v) for v in (getattr(anchors, attr, None) or []))
    return values


def _rel_files(event: "Event", paths: "MemoryPaths") -> set[str]:
    """事件的文件锚点，统一成项目内相对路径（与倒排 key 同口径）。"""
    anchors = getattr(event, "anchors", None)
    return {paths.relative(f) for f in (getattr(anchors, "files", None) or []) if str(f).strip()}


# ---------------------------------------------------------------- 显著性公式（SPEC §3.11）


def kind_default_prior(kind: str, status: str) -> str:
    """无模型补评时的 kind 规则表默认档（SPEC §3.11）。

    表里未列出的组合（如 explore/done）取 low —— 规则只做硬边界，拿不准归低档，
    抬升交给证据。
    """
    if kind == "decision":
        return "high"
    if kind == "fix":
        return "medium"
    if kind == "explore" and status == "abandoned":
        return "medium"
    return "low"


def _prior_of(event: "Event") -> str:
    """事件的先验档：优先用 L0 里的自评，缺失时按 kind 规则表取默认（不写回 L0）。"""
    prior = getattr(event, "salience_prior", None)
    if isinstance(prior, str) and prior in PRIOR_VALUES:
        return prior
    return kind_default_prior(str(event.kind), str(event.status))


def salience_score(event: "Event", prior: str, evidence: dict[str, Any]) -> float:
    """SPEC §3.11 的后验公式与 clamp 规则。

    ignored_decay 与 refs 同口径归一（min(n, 4)/4，SPEC 未给出 decay 定义）；
    「顺利 build」＝ build ＋ done ＋ 无报错锚点，其上限在有证据抬升时失效。
    """
    hits = max(0, int(evidence.get("hits") or 0))
    ignored = max(0, int(evidence.get("ignored") or 0))
    refs = max(0, int(evidence.get("refs") or 0))
    trigger = bool(evidence.get("superseded_trigger"))

    total = hits + ignored
    raw = (
        SALIENCE_W_PRIOR * PRIOR_VALUES.get(prior, PRIOR_VALUES["medium"])
        + SALIENCE_W_REFS * (min(refs, REFS_CAP) / REFS_CAP)
        + SALIENCE_W_HITS * (hits / total if total else 0.0)
        - SALIENCE_W_IGNORED * (min(ignored, IGNORED_CAP) / IGNORED_CAP)
        + SALIENCE_W_SUPERSEDE * (1.0 if trigger else 0.0)
    )
    score = min(1.0, max(0.0, raw))

    if event.kind == "decision":
        return max(score, DECISION_FLOOR)
    lifted = hits > 0 or refs > 0 or trigger
    smooth_build = (
        event.kind == "build"
        and event.status == "done"
        and not (getattr(event, "anchors", None) and event.anchors.error_sigs)
    )
    if smooth_build and not lifted:
        return min(score, SMOOTH_BUILD_CAP)
    return score


# ---------------------------------------------------------------- 脏量


def _total_events(paths: "MemoryPaths") -> int:
    events_dir = getattr(paths, "events_dir", None)
    if isinstance(events_dir, Path) and events_dir.is_dir():
        return sum(1 for _ in events_dir.glob("*.md"))
    index = getattr(paths, "project_index", None)  # 退化：数 project.md 的数据行
    if isinstance(index, Path) and index.exists():
        try:
            rows = [
                line
                for line in index.read_text(encoding="utf-8").splitlines()
                if line.startswith("|") and set(line.strip()) - set("|- :")
            ]
        except OSError:
            return 0
        return max(0, len(rows) - 1)
    return 0


def dirty_count(paths: "MemoryPaths") -> int:
    """上次深整理水位之后的新事件数。"""
    return max(0, _total_events(paths) - _read_int(_deep_watermark(paths)))


# ---------------------------------------------------------------- 采纳判定与埋点消费（SPEC §3.13）


class _ToolCall:
    """transcript 里的一次工具调用：名称、涉及文件、时间戳。"""

    __slots__ = ("name", "files", "ts")

    def __init__(self, name: str, files: set[str], ts: datetime | None) -> None:
        self.name = name
        self.files = files
        self.ts = ts


def _find_transcript(paths: "MemoryPaths", session: str) -> Path | None:
    """按会话 id 找 transcript／feed。

    SPEC §3.13 的 surfaced 记录不含 transcript 路径，因此按约定位置依次找：
    环境变量指定的目录 → 项目 log 目录（dsh feed）→ Claude Code 的 ~/.claude/projects。
    """
    names = (
        f"{session}.jsonl",
        f"feed-{session}.jsonl",
        f"dsh-feed-{session}.jsonl",  # dsh 插件（B-lite）落盘的 feed 文件名
        f"transcript-{session}.jsonl",
    )
    bases: list[Path] = []
    override = (os.environ.get(TRANSCRIPT_DIR_ENV) or "").strip()
    if override:
        bases.append(Path(override).expanduser())
    bases.append(memory_log_dir(paths))
    for base in bases:
        for name in names:
            candidate = base / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    projects = Path.home() / ".claude" / "projects"
    try:
        if projects.is_dir():
            for candidate in sorted(projects.glob(f"*/{session}.jsonl")):
                if candidate.is_file():
                    return candidate
    except OSError:
        pass
    return None


def _tool_calls(transcript: Path, paths: "MemoryPaths") -> list[_ToolCall]:
    """按出现顺序取 transcript 里的工具调用；单行损坏跳过。"""
    calls: list[_ToolCall] = []
    try:
        handle = transcript.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return calls
    with handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            ts = _parse_ts(record.get("timestamp"))
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else record.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                payload = block.get("input")
                payload = payload if isinstance(payload, dict) else {}
                calls.append(
                    _ToolCall(str(block.get("name") or ""), _tool_files(payload, paths), ts)
                )
    return calls


def _tool_files(payload: dict[str, Any], paths: "MemoryPaths") -> set[str]:
    """工具入参里的文件路径，规约成项目相对路径。"""
    files: set[str] = set()
    for key in _FILE_INPUT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            files.add(paths.relative(value.strip()))
    return files


def _judge_adoption(surfaced_ts: datetime | None, calls: list[_ToolCall], files: set[str]) -> bool:
    """浮现后 10 次工具调用内出现该事件文件的 Edit/Write 即 hit（SPEC §3.13）。

    埋点与 transcript 的时间戳对不齐时（任一侧缺时间），退化为全会话扫描——
    宁可放宽窗口，也不因为对不齐把所有浮现判成 ignored。
    """
    window = calls
    if surfaced_ts is not None and any(call.ts is not None for call in calls):
        window = [call for call in calls if call.ts is not None and call.ts >= surfaced_ts][
            :ADOPTION_WINDOW_TOOLS
        ]
    return any(call.name in _WRITE_TOOLS and (call.files & files) for call in window)


def _adoption_pass(
    paths: "MemoryPaths",
    events: list["Event"],
) -> dict[str, dict[str, int]]:
    """消费 log/surfaced-*.jsonl：累计 hit/ignored，顺带记预取命中，处理过的文件改名 .done。

    同一遍扫描里做两件事（SPEC §3.12 预取命中记录 ＋ §3.13 采纳判定），因为两者
    都需要「这个会话实际碰了哪些文件」。
    """
    log_dir = memory_log_dir(paths)
    deltas: dict[str, dict[str, int]] = {}
    try:
        surfaced_files = sorted(log_dir.glob(SURFACED_GLOB))
    except OSError:
        return deltas
    if not surfaced_files:
        return deltas

    by_id = {event.id: event for event in events}
    predicted = {a for a in load_prefetch(paths)["anchors"] if a.startswith("file:")}

    for path in surfaced_files:
        session = path.name[len(SURFACED_PREFIX) : -len(".jsonl")]
        transcript = _find_transcript(paths, session)
        calls = _tool_calls(transcript, paths) if transcript is not None else []

        if transcript is None:
            log_line(paths, f"deep: 会话 {session} 的 transcript 未找到，采纳判定跳过")
        else:
            for record in _read_jsonl(path):
                event_id = str(record.get("event_id") or "").strip()
                event = by_id.get(event_id)
                if event is None:
                    continue
                files = _rel_files(event, paths)
                if not files:
                    continue  # 无文件锚点则无从判定，不计入任何一侧
                bucket = deltas.setdefault(event_id, {"hits": 0, "ignored": 0})
                verdict = _judge_adoption(_parse_ts(record.get("ts")), calls, files)
                bucket["hits" if verdict else "ignored"] += 1

            actual = {anchor_key("file", f) for call in calls for f in call.files}
            _append_jsonl(
                log_dir / PREFETCH_OUTCOME_FILE,
                {
                    "session": session,
                    "predicted": len(predicted),
                    "hit": len(predicted & actual),
                    "ts": datetime.now().isoformat(timespec="seconds"),
                },
            )

        try:  # 改名防重复计数；glob 不匹配 .done 后缀
            path.rename(path.with_name(path.name + DONE_SUFFIX))
        except OSError as exc:
            log_line(paths, f"deep: surfaced 埋点改名失败 {path.name} {exc}")
    return deltas


# ---------------------------------------------------------------- 显著性后验（SPEC §3.11）


def _ref_counts(events: list["Event"], paths: "MemoryPaths") -> dict[str, int]:
    """refs：被后续事件的锚点交集或 parent 引用的次数（去重到「引用者事件数」）。"""
    buckets: dict[str, list[str]] = {}
    for event in events:
        for key in iter_anchor_keys(event, paths):
            if key.startswith("intent:"):
                continue  # intent 词元是检索用的，不算引用
            buckets.setdefault(key, []).append(event.id)

    refs: dict[str, set[str]] = {}
    for ids in buckets.values():
        ordered = sorted(set(ids))[-REFS_BUCKET_CAP:]
        for index, earlier in enumerate(ordered):
            for later in ordered[index + 1 :]:
                refs.setdefault(earlier, set()).add(later)
    for event in events:
        parent = getattr(event, "parent", None)
        if parent:
            refs.setdefault(str(parent), set()).add(event.id)
    return {event_id: len(referrers) for event_id, referrers in refs.items()}


def _recompute_salience(
    paths: "MemoryPaths",
    events: list["Event"],
    deltas: dict[str, dict[str, int]],
    now: datetime,
) -> None:
    """按证据重算后验并原子写 salience.json；hits/ignored 跨轮累加，其余每轮重算。"""
    previous = load_salience(paths)
    refs = _ref_counts(events, paths)
    triggers = {
        str(event.superseded_by) for event in events if getattr(event, "superseded_by", None)
    }
    stamp = _naive(now).isoformat(timespec="seconds")

    payload: dict[str, Any] = {}
    for event in events:
        old = previous.get(event.id) or {}
        old_evidence = old.get("evidence") if isinstance(old.get("evidence"), dict) else {}
        delta = deltas.get(event.id, {})
        try:
            hits = int(old_evidence.get("hits") or 0) + delta.get("hits", 0)
            ignored = int(old_evidence.get("ignored") or 0) + delta.get("ignored", 0)
        except (TypeError, ValueError):
            hits, ignored = delta.get("hits", 0), delta.get("ignored", 0)
        evidence = {
            "refs": refs.get(event.id, 0),
            "hits": hits,
            "ignored": ignored,
            "superseded_trigger": event.id in triggers,
        }
        prior = _prior_of(event)
        payload[event.id] = {
            "score": round(salience_score(event, prior, evidence), 4),
            "prior": prior,
            "evidence": evidence,
            "updated": stamp,
        }
    _write_json(paths, salience_file(paths), payload, "salience.json")


# ---------------------------------------------------------------- 先验补评（SPEC §3.11，light）


def _prior_targets(events: list["Event"]) -> list["Event"]:
    """已闭合且尚无自评的事件，新的优先。"""
    targets = [
        event
        for event in events
        if event.status != "open" and getattr(event, "salience_prior", None) is None
    ]
    targets.sort(key=lambda e: e.id, reverse=True)
    return targets


def _backfill_priors(
    store: "Store",
    paths: "MemoryPaths",
    client: "LLMClient | None",
    events: list["Event"],
) -> int:
    """给无 prior 的已闭合事件补评：有 client 走 LLM 批量，无 client 用 kind 规则表。"""
    setter = getattr(store, "set_salience_prior", None)
    if not callable(setter):
        log_line(paths, "light: Store 无 set_salience_prior，先验补评跳过")
        return 0
    targets = _prior_targets(events)[: LIGHT_BATCH * LIGHT_MAX_BATCHES]
    if not targets:
        return 0

    # 降级基线：规则表默认值。LLM 只在其上覆盖，调用失败即用基线，不整体放弃。
    verdicts: dict[str, tuple[str, str]] = {
        event.id: (kind_default_prior(str(event.kind), str(event.status)), "按 kind 规则表默认")
        for event in targets
    }
    if client is not None:
        for batch in _chunks(targets, LIGHT_BATCH)[:LIGHT_MAX_BATCHES]:
            payload = json.dumps([_event_brief(event) for event in batch], ensure_ascii=False)
            try:
                answer = client.complete_json(
                    PRIOR_SYSTEM,
                    "CLOSED EVENTS WITHOUT A PRIOR:\n" + payload + "\n\nReturn the JSON object.",
                    max_tokens=1024,
                )
            except Exception as exc:  # noqa: BLE001 —— 含 LLMError；补评失败退回规则表
                log_line(paths, f"light: 先验补评放弃，退回 kind 规则表：{exc}")
                break
            if not isinstance(answer, dict):
                continue
            for event in batch:
                item = answer.get(event.id)
                if not isinstance(item, dict):
                    continue
                prior = str(item.get("prior") or "").strip().lower()
                if prior not in PRIOR_VALUES:
                    continue
                reason = str(item.get("reason") or "").strip()[:120] or "模型补评"
                verdicts[event.id] = (prior, reason)

    written = 0
    for event in targets:
        prior, reason = verdicts[event.id]
        try:
            setter(event.id, prior, reason)
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 并发下已被写过等情况忽略
            log_line(paths, f"light: 先验写入跳过 {event.id} {exc}")
    return written


# ---------------------------------------------------------------- 预取（SPEC §3.12）


def _anchor_index(events: list["Event"], paths: "MemoryPaths") -> dict[str, list[str]]:
    """内存里的锚点倒排：预取跑在 rebuild_all 之前，不能依赖磁盘上的旧 anchors.json。"""
    mapping: dict[str, list[str]] = {}
    for event in events:
        for key in iter_anchor_keys(event, paths):
            mapping.setdefault(key, []).append(event.id)
    return mapping


def _prefetch_item(event: "Event", anchor: str, source: str, score: float) -> dict[str, Any]:
    text = (event.outcome or event.intent or "").strip()
    return {
        "event_id": event.id,
        "text": text[:200],
        "anchor": anchor,
        "source": source,
        "score": round(score, 4),
    }


def _rule_prefetch(
    events: list["Event"],
    paths: "MemoryPaths",
    anchors: dict[str, list[str]],
    scores: dict[str, float],
) -> list[dict[str, Any]]:
    """规则级预取：open 事件的文件锚点反查倒排，收关联的已闭合事件，按 salience 排序。"""
    by_id = {event.id: event for event in events}
    open_events = [event for event in events if event.status == "open"]
    # 前瞻标记（「下次先做 X」）是最强信号，排在普通 open 事件之前
    open_events.sort(key=lambda e: (bool(getattr(e, "prospective", False)), e.id), reverse=True)

    items: list[dict[str, Any]] = []
    taken: set[str] = set()
    for open_event in open_events:
        for path_value in sorted(_rel_files(open_event, paths)):
            for event_id in anchors.get(anchor_key("file", path_value), []):
                candidate = by_id.get(event_id)
                if candidate is None or candidate.status == "open" or event_id in taken:
                    continue
                if not (candidate.outcome or candidate.intent):
                    continue
                taken.add(event_id)
                items.append(
                    _prefetch_item(candidate, path_value, "rule", scores.get(event_id, 0.0))
                )
    items.sort(key=lambda item: item["score"], reverse=True)
    return items


def _model_prefetch(
    events: list["Event"],
    paths: "MemoryPaths",
    client: "LLMClient",
    anchors: dict[str, list[str]],
    scores: dict[str, float],
) -> list[dict[str, Any]]:
    """模型级预取：open 事件 ＋ 最近事件的锚点摘要 → ≤3 条入口预测，锚点白名单过滤。"""
    open_events = [event for event in events if event.status == "open"]
    if not open_events:
        return []
    recent = events[-PREFETCH_RECENT_EVENTS:]
    payload = json.dumps(
        {
            "open_events": [
                {
                    "id": event.id,
                    "intent": event.intent,
                    "prospective": bool(getattr(event, "prospective", False)),
                    "files": sorted(_rel_files(event, paths))[:8],
                }
                for event in open_events[-PREFETCH_RECENT_EVENTS:]
            ],
            "recent_anchors": [
                {"id": event.id, "files": sorted(_rel_files(event, paths))[:8]}
                for event in recent
            ],
        },
        ensure_ascii=False,
    )
    try:
        answer = client.complete_json(
            PREFETCH_SYSTEM,
            "MATERIAL:\n" + payload + "\n\nReturn the JSON object.",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 —— 含 LLMError；预测失败只丢模型级，规则级仍在
        log_line(paths, f"deep: 模型级预取放弃：{exc}")
        return []

    raw = answer.get("predictions") if isinstance(answer, dict) else answer
    if not isinstance(raw, list):
        return []
    by_id = {event.id: event for event in events}
    items: list[dict[str, Any]] = []
    taken: set[str] = set()
    for prediction in raw[:PREFETCH_MODEL_MAX]:
        if not isinstance(prediction, dict):
            continue
        why = str(prediction.get("text") or "").strip()[:120]
        values = prediction.get("anchors")
        for value in values if isinstance(values, list) else []:
            key = anchor_key("file", paths.relative(str(value).strip()))
            if key not in anchors:  # 白名单：倒排里没有的锚点一律丢弃
                continue
            for event_id in anchors[key]:
                candidate = by_id.get(event_id)
                if candidate is None or candidate.status == "open" or event_id in taken:
                    continue
                taken.add(event_id)
                item = _prefetch_item(
                    candidate, key.split(":", 1)[1], "model", scores.get(event_id, 0.0)
                )
                item["why"] = why
                items.append(item)
    return items


def _write_prefetch(
    paths: "MemoryPaths",
    events: list["Event"],
    scores: dict[str, float],
    client: "LLMClient | None",
    now: datetime,
    *,
    model_level: bool,
) -> None:
    """产出 prefetch.json：模型级预测在前，规则级候选在后，整体截断到上限。"""
    anchors = _anchor_index(events, paths)
    items: list[dict[str, Any]] = []
    if model_level and client is not None:
        items.extend(_model_prefetch(events, paths, client, anchors, scores))
    known = {item["event_id"] for item in items}
    items.extend(
        item for item in _rule_prefetch(events, paths, anchors, scores) if item["event_id"] not in known
    )
    items = items[:PREFETCH_MAX_ITEMS]
    payload = {
        "generated": _naive(now).isoformat(timespec="seconds"),
        "items": items,
        "anchors": sorted({anchor_key("file", item["anchor"]) for item in items if item["anchor"]}),
    }
    _write_json(paths, prefetch_file(paths), payload, "prefetch.json")


# ---------------------------------------------------------------- 轻整理


def _event_brief(event: "Event", body_chars: int = 300) -> dict[str, Any]:
    anchors = getattr(event, "anchors", None)
    brief: dict[str, Any] = {
        "id": event.id,
        "kind": event.kind,
        "status": event.status,
        "intent": event.intent,
    }
    body = (getattr(event, "body", "") or "").strip()
    if body:
        brief["actions"] = body[:body_chars]
    if _has_outcome(event):
        brief["outcome"] = event.outcome
    if anchors is not None:
        brief["anchors"] = {
            "files": list(getattr(anchors, "files", []) or [])[:8],
            "commits": list(getattr(anchors, "commits", []) or [])[:4],
            "tests": list(getattr(anchors, "tests", []) or [])[:4],
            "error_sigs": list(getattr(anchors, "error_sigs", []) or [])[:4],
        }
    return brief


def _chunks(items: list["Event"], size: int) -> list[list["Event"]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _outcome_lines(
    targets: list["Event"],
    client: "LLMClient | None",
    paths: "MemoryPaths",
) -> dict[str, str]:
    """为缺 outcome 的事件生成单行结论；无 client 或调用失败时复制 intent 降级。"""
    fallback = {event.id: event.intent for event in targets}
    if client is None or not targets:
        return fallback
    lines: dict[str, str] = dict(fallback)
    for batch in _chunks(targets, LIGHT_BATCH)[:LIGHT_MAX_BATCHES]:
        payload = json.dumps([_event_brief(event) for event in batch], ensure_ascii=False)
        try:
            answer = client.complete_json(
                OUTCOME_SYSTEM,
                "EVENTS MISSING AN OUTCOME:\n" + payload + "\n\nReturn the JSON object.",
                max_tokens=1024,
            )
        except LLMError as exc:
            log_line(paths, f"light: outcome 补写放弃，降级复制 intent：{exc}")
            break
        except Exception as exc:  # noqa: BLE001
            log_line(paths, f"light: outcome 补写异常，降级复制 intent：{exc}")
            break
        if not isinstance(answer, dict):
            continue
        for event in batch:
            value = answer.get(event.id)
            if isinstance(value, str) and value.strip():
                lines[event.id] = value.strip()[:200]
    return lines


def _write_outcome(store: "Store", paths: "MemoryPaths", event: "Event", outcome: str) -> None:
    """已闭合事件的 outcome 补全。Store 未提供 set_outcome 时记日志放弃，不改原文。"""
    setter = getattr(store, "set_outcome", None)
    if callable(setter):
        try:
            setter(event.id, outcome)
        except Exception as exc:  # noqa: BLE001
            log_line(paths, f"light: outcome 写入失败 {event.id} {exc}")
        return
    log_line(paths, f"light: {event.id} 缺 outcome，但 Store 无 set_outcome，跳过")


def light(
    store: "Store",
    paths: "MemoryPaths",
    budget: "Budget",
    client: "LLMClient | None",
    now: datetime,
) -> None:
    """会话结束后的秒级整理：补 outcome、规则闭合、补显著性先验、规则级预取、重建索引。"""
    events = _safe_events(store, paths)
    by_id = {event.id: event for event in events}

    # 规则闭合：todo 已 completed 但事件仍 open
    completed_ids = {
        str(record.get("event_id"))
        for record in load_todo_state(paths).values()
        if str(record.get("status") or "") == "completed" and record.get("event_id")
    }
    to_close = [by_id[eid] for eid in sorted(completed_ids) if eid in by_id and by_id[eid].status == "open"]
    # 已闭合但缺 outcome
    to_fill = [e for e in events if e.status != "open" and not _has_outcome(e)]

    targets = (to_close + to_fill)[: LIGHT_BATCH * LIGHT_MAX_BATCHES]
    target_ids = {event.id for event in targets}
    lines = _outcome_lines(targets, client, paths)

    closed = 0
    for event in to_close:
        if event.id not in target_ids:
            continue
        try:
            store.close(event.id, "done", lines.get(event.id) or event.intent)
            closed += 1
        except Exception as exc:  # noqa: BLE001 —— 已闭合等情况忽略
            log_line(paths, f"light: 规则闭合跳过 {event.id} {exc}")
    filled = 0
    for event in to_fill:
        if event.id not in target_ids:
            continue
        _write_outcome(store, paths, event, lines.get(event.id) or event.intent)
        filled += 1

    if closed or filled:
        events = _safe_events(store, paths)  # 状态与 outcome 变了，先验补评要看新值
    # 先验补评在 outcome 之后：模型评的是「已经写完结论的事件」
    priors = _backfill_priors(store, paths, client, events)
    if priors:
        events = _safe_events(store, paths)

    # 预取：轻整理只做规则级（SPEC §3.12），且必须在 rebuild_all 之前写好
    _write_prefetch(paths, events, index_salience_scores(paths), client, now, model_level=False)
    rebuild_all(store, paths, budget, now)
    log_line(
        paths,
        f"light: 闭合 {closed} 补 outcome {filled} 补先验 {priors} 事件总数 {len(events)}",
    )


# ---------------------------------------------------------------- 深整理


def _stale_ids(events: list["Event"], budget: "Budget", now: datetime) -> set[str]:
    """open 超过 stale_days 的事件 id（DESIGN §4.2 闭合信号 4）。"""
    days = int(getattr(budget, "stale_days", DEFAULT_STALE_DAYS) or DEFAULT_STALE_DAYS)
    deadline = _naive(now) - timedelta(days=days)
    stale: set[str] = set()
    for event in events:
        if event.status != "open":
            continue
        moment = id_to_datetime(event.id)
        if moment is not None and moment < deadline:
            stale.add(event.id)
    return stale


def _distill_lessons(
    store: "Store",
    paths: "MemoryPaths",
    client: "LLMClient | None",
    events: list["Event"],
) -> int:
    """对无 lesson 的 abandoned / fix 事件批量蒸馏；门槛写在 prompt 里。"""
    if client is None:
        return 0
    candidates = [
        event
        for event in events
        if not (getattr(event, "lesson", None) or "").strip()
        and (event.status == "abandoned" or event.kind == "fix")
    ]
    candidates.sort(key=lambda e: e.id, reverse=True)  # 新事件优先
    candidates = candidates[:DEEP_MAX_LESSON_EVENTS]
    written = 0
    for batch in _chunks(candidates, DEEP_LESSON_BATCH):
        payload = json.dumps([_event_brief(event, body_chars=600) for event in batch], ensure_ascii=False)
        try:
            answer = client.complete_json(
                LESSON_SYSTEM,
                "EVENTS TO CONSIDER:\n" + payload + "\n\nReturn the JSON object.",
                max_tokens=1024,
            )
        except LLMError as exc:
            log_line(paths, f"deep: lesson 蒸馏放弃：{exc}")
            break
        except Exception as exc:  # noqa: BLE001
            log_line(paths, f"deep: lesson 蒸馏异常：{exc}")
            break
        if not isinstance(answer, dict):
            continue
        for event in batch:
            value = answer.get(event.id)
            if not isinstance(value, str):
                continue
            lesson = value.strip()[:300]
            # 只是复述 intent／outcome 的不写（DESIGN §8.4 错误蒸馏的毒性）
            if len(lesson) < 8 or _norm(lesson) in (_norm(event.intent), _norm(event.outcome or "")):
                continue
            try:
                store.set_lesson(event.id, lesson)
                written += 1
            except Exception as exc:  # noqa: BLE001
                log_line(paths, f"deep: lesson 写入失败 {event.id} {exc}")
    return written


def _load_lesson_state(paths: "MemoryPaths") -> dict[str, Any]:
    try:
        data = json.loads(_lesson_state_path(paths).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"runs": 0, "events": {}}
    if not isinstance(data, dict):
        return {"runs": 0, "events": {}}
    events = data.get("events")
    return {"runs": int(data.get("runs") or 0), "events": events if isinstance(events, dict) else {}}


def _save_lesson_state(paths: "MemoryPaths", state: dict[str, Any]) -> None:
    try:
        atomic_write(_lesson_state_path(paths), json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        log_line(paths, f"deep: lesson 状态写入失败 {exc}")


def _referenced(lesson: str, new_events: list["Event"]) -> bool:
    """本轮新事件里出现近似重复的表述，视为该 lesson 被引用。"""
    for event in new_events:
        for text in (getattr(event, "lesson", None), event.intent, getattr(event, "outcome", None)):
            if text and near_duplicate(lesson, text):
                return True
    return False


def _update_lesson_state(
    paths: "MemoryPaths",
    events: list["Event"],
    new_events: list["Event"],
) -> dict[str, Any]:
    """晋升／退休（SPEC §3.8 deep-3）。状态存 log/lesson-state.json。"""
    state = _load_lesson_state(paths)
    prior = state["events"]
    state["runs"] = int(state.get("runs") or 0) + 1

    lessons = [(e.id, (e.lesson or "").strip()) for e in events if (getattr(e, "lesson", None) or "").strip()]
    # 近似重复聚类：代表文本 → 成员 id
    clusters: list[tuple[str, list[str]]] = []  # (代表文本, 成员 id)
    for event_id, text in lessons:
        for representative, members in clusters:
            if near_duplicate(representative, text):
                members.append(event_id)
                break
        else:
            clusters.append((text, [event_id]))
    repeated: set[str] = {mid for _, members in clusters if len(members) >= PROMOTE_MIN_REPEATS for mid in members}

    updated: dict[str, dict[str, Any]] = {}
    for event_id, text in lessons:
        record = prior.get(event_id) if isinstance(prior.get(event_id), dict) else {}
        status = str(record.get("status") or "candidate")
        unused = int(record.get("unused_runs") or 0)
        if status in ("retired", "adopted"):
            pass  # 退休后不自动复活；已进 CLAUDE.md 的也不再回到工作集
        elif status == "promoted":
            if _referenced(text, new_events):
                unused = 0
            else:
                unused += 1
                if unused >= RETIRE_AFTER_RUNS:
                    status = "retired"
        else:
            status = "promoted" if event_id in repeated else "candidate"
            unused = 0
        updated[event_id] = {"status": status, "unused_runs": unused, "text": text[:300]}
    state["events"] = updated
    _save_lesson_state(paths, state)
    return state


def _annotate_lessons(paths: "MemoryPaths", state: dict[str, Any]) -> bool:
    """把晋升状态写回 lessons.md 的标签；rebuild_all 会沿用文件里的状态。返回是否有改动。"""
    target = getattr(paths, "lessons", None)
    if not isinstance(target, Path) or not target.exists():
        return False
    statuses = {eid: rec.get("status", "candidate") for eid, rec in state.get("events", {}).items()}
    if not statuses:
        return False
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    changed = False
    for index, line in enumerate(lines):
        match = _LESSON_LINE_RE.match(line)
        if not match:
            continue
        status = statuses.get(match.group("id"))
        if not status or status == match.group("tag"):
            continue
        lines[index] = f"{match.group('prefix')}({status}) {match.group('rest')}".rstrip()
        changed = True
    if changed:
        try:
            atomic_write(target, "\n".join(lines) + "\n")
        except OSError as exc:
            log_line(paths, f"deep: lessons.md 标注失败 {exc}")
            return False
    return changed


def _annotate_stale(paths: "MemoryPaths", stale_ids: set[str]) -> None:
    """working-set 的 Open events 行尾追加 (stale)。"""
    target = getattr(paths, "working_set", None)
    if not stale_ids or not isinstance(target, Path) or not target.exists():
        return
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    in_open_section = False
    changed = False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            in_open_section = "open" in line.lower()
            continue
        if not in_open_section or line.rstrip().endswith("(stale)"):
            continue
        match = _WS_ID_RE.search(line)
        if match and match.group("id") in stale_ids:
            lines[index] = line.rstrip() + " (stale)"
            changed = True
    if changed:
        try:
            atomic_write(target, "\n".join(lines) + "\n")
        except OSError as exc:
            log_line(paths, f"deep: working-set stale 标注失败 {exc}")


# ---------------------------------------------------------------- 粒度自适应（SPEC §3.16）


def _chains(previous: "Event", current: "Event") -> bool:
    """两个事件能否接进同一个合并组：同 parent、锚点交集非空、间隔 < 30 分钟。"""
    if (getattr(previous, "parent", None) or None) != (getattr(current, "parent", None) or None):
        return False
    if not (_non_dialog_anchors(previous) & _non_dialog_anchors(current)):
        return False
    start, end = id_to_datetime(previous.id), id_to_datetime(current.id)
    if start is None or end is None:
        return False
    return abs((end - start).total_seconds()) < MERGE_MAX_GAP_MINUTES * 60


def _merge_candidates(events: list["Event"]) -> list[list["Event"]]:
    """连续已闭合事件里成链且 ≥3 个的段落。锚点交集按相邻两两判定（SPEC 未细化到组）。"""
    groups: list[list["Event"]] = []
    run: list["Event"] = []
    for event in [e for e in events if e.status != "open"]:
        if run and _chains(run[-1], event):
            run.append(event)
            continue
        if len(run) >= MERGE_MIN_GROUP:
            groups.append(run)
        run = [event]
    if len(run) >= MERGE_MIN_GROUP:
        groups.append(run)
    return groups[-MERGE_MAX_GROUPS:]


def _dialog_span(event: "Event") -> int:
    """事件 dialog 指针覆盖的最大行数跨度。"""
    anchors = getattr(event, "anchors", None)
    widest = 0
    for pointer in getattr(anchors, "dialog", None) or []:
        match = _DIALOG_SPAN_RE.search(str(pointer))
        if match:
            widest = max(widest, int(match.group("b")) - int(match.group("a")))
    return widest


def _coarse_candidates(
    events: list["Event"], paths: "MemoryPaths"
) -> list[tuple["Event", list[str]]]:
    """粗事件：文件锚点 ≥8 或 dialog 跨度 > 400 行；无文件锚点则无从分段。"""
    found: list[tuple["Event", list[str]]] = []
    for event in events:
        files = sorted(_rel_files(event, paths))
        if len(files) >= COARSE_MIN_FILES or _dialog_span(event) > COARSE_MAX_DIALOG_LINES:
            if files:
                found.append((event, files))
    return found[-COARSE_MAX_EVENTS:]


def _group_summaries(
    paths: "MemoryPaths", client: "LLMClient", groups: list[list["Event"]]
) -> tuple[dict[str, str], bool]:
    """给每个候选组要一句概括；返回 (概括表, LLM 是否成功)。"""
    payload = json.dumps(
        [
            {
                "group_id": group[0].id,
                "members": [_event_brief(event, body_chars=200) for event in group],
            }
            for group in groups
        ],
        ensure_ascii=False,
    )
    try:
        answer = client.complete_json(
            MERGE_SYSTEM,
            "CANDIDATE GROUPS:\n" + payload + "\n\nReturn the JSON object.",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 —— 含 LLMError
        log_line(paths, f"deep: 合并概括放弃：{exc}")
        return {}, False
    if not isinstance(answer, dict):
        return {}, True
    summaries: dict[str, str] = {}
    for group in groups:
        value = answer.get(group[0].id)
        if isinstance(value, str) and value.strip():
            summaries[group[0].id] = value.strip()[:200]
    return summaries, True


def _event_segments(
    paths: "MemoryPaths", client: "LLMClient", candidates: list[tuple["Event", list[str]]]
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """给粗事件按锚点聚类要 ≤4 段；文件白名单过滤。返回 (分段表, LLM 是否成功)。"""
    payload = json.dumps(
        [
            {"id": event.id, "intent": event.intent, "outcome": event.outcome, "files": files}
            for event, files in candidates
        ],
        ensure_ascii=False,
    )
    try:
        answer = client.complete_json(
            SEGMENT_SYSTEM,
            "COARSE EVENTS:\n" + payload + "\n\nReturn the JSON object.",
            max_tokens=1536,
        )
    except Exception as exc:  # noqa: BLE001 —— 含 LLMError
        log_line(paths, f"deep: 粗事件分段放弃：{exc}")
        return {}, False
    if not isinstance(answer, dict):
        return {}, True

    out: dict[str, list[dict[str, Any]]] = {}
    for event, files in candidates:
        raw = answer.get(event.id)
        if not isinstance(raw, list):
            continue
        allowed = set(files)
        used: set[str] = set()
        segments: list[dict[str, Any]] = []
        for item in raw[:COARSE_MAX_SEGMENTS]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()[:120]
            values = item.get("files")
            picked = [
                str(f).strip()
                for f in (values if isinstance(values, list) else [])
                if str(f).strip() in allowed and str(f).strip() not in used
            ]
            used.update(picked)
            if label and picked:
                segments.append({"label": label, "files": picked})
        if segments:
            out[event.id] = segments
    return out, True


def _detect_granularity(
    paths: "MemoryPaths",
    events: list["Event"],
    client: "LLMClient | None",
) -> None:
    """写 granularity.json：规则先筛候选，LLM 给概括与分段。

    没有候选时写空结构（清掉上一轮的陈旧视图）；有候选但 LLM 全部失败时不写，
    保留上一轮的视图而不是把它抹平。
    """
    groups = _merge_candidates(events)
    coarse = _coarse_candidates(events, paths)
    if not groups and not coarse:
        _write_json(paths, granularity_file(paths), {"merged": [], "coarse": []}, "granularity.json")
        return
    if client is None:
        log_line(paths, "deep: 无 LLM，粒度检测跳过（沿用上一轮视图）")
        return

    summaries, merge_ok = _group_summaries(paths, client, groups) if groups else ({}, True)
    segments, segment_ok = _event_segments(paths, client, coarse) if coarse else ({}, True)
    if not merge_ok and not segment_ok:
        return  # 两个 LLM 调用都失败：不动上一轮的 granularity.json

    previous = load_granularity(paths)
    if merge_ok:
        merged_payload = [
            {
                "ids": [event.id for event in group],
                "summary": summaries[group[0].id],
                "anchors_union": sorted(set().union(*(_non_dialog_anchors(e) for e in group))),
            }
            for group in groups
            if group[0].id in summaries
        ]
    else:  # 只有一半失败时，把没跑成的那一半按上一轮原样写回，不因单次失败抹平视图
        merged_payload = [
            {"ids": list(g.ids), "summary": g.summary, "anchors_union": list(g.anchors_union)}
            for g in previous.merged
        ]
    if segment_ok:
        coarse_payload = [{"id": event_id, "segments": items} for event_id, items in segments.items()]
    else:
        coarse_payload = [
            {"id": event_id, "segments": [{"label": s.label, "files": list(s.files)} for s in segs]}
            for event_id, segs in previous.coarse.items()
        ]
    _write_json(
        paths,
        granularity_file(paths),
        {"merged": merged_payload, "coarse": coarse_payload},
        "granularity.json",
    )


# ---------------------------------------------------------------- CLAUDE.md 建议（SPEC §3.15）


def _cluster_texts(pairs: list[tuple[str, str]], threshold: float) -> list[tuple[str, list[str]]]:
    """把 (id, 文本) 按近似重复聚成 (代表文本, 成员 id 列表)。"""
    clusters: list[tuple[str, list[str]]] = []
    for item_id, text in pairs:
        for representative, members in clusters:
            if near_duplicate(representative, text, threshold):
                members.append(item_id)
                break
        else:
            clusters.append((text, [item_id]))
    return clusters


def _promoted_lessons(events: list["Event"], state: dict[str, Any]) -> list[tuple[str, str]]:
    """当前处于 promoted 的 lesson（id, 文本）。"""
    statuses = state.get("events", {})
    out: list[tuple[str, str]] = []
    for event in events:
        text = (getattr(event, "lesson", None) or "").strip()
        record = statuses.get(event.id)
        if text and isinstance(record, dict) and record.get("status") == "promoted":
            out.append((event.id, text))
    return out


def _project_claude_md(paths: "MemoryPaths") -> str:
    """项目 CLAUDE.md 的归一化全文；不存在返回空串。"""
    target = getattr(paths, "project_dir", None)
    if not isinstance(target, Path):
        return ""
    try:
        return _norm((target / "CLAUDE.md").read_text(encoding="utf-8"))
    except OSError:
        return ""


def _write_claude_md_suggestions(
    paths: "MemoryPaths",
    events: list["Event"],
    state: dict[str, Any],
) -> set[str]:
    """写 index/claude-md-suggestions.md；返回已被用户采纳的来源事件 id。

    永不改用户的 CLAUDE.md（SPEC §3.15）：只生成可粘贴的一行，采纳与否由用户决定。
    """
    promoted = _promoted_lessons(events, state)
    claude_md = _project_claude_md(paths) if promoted else ""

    adopted: set[str] = set()
    blocks: list[str] = []
    for index, (text, members) in enumerate(_cluster_texts(promoted, NEAR_DUP_THRESHOLD), start=1):
        flat = _one_line_text(text)
        if claude_md and _norm(flat) in claude_md:
            adopted.update(members)  # 建议文本已进 CLAUDE.md：移出建议、标 (adopted)
            continue
        chain = " ".join(f"[{member}]" for member in members)
        blocks.append(
            f"## {index}. {flat}\n\n- lesson: {flat}\n- 来源: {chain}\n- 可粘贴: - {flat}"
        )

    target = claude_md_suggestions_file(paths)
    if not blocks:  # 没有待采纳建议＝文件不存在（`eventmem status` 据此不提示）
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            log_line(paths, f"deep: CLAUDE.md 建议清理失败 {exc}")
        return adopted

    header = (
        "# CLAUDE.md 晋升建议\n\n"
        "由深整理生成。eventmem 永不自动修改项目的 CLAUDE.md；"
        "把「可粘贴」那一行复制进去即可，下次深整理会自动把它标为 adopted 并从本文件移除。"
    )
    try:
        atomic_write(target, header + "\n\n" + "\n\n".join(blocks) + "\n")
    except OSError as exc:
        log_line(paths, f"deep: CLAUDE.md 建议写入失败 {exc}")
    return adopted


def _one_line_text(text: str, limit: int = 200) -> str:
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


# ---------------------------------------------------------------- 跨项目晋升（SPEC §3.18）


def _scrubbed(text: str) -> str:
    """进 global 的文本必须已过 scrub；scrub 模块不可用时原样返回。"""
    try:
        from eventmem.scrub import scrub
    except Exception:  # noqa: BLE001
        return text
    try:
        return scrub(text)
    except Exception:  # noqa: BLE001
        return text


def _project_id(paths: "MemoryPaths") -> str:
    """项目在 global 状态里的标识：绝对路径的短哈希，不落项目路径本身。"""
    target = getattr(paths, "project_dir", None)
    raw = str(target) if target is not None else str(getattr(paths, "root", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _load_global_state(directory: Path) -> dict[str, Any]:
    try:
        data = json.loads((directory / GLOBAL_STATE_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"candidates": []}
    if not isinstance(data, dict):
        return {"candidates": []}
    candidates = data.get("candidates")
    return {"candidates": [c for c in candidates if isinstance(c, dict)] if isinstance(candidates, list) else []}


def _global_candidate_for(state: dict[str, Any], text: str) -> dict[str, Any] | None:
    for candidate in state["candidates"]:
        if near_duplicate(str(candidate.get("text") or ""), text, GLOBAL_NEAR_DUP_THRESHOLD):
            return candidate
    return None


def _write_global_files(paths: "MemoryPaths", directory: Path, state: dict[str, Any]) -> None:
    """写 global 状态与 global-lessons.md（只放 promoted，它是注入源）。"""
    lines = ["# Global lessons (user level)", ""]
    lines.extend(
        f"- (promoted) {_one_line_text(str(candidate.get('text') or ''))}"
        for candidate in state["candidates"]
        if candidate.get("promoted")
    )
    try:
        atomic_write(directory / GLOBAL_STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        atomic_write(directory / GLOBAL_LESSONS_FILE, "\n".join(lines) + "\n")
    except OSError as exc:
        log_line(paths, f"deep: 用户级 lesson 写入失败 {exc}")


def _promote_global_lessons(
    paths: "MemoryPaths",
    events: list["Event"],
    state: dict[str, Any],
    client: "LLMClient | None",
    now: datetime,
) -> None:
    """promoted lesson → 可移植性判定 → 用户级候选 → ≥2 个项目出现即用户级 promoted。"""
    if not global_lessons_enabled(paths):
        return
    directory = global_dir()
    if directory is None:
        return  # 目录不存在即静默跳过（SPEC §3.18），本模块不创建它
    if client is None:
        log_line(paths, "deep: 无 LLM，可移植性判定跳过")
        return

    project = _project_id(paths)
    global_state = _load_global_state(directory)
    todo: list[tuple[str, str]] = []
    for event_id, text in _promoted_lessons(events, state):
        candidate = _global_candidate_for(global_state, text)
        if candidate is not None and project in (candidate.get("projects") or []):
            continue  # 本项目已登记过这条，不重复判定
        todo.append((event_id, text))
    if not todo:
        return
    todo = todo[:GLOBAL_MAX_JUDGED]

    payload = json.dumps([{"id": i, "lesson": t} for i, t in todo], ensure_ascii=False)
    try:
        answer = client.complete_json(
            PORTABILITY_SYSTEM,
            "LESSONS TO CHECK:\n" + payload + "\n\nReturn the JSON object.",
            max_tokens=512,
        )
    except Exception as exc:  # noqa: BLE001 —— 含 LLMError
        log_line(paths, f"deep: 可移植性判定放弃：{exc}")
        return
    if not isinstance(answer, dict):
        return

    stamp = _naive(now).isoformat(timespec="seconds")
    changed = False
    for event_id, text in todo:
        if answer.get(event_id) is not True:
            continue
        clean = _one_line_text(_scrubbed(text))
        candidate = _global_candidate_for(global_state, clean)
        if candidate is None:
            global_state["candidates"].append(
                {"text": clean, "projects": [project], "first_seen": stamp, "updated": stamp, "promoted": False}
            )
            changed = True
            continue
        projects = candidate.setdefault("projects", [])
        if project not in projects:
            projects.append(project)
            candidate["updated"] = stamp
            changed = True

    for candidate in global_state["candidates"]:
        if not candidate.get("promoted") and len(set(candidate.get("projects") or [])) >= GLOBAL_MIN_PROJECTS:
            candidate["promoted"] = True
            changed = True
    if changed:
        _write_global_files(paths, directory, global_state)


# ---------------------------------------------------------------- 分级遗忘（SPEC §3.19）
#
# 压缩访问结构，不压缩信息。冷却只动派生层：archive-index.md 留一行，事件被 rebuild_all
# 排除在全部索引之外。冻结把 L0 文件整包搬进 archive/——tar 包是 L0 的另一种存放形态，
# 不是删除。events/ 散文件的 unlink 只发生在「打包 → 解包逐字节校验通过 → 纪元摘要落盘」
# 全部成功之后，这是 SPEC 明文允许的唯一一处删除 L0 文件的例外；任何一步失败都回滚
# （删掉半成品包，散文件原样保留）并记日志。


class _ArchiveConfig:
    """分级遗忘的四个可配项；config.yml 缺省即默认值。"""

    __slots__ = ("enabled", "cold_days", "frozen_days", "salience_floor")

    def __init__(self, enabled: bool, cold_days: int, frozen_days: int, salience_floor: float) -> None:
        self.enabled = enabled
        self.cold_days = cold_days
        self.frozen_days = frozen_days
        self.salience_floor = salience_floor


def _archive_config(paths: "MemoryPaths") -> _ArchiveConfig:
    """读 cold_days / frozen_days / salience_floor / archive 开关（缺省 true）。"""
    data = _load_config(paths)

    def _positive_int(key: str, fallback: int) -> int:
        try:
            return max(1, int(data.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    try:
        floor = float(data.get("salience_floor", DEFAULT_SALIENCE_FLOOR))
    except (TypeError, ValueError):
        floor = DEFAULT_SALIENCE_FLOOR
    raw = data.get("archive", True)
    enabled = raw if isinstance(raw, bool) else str(raw).strip().lower() not in ("false", "0", "no", "off")
    return _ArchiveConfig(
        enabled=enabled,
        cold_days=_positive_int("cold_days", DEFAULT_COLD_DAYS),
        frozen_days=_positive_int("frozen_days", DEFAULT_FROZEN_DAYS),
        salience_floor=min(1.0, max(0.0, floor)),
    )


def _int0(value: Any) -> int:
    """宽容取整：非数字一律按 0，脏数据不改变冷却判据的方向。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _thaw_moment(paths: "MemoryPaths", event_id: str) -> datetime | None:
    """解冻时间戳；没解冻过返回 None。"""
    marker = getattr(paths, "thaw_marker", None)
    if not callable(marker):
        return None
    try:
        text = marker(event_id).read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_ts(text.strip())


def _age_days(paths: "MemoryPaths", event_id: str, now: datetime) -> float | None:
    """事件年龄（天）：解冻过的从解冻时刻起算，其余按 id 时间戳；无法定时返回 None。"""
    moment = _thaw_moment(paths, event_id) or id_to_datetime(event_id)
    if moment is None:
        return None
    return (_naive(now) - _naive(moment)).total_seconds() / 86400.0


def _no_cool_ids(events: list["Event"], state: dict[str, Any]) -> set[str]:
    """永不冷却的事件：open、前瞻标记、promoted（含 adopted）lesson 的来源事件。"""
    protected: set[str] = set()
    records = state.get("events") if isinstance(state.get("events"), dict) else {}
    for event_id, record in records.items():
        if isinstance(record, dict) and str(record.get("status") or "") in ("promoted", "adopted"):
            protected.add(str(event_id))
    for event in events:
        if event.status == "open" or getattr(event, "prospective", False):
            protected.add(event.id)
    return protected


def _linked_ids(events: list["Event"], active_ids: set[str]) -> set[str]:
    """被活跃事件的 superseded_by 或 parent 指向的 id——链目标必须可读，故不降级。"""
    linked: set[str] = set()
    for event in events:
        if event.id not in active_ids:
            continue
        for target in (getattr(event, "superseded_by", None), getattr(event, "parent", None)):
            if target:
                linked.add(str(target))
    return linked


def _cool_pass(
    paths: "MemoryPaths",
    events: list["Event"],
    state: dict[str, Any],
    cfg: _ArchiveConfig,
    now: datetime,
) -> list["ArchiveRow"]:
    """挑出本轮该冷却的已闭合事件，判据全部满足才算数（SPEC §3.19）。"""
    archived = load_archive_index(paths)
    salience = load_salience(paths)
    protected = _no_cool_ids(events, state)

    candidates: list["Event"] = []
    for event in events:
        if event.id in archived or event.id in protected or event.status == "open":
            continue
        record = salience.get(event.id)
        if not isinstance(record, dict):
            continue  # 没有显著性记录即证据不足，本轮不冷却
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        if _int0(evidence.get("refs")) > 0 or _int0(evidence.get("hits")) > 0:
            continue
        try:
            score = float(record.get("score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if score >= cfg.salience_floor:
            continue
        age = _age_days(paths, event.id, now)
        if age is None or age <= cfg.cold_days:
            continue
        candidates.append(event)

    if not candidates:
        return []
    # 活跃＝既不在归档索引里，也不在本轮候选里；被活跃事件引用的目标不冷却
    candidate_ids = {event.id for event in candidates}
    active_ids = {e.id for e in events if e.id not in candidate_ids and e.id not in archived}
    linked = _linked_ids(events, active_ids)
    return [
        ArchiveRow(id=event.id, epoch=epoch_of(event.id), intent=event.intent)
        for event in candidates
        if event.id not in linked
    ]


def _prune_granularity(paths: "MemoryPaths", cold_ids: set[str]) -> None:
    """把冷事件从粒度视图里摘掉：granularity 也在「逐出全部索引」的范围内。"""
    view = load_granularity(paths)
    if view.is_empty():
        return
    changed = False
    merged: list[dict[str, Any]] = []
    for group in view.merged:
        ids = [event_id for event_id in group.ids if event_id not in cold_ids]
        if len(ids) != len(group.ids):
            changed = True
        if len(ids) >= 2:  # 只剩一个成员的组没有展示价值
            merged.append(
                {"ids": ids, "summary": group.summary, "anchors_union": list(group.anchors_union)}
            )
    coarse: list[dict[str, Any]] = []
    for event_id, segments in view.coarse.items():
        if event_id in cold_ids:
            changed = True
            continue
        coarse.append(
            {
                "id": event_id,
                "segments": [{"label": s.label, "files": list(s.files)} for s in segments],
            }
        )
    if changed:
        _write_json(paths, granularity_file(paths), {"merged": merged, "coarse": coarse}, "granularity.json")


def _pack_member_bytes(pack: Path, event_id: str) -> bytes | None:
    """从包里取一个成员的原始字节；包损坏或无此成员返回 None。"""
    try:
        with tarfile.open(pack, "r:gz") as archive:
            handle = archive.extractfile(f"{event_id}.md")
            return handle.read() if handle is not None else None
    except (OSError, tarfile.TarError, KeyError):
        return None


def _already_packed(paths: "MemoryPaths", epoch: str, event_id: str, event_path: Path) -> bool:
    """该事件是否已有逐字节相同的副本在本纪元的某个包里。

    内容不同（解冻后又写过 lesson）时返回 False：这份新内容要进续包，绝不能按
    「已归档」删掉。
    """
    try:
        current = event_path.read_bytes()
    except OSError:
        return False
    for pack in paths.epoch_packs(epoch):
        if _pack_member_bytes(pack, event_id) == current:
            return True
    return False


def _next_pack_seq(paths: "MemoryPaths", epoch: str) -> int:
    """本纪元下一个空位：首包 1，之后 2、3……（续包，不合并已有包）。0 表示位置用尽。"""
    for seq in range(1, MAX_PACK_SEQ + 1):
        if not paths.epoch_pack(epoch, seq).is_file():
            return seq
    return 0


def _verify_pack(paths: "MemoryPaths", pack: Path, expected: dict[str, bytes]) -> bool:
    """解包校验：成员数量与 id 逐一比对，且内容逐字节相同。任何不符即失败。"""
    try:
        with tarfile.open(pack, "r:gz") as archive:
            names = archive.getnames()
            if sorted(names) != sorted(expected):
                log_line(
                    paths,
                    f"deep: 归档包校验失败 {pack.name}：成员 {len(names)} 与预期 {len(expected)} 不一致",
                )
                return False
            for name in names:
                handle = archive.extractfile(name)
                if handle is None or handle.read() != expected[name]:
                    log_line(paths, f"deep: 归档包校验失败 {pack.name}：{name} 内容不一致")
                    return False
    except (OSError, tarfile.TarError) as exc:
        log_line(paths, f"deep: 归档包校验异常 {pack.name} {exc}")
        return False
    return True


def _write_pack(
    paths: "MemoryPaths", epoch: str, seq: int, members: list[tuple[str, Path]]
) -> Path | None:
    """写并校验一个临时包；返回临时文件路径（校验已通过），失败返回 None 并已清理。"""
    target = paths.epoch_pack(epoch, seq)
    expected: dict[str, bytes] = {}
    try:  # 临时名带随机后缀：两次整理意外并发时互不覆盖（与 atomic_write 同口径）
        paths.archive_dir.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(
            dir=str(paths.archive_dir), prefix=f".{target.name}.", suffix=".tmp"
        )
        os.close(handle)
        tmp = Path(name)
    except OSError as exc:
        log_line(paths, f"deep: 归档临时文件创建失败 {target.name} {exc}，散文件保留")
        return None
    try:
        for event_id, path in members:
            expected[f"{event_id}.md"] = path.read_bytes()
        with tarfile.open(tmp, "w:gz") as archive:
            for event_id, path in members:
                archive.add(str(path), arcname=f"{event_id}.md")
    except (OSError, tarfile.TarError) as exc:
        log_line(paths, f"deep: 归档包写入失败 {target.name} {exc}，散文件保留")
        tmp.unlink(missing_ok=True)
        return None
    if not _verify_pack(paths, tmp, expected):
        tmp.unlink(missing_ok=True)  # 回滚：半成品包删掉，散文件原样保留
        return None
    return tmp


def _epoch_summary_text(
    paths: "MemoryPaths", client: "LLMClient | None", epoch: str, rows: list["ArchiveRow"]
) -> str:
    """纪元摘要正文；无 LLM 或调用失败时降级为成员 intent 拼接。"""
    fallback = _one_line_text("；".join(row.intent for row in rows), EPOCH_SUMMARY_CHARS)
    if client is None:
        log_line(paths, f"deep: 无 LLM，纪元 {epoch} 摘要降级为 intent 拼接")
        return fallback
    payload = json.dumps(
        {
            "epoch": epoch,
            "events": [{"id": row.id, "intent": row.intent} for row in rows[:EPOCH_PROMPT_MEMBERS]],
        },
        ensure_ascii=False,
    )
    try:
        answer = client.complete_json(
            EPOCH_SYSTEM,
            "EPOCH TO SUMMARIZE:\n" + payload + "\n\nReturn the JSON object.",
            max_tokens=768,
        )
    except Exception as exc:  # noqa: BLE001 —— 含 LLMError
        log_line(paths, f"deep: 纪元 {epoch} 摘要放弃，降级为 intent 拼接：{exc}")
        return fallback
    text = answer.get("summary") if isinstance(answer, dict) else None
    if isinstance(text, str) and text.strip():
        return _one_line_text(text.strip(), EPOCH_SUMMARY_CHARS)
    return fallback


def _append_epoch_summary(
    paths: "MemoryPaths",
    epoch: str,
    pack_name: str,
    summary: str,
    rows: list["ArchiveRow"],
    now: datetime,
) -> bool:
    """把一段摘要与本批成员清单追加进 archive/epoch-<epoch>.md；已写过同一个包即跳过。"""
    target = paths.epoch_summary(epoch)
    try:
        old = target.read_text(encoding="utf-8")
    except OSError:
        old = ""
    if f"包 {pack_name}" in old:
        return True  # 上一轮写到一半留下的区块，重跑时不重复追加
    body = old.rstrip("\n") if old.strip() else f"# 纪元 {epoch}"
    stamp = _naive(now).isoformat(timespec="seconds")
    section = old.count("## 摘要 ") + 1
    lines = [
        body,
        "",
        f"## 摘要 {section}（生成 {stamp}，成员 {len(rows)}，包 {pack_name}）",
        "",
        summary,
        "",
        "### 成员",
        "",
    ]
    lines.extend(f"- {row.id} | {_one_line_text(row.intent, 120)}" for row in rows)
    try:
        paths.archive_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(target, "\n".join(lines) + "\n")
    except OSError as exc:
        log_line(paths, f"deep: 纪元 {epoch} 摘要写入失败 {exc}")
        return False
    return True


def _freeze_pass(
    paths: "MemoryPaths",
    events: list["Event"],
    cfg: _ArchiveConfig,
    client: "LLMClient | None",
    now: datetime,
) -> tuple[int, int]:
    """cold 且年龄过线的事件按季度打包移出 events/；返回 (冻结事件数, 新增包数)。"""
    archived = load_archive_index(paths)
    if not archived:
        return 0, 0
    active_ids = {event.id for event in events if event.id not in archived}
    linked = _linked_ids(events, active_ids)  # 活跃 superseded 链／parent 的目标永不冻结

    groups: dict[str, list["ArchiveRow"]] = {}
    for event_id, row in archived.items():
        if event_id in linked or not paths.event_file(event_id).is_file():
            continue  # 后者：本就已冻结
        age = _age_days(paths, event_id, now)
        if age is None or age <= cfg.frozen_days:
            continue
        groups.setdefault(row.epoch or epoch_of(event_id), []).append(row)

    frozen = 0
    packs = 0
    for epoch in sorted(groups):
        pending: list["ArchiveRow"] = []
        for row in sorted(groups[epoch], key=lambda r: r.id):
            path = paths.event_file(row.id)
            if _already_packed(paths, epoch, row.id, path):
                # 上一轮打包成功但删除中断：包内已有逐字节相同的副本，补删即可
                try:
                    path.unlink()
                    frozen += 1
                except OSError as exc:
                    log_line(paths, f"deep: 冻结后删除散文件失败 {row.id} {exc}")
                continue
            pending.append(row)
        if not pending:
            continue

        seq = _next_pack_seq(paths, epoch)
        if seq == 0:
            log_line(paths, f"deep: 纪元 {epoch} 续包序号用尽，本轮跳过")
            continue
        tmp = _write_pack(paths, epoch, seq, [(row.id, paths.event_file(row.id)) for row in pending])
        if tmp is None:
            continue  # 已回滚
        target = paths.epoch_pack(epoch, seq)
        summary = _epoch_summary_text(paths, client, epoch, pending)
        if not _append_epoch_summary(paths, epoch, target.name, summary, pending, now):
            tmp.unlink(missing_ok=True)  # 摘要写不成即整体回滚，宁可下轮重来
            continue
        try:
            os.replace(tmp, target)
        except OSError as exc:
            log_line(paths, f"deep: 归档包落位失败 {target.name} {exc}，散文件保留")
            tmp.unlink(missing_ok=True)
            continue
        packs += 1
        # 校验通过且摘要已落盘，此时才删散文件——SPEC §3.19 明文的唯一删除例外
        for row in pending:
            try:
                paths.event_file(row.id).unlink()
                frozen += 1
            except OSError as exc:
                log_line(paths, f"deep: 冻结后删除散文件失败 {row.id} {exc}")
    return frozen, packs


def archive_pass(
    paths: "MemoryPaths",
    events: list["Event"],
    state: dict[str, Any],
    client: "LLMClient | None",
    now: datetime,
) -> dict[str, int]:
    """深整理的最后一个 pass：冷却 → 冻结。整份幂等，结果只取决于当前状态。"""
    result = {"cooled": 0, "frozen": 0, "packs": 0}
    cfg = _archive_config(paths)
    if not cfg.enabled:
        return result
    rows = _cool_pass(paths, events, state, cfg, now)
    if rows:
        result["cooled"] = append_archive_rows(paths, rows)
        _prune_granularity(paths, {row.id for row in rows})
    result["frozen"], result["packs"] = _freeze_pass(paths, events, cfg, client, now)
    if result["cooled"] or result["frozen"]:
        log_line(
            paths,
            f"deep: 分级遗忘 冷却 {result['cooled']} 冻结 {result['frozen']} 新包 {result['packs']}",
        )
    return result


def deep(
    store: "Store",
    paths: "MemoryPaths",
    budget: "Budget",
    client: "LLMClient | None",
    now: datetime,
) -> None:
    """脏量达标才执行的分钟级整理。

    顺序有依赖：采纳判定要在预取覆写 prefetch.json 之前读到上一轮的预测；显著性
    重算要在采纳判定之后；预取按显著性排序，因此排在重算之后；三个派生文件都要
    在 rebuild_all 之前写好，工作集才看得到。
    """
    threshold = _deep_threshold(paths, budget)
    dirty = dirty_count(paths)
    if dirty < threshold:
        log_line(paths, f"deep: 脏量 {dirty} < 阈值 {threshold}，跳过")
        return

    events = _safe_events(store, paths)
    if not events:
        return
    stale_ids = _stale_ids(events, budget, now)

    # 1) 埋点消费：采纳判定 ＋ 预取命中记录（纯文件扫描，不调 LLM）
    deltas = _adoption_pass(paths, events)

    # 2) lesson 蒸馏与晋升／退休
    if _distill_lessons(store, paths, client, events):
        events = _safe_events(store, paths)  # lesson 写入后重读

    watermark = _read_int(_deep_watermark(paths))
    ordered = sorted(events, key=lambda e: e.id)
    new_events = ordered[watermark:] if watermark < len(ordered) else []
    state = _update_lesson_state(paths, events, new_events)

    # 3) 显著性后验重算 → 4) 预取 → 5) 粒度视图
    _recompute_salience(paths, events, deltas, now)
    scores = index_salience_scores(paths)
    _write_prefetch(paths, events, scores, client, now, model_level=True)
    _detect_granularity(paths, events, client)

    # 6) CLAUDE.md 建议：被采纳的条目改标 (adopted)，随后不再进工作集
    adopted = _write_claude_md_suggestions(paths, events, state)
    if adopted:
        for event_id in adopted:
            record = state["events"].get(event_id)
            if isinstance(record, dict):
                record["status"] = "adopted"
        _save_lesson_state(paths, state)

    # 7) 跨项目晋升（用户级目录不存在时静默跳过）
    _promote_global_lessons(paths, events, state, client, now)

    # 8) 分级遗忘：冷却与冻结。放在最后、且在 rebuild_all 之前——重建才看得到新的
    # archive-index（逐出冷事件）与已经搬走的 frozen 文件
    archived = archive_pass(paths, events, state, client, now)

    _annotate_lessons(paths, state)  # 重建前写一遍：rebuild_all 沿用文件里的状态
    rebuild_all(store, paths, budget, now)
    if _annotate_lessons(paths, state):  # 本轮新增的 lesson 行由重建补出，再标一次
        rebuild_all(store, paths, budget, now)  # 让 promoted 进入工作集
    _annotate_stale(paths, stale_ids)  # 兜底：rebuild_all 已按 stale_days 标注

    # 水位取当前 events/ 的文件数而非本轮扫到的事件数：冻结会搬走散文件，用旧计数会
    # 让脏量在被搬走的数量补回来之前一直判为 0
    try:
        atomic_write(_deep_watermark(paths), str(_total_events(paths)))
    except OSError as exc:
        log_line(paths, f"deep: 水位写入失败 {exc}")
    log_line(
        paths,
        f"deep: 事件 {len(events)} stale {len(stale_ids)} "
        f"promoted {sum(1 for r in state['events'].values() if r.get('status') == 'promoted')} "
        f"采纳判定 {sum(v['hits'] for v in deltas.values())}/{sum(v['hits'] + v['ignored'] for v in deltas.values())} "
        f"冷却 {archived['cooled']} 冻结 {archived['frozen']}",
    )
