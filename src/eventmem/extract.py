"""transcript → 事件（SPEC §3.7）。

两层：
1. 机械收集（不调 LLM）：TodoWrite 的状态变化→声明式事件开/闭；Task/Agent 调用
   →委托事件（SPEC §3.17）；git commit、报错签名、Read/Edit/Write 的文件路径→锚点。
2. LLM 判断（可选）：机械收集摘要＋对话摘录交给 complete_json，产出补充事件、
   闭合事件的显著性自评（SPEC §3.11）与前瞻标记（SPEC §3.12）。

两层的产物在 store.append 之前一律过 scrub（SPEC §3.14），报错签名的输入文本也先过。

水位存 log/extract-watermark-<session_id>（值为已处理行数），只处理新行。
transcript 行结构一律宽容解析：解析不了的行跳过并计数，不让单行格式炸掉整次抽取。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from eventmem.llm import LLMError
from eventmem.paths import atomic_write
from eventmem.schema import SALIENCE_PRIORS, Anchors, Event, SaliencePrior, new_id
from eventmem.scrub import scrub, scrub_event

if TYPE_CHECKING:  # 仅注解用，避免集成早期的硬依赖
    from eventmem.llm import LLMClient
    from eventmem.paths import MemoryPaths
    from eventmem.store import Store

__all__ = [
    "extract_events",
    "EXTRACT_SYSTEM",
    "load_todo_state",
    "save_todo_state",
    "memory_log_dir",
    "log_line",
]

# ---------------------------------------------------------------- 常量

MAX_EXCERPT_CHARS: Final[int] = 8000
MAX_ENTRY_CHARS: Final[int] = 400
MAX_LLM_EVENTS: Final[int] = 20
MAX_HARVEST_ITEMS: Final[int] = 20
TODO_STATE_FILE: Final[str] = "todo-state.json"
LOG_FILE: Final[str] = "eventmem.log"

# 委托事件（SPEC §3.17）：工具名按 Claude Code 实际取值，Agent 为兼容别名
DELEGATION_TOOLS: Final[frozenset[str]] = frozenset({"Task", "Agent", "Subagent"})  # Subagent 来自 dsh 侧 feed 的通用首字母大写化
MAX_DELEGATION_INTENT: Final[int] = 80  # SPEC §3.17：任务描述首行截 80 字
MAX_DELEGATION_OUTCOME: Final[int] = 200
MAX_DELEGATION_ANCHORS: Final[int] = 20
DELEGATION_BODY_PREFIX: Final[str] = "委托: "

# 前瞻标记（SPEC §3.12）：intent 统一前缀，便于索引层与人一眼分辨
PROSPECTIVE_PREFIX: Final[str] = "下次："
MAX_SALIENCE_REASON: Final[int] = 200

_FILE_TOOLS: Final[frozenset[str]] = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_KINDS: Final[frozenset[str]] = frozenset({"decision", "build", "explore", "fix"})
_OPEN_STATUSES: Final[frozenset[str]] = frozenset({"pending", "in_progress"})
# 模型偶尔给出近义 kind，映射到枚举内；仍无法映射的条目直接丢弃
_KIND_ALIASES: Final[dict[str, str]] = {
    "research": "explore",
    "investigate": "explore",
    "investigation": "explore",
    "experiment": "explore",
    "abandoned": "explore",
    "implement": "build",
    "implementation": "build",
    "feature": "build",
    "refactor": "build",
    "bug": "fix",
    "bugfix": "fix",
    "debug": "fix",
    "error": "fix",
    "choice": "decision",
    "design": "decision",
    "architecture": "decision",
}
_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]\n]{1,60}?\s([0-9a-f]{7,40})\]")
_DIALOG_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<sid>[^#\s]+)#L(?P<a>\d+)-L(?P<b>\d+)$")
_TEST_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(pytest|py\.test|unittest|go\s+test|cargo\s+test|jest|vitest|npm\s+(run\s+)?test|"
    r"pnpm\s+(run\s+)?test|yarn\s+test|make\s+test)\b"
)
_HEX_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{2,}\b")
_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(/[^\s:'\"]+)+")
_TS_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?")
_SAFE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")
# 委托返回文本里的锚点白名单：带扩展名的路径候选、至少含一个 a-f 的短/长 hash
_PATHLIKE_RE: Final[re.Pattern[str]] = re.compile(r"(?:/|\.{1,2}/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9_]{1,10}")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")

# ---------------------------------------------------------------- prompt

EXTRACT_SYSTEM: Final[str] = """你负责从编程任务记录中提取事件。一个事件包含意图、动作与结果。输入包括已由规则解析的事件和带行号的会话片段；只补充规则提取遗漏的真实事件，不重复记录已有项。
无法确认真实闭环时不记录。普通闲聊、未执行的计划、缺少目标的单次工具调用可返回空结果。最多 20 项，不写 lesson。
intent 用中文写目标。kind 为 decision（有选项和依据的决定）、build（做成模块或功能）、explore（验证后有理由放弃的假设）或 fix（找到并处理错误）；优先保留仓库代码无法说明的取舍与探索。status 为 open、done 或 abandoned；仅在片段结束仍未完成时用 open。done/abandoned 的 outcome 写实际结果，open 不写 outcome。
anchors 可含 files、commits、error_sigs、dialog，只引用规则提取资料中原样出现的路径、提交或 <session>#L<start>-L<end>，不编造标识。
已结束事件给出 salience_prior：high 表示值得保留的方案取舍、难找原因或放弃依据，medium 表示改变行为的工作，low 表示常规步骤。salience_reason 用中文简述依据，不重复结果。
明确约定下次要做的具体事项可标 prospective=true、status=open、kind=build，intent 以“下次：”开头，不填 outcome 或 salience_prior。每段最多一至两项，愿望、猜测与已经开工的任务不冒充下次约定。
用准确、完整、简洁的中文描述实际发生的事，不写营销评价。只返回 JSON {"events": [...]}，没有合适事项则 {"events": []}。"""


# ---------------------------------------------------------------- 路径与日志


def memory_log_dir(paths: "MemoryPaths") -> Path:
    """log 目录。兼容 MemoryPaths.log 是目录或是日志文件两种实现。"""
    for attr in ("log_dir", "logs_dir"):
        value = getattr(paths, attr, None)
        if isinstance(value, Path):
            return value
    value = getattr(paths, "log", None)
    if isinstance(value, Path):
        return value.parent if value.suffix else value
    return Path(getattr(paths, "root")) / "log"


def _log_file(paths: "MemoryPaths") -> Path:
    value = getattr(paths, "log", None)
    if isinstance(value, Path) and value.suffix:
        return value
    return memory_log_dir(paths) / LOG_FILE


def log_line(paths: "MemoryPaths", message: str) -> None:
    """护栏日志，尽力而为：写日志本身不得抛异常。"""
    try:
        target = _log_file(paths)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001 —— 日志失败静默
        pass


def _safe_name(text: str) -> str:
    return _SAFE_NAME_RE.sub("_", text)[:80] or "unknown"


# ---------------------------------------------------------------- 配置


def _load_config(paths: "MemoryPaths") -> dict[str, Any]:
    """config.yml 缺省即默认值（SPEC §3.10）；损坏的配置按缺省处理，不阻断抽取。"""
    config = getattr(paths, "config", None)
    if not isinstance(config, Path) or not config.exists():
        return {}
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def scrub_enabled(paths: "MemoryPaths") -> bool:
    """`config.yml: scrub: false` 才关闭清洗，缺省开启（SPEC §3.14）。"""
    value = _load_config(paths).get("scrub", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "off", "0")


# ---------------------------------------------------------------- todo 状态旁路文件


def _todo_state_path(paths: "MemoryPaths") -> Path:
    return memory_log_dir(paths) / TODO_STATE_FILE


def load_todo_state(paths: "MemoryPaths") -> dict[str, dict[str, Any]]:
    """{归一化 todo 文本: {"status", "event_id", "text", "session", "line"}}。

    SPEC 未规定 todo 状态的持久化位置；这里落在 log/ 下，供 consolidate.light
    的规则闭合复用（它不解析 transcript）。
    """
    try:
        raw = _todo_state_path(paths).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 缺失或损坏都按空状态处理
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_todo_state(paths: "MemoryPaths", state: dict[str, dict[str, Any]]) -> None:
    try:
        atomic_write(_todo_state_path(paths), json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        log_line(paths, f"extract: todo 状态写入失败 {exc}")


# ---------------------------------------------------------------- 水位


def _watermark_path(paths: "MemoryPaths", session_id: str) -> Path:
    getter = getattr(paths, "extract_watermark", None)
    if callable(getter):
        try:
            value = getter(session_id)
            if isinstance(value, Path):
                return value
        except Exception:  # noqa: BLE001
            pass
    return memory_log_dir(paths) / f"extract-watermark-{_safe_name(session_id)}"


def _read_watermark(paths: "MemoryPaths", session_id: str) -> int:
    try:
        return max(0, int(_watermark_path(paths, session_id).read_text(encoding="utf-8").strip()))
    except Exception:  # noqa: BLE001
        return 0


def _write_watermark(paths: "MemoryPaths", session_id: str, lines: int) -> None:
    try:
        atomic_write(_watermark_path(paths, session_id), str(max(0, lines)))
    except Exception as exc:  # noqa: BLE001
        log_line(paths, f"extract: 水位写入失败 {exc}")


# ---------------------------------------------------------------- 机械收集


def _prepare(event: Event, scrub_on: bool) -> Event:
    """落盘前的最后一道：清洗敏感信息（SPEC §3.14 唯一入口）。"""
    return scrub_event(event) if scrub_on else event


@dataclass
class _Touch:
    """带行号的锚点观测。"""

    line: int
    value: str


@dataclass
class _Delegation:
    """一次子 agent 委托（Task/Agent 工具调用）及其返回文本（SPEC §3.17）。"""

    tool_use_id: str
    tool: str
    subagent: str
    description: str
    line: int
    result_line: int = 0
    result_text: str = ""


@dataclass
class Harvest:
    """一次窗口内的机械收集结果。"""

    start_line: int = 1
    end_line: int = 0
    total_lines: int = 0
    skipped_lines: int = 0
    scrub_on: bool = True
    todo_snapshots: list[tuple[int, list[dict[str, Any]]]] = field(default_factory=list)
    files: list[_Touch] = field(default_factory=list)
    commits: list[_Touch] = field(default_factory=list)
    errors: list[_Touch] = field(default_factory=list)
    tests: list[_Touch] = field(default_factory=list)
    commands: list[_Touch] = field(default_factory=list)
    delegations: list[_Delegation] = field(default_factory=list)
    excerpt: list[tuple[int, str, str]] = field(default_factory=list)  # (行号, 角色, 文本)

    def is_empty(self) -> bool:
        return not (
            self.todo_snapshots
            or self.files
            or self.commits
            or self.errors
            or self.delegations
            or self.excerpt
        )


def _fallback_error_signature(stderr: str) -> str:
    """recall.error_signature 不可用时的等价实现（SPEC §3.5 规范化规则）。"""
    line = ""
    for candidate in (stderr or "").splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return ""
    line = _TS_RE.sub("<ts>", line)
    line = _PATH_RE.sub("<path>", line)
    line = _HEX_RE.sub("<addr>", line)
    line = _NUM_RE.sub("<n>", line)
    return " ".join(line.split())[:120]


def _error_signature(stderr: str, scrub_on: bool = True) -> str:
    """优先用 recall 的实现，保持与索引侧签名一致；输入文本先清洗（SPEC §3.14）。"""
    text = scrub(stderr) if scrub_on else stderr
    try:
        from eventmem.recall import error_signature as _impl  # 延迟导入，缺失则降级
    except Exception:  # noqa: BLE001
        return _fallback_error_signature(text)
    try:
        return _impl(text)
    except Exception:  # noqa: BLE001
        return _fallback_error_signature(text)


def _rel_path(raw: str, paths: "MemoryPaths") -> str:
    """文件锚点存项目相对路径，与 index 的 file: 倒排 key 保持同一形态。"""
    text = (raw or "").strip()
    if not text:
        return ""
    relative = getattr(paths, "relative", None)
    if callable(relative):
        try:
            return str(relative(text))
        except Exception:  # noqa: BLE001
            pass
    root = getattr(paths, "root", None)
    if isinstance(root, Path):
        try:
            return Path(text).resolve().relative_to(root.parent.resolve()).as_posix()
        except Exception:  # noqa: BLE001 —— 项目外路径原样保留
            pass
    return text


def _as_text(value: Any) -> str:
    """tool_result / message content 可能是 str、list[block]、dict，统一取文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "stdout", "content", "output"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
    return ""


def _iter_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """取一行记录里的 content block 列表（宽容：str content 视为无 block）。"""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _plain_text(record: dict[str, Any]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        return "\n".join(p for p in parts if p.strip())
    return ""


def scan_transcript(transcript_path: Path, start_line: int, paths: "MemoryPaths") -> Harvest:
    """逐行宽容解析 jsonl，收集锚点与 todo 快照。行号为 1-based。"""
    harvest = Harvest(start_line=max(1, start_line + 1), scrub_on=scrub_enabled(paths))
    pending_tools: dict[str, tuple[str, dict[str, Any], int]] = {}  # tool_use_id -> (名称, 入参, 行号)
    try:
        handle = transcript_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return harvest
    with handle:
        for index, raw in enumerate(handle, start=1):
            harvest.total_lines = index
            if index <= start_line:
                continue
            harvest.end_line = index
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                harvest.skipped_lines += 1
                continue
            if not isinstance(record, dict):
                harvest.skipped_lines += 1
                continue
            try:
                _scan_record(record, index, harvest, pending_tools, paths)
            except Exception:  # noqa: BLE001 —— 单行结构异常不影响整次抽取
                harvest.skipped_lines += 1
    if harvest.end_line < harvest.start_line:
        harvest.end_line = harvest.total_lines
    return harvest


def _scan_record(
    record: dict[str, Any],
    line: int,
    harvest: Harvest,
    pending_tools: dict[str, tuple[str, dict[str, Any], int]],
    paths: "MemoryPaths",
) -> None:
    kind = record.get("type") or record.get("role") or ""
    role = "assistant" if kind == "assistant" else "user" if kind == "user" else str(kind)

    # 顶层 toolUseResult（Claude Code 对 Bash 等工具的结构化结果）
    tool_result = record.get("toolUseResult")
    if isinstance(tool_result, dict):
        stderr = tool_result.get("stderr")
        if isinstance(stderr, str) and stderr.strip():
            _record_error(stderr, line, harvest)
        stdout = tool_result.get("stdout")
        if isinstance(stdout, str):
            _record_commit(stdout, line, harvest)

    for block in _iter_blocks(record):
        btype = block.get("type")
        if btype == "tool_use":
            name = str(block.get("name") or "")
            payload = block.get("input")
            payload = payload if isinstance(payload, dict) else {}
            tool_id = block.get("id")
            if isinstance(tool_id, str):
                pending_tools[tool_id] = (name, payload, line)
            _scan_tool_use(name, payload, line, harvest, paths, tool_id)
        elif btype == "tool_result":
            text = _as_text(block.get("content"))
            result_id = block.get("tool_use_id")
            origin = pending_tools.get(result_id, ("", {}, line))
            if block.get("is_error") or _looks_like_error(text):
                _record_error(text, line, harvest)
            if origin[0] == "Bash":
                _record_commit(text, line, harvest)
            if origin[0] in DELEGATION_TOOLS:
                _record_delegation_result(str(result_id), text, line, harvest)
        elif btype == "text":
            pass  # 文本摘录统一在下方处理

    text = _plain_text(record)
    if text.strip() and role in ("user", "assistant"):
        harvest.excerpt.append((line, role, text.strip()[:MAX_ENTRY_CHARS]))


def _scan_tool_use(
    name: str,
    payload: dict[str, Any],
    line: int,
    harvest: Harvest,
    paths: "MemoryPaths",
    tool_id: str | None = None,
) -> None:
    if name in DELEGATION_TOOLS:
        _record_delegation(name, payload, line, harvest, tool_id)
        return
    if name == "TodoWrite":
        todos = payload.get("todos")
        if isinstance(todos, list):
            items = [t for t in todos if isinstance(t, dict)]
            if items:
                harvest.todo_snapshots.append((line, items))
        return
    if name in _FILE_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                rel = _rel_path(value, paths)
                if rel:
                    harvest.files.append(_Touch(line, rel))
                break
        return
    if name == "Bash":
        command = payload.get("command")
        if isinstance(command, str) and command.strip():
            flat = " ".join(command.split())
            harvest.commands.append(_Touch(line, flat[:200]))
            if "git commit" in flat:
                harvest.commits.append(_Touch(line, "pending"))
            if _TEST_RE.search(flat):
                harvest.tests.append(_Touch(line, flat[:120]))


def _looks_like_error(text: str) -> bool:
    head = (text or "")[:400].lower()
    return any(
        marker in head
        for marker in ("traceback (most recent call last)", "error:", "exception:", "fatal:", "command failed")
    )


def _record_error(text: str, line: int, harvest: Harvest) -> None:
    """同一行只记一个签名：结构化 stderr 与 tool_result 文本常是同一个错误。"""
    if any(touch.line == line for touch in harvest.errors):
        return
    signature = _error_signature(text, harvest.scrub_on)
    if signature:
        harvest.errors.append(_Touch(line, signature))


def _record_commit(text: str, line: int, harvest: Harvest) -> None:
    """从 git commit 的输出里取短 hash，替换掉占位的 pending 记录。"""
    match = _COMMIT_RE.search(text or "")
    if not match:
        return
    sha = match.group(1)
    for touch in harvest.commits:
        if touch.value == "pending":
            touch.value = sha
            return
    harvest.commits.append(_Touch(line, sha))


# ---------------------------------------------------------------- 委托（SPEC §3.17）


def _first_line(text: str, limit: int) -> str:
    """取首个非空行并截断；用于把任务描述与返回摘要压成单行。"""
    for candidate in (text or "").splitlines():
        stripped = candidate.strip()
        if stripped:
            return stripped[:limit]
    return ""


def _record_delegation(
    name: str,
    payload: dict[str, Any],
    line: int,
    harvest: Harvest,
    tool_id: str | None,
) -> None:
    """记下一次 Task/Agent 调用；返回文本到达时再补齐（见 _record_delegation_result）。"""
    description = ""
    for key in ("description", "prompt", "task", "instructions"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            description = value
            break
    if not description:
        return
    subagent = payload.get("subagent_type")
    harvest.delegations.append(
        _Delegation(
            tool_use_id=tool_id or f"__line{line}",
            tool=name,
            subagent=str(subagent).strip() if isinstance(subagent, str) and subagent.strip() else "",
            description=description,
            line=line,
        )
    )


def _record_delegation_result(tool_use_id: str, text: str, line: int, harvest: Harvest) -> None:
    """把返回文本挂回对应的委托调用；子 agent 的 transcript 不解析（SPEC §3.17 已知限制）。"""
    for delegation in harvest.delegations:
        if delegation.tool_use_id == tool_use_id and not delegation.result_text:
            delegation.result_text = text or ""
            delegation.result_line = line
            return


def _delegation_anchors(
    delegation: _Delegation,
    text: str,
    session_id: str,
    paths: "MemoryPaths",
) -> Anchors:
    """从返回文本抽锚点：项目内确实存在的文件路径、40 位内的 hex commit。

    白名单是「存在性」而非「像不像」：子 agent 的返回摘要里编造的路径直接落空。
    入参 text 已清洗，否则密钥里的一段 hex 会被当成 commit 写进锚点（锚点不在
    scrub 的作用域内，只能在进来之前挡住）。
    """
    project_dir = getattr(paths, "project_dir", None)
    files: list[str] = []
    for candidate in _PATHLIKE_RE.findall(text):
        rel = _rel_path(candidate, paths)
        # 规约后仍是绝对路径的，说明落在项目之外，不收
        if not rel or rel in files or Path(rel).is_absolute() or project_dir is None:
            continue
        if (project_dir / rel).is_file():
            files.append(rel)
        if len(files) >= MAX_DELEGATION_ANCHORS:
            break
    commits = sorted(set(_SHA_RE.findall(text)))[:MAX_DELEGATION_ANCHORS]
    end = delegation.result_line or delegation.line
    return Anchors(
        commits=commits,
        files=sorted(files),
        tests=[],
        dialog=[f"{session_id}#L{delegation.line}-L{end}"],
        error_sigs=[],
    )


def _delegation_events(
    harvest: Harvest,
    store: "Store",
    paths: "MemoryPaths",
    session_id: str,
    now: datetime,
    existing: set[str],
) -> tuple[list[str], list[str]]:
    """把本窗口的每次委托落成一个事件，返回 (新事件 id 列表, 已记录的 intent 列表)。

    kind 取默认值 build；委托的实际性质要看子 agent 内部，主 transcript 无从判断。
    """
    created: list[str] = []
    intents: list[str] = []
    for delegation in harvest.delegations:
        intent = _first_line(delegation.description, MAX_DELEGATION_INTENT)
        if len(intent) < 4:
            continue
        result = delegation.result_text or ""
        if harvest.scrub_on:
            result = scrub(result)
        outcome = _first_line(result, MAX_DELEGATION_OUTCOME)
        event = Event(
            id=new_id(now, existing),
            parent=None,
            kind="build",
            status="done" if outcome else "open",
            superseded_by=None,
            intent=intent,
            anchors=_delegation_anchors(delegation, result, session_id, paths),
            outcome=outcome or None,
            lesson=None,
            body=f"{DELEGATION_BODY_PREFIX}{delegation.subagent or delegation.tool}",
        )
        try:
            event_id = store.append(_prepare(event, harvest.scrub_on))
        except Exception as exc:  # noqa: BLE001
            log_line(paths, f"extract: 委托事件写入失败 {intent[:40]!r} {exc}")
            continue
        existing.add(event_id)
        created.append(event_id)
        intents.append(intent)
    return created, intents


# ---------------------------------------------------------------- 声明式事件（todo）


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


@dataclass
class _TodoPlan:
    key: str
    text: str
    event_id: str | None = None
    open_line: int | None = None
    close_line: int | None = None
    final_status: str = "pending"
    changed: bool = False


def _plan_todos(
    harvest: Harvest,
    state: dict[str, dict[str, Any]],
    open_by_intent: dict[str, str],
) -> tuple[list[_TodoPlan], list[tuple[int, str | None]]]:
    """比对 todo 快照与已知状态，产出本窗口的开/闭计划与 in_progress 时间线。

    只复用仍处于 open 的事件：同名 todo 在新会话里重新出现时另开一个事件，
    不把新一轮的锚点挂到上一轮已闭合的事件上。

    返回 (plans, spans)。spans 为 [(快照行号, 该时刻 in_progress 的 todo key 或 None)]，
    用于把文件／commit／报错锚点归属到当时在做的那一条 todo。
    """
    plans: dict[str, _TodoPlan] = {}
    spans: list[tuple[int, str | None]] = []
    open_ids = set(open_by_intent.values())
    known: dict[str, str] = {key: str(value.get("status") or "") for key, value in state.items()}
    for line, items in harvest.todo_snapshots:
        active: str | None = None
        for item in items:
            text = item.get("content") or item.get("activeForm") or item.get("task") or ""
            text = str(text).strip()
            if not text:
                continue
            key = _norm(text)
            status = str(item.get("status") or "pending").strip().lower()
            if status not in _OPEN_STATUSES and status != "completed":
                status = "pending"
            plan = plans.get(key)
            if plan is None:
                prior = state.get(key) or {}
                prior_id = prior.get("event_id")
                reusable = prior_id if prior_id in open_ids else open_by_intent.get(key)
                plan = _TodoPlan(
                    key=key,
                    text=text,
                    event_id=reusable,
                    final_status=known.get(key, "") if prior_id in open_ids else "",
                )
                plans[key] = plan
            if status == "in_progress" and active is None:
                active = key
            if status == known.get(key, ""):
                continue  # 无状态变化
            known[key] = status
            plan.changed = True
            plan.final_status = status
            if status in _OPEN_STATUSES and plan.open_line is None:
                plan.open_line = line
            if status == "completed":
                plan.close_line = line
        spans.append((line, active))
    return [p for p in plans.values() if p.changed], spans


def _owner_at(line: int, spans: list[tuple[int, str | None]]) -> str | None:
    """某一行的锚点归属：取最近一次 in_progress 标记；之前没有则向后看一次。

    向后看是为了兼容「先动手、后标 in_progress」的写法。
    """
    owner: str | None = None
    for span_line, key in spans:
        if span_line <= line:
            owner = key
        else:
            break
    if owner is not None:
        return owner
    for span_line, key in spans:
        if span_line > line and key is not None:
            return key
    return None


def _window_anchors(
    harvest: Harvest,
    start: int,
    end: int,
    session_id: str,
    key: str | None = None,
    spans: list[tuple[int, str | None]] | None = None,
) -> Anchors:
    """取 [start, end] 行窗口内的锚点，dialog 用区间指针。

    spans 里存在 in_progress 标记时，只收归属于本 todo 的锚点，避免同窗口内
    并存的多个 todo 互相沾染彼此的文件与 commit。
    """
    lo, hi = min(start, end), max(start, end)
    by_owner = bool(key and spans and any(owner for _, owner in spans))

    def pick(touches: list[_Touch]) -> list[str]:
        values: set[str] = set()
        for touch in touches:
            if not (lo <= touch.line <= hi) or not touch.value or touch.value == "pending":
                continue
            if by_owner and _owner_at(touch.line, spans or []) != key:
                continue
            values.add(touch.value)
        return sorted(values)

    return Anchors(
        commits=pick(harvest.commits),
        files=pick(harvest.files),
        tests=pick(harvest.tests),
        dialog=[f"{session_id}#L{lo}-L{hi}"],
        error_sigs=pick(harvest.errors),
    )


def _apply_todo_plans(
    plans: list[_TodoPlan],
    spans: list[tuple[int, str | None]],
    harvest: Harvest,
    store: "Store",
    paths: "MemoryPaths",
    session_id: str,
    now: datetime,
    existing: set[str],
    state: dict[str, dict[str, Any]],
) -> list[str]:
    """落盘声明式事件：新开事件、补锚点；闭合信号只记状态，由轻整理带 outcome 闭合。

    Store.close 要求写入 outcome，而机械层给不出结论；写空 outcome 后无接口可补，
    因此这里不闭合（SPEC §3.8 light-2 正是「todo 已 completed 但事件仍 open」这条规则）。
    """
    created: list[str] = []
    for plan in plans:
        start = plan.open_line or harvest.start_line
        end = plan.close_line or harvest.end_line or start
        anchors = _window_anchors(harvest, start, end, session_id, plan.key, spans)
        event_id = plan.event_id

        if event_id is None:
            event = Event(
                id=new_id(now, existing),
                parent=None,
                kind="build",
                status="open",
                superseded_by=None,
                intent=plan.text,
                anchors=anchors,
                outcome=None,
                lesson=None,
                body="",
            )
            try:
                event_id = store.append(_prepare(event, harvest.scrub_on))
            except Exception as exc:  # noqa: BLE001
                log_line(paths, f"extract: 事件写入失败 {plan.text[:40]!r} {exc}")
                continue
            existing.add(event_id)
            created.append(event_id)
        else:
            try:
                store.add_anchors(event_id, anchors)
            except Exception as exc:  # noqa: BLE001 —— 事件文件缺失等情况不阻断
                log_line(paths, f"extract: 锚点追加失败 {event_id} {exc}")

        state[plan.key] = {
            "status": plan.final_status,
            "event_id": event_id,
            "text": plan.text,
            "session": session_id,
            "line": end,
        }
    return created


# ---------------------------------------------------------------- LLM 层


def _render_harvest(harvest: Harvest, session_id: str, mech_intents: list[str]) -> str:
    def block(title: str, values: list[str]) -> str:
        picked: list[str] = []
        for value in values:
            if value and value not in picked:
                picked.append(value)
            if len(picked) >= MAX_HARVEST_ITEMS:
                break
        return f"{title}:\n" + ("\n".join(f"  - {v}" for v in picked) if picked else "  (none)")

    parts = [
        f"session_id: {session_id}",
        f"transcript lines in this window: L{harvest.start_line}-L{harvest.end_line} "
        f"({harvest.skipped_lines} unparsable lines skipped)",
        block("events already recorded (do not repeat these)", mech_intents),
        block("files touched", [t.value for t in harvest.files]),
        block("commits", [t.value for t in harvest.commits if t.value != "pending"]),
        block("error signatures", [t.value for t in harvest.errors]),
        block("test commands", [t.value for t in harvest.tests]),
        block("shell commands", [t.value for t in harvest.commands]),
    ]
    return "\n\n".join(parts)


def _render_excerpt(harvest: Harvest, budget: int) -> str:
    """按新近度倒着填进 budget，再按行号升序输出。"""
    picked: list[tuple[int, str]] = []
    used = 0
    for line, role, text in reversed(harvest.excerpt):
        entry = f"L{line} {role}: {text}"
        if used + len(entry) > budget:
            break
        picked.append((line, entry))
        used += len(entry) + 1
    picked.sort(key=lambda item: item[0])
    if not picked:
        return "(no dialogue captured in this window)"
    head = "" if len(picked) == len(harvest.excerpt) else "(earlier turns omitted)\n"
    return head + "\n".join(entry for _, entry in picked)


def _coerce_kind(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    if value in _KINDS:
        return value
    return _KIND_ALIASES.get(value)


def _coerce_bool(raw: Any) -> bool:
    """模型给 true / "true" / "yes" 都算真，其余一律假。"""
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("true", "yes", "1")


def _coerce_prior(raw: Any) -> SaliencePrior | None:
    """显著性先验只认三档枚举；非法取值丢字段，不丢事件（SPEC §3.11）。"""
    value = str(raw or "").strip().lower()
    return value if value in SALIENCE_PRIORS else None  # type: ignore[return-value]


def _coerce_anchors(
    raw: Any,
    harvest: Harvest,
    session_id: str,
) -> Anchors:
    """锚点只接受机械收集里出现过的值，杜绝模型编造路径与 hash。"""
    allowed_files = {t.value for t in harvest.files}
    allowed_commits = {t.value for t in harvest.commits if t.value != "pending"}
    allowed_errors = {t.value for t in harvest.errors}
    allowed_tests = {t.value for t in harvest.tests}
    payload = raw if isinstance(raw, dict) else {}

    def keep(key: str, allowed: set[str]) -> list[str]:
        values = payload.get(key)
        if not isinstance(values, list):
            return []
        return sorted({str(v).strip() for v in values if isinstance(v, str) and str(v).strip() in allowed})

    dialog: list[str] = []
    raw_dialog = payload.get("dialog")
    if isinstance(raw_dialog, list):
        for pointer in raw_dialog:
            match = _DIALOG_RE.match(str(pointer).strip()) if isinstance(pointer, str) else None
            if not match or match.group("sid") != session_id:
                continue
            start, end = int(match.group("a")), int(match.group("b"))
            if start > end or end > max(harvest.end_line, harvest.total_lines):
                continue
            dialog.append(f"{session_id}#L{start}-L{end}")
    if not dialog:
        dialog = [f"{session_id}#L{harvest.start_line}-L{harvest.end_line}"]

    return Anchors(
        commits=keep("commits", allowed_commits),
        files=keep("files", allowed_files),
        tests=keep("tests", allowed_tests),
        dialog=dialog[:3],
        error_sigs=keep("error_sigs", allowed_errors),
    )


def _coerce_event(
    item: Any,
    harvest: Harvest,
    session_id: str,
    now: datetime,
    existing: set[str],
    taken_intents: set[str],
) -> Event | None:
    """把 LLM 的一条输出转成 Event；不合格返回 None（宁漏勿滥）。"""
    if not isinstance(item, dict):
        return None
    intent = str(item.get("intent") or "").strip()
    if len(intent) < 4:
        return None
    prospective = _coerce_bool(item.get("prospective"))
    if prospective and not intent.startswith(PROSPECTIVE_PREFIX):
        intent = PROSPECTIVE_PREFIX + intent
    if _norm(intent) in taken_intents:
        return None
    # 前瞻标记的三个字段由 SPEC §3.12 定死，不看模型给的 kind／status
    kind = "build" if prospective else _coerce_kind(item.get("kind"))
    if kind is None:
        return None
    status = str(item.get("status") or "").strip().lower()
    outcome_raw = item.get("outcome")
    outcome = str(outcome_raw).strip() if isinstance(outcome_raw, str) else ""
    if status not in ("open", "done", "abandoned"):
        status = "done" if outcome else "open"
    if prospective:
        status = "open"
    if status == "open":
        outcome = ""
    parent = item.get("parent")
    parent_id = parent if isinstance(parent, str) and parent in existing else None
    body_raw = item.get("body") or item.get("actions")
    body = str(body_raw).strip()[:800] if isinstance(body_raw, str) else ""
    # 自评是闭合时的动作：open 事件（含前瞻标记）不带先验
    prior = None if status == "open" else _coerce_prior(item.get("salience_prior"))
    reason_raw = item.get("salience_reason")
    reason = str(reason_raw).strip()[:MAX_SALIENCE_REASON] if isinstance(reason_raw, str) else ""
    return Event(
        id=new_id(now, existing),
        parent=parent_id,
        kind=kind,
        status=status,
        superseded_by=None,
        intent=intent[:500],
        anchors=_coerce_anchors(item.get("anchors"), harvest, session_id),
        outcome=(outcome[:500] or None) if status != "open" else None,
        lesson=None,  # lesson 由深整理写，抽取层一律留空
        body=body,
        salience_prior=prior,
        salience_reason=reason if (prior is not None and reason) else None,
        prospective=prospective,
    )


def _llm_events(payload: Any) -> list[Any]:
    """容忍 {"events": [...]} 与裸数组两种返回形态。"""
    if isinstance(payload, dict):
        for key in ("events", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []
    return payload if isinstance(payload, list) else []


def _run_llm_phase(
    harvest: Harvest,
    store: "Store",
    paths: "MemoryPaths",
    client: "LLMClient",
    session_id: str,
    now: datetime,
    existing: set[str],
    taken_intents: set[str],
    mech_intents: list[str],
) -> list[str]:
    summary = _render_harvest(harvest, session_id, mech_intents)
    budget = max(1000, MAX_EXCERPT_CHARS - len(summary))
    user = (
        summary
        + "\n\nTRANSCRIPT EXCERPT (line-numbered):\n"
        + _render_excerpt(harvest, budget)
        + "\n\nReport the events this harvest missed, as JSON."
    )
    payload = client.complete_json(EXTRACT_SYSTEM, user, max_tokens=4096)
    created: list[str] = []
    for item in _llm_events(payload)[:MAX_LLM_EVENTS]:
        event = _coerce_event(item, harvest, session_id, now, existing, taken_intents)
        if event is None:
            continue
        try:
            event_id = store.append(_prepare(event, harvest.scrub_on))
        except Exception as exc:  # noqa: BLE001
            log_line(paths, f"extract: LLM 事件写入失败 {exc}")
            continue
        existing.add(event_id)
        taken_intents.add(_norm(event.intent))
        created.append(event_id)
    return created


# ---------------------------------------------------------------- 入口


def extract_events(
    transcript_path: Path,
    store: "Store",
    client: "LLMClient | None",
    session_id: str,
    now: datetime,
) -> list[str]:
    from .locking import exclusive
    with exclusive(_paths_of(store).root / 'extract.lock'):
        return _extract_events(transcript_path, store, client, session_id, now)


def _extract_events(transcript_path, store, client, session_id, now):
    """从 transcript 抽取事件，返回新事件 id 列表。client 为 None 即纯机械模式。"""
    paths = _paths_of(store)
    transcript_path = Path(transcript_path)
    watermark = _read_watermark(paths, session_id)
    model_path = _watermark_path(paths, session_id).with_name(_watermark_path(paths, session_id).name + '.model')
    try:
        model_watermark = max(0, int(model_path.read_text()))
    except (OSError, ValueError):
        model_watermark = 0
    harvest = scan_transcript(transcript_path, watermark, paths)

    if harvest.total_lines < watermark:  # transcript 被截断或替换：水位重置重扫
        watermark = 0
        harvest = scan_transcript(transcript_path, 0, paths)
    if harvest.total_lines < model_watermark:
        model_watermark = 0
    if harvest.total_lines <= watermark and (client is None or harvest.total_lines <= model_watermark):
        return []  # 无新行

    existing: set[str] = set()
    open_by_intent: dict[str, str] = {}
    recent: list[str] = []  # 最近的事件 intent，进 prompt 供模型避开重复
    try:
        for event in store.iter_events():
            existing.add(event.id)
            recent.append(f"({event.status}) {event.intent}")
            if event.status == "open":
                open_by_intent[_norm(event.intent)] = event.id
    except Exception as exc:  # noqa: BLE001 —— 空库或读失败都按空处理
        log_line(paths, f"extract: 事件库读取失败 {exc}")

    state = load_todo_state(paths)
    plans, spans = _plan_todos(harvest, state, open_by_intent)
    created = _apply_todo_plans(plans, spans, harvest, store, paths, session_id, now, existing, state)
    save_todo_state(paths, state)

    delegated_ids, delegated_intents = _delegation_events(harvest, store, paths, session_id, now, existing)
    created += delegated_ids

    mech_intents = [p.text for p in plans] + delegated_intents + recent[-MAX_HARVEST_ITEMS:][::-1]
    taken_intents = (
        set(open_by_intent)
        | {_norm(p.text) for p in plans}
        | {_norm(text) for text in delegated_intents}
    )

    model_harvest = scan_transcript(transcript_path, model_watermark, paths) if client is not None else harvest
    if client is not None and not model_harvest.is_empty():
        try:
            created += _run_llm_phase(
                model_harvest, store, paths, client, session_id, now, existing, taken_intents, mech_intents
            )
            atomic_write(model_path, str(model_harvest.total_lines))
        except LLMError as exc:
            log_line(paths, f"extract: LLM 判断放弃（机械结果已落盘）：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 抽取不得中断 flush
            log_line(paths, f"extract: LLM 判断异常放弃：{exc}")

    _write_watermark(paths, session_id, harvest.total_lines)
    log_line(
        paths,
        f"extract: session={session_id} 行 {harvest.start_line}-{harvest.total_lines} "
        f"新事件 {len(created)} 跳过行 {harvest.skipped_lines}",
    )
    return created


def _paths_of(store: "Store") -> "MemoryPaths":
    """Store 由 MemoryPaths 构造；兼容公开属性与下划线属性两种写法。"""
    for attr in ("paths", "_paths"):
        value = getattr(store, attr, None)
        if value is not None and hasattr(value, "root"):
            return value
    raise AttributeError("Store 未暴露 MemoryPaths（需要 store.paths）")
