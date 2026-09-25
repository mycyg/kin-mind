"""What a fork read while it answered, where nothing else names it (CL6D-MM-01, the same kind).

A contact draft is written in a fork whose model can read memory with its tools: its attempt row
names what it had before it -- the wishes and sends it was offered, the ids the memory context and
the state handed to it name, what its tools returned -- and Kin's words from it are written only
while none of that is deleted; a reason copied onto a wish goes when that goes. A draft already
sent is her message: the conversation keeps it, and only the row's copy goes.

The host's record of a fork turn stops at 64 calls, 1000 ids a call and 2000 a turn, and says
`truncated` there or where the owned ACP says it could not read a call's result whole (CL6E-MM-04):
what was read past it is named nowhere, so any delete since the attempt began counts, a proposal
kept for reuse is asked again in full, and a compression is used and not kept.

Each case blocks where the model answers or the draft is about to be settled, deletes, lets it go
on, and then reads every table of the store for the words."""
import json
import re
from datetime import timedelta
from pathlib import Path

from eventmem.core.models import SourceInput

from kin_mind.appraisal import Appraisal, Appraisals, DailyReview, appraisal_context
from kin_mind.context import Contexts
from kin_mind.erasure import ERASED, reads_truncated
from kin_mind.memory import MemoryContinuity
from kin_mind.state import DRAFT_SHOWN_IDS, fork_reads
from eventmem.core.db import NAMED
from test_derived_erasure import answered, paid, prospective_check, queue_row, read_note
from test_erasure import settle, system, texts_everywhere  # noqa: F401  (`system`: the compression fixture)
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)

MARKER = "quillwick"


def fork_receipt(*ids, truncated=False):
    """What the host records of a draft's fork turn (boundaries.mjs `forkReceipt`): a read-only
    tool call and the ids it returned, and `truncated` when the record was cut short."""
    return {"channel": "fork", "tool_calls": [{"name": "memorypalace.read_continuity_context", "ok": True,
                                               "ids": [{"id": identifier, "revision": 1} for identifier in ids]}],
            **({"truncated": True} if truncated else {})}


def contact_row(mind, attempt_id):
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT state,data FROM mind_contacts WHERE id=?", (attempt_id,)).fetchone()
    return row["state"], json.loads(row["data"])


def stored_wish(mind, desire_id):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)["desires"][desire_id]


def test_a_draft_back_after_what_it_read_was_deleted_keeps_no_words_in_any_table(setup):
    """Blocked while Kin drafts, a note her fork read with its tool is deleted; the draft comes back
    with a wait whose reason repeats it. Drafted again, a note the memory context handed her is
    deleted the same way; the draft comes back with a text about to be sent that repeats it.
    Neither is kept: the settlement refuses both in the write that would keep them, the wish waits
    to be drafted again, nothing is sent, and no table holds the words (CL6D-MM-01)."""
    mind, source, clock = setup
    read_source, read_record = read_note(mind, clock, "draft-read", f"她说想去 {MARKER} 看看")
    shown_source, shown_record = read_note(mind, clock, "draft-shown", f"上次聊到 {MARKER} 的灯")
    wish(mind, source, "draft-wish", content="Tell her about the lamps")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    mind.engine.delete(read_source)
    waited = mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision", desire_ids=[did],
                                 decision={"action": "wait", "condition": "owner_reply", "reason": f"等她说完 {MARKER} 的事"},
                                 shown_ids=[], draft_receipt=fork_receipt(read_record))
    assert waited["state"] == "canceled" and waited["reason"] == "draft-sources-deleted"
    assert read_record in waited["evaluated_ids"] and MARKER not in json.dumps(waited, ensure_ascii=False)
    desire = stored_wish(mind, did)
    assert desire["status"] == "waiting" and desire["contact_wait"]["condition"] == "time"
    assert "reason_evidence_ids" not in desire and MARKER not in desire["reason"]
    # Drafted again five minutes later, from what is there then.
    clock[0] += timedelta(seconds=301)
    assert mind.reconsider_contacts(owner_epoch="owner-1")["resumed"] == [did]
    again = mind.claim_contact(owner_epoch="owner-1")
    mind.engine.delete(shown_source)
    held = mind.settle_contact(attempt_id=again["id"], state="pending", desire_ids=[did], text=f"还记得 {MARKER} 的灯吗",
                               shown_ids=[shown_source, shown_record], draft_receipt=fork_receipt())
    assert held["state"] == "canceled" and held["reason"] == "draft-sources-deleted"
    assert not {"text_excerpt", "text_digest", "chosen"} & set(held)
    assert {shown_source, shown_record} <= set(held["evaluated_ids"])
    assert stored_wish(mind, did)["status"] == "waiting"
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    assert mind.read()["contact_unconfirmed"] == [] and mind.contact_candidate()["reason"] != "attempt-in-progress"


def test_a_wait_kin_decided_loses_its_reason_on_the_wish_once_what_her_draft_was_shown_is_deleted(setup):
    """Back while the note the memory context handed her stands, her wait is kept: its reason on
    the wish and in its wait names what the draft had before it, as the attempt row does. Rendered
    for a model the wait carries its words, not that list. The note is deleted later: the reason
    goes from the wish, its wait, the attempt row and every revision of the state; the wish's own
    words, the wait's condition and what was decided stay (CL6D-MM-01)."""
    mind, source, clock = setup
    shown_source, shown_record = read_note(mind, clock, "wait-shown", f"她下周要去 {MARKER}")
    wish(mind, source, "wait-wish", content="Ask about her trip")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision",
                        decision={"action": "wait", "condition": "owner_reply", "reason": f"等她从 {MARKER} 回来再问"},
                        shown_ids=[shown_source, shown_record], draft_receipt=fork_receipt())
    desire = stored_wish(mind, did)
    assert MARKER in desire["reason"] and MARKER in desire["contact_wait"]["reason"]
    assert shown_record in desire["reason_evidence_ids"] and shown_record in desire["contact_wait"]["reason_evidence_ids"]
    assert shown_record in contact_row(mind, attempt["id"])[1]["evaluated_ids"]
    rendered = next(d for d in appraisal_context({"state": mind.read(), "new_evidence": []})["state"]["desires"] if d["id"] == did)
    assert MARKER in rendered["contact_wait"]["reason"] and "reason_evidence_ids" not in rendered["contact_wait"]
    mind.engine.delete(shown_source)
    settle(mind.engine)
    desire = stored_wish(mind, did)
    assert desire["reason"] == ERASED and desire["contact_wait"]["reason"] == ERASED
    assert desire["status"] == "waiting" and desire["contact_wait"]["condition"] == "owner_reply"
    assert desire["content"] == "Ask about her trip"
    state, row = contact_row(mind, attempt["id"])
    assert state == "canceled" and row["decision"]["action"] == "wait" and row["decision"]["reason"] == ERASED
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_sent_draft_stays_her_message_and_only_its_row_loses_the_words(setup):
    """The draft read a note with its tool and was sent: the text is Kin's message to her, which
    the conversation keeps. The note is deleted afterwards. The message is not deleted after the
    fact; the attempt row's copy of the text goes, its digest stays, and no other layer of the mind
    holds the words (CL6D-MM-01)."""
    mind, source, clock = setup
    read_source, read_record = read_note(mind, clock, "sent-read", f"那家店叫 {MARKER}")
    wish(mind, source, "sent-wish", content="Tell her about the shop")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    text = f"今天路过 {MARKER} 了"
    pending = mind.settle_contact(attempt_id=attempt["id"], state="pending", text=text, shown_ids=[],
                                  draft_receipt=fork_receipt(read_record))
    assert pending["state"] == "pending" and read_record in pending["evaluated_ids"]
    mind.settle_contact(attempt_id=attempt["id"], state="accepted", message_id="om-1", message_ids=["om-1"])
    # As the delivery journal keeps what was sent: her message, in the conversation.
    sent = mind.engine.receive(SourceInput(namespace="kin-assistant-output", key="om-1", scope=mind.scope, text=text,
                                           authority="model", occurred_at=clock[0].isoformat(),
                                           metadata={"host_event": "assistant-result", "role": "assistant", "channel": "wechat"}))["id"]
    mind.engine.delete(read_source)
    settle(mind.engine)
    state, row = contact_row(mind, attempt["id"])
    assert state == "accepted" and row["text_excerpt"] == ERASED and row["text_digest"]
    assert MARKER in mind.engine.get(mind.engine.source(sent)["record_ids"][0])["content"], "her message stays"
    found = texts_everywhere(mind.engine, MARKER)
    assert found and not {table for table, _ in found if table.startswith("mind_")}, found


def test_a_send_of_unknown_outcome_loses_its_excerpt_in_every_copy(setup):
    """A send of unknown outcome is told to the next draft and shown in the state by an excerpt,
    which names what its draft had before it beside every copy. Once that is deleted the excerpt
    goes from its own row, from the next attempt's copy and from the state; its digest still
    recognises the same words (CL6D-MM-01)."""
    mind, source, clock = setup
    shown_source, shown_record = read_note(mind, clock, "unknown-shown", f"她养的猫叫 {MARKER}")
    wish(mind, source, "unknown-wish", content="Ask about the cat")
    first = mind.claim_contact(owner_epoch="owner-1")
    said = f"{MARKER} 今天乖吗"
    mind.settle_contact(attempt_id=first["id"], state="pending", text=said, shown_ids=[shown_source, shown_record],
                        draft_receipt=fork_receipt())
    mind.settle_contact(attempt_id=first["id"], state="unconfirmed", reason="timeout")
    [fact] = mind.read()["contact_unconfirmed"]
    assert MARKER in fact["excerpt"] and shown_record in fact["excerpt_evidence_ids"]
    assert "excerpt_evidence_ids" not in appraisal_context({"state": mind.read(), "new_evidence": []})["state"]["contact_unconfirmed"][0]
    wish(mind, source, "second-wish", content="Ask how the day went")
    second = mind.claim_contact(owner_epoch="owner-1")
    assert MARKER in second["unconfirmed"][0]["excerpt"]
    mind.engine.delete(shown_source)
    settle(mind.engine)
    assert contact_row(mind, first["id"])[1]["text_excerpt"] == ERASED
    assert contact_row(mind, second["id"])[1]["unconfirmed"][0]["excerpt"] == ERASED
    assert mind.read()["contact_unconfirmed"][0]["excerpt"] == ERASED
    assert texts_everywhere(mind.engine, MARKER) == set()
    assert mind.check_contact(second["id"], "owner-1", text=said)["reason"] == "repeats-unconfirmed-send"


def test_a_draft_whose_reads_were_cut_short_counts_any_delete_since_its_claim(setup):
    """The host's record stops at 64 calls and 100 ids a call and then says `truncated`: what the
    fork read past it is named nowhere. A delete before the claim cannot have been read and counts
    for nothing; one after it may have been, and the settlement refuses her words as for anything
    named. With a full record the same delete stops nothing (CL6D-MM-01)."""
    mind, source, clock = setup
    before, _ = read_note(mind, clock, "before-claim", "早就删掉的一条")
    during, _ = read_note(mind, clock, "during-draft", "没被点名的一条")
    unrelated, _ = read_note(mind, clock, "unrelated", "无关的一条")
    wish(mind, source, "cut-wish", content="Say good night")
    mind.engine.delete(before)
    attempt = mind.claim_contact(owner_epoch="owner-1")
    did = attempt["desire_ids"][0]
    kept = mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision",
                               decision={"action": "wait", "condition": "time", "retry_after_seconds": 600, "reason": "她还在忙"},
                               shown_ids=[], draft_receipt=fork_receipt(truncated=True))
    assert kept["reason"] == "draft-decision" and kept["reads_truncated"] is True
    assert stored_wish(mind, did)["reason"] == "她还在忙"
    clock[0] += timedelta(seconds=601)
    mind.reconsider_contacts(owner_epoch="owner-1")
    again = mind.claim_contact(owner_epoch="owner-1")
    mind.engine.delete(during)
    refused = mind.settle_contact(attempt_id=again["id"], state="pending", text=f"晚安，{MARKER}", shown_ids=[],
                                  draft_receipt=fork_receipt(truncated=True))
    assert refused["state"] == "canceled" and refused["reason"] == "draft-sources-deleted"
    clock[0] += timedelta(seconds=301)
    mind.reconsider_contacts(owner_epoch="owner-1")
    third = mind.claim_contact(owner_epoch="owner-1")
    mind.engine.delete(unrelated)
    assert mind.settle_contact(attempt_id=third["id"], state="pending", text="晚安", shown_ids=[],
                               draft_receipt=fork_receipt())["state"] == "pending"
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_the_row_keeps_the_hosts_record_within_its_bounds_and_says_when_it_was_cut():
    """What the row keeps of the host's record: each call, whether it succeeded and the ids it
    returned. Past 64 calls, 1000 ids a call or 2000 a turn -- the host's own bounds -- it is cut
    here too and says so, as a record the host cut says so; an entry that is no id is dropped
    (CL6D-MM-01, CL6E-MM-04)."""
    call = {"name": "memorypalace.read", "ok": True, "ids": [{"id": "mem_" + "1" * 32, "revision": 2}, {"id": 7}, {"id": "x" * 201}]}
    assert fork_reads({"tool_calls": [call], "usage": {"input_tokens": 9}}) == {
        "tool_calls": [{"name": "memorypalace.read", "ok": True, "ids": [{"id": "mem_" + "1" * 32, "revision": 2}]}]}
    assert not reads_truncated(fork_reads({"tool_calls": [call] * 64}))
    assert reads_truncated(fork_reads({"tool_calls": [call] * 65}))
    assert len(fork_reads({"tool_calls": [call] * 65})["tool_calls"]) == 64

    def many(count, start=0):
        return {"name": "memorypalace.read", "ok": True, "ids": [{"id": f"mem_{n:032x}"} for n in range(start, start + count)]}
    assert not reads_truncated(fork_reads({"tool_calls": [many(1000)]}))
    assert reads_truncated(fork_reads({"tool_calls": [many(1001)]}))
    assert len(fork_reads({"tool_calls": [many(1001)]})["tool_calls"][0]["ids"]) == 1000
    assert not reads_truncated(fork_reads({"tool_calls": [many(1000), many(1000, 1000)]}))
    turn = fork_reads({"tool_calls": [many(900), many(900, 900), many(900, 1800)]})
    assert reads_truncated(turn) and [len(c["ids"]) for c in turn["tool_calls"]] == [900, 900, 200]
    assert reads_truncated(fork_reads({"tool_calls": [], "truncated": True}))
    assert fork_reads(None) is None and DRAFT_SHOWN_IDS == 4000


# Where a store id begins and ends: beside Chinese words, a space, full-width brackets, a label or a
# letter outside ASCII it stands alone; right after an ASCII letter, digit or underscore, or right
# before one, it is part of another word (CL7B-FLOW-02).
STORE_ID_BOUNDARIES = (("记忆{id}的", True), ("记忆 {id} 的", True), ("（{id}）", True), ("id:{id}", True), ("é{id}", True),
                       ("{id}的", True), ("a{id}", False), ("7{id}", False), ("_{id}", False), ("{id}g", False), ("{id}0", False))


def test_the_owned_acp_reads_the_store_ids_the_store_writes():
    """The owned ACP names what a fork's tools returned by the pattern the store's own ids have, the
    one a delete finds its rows by (`eventmem.core.db.NAMED`): the two are one pattern (CL6E-MM-04),
    and they end words alike. JS's word boundary knows only ASCII letters, digits and `_`; so does
    the store's (re.ASCII), where Python's own would take a Chinese character for a letter and miss
    an id written right against Chinese words, which the ACP reads (CL7B-FLOW-02)."""
    patch = (Path(__file__).resolve().parents[2] / "adapters" / "codex-runtime-patch.mjs").read_text()
    # The helper is template text: each backslash of the generated source is written twice.
    found = re.search(r"const KIN_STORE_ID = /(.+)/g;", patch)
    assert found and found.group(1).replace("\\\\", "\\") == NAMED.pattern
    assert NAMED.flags & re.ASCII
    identifier = "mem_" + "ab" * 16
    for text, named in STORE_ID_BOUNDARIES:
        assert NAMED.findall(text.format(id=identifier)) == ([identifier] if named else []), text


def test_more_shown_than_a_row_keeps_counts_as_cut_short(setup):
    """The ids the memory context and the state name are kept up to a bound; past it the row says
    its record was cut short, and any delete since the claim refuses her words (CL6D-MM-01)."""
    mind, source, clock = setup
    during, _ = read_note(mind, clock, "during-many", "没被点名的一条")
    wish(mind, source, "many-wish", content="Say hello")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.engine.delete(during)
    shown = [f"mem_{n:032x}" for n in range(DRAFT_SHOWN_IDS + 1)]
    refused = mind.settle_contact(attempt_id=attempt["id"], state="pending", text="你好", shown_ids=shown, draft_receipt=fork_receipt())
    assert refused["reads_truncated"] is True and refused["reason"] == "draft-sources-deleted"


def test_a_host_reason_that_replaces_kins_is_no_copy_and_stays_when_her_sources_go(setup):
    """Kin's reason on a wish names what it was written from. Where the host's own words replace it
    -- a declared condition ready again, a later draft that failed, a trait the wish rested on that
    moved -- what the old reason named goes with it, and a later delete leaves the host's words
    alone (CL6D-MM-01)."""
    from kin_mind.trait_refs import WAITING_REASON, settle_wishes

    mind, source, clock = setup
    shown_source, shown_record = read_note(mind, clock, "replaced-shown", f"她提过 {MARKER}")
    ready = wish(mind, source, "ready-wish", content="Ask about the weekend")["desire_id"]
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision", desire_ids=[ready],
                        decision={"action": "wait", "condition": "time", "retry_after_seconds": 300, "reason": f"等 {MARKER} 之后"},
                        shown_ids=[shown_source, shown_record], draft_receipt=fork_receipt())
    assert stored_wish(mind, ready)["reason_evidence_ids"]
    clock[0] += timedelta(seconds=301)
    mind.reconsider_contacts(owner_epoch="owner-1")
    failed = wish(mind, source, "failed-wish", strength=40, content="Tell her about the rain")["desire_id"]
    moved = wish(mind, source, "moved-wish", strength=30, content="Tease her about the tea")["desire_id"]
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for did in (failed, moved):
            state["desires"][did].update(reason=f"因为 {MARKER}", reason_evidence_ids=[shown_record])
        state["desires"][moved]["trait_revisions"] = {"trait_playful": 1}
        mind._save(conn, state)
    attempt = mind.claim_contact(owner_epoch="owner-1")
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-failed", desire_ids=[failed])
    assert settle_wishes(mind, apply=True)["settled"] == [moved]
    expected = {ready: "Declared contact condition became ready: time", failed: "Draft generation or parsing failed", moved: WAITING_REASON}
    for did, reason in expected.items():
        assert stored_wish(mind, did)["reason"] == reason and "reason_evidence_ids" not in stored_wish(mind, did)
    mind.engine.delete(shown_source)
    settle(mind.engine)
    for did, reason in expected.items():
        assert stored_wish(mind, did)["reason"] == reason, did


class Truncated:
    """A fork turn whose receipt was cut short: it names nothing it read."""
    receipt = {"native_receipt": {"channel": "fork", "tool_calls": [], "truncated": True}}


def test_an_appraisal_whose_reads_were_cut_short_is_refused_by_any_delete_while_it_answers(setup):
    """Its receipt is cut short (`truncated`): a note deleted while it answers is named nowhere and
    may have been read. The commit refuses as for anything it was shown, the row names the note as
    read and keeps none of the words, and the retry is asked in full. A delete from before an
    attempt counts for nothing in it; with a full receipt an unrelated delete stops nothing
    (CL6D-MM-01)."""
    mind, source, clock = setup
    early, _ = read_note(mind, clock, "before-attempt", "早先删掉的一条")
    unnamed, unnamed_record = read_note(mind, clock, "cut-read", f"没被点名的 {MARKER}")
    unrelated, _ = read_note(mind, clock, "unrelated-read", "无关的一条")
    primary = source("today", "今天天气不错")
    mind.engine.delete(early)

    class Cut:
        def appraise(self, context):
            mind.engine.delete(unnamed)
            paid(self)
            proposal, receipt = answered(Appraisal(reason=f"想起 {MARKER}", values={"curiosity": 66}))
            return proposal, {**receipt, **Truncated.receipt}

    class Again:
        def appraise(self, context):
            paid(self)
            proposal, receipt = answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))
            return proposal, {**receipt, **Truncated.receipt}

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal whose reads were cut short is not put to a light question")

    class Full:
        def appraise(self, context):
            mind.engine.delete(unrelated)
            paid(self)
            proposal, receipt = answered(Appraisal(reason="又是平常的一天", values={"curiosity": 58}))
            return proposal, {**receipt, "native_receipt": {"channel": "fork", "tool_calls": []}}

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Cut())
    settle(mind.engine)
    state, count, data = queue_row(mind, job["id"])
    # Refused for the delete, not for the judgment: no charged attempt (CL6E-MM-03).
    assert state != "complete" and data["error_detail"]["code"] == "shown-deleted" and count == 0
    assert unnamed_record in data["evaluated_ids"] and mind.read()["dimensions"]["curiosity"]["value"] != 66
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    assert queue_row(mind, job["id"])[0] == "complete" and mind.read()["dimensions"]["curiosity"]["value"] == 55
    other = jobs.enqueue([source("tomorrow", "明天也许下雨")], "synthetic-v1")
    jobs.run_one(Full())
    assert queue_row(mind, other["id"])[0] == "complete" and mind.read()["dimensions"]["curiosity"]["value"] == 58


def test_a_stored_proposal_whose_reads_were_cut_short_is_asked_again_in_full(setup):
    """A proposal kept for a later attempt after a conflict it may be reused over: its receipt was
    cut short, so nothing can say all it read still stands. The next attempt neither reuses it nor
    puts it to a light question; the model is asked in full (CL6D-MM-01)."""
    from eventmem.core.models import RevisionInput

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": "昨天说的一句话"})
    primary = source("today", "今天天气不错")
    asked = []

    class Reader:
        def appraise(self, context):
            record = mind.engine.get(recalled["record_id"])
            mind.engine.revise(recalled["record_id"], RevisionInput(expected_revision=record["revision"], command_id="fix-earlier",
                                                                    action="correct", content="昨天说的另一句话", reason="更正"))
            paid(self)
            proposal, receipt = answered(Appraisal(reason="想出去走走", values={"curiosity": 66}))
            return proposal, {**receipt, **Truncated.receipt}

    class Again:
        def appraise(self, context):
            asked.append("full")
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal whose reads were cut short is not put to a light question")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data.get("reuse"), "the proposal was kept for a later attempt"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and asked == ["full"], data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_a_daily_review_whose_reads_were_cut_short_keeps_no_reason_and_no_change_once_anything_is_deleted(setup):
    """With the behaviour chain off the day is reviewed in the fork, whose receipt is cut short. A
    note deleted while it answers is named nowhere: the change of personality is not recorded and
    the reason is not kept (CL6D-MM-01)."""
    from kin_mind.state import Evolution

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    claim, assessment = prospective_check(mind, source, clock)
    unnamed, _ = read_note(mind, clock, "daily-cut", f"没被点名的 {MARKER}")
    baseline = mind.read()["dimensions"]["curiosity"]["baseline"]

    class Daily:
        def appraise(self, context):
            mind.engine.delete(unnamed)
            proposal, receipt = answered(Appraisal(reason=f"因为 {MARKER}，更想去查证", evolution=Evolution(
                claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": baseline + 5})))
            return proposal, {**receipt, **Truncated.receipt}

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "needs-review" and reviewed["error"] == "Conflict"
    assert reviewed["reason_withheld"] == "sources-changed" and "reason" not in reviewed
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == baseline, "no change was recorded"
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_compression_whose_reads_were_cut_short_is_used_and_not_kept(system):
    """An appraisal's evidence compressed in the fork, whose receipt is cut short: no later delete
    could be sure to find a cached copy, so it is used by the appraisal that asked for it -- whose
    commit counts every delete since it began -- and not kept. A full receipt is kept
    (CL6D-MM-01)."""
    mind, memory, source, clock = system
    engine = mind.engine
    filler = "They checked the harbour path, the tide table and the lamps along the pier. " * 12
    contexts = Contexts(mind)

    def compress(key, cut):
        items = [contexts.record_item(engine.get(engine.source(source(f"{key}-{i}", f"Harbour walk note {key} {i}. " + filler))["record_ids"][0]))
                 for i in range(3)]

        class Fork:
            model = "synthetic-fork"

            def structured(self, name, schema, system, payload, **kwargs):
                value = schema.model_validate({"entries": [{"item_ids": payload["allowed_item_ids"], "summary": "They walked the harbour."}],
                                               "omitted_ids": []})
                return value, {"model": self.model, "channel": "fork", "tool_calls": [], **({"truncated": True} if cut else {})}

        return contexts.pack(items, "harbour walk " + key, 600, provider=Fork(), allow_model=True, persist=True)

    def cached():
        with engine.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM mind_context_cache").fetchone()[0]

    cut = compress("cut", True)
    assert cut["state"] == "compressed" and "They walked the harbour." in cut["text"]
    assert cached() == 0
    full = compress("full", False)
    assert full["state"] == "compressed" and cached() > 0
