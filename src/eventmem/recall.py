"""召回层：主路径是锚点精确命中的线索浮现，兜底是 BM25 文本检索。

不引任何检索或向量库；surface 只查倒排表，目标毫秒级，不调用 LLM。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from .index import (
    Budget,
    Granularity,
    anchor_key,
    intent_tokens,
    load_anchor_map,
    load_archive_index,
    load_granularity,
    load_project_rows,
    salience_scores,
    tokenize,
)
from .paths import MemoryPaths
from .schema import Event
from .store import EventNotFound, SchemaError, Store

CueKind = Literal["file", "error", "intent"]

# 浮现与检索单行的截断长度
LINE_CHARS = 120
# 错误签名截断长度
SIGNATURE_CHARS = 120

# 状态权重：已闭合的经验价值高于进行中的，被推翻的最低（DESIGN §2.4）
_STATUS_WEIGHT: Mapping[str, int] = MappingProxyType(
    {"done": 3, "abandoned": 2, "open": 1, "superseded": 0}
)

# salience.json 存在但尚未覆盖到某事件时的取值（相当于 medium 先验、零证据）：
# 上次深整理之后新建的事件走这条路，取中间值以免把最新的事件压到最后
MISSING_SALIENCE = 0.175

_BM25_K1 = 1.5
_BM25_B = 0.75

# 规范化：时间戳 → 路径 → 行号 → 十六进制地址，顺序不可换（后者会吃掉前者的数字）
_TS_FULL_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_TS_CLOCK_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
_POSIX_PATH_RE = re.compile(r"(?<![\w:/])(?:/[^/\s'\"`:,;()\[\]]+)+")
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s'\"`,;()\[\]]+\\?)+")
_LINENO_WORD_RE = re.compile(r"\bline \d+", re.IGNORECASE)
_LINENO_COLON_RE = re.compile(r"(\.[A-Za-z]{1,5}):\d+(?::\d+)?")
_HEX_ADDR_RE = re.compile(r"\b0[xX][0-9a-fA-F]+")
_TRACEBACK_HEAD = "Traceback (most recent call last)"


@dataclass(frozen=True)
class SurfaceHit:
    """一条浮现结果：事件 id ＋ 注入用的单行。"""

    event_id: str
    line: str


def surface(
    cue: str,
    kind: CueKind,
    store: Store,
    paths: MemoryPaths,
    budget: Budget,
    seen: set[str],
) -> list[SurfaceHit]:
    """线索浮现：锚点精确命中 → 过滤 seen → 排序 → 截 surface_k。

    排序键为（salience 降序，锚点重合数降序，新近度降序）——重合数是相关性信号，
    排在新近度之前才可能生效（事件 id 唯一，新近度不存在并列）；salience.json
    缺失时退回（状态权重，重合数，新近度）。
    """
    keys = _cue_keys(cue, kind, paths)
    if not keys:
        return []
    anchor_map = load_anchor_map(paths)

    overlap: Counter[str] = Counter()
    for key in keys:
        for event_id in anchor_map.get(key, []):
            overlap[event_id] += 1
    if not overlap:
        return []

    granularity = load_granularity(paths)
    scores = salience_scores(paths)

    events: list[tuple[Event, int]] = []
    for event_id, hits in overlap.items():
        if _is_seen(event_id, seen, granularity):  # 组内已 seen 任一成员则整组去重
            continue
        try:
            event = store.read(event_id)
            if event.status != 'superseded':
                events.append((event, hits))
        except (EventNotFound, SchemaError):
            continue  # 索引比存储旧时跳过，宁漏勿胀
    if not events:
        return []

    if scores:
        events.sort(
            key=lambda pair: (_salience_of(pair[0].id, scores, granularity), pair[1], pair[0].id),
            reverse=True,
        )
    else:
        events.sort(
            key=lambda pair: (_STATUS_WEIGHT.get(pair[0].status, 0), pair[1], pair[0].id),
            reverse=True,
        )

    cue_file = paths.relative(cue.strip()) if kind == "file" else ""
    hits_out: list[SurfaceHit] = []
    emitted: set[str] = set()
    for event, _ in events:
        group = granularity.group_of(event.id)
        if group is not None:
            if group.key in emitted:
                continue  # 同一组的多个成员命中同一线索时只出一行
            emitted.add(group.key)
            hits_out.append(SurfaceHit(event_id=group.key, line=group.line))
        else:
            hits_out.append(SurfaceHit(event_id=event.id, line=_hit_line(event, granularity, cue_file)))
        if len(hits_out) >= budget.surface_k:
            break
    return hits_out


def search(query: str, store: Store, paths: MemoryPaths, top: int = 10) -> list[SurfaceHit]:
    """兜底检索：对 project.md 各行的 intent＋outcome 域做 BM25，返回 outcome 单行。"""
    events = _search_corpus(store, paths)
    if not events:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    docs = [tokenize(f"{e.intent} {e.outcome or ''}") for e in events]
    scores = _bm25(docs, query_tokens)
    ranked = [(score, e) for score, e in zip(scores, events) if score > 0.0]
    ranked.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
    return [SurfaceHit(event_id=e.id, line=_hit_line(e)) for _, e in ranked[:top]]


def search_archive(query: str, paths: MemoryPaths, top: int = 10) -> list[SurfaceHit]:
    """归档层检索（SPEC §3.19 的 `--all`）：只读 archive-index 行与纪元摘要文本。

    不解包：frozen 事件的原文在 tar 包里，这里能给出的最多是「哪一行、哪一个纪元」，
    要读原文得先 thaw。结果行一律以 [archived] 结尾，与活跃层的行区分开。
    """
    rows = list(load_archive_index(paths).values())
    epochs = _epoch_texts(paths)
    if not rows and not epochs:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    docs = [tokenize(f"{row.id} {row.intent}") for row in rows]
    docs.extend(tokenize(text) for _, text, _ in epochs)
    lines = [f"[{row.id}] {_flat(row.intent)} — 纪元 {row.epoch} [archived]" for row in rows]
    lines.extend(f"[{epoch}] {_flat(snippet)} — 纪元摘要 [archived]" for epoch, _, snippet in epochs)
    keys = [row.id for row in rows] + [epoch for epoch, _, _ in epochs]

    scores = _bm25(docs, query_tokens)
    ranked = [(score, key, line) for score, key, line in zip(scores, keys, lines) if score > 0.0]
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [SurfaceHit(event_id=key, line=line) for _, key, line in ranked[:top]]


def _epoch_texts(paths: MemoryPaths) -> list[tuple[str, str, str]]:
    """每个纪元摘要文件的 (纪元, 全文, 摘要段落)；全文进检索，段落进结果行。"""
    directory = paths.archive_dir
    if not directory.is_dir():
        return []
    out: list[tuple[str, str, str]] = []
    for path in sorted(directory.glob("epoch-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        epoch = path.stem[len("epoch-") :]
        paragraphs = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith(("#", "-"))
        ]
        out.append((epoch, text, paragraphs[0] if paragraphs else epoch))
    return out


def error_signature(stderr: str) -> str:
    """错误签名规范化：取错误行，去绝对路径、行号、地址、时间戳，压空白后截 120 字符。"""
    line = _error_line(stderr)
    if not line:
        return ""
    line = _TS_FULL_RE.sub("<TS>", line)
    line = _TS_CLOCK_RE.sub("<TS>", line)
    line = _POSIX_PATH_RE.sub(lambda m: m.group(0).rsplit("/", 1)[-1], line)
    line = _WIN_PATH_RE.sub(lambda m: m.group(0).rstrip("\\").rsplit("\\", 1)[-1], line)
    line = _LINENO_WORD_RE.sub("line N", line)
    line = _LINENO_COLON_RE.sub(r"\1:N", line)
    line = _HEX_ADDR_RE.sub("<ADDR>", line)
    line = " ".join(line.split())
    return line[:SIGNATURE_CHARS]


# ---- 内部 ----


def _cue_keys(cue: str, kind: CueKind, paths: MemoryPaths) -> list[str]:
    """把线索转成倒排 key；intent 线索按词元展开成多个 key。"""
    text = cue.strip()
    if not text:
        return []
    if kind == "file":
        return [anchor_key("file", paths.relative(text))]
    if kind == "error":
        sig = error_signature(text)  # 规范化幂等，已是签名的入参不会被二次改写
        return [anchor_key("error", sig)] if sig else []
    return [anchor_key("intent", token) for token in intent_tokens(text)]


def _is_seen(event_id: str, seen: set[str], granularity: Granularity) -> bool:
    """同会话去重：合并组内任一成员进过 seen，整组都不再浮现（SPEC §3.16）。"""
    group = granularity.group_of(event_id)
    if group is None:
        return event_id in seen
    return any(member in seen for member in group.ids)


def _salience_of(event_id: str, scores: Mapping[str, float], granularity: Granularity) -> float:
    """事件（或其所在合并组）的显著性；组取成员最大值，无记录取中性默认。"""
    group = granularity.group_of(event_id)
    if group is None:
        return scores.get(event_id, MISSING_SALIENCE)
    return max((scores.get(mid, MISSING_SALIENCE) for mid in group.ids), default=MISSING_SALIENCE)


def _search_corpus(store: Store, paths: MemoryPaths) -> list[Event]:
    """检索语料：以 project.md 的行集为准；索引尚未建立时退回遍历 L0。

    合并组行只占一行，这里按 granularity.json 展开回全部成员——折叠是展示层的事，
    检索语料不能因此丢事件。
    """
    rows = load_project_rows(paths)
    if not rows:
        return list(store.iter_events())
    granularity = load_granularity(paths)
    events: list[Event] = []
    for row in rows:
        group = granularity.group_of(row.id) if row.group_size > 1 else None
        for event_id in group.ids if group is not None else (row.id,):
            try:
                events.append(store.read(event_id))
            except (EventNotFound, SchemaError):
                continue
    return events


def _hit_line(e: Event, granularity: Granularity | None = None, cue_file: str = "") -> str:
    """单行召回格式：有 outcome 用 outcome，否则退回 intent。

    命中粗事件的某个文件锚点时，改用该锚点所属 segment 的 label（SPEC §3.16）。
    """
    text = ""
    if granularity is not None and cue_file:
        text = granularity.segment_label(e.id, cue_file) or ""
    if not text:
        text = e.outcome if e.outcome else e.intent
    flat = " ".join(text.split())
    if len(flat) > LINE_CHARS:
        flat = flat[: LINE_CHARS - 1] + "…"
    suffix = f" [superseded by {e.superseded_by}]" if e.status == 'superseded' else ''
    return f"[{e.id}] {flat}{suffix}"


def _flat(text: str) -> str:
    """压成单行并按召回行长度截断。"""
    flat = " ".join(text.split())
    return flat[: LINE_CHARS - 1] + "…" if len(flat) > LINE_CHARS else flat


def _error_line(stderr: str) -> str:
    """取用于签名的那一行：Python traceback 取末行异常，其余取首个非空行。"""
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return ""
    if any(ln.lstrip().startswith(_TRACEBACK_HEAD) for ln in lines):
        return lines[-1].strip()
    return lines[0].strip()


def _bm25(docs: Sequence[Sequence[str]], query: Sequence[str]) -> list[float]:
    """手写 BM25（k1=1.5, b=0.75），返回每篇文档的得分。"""
    n = len(docs)
    if n == 0:
        return []
    lengths = [len(d) for d in docs]
    avgdl = (sum(lengths) / n) or 1.0
    term_freqs = [Counter(d) for d in docs]
    doc_freq: Counter[str] = Counter()
    for tf in term_freqs:
        doc_freq.update(tf.keys())

    scores = [0.0] * n
    # 按排序后的唯一词累加：浮点加法不满足结合律，`set` 的遍历顺序又随哈希种子变化，
    # 同一次查询会因此给出不同的得分与排序。排序后结果只取决于输入。
    for term in sorted(set(query)):
        df = doc_freq.get(term, 0)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, tf in enumerate(term_freqs):
            freq = tf.get(term, 0)
            if freq == 0:
                continue
            denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * lengths[i] / avgdl)
            scores[i] += idf * (freq * (_BM25_K1 + 1)) / denom
    return scores
