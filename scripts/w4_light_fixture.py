"""W4 light-path benchmark fixture: deterministic synthetic ~10k-record corpus.

The shape comes from aggregate statistics of a private replay copy (record
counts, per-class length buckets, graph degree, feature flags, state sizes);
no private content is used. Every text below is synthetic and generated from
the fixed topic vocabulary in this file. Rebuilding with the same --seed and
--anchor-date reproduces the same corpus and the same fixed query set.

Usage:
    uv run python scripts/w4_light_fixture.py --root <dir> [--scale 1.0]
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.db import digest, tokenize
from eventmem.core.models import RecordInput, Scope, SourceInput
from kin_mind.continuity import ConcernChange
from kin_mind.habits import ConversationHabits
from kin_mind.memory import MemoryContinuity
from kin_mind.state import AffectiveEvent, DesireChange, Mind

SCOPE = Scope(project="personal", persona="Kin", collection="default", world="real")
# The feature set of the reference store the shapes were taken from. Read-path
# relevant: context, adaptive_recall, graph(+recall), manifests, event_lifecycle,
# temperature_shadow (ranking off), usage_reinforcement, sharing, associations.
FEATURES = {
    "records": True, "semantic": True, "context": True, "idle": True,
    "operational_lanes": True, "manifests": True, "manifest_restore": True,
    "context_receipts": True, "continuity_overviews": True, "continuity_quality": True,
    "sharing": True, "graph": True, "associations": True, "graph_recall": True,
    "event_lifecycle": True, "adaptive_recall": True, "auto_volumes": True,
    "temperature_shadow": True, "temperature_ranking": False,
    "semantic_actions": True, "autonomous_plans": True, "creative_execution": True,
    "usage_reinforcement": True, "procedure_learning": True,
}

# 24 everyday companion topics. Term overlap across records is what makes the
# FTS lanes return production-like candidate counts (~hundreds for a hot term).
TOPICS = {
    "running": ["跑步", "晨跑", "配速", "心率", "公里", "拉伸", "马拉松", "训练"],
    "coffee": ["咖啡", "手冲", "美式", "拿铁", "萃取", "豆子", "研磨", "风味"],
    "sleep": ["睡眠", "早睡", "熬夜", "作息", "失眠", "午睡", "闹钟", "精力"],
    "project": ["项目", "里程碑", "进度", "排期", "验收", "需求", "评审", "上线"],
    "meeting": ["周会", "会议纪要", "议程", "发言", "结论", "行动项", "跟进", "例会"],
    "reading": ["读书", "章节", "笔记", "作者", "观点", "书评", "摘录", "阅读"],
    "travel": ["旅行", "机票", "酒店", "行程", "签证", "攻略", "行李", "目的地"],
    "food": ["晚餐", "食谱", "外卖", "蔬菜", "蛋白质", "清淡", "聚餐", "口味"],
    "music": ["音乐", "歌单", "专辑", "耳机", "旋律", "演唱会", "练琴", "和弦"],
    "coding": ["代码", "重构", "接口", "补丁", "测试", "分支", "合并", "调试"],
    "release": ["发布", "版本", "回滚", "灰度", "日志", "监控", "告警", "部署"],
    "research": ["调研", "对比", "方案", "结论", "样本", "数据", "报告", "评估"],
    "family": ["家里", "妈妈", "爸爸", "周末", "聚餐", "电话", "视频", "礼物"],
    "fitness": ["健身", "器械", "深蹲", "卧推", "组数", "体脂", "训练计划", "拉伸"],
    "weather": ["天气", "下雨", "降温", "台风", "湿度", "出门", "雨伞", "气温"],
    "budget": ["预算", "开销", "记账", "报销", "账单", "储蓄", "花费", "理财"],
    "study": ["课程", "学习计划", "复习", "考试", "笔记", "作业", "练习", "打卡"],
    "pet": ["猫", "猫砂", "喂食", "疫苗", "宠物", "玩耍", "掉毛", "洗澡"],
    "photo": ["照片", "相机", "镜头", "光圈", "拍摄", "构图", "后期", "相册"],
    "game": ["游戏", "关卡", "副本", "队友", "战绩", "更新", "活动", "抽卡"],
    "language": ["英语", "单词", "口语", "听力", "语法", "打卡", "练习", "外教"],
    "volunteer": ["志愿者", "活动", "服务", "社区", "报名", "安排", "时长", "证明"],
    "garden": ["植物", "浇水", "施肥", "阳台", "花盆", "发芽", "修剪", "阳光"],
    "mood": ["心情", "焦虑", "放松", "压力", "开心", "烦躁", "平静", "状态"],
}
TOPIC_KEYS = sorted(TOPICS)

# Per-day record counts over the 8 days ending at the anchor date, from the
# reference store. Used as sampling weights.
DAY_COUNTS = [5109, 1142, 1669, 374, 459, 726, 534, 311]

# Record composition (reference store): 676 owner turns, 2059 assistant turns,
# 678 long root documents, 6911 derived records; kinds and generated flags as
# measured. Lengths follow the measured per-class buckets (in characters).
USER_LEN_BUCKETS = [(226, 5, 20), (145, 20, 60), (28, 60, 150), (26, 150, 400),
                    (5, 400, 1000), (11, 1000, 5000), (235, 5000, 45000)]
ASSISTANT_LEN_BUCKETS = [(371, 10, 60), (122, 60, 200), (899, 200, 500),
                         (640, 500, 1500), (23, 1500, 5000), (4, 5000, 8000)]
# kind, count, generated, length range
DERIVED_KINDS = [
    ("knowledge", 3256, False, 4000, 20000),
    ("episode", 1540, True, 100, 900),
    ("fact", 912, True, 40, 300),
    ("preference", 369, False, 20, 200),
    ("commitment", 306, True, 30, 300),
    ("procedure", 303, True, 100, 800),
    ("relationship", 134, False, 40, 300),
    ("observation", 55, True, 60, 400),
    ("self_narrative", 24, True, 200, 1200),
    ("state", 5, False, 40, 200),
    ("reminder", 1, False, 20, 60),
]
# Status mix: active 8553, unverified 1716, archived 51, superseded 4.
STATUS_MIX = [("unverified", 1716), ("archived", 51), ("superseded", 4)]

# Graph node kinds beyond the memory-node projections (share/topic/work/artifact
# come from mind_memory_nodes). Counts from the reference store.
GRAPH_ONLY_KINDS = [("knowledge", 3790), ("event", 2176), ("fact", 868), ("episode", 531),
                    ("observation", 496), ("finding", 332), ("procedure", 300), ("preference", 283),
                    ("commitment", 225), ("relationship", 143), ("entity", 53), ("exploration", 24),
                    ("self_narrative", 17), ("state", 4), ("association", 2)]
MEMORY_NODE_KINDS = [("share", 878), ("topic", 850), ("work", 38), ("artifact", 46)]
EDGE_PREDICATES = [("related", 8653), ("supports", 6765), ("part_of", 4556), ("delivers", 1371),
                   ("participates", 484), ("about", 198), ("follows", 95), ("shares", 94),
                   ("produces", 86), ("responds_to", 74), ("continues", 44), ("resolves", 50)]

USER_TEMPLATES = [
    "今天{t1}怎么样？我感觉{a}。",
    "帮我记一下：{t1}安排到{b}点，别跟{t2}冲突。",
    "我更喜欢{t1}，{t2}就算了。",
    "{t1}这件事后来定的方案是什么来着？",
    "周末想做{t1}，顺便把{t2}也处理了。",
    "你觉得我{t1}的频率是不是太高了？{a}。",
    "确认一下，{t1}就按上次说的办。",
    "以后{t1}不要在{b}点之前提醒我。",
]
ASSISTANT_TEMPLATES = [
    "好的，已经记下{t1}的安排。{t2}那边我会另外留意。",
    "关于{t1}，上次你说过偏好{a}，这次我先按这个准备。",
    "收到。{t1}的进展我会在{b}点前同步给你。",
    "这条关于{t1}的更正我已保存，之前的说法作废。",
]
DOC_TEMPLATES = [
    "【{topic}资料整理】",
    "一、{t1}的现状：{a}，需要在{b}点前完成复核。",
    "二、{t1}与{t2}的关系：两者互相影响，单独推进会反复。",
    "三、下一步：先处理{t1}，再回头看{t2}；风险是{a}。",
    "备注：以上结论来自这几天的记录，如有出入以最新一次为准。",
]
CONNECTORS = ["整体顺利", "有点超预期", "比想象中麻烦", "暂时搁置", "需要再看", "进展不错", "还在观察", "已经确认"]


def scaled(count, scale):
    return max(1, round(count * scale))


def length_plan(rng, buckets, scale):
    lengths = []
    for count, low, high in buckets:
        for _ in range(scaled(count, scale)):
            lengths.append(rng.randint(low, high))
    rng.shuffle(lengths)
    return lengths


def make_text(rng, topics, templates, target_len):
    parts, length = [], 0
    while length < target_len:
        topic = rng.choice(topics)
        terms = TOPICS[topic]
        sentence = rng.choice(templates).format(
            t1=rng.choice(terms), t2=rng.choice(terms), topic=topic,
            a=rng.choice(CONNECTORS), b=rng.randint(7, 23))
        parts.append(sentence)
        length += len(sentence) + 1
    return "\n".join(parts)


def stamper(rng, anchor):
    """valid_from values spread over the measured per-day shape."""
    def stamp():
        day = anchor - timedelta(days=rng.choices(range(len(DAY_COUNTS)), weights=DAY_COUNTS)[0])
        return day.replace(hour=rng.randint(7, 23), minute=rng.randint(0, 59),
                           second=rng.randint(0, 59), microsecond=rng.randint(0, 999999)).isoformat()
    return stamp


def evidence_ref(conn, engine, sid):
    """The exact reference `_reference` would compute, so freshness checks pass."""
    row = conn.execute("SELECT * FROM sources WHERE id=? AND deleted=0", (sid,)).fetchone()
    data = json.loads(row["data"])
    rid = "mem_" + digest([sid, "root"])[:32]
    record = engine._get(conn, rid)
    return {"source_id": sid, "hash": row["hash"], "record_id": rid, "revision": record["revision"],
            "authority": data["authority"], "session": row["session"], "occurred_at": row["occurred_at"],
            "received_at": row["received_at"], "namespace": row["namespace"], "source_key": row["source_key"],
            "metadata": data.get("metadata", {})}


def build(root: Path, *, seed: int, anchor: datetime, scale: float = 1.0) -> dict:
    rng = random.Random(seed)
    engine = Engine(root)
    # End of the anchor day with explicit microseconds: every generated stamp
    # compares <= the clock lexicographically, so evidence stays current.
    clock_value = anchor.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    mind = Mind(engine, SCOPE, clock=lambda: clock_value)
    memory = MemoryContinuity(mind)
    memory.configure(FEATURES)
    stamp = stamper(rng, anchor)

    # --- sources + root records ---------------------------------------------------
    counts = {"user": scaled(676, scale), "assistant": scaled(2059, scale),
              "document": scaled(678, scale)}
    plans = {"user": length_plan(rng, USER_LEN_BUCKETS, scale),
             "assistant": length_plan(rng, ASSISTANT_LEN_BUCKETS, scale)}
    source_ids = {"user": [], "assistant": [], "document": []}
    topic_cycle = [rng.choice(TOPIC_KEYS), rng.choice(TOPIC_KEYS)]
    n = 0
    for role, authority, namespace in (("user", "explicit", "kin-owner-input"),
                                       ("assistant", "model", "kin-assistant-output"),
                                       ("document", "document", "kin-notes")):
        for i in range(counts[role]):
            n += 1
            if n % 17 == 0:
                topic_cycle = [rng.choice(TOPIC_KEYS), rng.choice(TOPIC_KEYS)]
            topics = topic_cycle if rng.random() < 0.7 else [rng.choice(TOPIC_KEYS), rng.choice(TOPIC_KEYS)]
            if role == "user":
                kind, metadata = "observation", {"role": "user", "host_event": "message"}
                templates = USER_TEMPLATES
            elif role == "assistant":
                kind, metadata = "observation", {"role": "assistant", "host_event": "assistant-result"}
                templates = ASSISTANT_TEMPLATES
            else:
                kind, metadata = "knowledge", {}
                templates = DOC_TEMPLATES
            target = plans[role][i % len(plans[role])] if role in plans else rng.randint(5000, 20000)
            result = engine.receive(SourceInput(
                namespace=namespace, key=f"{role}-{i}", scope=SCOPE,
                text=make_text(rng, topics, templates, target),
                title=f"{namespace}-{i}", authority=authority, occurred_at=stamp(),
                kind=kind, metadata=metadata))
            source_ids[role].append(result["id"])
    all_sources = [s for ids in source_ids.values() for s in ids]

    # --- derived records ------------------------------------------------------------
    statuses = itertools.chain(
        (status for status, count in STATUS_MIX for _ in range(scaled(count, scale))),
        itertools.repeat("active"))
    derived_ids = []
    with engine.db.connect(write=True) as conn:
        n = 0
        for kind, count, generated, low, high in DERIVED_KINDS:
            for _ in range(scaled(count, scale)):
                sid = rng.choice(all_sources)
                record = engine._insert(conn, RecordInput(
                    id="mem_" + digest(["w4-fixture-derived", n])[:32],
                    kind=kind, title=f"{kind}-{n}", scope=SCOPE,
                    content=make_text(rng, [rng.choice(TOPIC_KEYS), rng.choice(TOPIC_KEYS)],
                                      DOC_TEMPLATES, rng.randint(low, high)),
                    source_ids=[sid], valid_from=stamp(), confirmation="explicit",
                    generated=generated, status=next(statuses)))
                derived_ids.append(record["id"])
                n += 1
        engine.db.bump(conn)
    record_ids = ["mem_" + digest([sid, "root"])[:32] for sid in all_sources] + derived_ids

    # --- graph + memory nodes ---------------------------------------------------------
    from kin_mind.graph import EventGraph
    graph = EventGraph(mind)
    node_ids = []
    with engine.db.connect(write=True) as conn:
        for kind, count in MEMORY_NODE_KINDS:
            for i in range(scaled(count, scale)):
                sid = rng.choice(all_sources)
                rid = "mem_" + digest([sid, "root"])[:32]
                topic = rng.choice(TOPIC_KEYS)
                node = {"id": memory._id(kind, f"{kind}-{i}"), "kind": kind,
                        "title": f"{topic}-{rng.choice(TOPICS[topic])}-{i}",
                        "topic": topic, "summary": make_text(rng, [topic], DOC_TEMPLATES, rng.randint(60, 300)),
                        "name": f"{kind}-{i}", "record_ids": [rid], "source_ids": [sid],
                        "task_ids": [], "about_ids": [], "versions": [],
                        "bubbles": {}, "first_at": stamp(), "last_at": stamp()}
                memory._put(conn, node)
                node_ids.append(node["id"])
        for kind, count in GRAPH_ONLY_KINDS:
            for i in range(scaled(count, scale)):
                sid = rng.choice(all_sources)
                rid = "mem_" + digest([sid, "root"])[:32]
                topic = rng.choice(TOPIC_KEYS)
                node = {"id": graph.identifier(kind, f"{kind}-{i}"), "kind": kind,
                        "title": f"{topic}-{rng.choice(TOPICS[topic])}-{i}",
                        "text": make_text(rng, [topic], DOC_TEMPLATES, rng.randint(20, 120)),
                        "aliases": [], "record_ids": [rid], "source_ids": [sid],
                        "evidence": [evidence_ref(conn, engine, sid)], "basis": "observed",
                        "occurred_at": stamp()}
                graph._put(conn, node)
                node_ids.append(node["id"])
        for predicate, count in EDGE_PREDICATES:
            for _ in range(scaled(count, scale)):
                subject, object_ = rng.choice(node_ids), rng.choice(node_ids)
                if subject == object_:
                    continue
                sid = rng.choice(all_sources)
                graph._put(conn, {"id": graph.identifier("edge", f"{subject}:{predicate}:{object_}"),
                                  "kind": "edge", "subject": subject, "object": object_,
                                  "predicate": predicate, "layer": "evidence", "basis": "observed",
                                  "confidence": 1, "reason": "synthetic fixture edge",
                                  "source_ids": [sid], "evidence": [evidence_ref(conn, engine, sid)],
                                  "valid_from": None, "valid_until": None,
                                  "assessment_event": None, "state": "active"}, edge=True)
        engine.db.bump(conn)

    # --- mind state: initialize, desires, concerns, habits ------------------------------
    def state_source(key):
        # Each state mutation needs evidence that has not been appraised before.
        return engine.receive(SourceInput(namespace="kin-fixture", key=key, scope=SCOPE,
                                          text=f"合成夹具状态依据 {key}。",
                                          authority="explicit", occurred_at=clock_value))["id"]

    init_src = state_source("initialize")
    mind.initialize(agent_version="synthetic-w4-v1", evidence_ids=[init_src])
    from kin_mind.continuity import ContinuityConfig
    mind.configure_continuity(ContinuityConfig(
        command_id="fixture-continuity", agent_version="synthetic-w4-v1",
        expected_revision=mind.read()["revision"], evidence_ids=[state_source("continuity")],
        features={"concerns": True}, reason="Synthetic fixture continuity features"))

    for event_no in range(12):
        mind.record(AffectiveEvent(
            command_id=f"fixture-event-{event_no}", agent_version="synthetic-w4-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[state_source(f"event-{event_no}")],
            values={k: rng.randint(20, 90) for k in ("mood", "focus", "curiosity")},
            reason="Synthetic fixture affective event"))
    pad = "这是一段用于达到参考状态规模的合成填充文字，描述一次完整的偏好确认过程与依据。"
    for i in range(scaled(120, scale)):
        topic = rng.choice(TOPIC_KEYS)
        mind.manage_desire(DesireChange(
            command_id=f"fixture-desire-{i}", agent_version="synthetic-w4-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[state_source(f"desire-{i}")],
            action="create", content=(f"围绕{topic}的合成愿望：" + pad * 14)[:2000],
            topic=topic, kind=rng.choice(["contact", "explore", "create"]),
            strength=rng.randint(20, 95), expires_at="2027-01-01T00:00:00+00:00",
            completion=("合成完成条件。" + pad * 3)[:1000], reason=("合成依据。" + pad * 4)[:1200]))
    for i in range(scaled(30, scale)):
        topic = rng.choice(TOPIC_KEYS)
        mind.manage_concern(ConcernChange(
            command_id=f"fixture-concern-{i}", agent_version="synthetic-w4-v1",
            expected_revision=mind.read()["revision"], evidence_ids=[state_source(f"concern-{i}")],
            action="create", key=f"concern-{i}", kind="care",
            content=(f"关于{topic}的合成关切：" + pad * 8)[:2000],
            topic=topic, intensity=rng.randint(20, 90), basis="explicit",
            confidence=0.9, reason=("合成关切依据。" + pad * 3)[:1200]))
    ConversationHabits(mind).update({"command_id": "fixture-habits", "preferences": {
        "reply_choice": "autonomous", "exploration_min_interval_minutes": 30},
        "evidence_ids": [source_ids["user"][0]],
        "reason": "Synthetic fixture habits", "expected_revision": 0})

    # --- temperature rows (one per record, mostly hot) -----------------------------------
    with engine.db.connect(write=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO mind_memory_temperature VALUES(?,?,?,?,?,?)",
            [(SCOPE.key(), rid, "hot", 0, clock_value, "{}") for rid in record_ids])

    # --- synthetic overview cache, mirroring warm()'s coverage shape -----------------------
    from eventmem.core.read_policy import ReadPolicy
    from kin_mind.context import Contexts
    contexts = Contexts(mind)
    policy = ReadPolicy.load(engine, SCOPE, "experience_recall")
    cached = 0
    with engine.db.connect(write=True) as conn:
        for rid in rng.sample(derived_ids, min(len(derived_ids), scaled(431, scale))):
            record = engine._get(conn, rid)
            item = contexts.record_item(record, policy=policy)
            if tokenize(item["text"]).count(" ") < 120:
                continue
            key = contexts._overview_key(item)
            summary = make_text(rng, [rng.choice(TOPIC_KEYS)], ASSISTANT_TEMPLATES, 160)
            conn.execute("INSERT OR REPLACE INTO mind_context_cache VALUES(?,?,?,?)",
                         (key, SCOPE.key(), json.dumps({"text": summary, "source": item,
                                                        "receipt": {"provider": "synthetic-fixture"},
                                                        "coverage": "overview"}, ensure_ascii=False), clock_value))
            cached += 1

    manifest = {
        "seed": seed, "anchor_date": anchor.date().isoformat(), "scale": scale,
        "scope": SCOPE.model_dump(), "features": FEATURES,
        "counts": {"sources": len(all_sources), "records": len(record_ids),
                   "graph_nodes": len(node_ids), "temperature_rows": len(record_ids),
                   "overview_cache": cached},
        "sample_record_ids": record_ids[:8],
        "shape_source": "aggregate statistics of a private replay copy; synthetic content only",
    }
    (root / "w4-fixture-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def build_queries(root: Path, *, seed: int, anchor: datetime, manifest: dict) -> list[dict]:
    """The fixed 50-query set. Its own RNG stream, so `--queries-only` regenerates
    the same file for the same seed/anchor without rebuilding the corpus."""
    rng = random.Random(seed * 1_000_003 + 17)
    anchor = anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)

    def terms(topic, n):
        return rng.sample(TOPICS[topic], n)

    queries = []

    def add(shape, query):
        queries.append({"id": f"q{len(queries) + 1:02d}-{shape}", "shape": shape, "query": query})

    for _ in range(10):
        topic = rng.choice(TOPIC_KEYS)
        add("keyword", " ".join(terms(topic, rng.randint(1, 3))))
    for _ in range(6):
        t1 = terms(rng.choice(TOPIC_KEYS), 1)[0]
        add("preference", rng.choice(["我有没有说过喜欢{t}？", "我对{t}有什么偏好？",
                                      "关于{t}，我之前的原话是什么？"]).format(t=t1))
    for _ in range(6):
        t1 = terms(rng.choice(TOPIC_KEYS), 1)[0]
        add("owner_words", rng.choice(["我之前确认过{t}的安排吗？", "你说过{t}后来怎么样了？",
                                       "我要求过{t}怎么改来着？", "我当时对{t}说过什么？"]).format(t=t1))
    for offset in (7, 6, 2, 1, 0):
        day = anchor - timedelta(days=offset)
        topic = rng.choice(TOPIC_KEYS)
        add("date", f"{day.month}月{day.day}日我说过什么关于{TOPICS[topic][0]}的事？")
    for _ in range(5):
        t1 = terms(rng.choice(TOPIC_KEYS), 1)[0]
        add("correction", rng.choice(["{t}的方案后来改成什么了？", "{t}那次更正之后现在的说法是什么？"]).format(t=t1))
    for _ in range(4):
        t1 = terms(rng.choice(TOPIC_KEYS), 1)[0]
        add("commitment", rng.choice(["我答应过什么时候交{t}？", "我承诺过的{t}是哪一天？"]).format(t=t1))
    for _ in range(4):
        topic = rng.choice(TOPIC_KEYS)
        add("work_share", rng.choice(["那篇关于{t}的内容分享了吗？", "{t}的任务进展到哪一步了？"]).format(t=TOPICS[topic][0]))
    for rid in manifest["sample_record_ids"][:2]:
        add("id_lookup", rid)
    for _ in range(3):
        ta, tb = rng.sample(TOPIC_KEYS, 2)
        add("long_verbose",
            f"帮我想一下，最近这一周里关于{TOPICS[ta][0]}和{TOPICS[ta][1]}我分别说过哪些安排，"
            f"还有{TOPICS[tb][0]}那边后来确认的结论是什么，中间有没有改过主意？")
    add("empty", "")
    add("empty", " ")
    add("gibberish", "zzqq 不存在的东西啊哈")
    add("gibberish", "asdfqwer 完全不相关")
    add("mixed", "review 一下代码重构的进展")
    assert len(queries) == 50, len(queries)
    (root / "w4-queries.json").write_text(json.dumps(queries, ensure_ascii=False, indent=2))
    return queries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--anchor-date", default=datetime.now(timezone.utc).date().isoformat(),
                        help="Last day of the 8-day record spread (default: today, UTC)")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--queries-only", action="store_true",
                        help="Only (re)generate w4-queries.json from the manifest in --root")
    args = parser.parse_args()
    anchor = datetime.fromisoformat(args.anchor_date).replace(tzinfo=timezone.utc)
    if args.queries_only:
        manifest = json.loads((args.root / "w4-fixture-manifest.json").read_text())
        queries = build_queries(args.root, seed=args.seed, anchor=anchor, manifest=manifest)
        print(json.dumps({"queries": len(queries)}))
        return
    manifest = build(args.root, seed=args.seed, anchor=anchor, scale=args.scale)
    queries = build_queries(args.root, seed=args.seed, anchor=anchor, manifest=manifest)
    print(json.dumps({**manifest["counts"], "queries": len(queries)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
