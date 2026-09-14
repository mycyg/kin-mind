"""recall.py：锚点浮现（surface）、BM25 兜底检索（search）、错误签名规范化。"""

from __future__ import annotations

from datetime import datetime

from eventmem.index import Budget, rebuild_all
from eventmem.recall import error_signature, search, surface
from eventmem.schema import Anchors
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)

# ---------------------------------------------------------------- surface：三种 cue kind


def test_surface_file_cue_hits_events_sharing_that_file(store: Store, paths, event_factory) -> None:
    hit = store.append(
        event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["train/launcher.py"]))
    )
    store.append(event_factory(status="done", outcome="无关事件", anchors=Anchors(files=["other.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("train/launcher.py", "file", store, paths, Budget(), seen=set())
    assert [h.event_id for h in hits] == [hit]
    assert hits[0].line == f"[{hit}] 端口冲突已修复"


def test_surface_error_cue_normalizes_before_matching(store: Store, paths, event_factory) -> None:
    """cue 是原始 stderr 文本，surface 内部要先规范化成签名再查表。"""
    hit = store.append(
        event_factory(
            status="done",
            outcome="改用独立端口区间",
            anchors=Anchors(error_sigs=[error_signature("ValueError: port busy at 0x7fabc1234")]),
        )
    )
    rebuild_all(store, paths, Budget(), NOW)

    raw_stderr = "ValueError: port busy at 0x1234abcd"  # 不同的十六进制地址，规范化后应等价
    hits = surface(raw_stderr, "error", store, paths, Budget(), seen=set())
    assert [h.event_id for h in hits] == [hit]


def test_surface_intent_cue_hits_via_shared_token(store: Store, paths, event_factory) -> None:
    hit = store.append(event_factory(status="open", intent="修复端口冲突"))
    store.append(event_factory(status="open", intent="前端按钮颜色对比度不足"))
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("端口相关的问题", "intent", store, paths, Budget(), seen=set())
    assert [h.event_id for h in hits] == [hit]


def test_surface_returns_empty_list_when_no_anchor_matches(store: Store, paths, event_factory) -> None:
    store.append(event_factory(anchors=Anchors(files=["a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    assert surface("nonexistent/path.py", "file", store, paths, Budget(), seen=set()) == []


def test_surface_returns_empty_list_for_blank_cue(store: Store, paths, event_factory) -> None:
    store.append(event_factory(anchors=Anchors(files=["a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    assert surface("   ", "file", store, paths, Budget(), seen=set()) == []


# ---------------------------------------------------------------- surface：seen 过滤


def test_surface_filters_out_seen_event_ids(store: Store, paths, event_factory) -> None:
    e1 = store.append(event_factory(status="done", outcome="第一次浮现", anchors=Anchors(files=["a.py"])))
    e2 = store.append(event_factory(status="done", outcome="第二次浮现", anchors=Anchors(files=["a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("a.py", "file", store, paths, Budget(), seen={e1})
    assert [h.event_id for h in hits] == [e2]


# ---------------------------------------------------------------- surface：K 截断


def test_surface_truncates_to_surface_k(store: Store, paths, event_factory) -> None:
    ids = [
        store.append(event_factory(status="done", outcome=f"结论{i}", anchors=Anchors(files=["shared.py"])))
        for i in range(5)
    ]
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=2), seen=set())
    assert len(hits) == 2
    # 全部同状态（done）时按新近度（id 越大越新）取前 2
    assert [h.event_id for h in hits] == [ids[4], ids[3]]


# ---------------------------------------------------------------- surface：状态权重排序


def test_surface_orders_by_status_weight_done_over_abandoned_over_open(store: Store, paths, event_factory) -> None:
    e_open = store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"])))
    e_abandoned = store.append(
        event_factory(status="abandoned", outcome="放弃", anchors=Anchors(files=["shared.py"]))
    )
    e_done = store.append(event_factory(status="done", outcome="完成", anchors=Anchors(files=["shared.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=3), seen=set())
    assert [h.event_id for h in hits] == [e_done, e_abandoned, e_open]


def test_surface_excludes_superseded_from_current_context(store: Store, paths, event_factory) -> None:
    e_open = store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"])))
    e_superseded = store.append(
        event_factory(
            status="superseded",
            superseded_by="2026-09-01_000000",
            outcome="被取代",
            anchors=Anchors(files=["shared.py"]),
        )
    )
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=2), seen=set())
    assert [h.event_id for h in hits] == [e_open]


# ---------------------------------------------------------------- search：BM25 相关性排序


def test_search_ranks_by_bm25_relevance_with_distinct_corpus(store: Store, paths, event_factory) -> None:
    db_event = store.append(
        event_factory(
            status="done",
            intent="数据库连接池耗尽导致请求超时",
            outcome="为连接池设置上限并增加排队重试",
        )
    )
    store.append(
        event_factory(
            status="done",
            intent="前端按钮颜色对比度不足",
            outcome="调整按钮配色符合可访问性标准",
        )
    )
    rebuild_all(store, paths, Budget(), NOW)

    hits = search("数据库连接池", store, paths, top=10)
    assert [h.event_id for h in hits] == [db_event]
    assert hits[0].line == f"[{db_event}] 为连接池设置上限并增加排队重试"


def test_search_returns_empty_list_when_no_term_overlaps(store: Store, paths, event_factory) -> None:
    store.append(event_factory(intent="数据库连接池耗尽", outcome="已修复", status="done"))
    rebuild_all(store, paths, Budget(), NOW)

    assert search("zzqxvv123nonexistent", store, paths, top=10) == []


def test_search_respects_top_truncation(store: Store, paths, event_factory) -> None:
    for i in range(5):
        store.append(
            event_factory(status="done", intent=f"缓存穿透问题排查第{i}次", outcome=f"结论{i}")
        )
    rebuild_all(store, paths, Budget(), NOW)

    hits = search("缓存穿透问题", store, paths, top=2)
    assert len(hits) == 2


def test_search_falls_back_to_raw_store_when_project_index_missing(store: Store, paths, event_factory) -> None:
    """project.md 尚未建立时，search 退回遍历 L0，而不是报错或返回空。"""
    store.append(event_factory(intent="数据库连接池耗尽", outcome="已修复连接池问题", status="done"))
    assert not paths.project_index.exists()

    hits = search("数据库连接池", store, paths, top=10)
    assert len(hits) == 1


# ---------------------------------------------------------------- error_signature


def test_error_signature_python_traceback_uses_last_line() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/Users/synthetic/project/foo.py", line 42, in bar\n'
        '    raise ValueError("bad value")\n'
        "ValueError: bad value"
    )
    assert error_signature(tb) == "ValueError: bad value"


def test_error_signature_non_traceback_uses_first_nonblank_line() -> None:
    stderr = "\n\nfirst real line: something failed\nsecond line: irrelevant"
    assert error_signature(stderr) == "first real line: something failed"


def test_error_signature_strips_posix_path_to_basename() -> None:
    sig = error_signature("error at /Users/synthetic/project/src/foo.py during load")
    assert "/Users/synthetic/project" not in sig
    assert "foo.py" in sig


def test_error_signature_normalizes_line_number_word_form() -> None:
    sig = error_signature("SyntaxError at line 42 in module")
    assert "42" not in sig
    assert "line N" in sig


def test_error_signature_normalizes_line_number_colon_form() -> None:
    sig = error_signature("error at foo.py:42 during import")
    assert ":42" not in sig
    assert "foo.py:N" in sig


def test_error_signature_normalizes_hex_address() -> None:
    sig = error_signature("segfault at address 0x7fabc1234")
    assert "0x7fabc1234" not in sig
    assert "<ADDR>" in sig


def test_error_signature_normalizes_full_timestamp() -> None:
    sig = error_signature("request failed at 2026-08-25T14:32:01Z")
    assert "2026-08-25T14:32:01Z" not in sig
    assert "<TS>" in sig


def test_error_signature_combined_normalization_matches_expected_line() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "foo.py", line 10, in <module>\n'
        "ValueError: bad value at 0x7fabc1234 on 2026-08-25T14:32:01"
    )
    assert error_signature(tb) == "ValueError: bad value at <ADDR> on <TS>"


def test_error_signature_is_idempotent_on_renormalization() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/Users/synthetic/project/foo.py", line 42, in bar\n'
        "ValueError: bad value at 0x7fabc1234 on 2026-08-25T14:32:01"
    )
    once = error_signature(tb)
    twice = error_signature(once)
    assert once == twice


def test_error_signature_truncates_to_120_chars() -> None:
    sig = error_signature("ValueError: " + "x" * 500)
    assert len(sig) == 120


def test_error_signature_empty_for_blank_input() -> None:
    assert error_signature("") == ""
    assert error_signature("\n\n   \n") == ""
