"""The input manifest: what one appraisal attempt was actually shown, recorded by the host.

The host writes it while it builds the context, before any model call, and stores it content
addressed in `mind_appraisal_manifests`; the queue row keeps the digest only. It holds identifiers,
revisions, digests, enumerated states and the numeric and time values shown to the model. It never
holds a message body, a proposal or any other free text.

Two readers share this one record:

* the commit transaction, whose rebase asks whether a moved mind revision touched anything this
  judgment wrote or relied on (`rebase`);
* the next attempt after a commit conflict, which rebuilds its context as usual, compares the two
  manifests class by class (`compare`) and decides whether the stored proposal may be reused.

Relevance is decided by the **judgment type**, never by what the proposal happened to output: an
empty section is a judgment too. A judgment type or a class this module does not know is relevant.
Inside a class, the fields named in BOOKKEEPING are the only ones whose change does not count.

Equal request digests prove that two requests were equal, not that the context is still fresh. So
the manifest also keeps the clock the model saw, the time-derived values shown to it and the time
boundaries shown, and states its own validity: one ordinary attempt's bound or the nearest shown
boundary, whichever comes first. An old judgment is never valid merely because no predicate
flipped and no curve parameter changed.
"""

from __future__ import annotations

import json
from datetime import timedelta

from eventmem.core.db import digest, dumps

from .autonomy_schema import optimized
from .state import timestamp

MANIFEST_VERSION = "appraisal-manifest-v1"
# Default-on switches (mind_memory_config). All three off restores the stage-1 behavior exactly:
# no manifest is recorded, nothing is reused and the rebase compares what it compared before.
REBASE, REUSE, REVALIDATION = "manifest_rebase", "appraisal_reuse", "appraisal_revalidation"
RETENTION_DAYS = 7

# One table, content addressed at two levels. A manifest row names its classes by digest, and each
# class is a row of its own (lane '#part'), so consecutive attempts share whatever did not move.
SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_appraisal_manifests(
 digest TEXT PRIMARY KEY,scope TEXT NOT NULL,judgment TEXT NOT NULL,lane TEXT NOT NULL,
 built_at TEXT NOT NULL,valid_until TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_appraisal_manifest_time ON mind_appraisal_manifests(scope,built_at);
"""
PART = "#part"

HISTORY, MAINTENANCE, DELIVERY = "memory-history", "session-maintenance", "delivery"
MIND_STATE = ("dimensions", "desires", "concerns", "decisions", "rhythm", "assessment", "timing", "style")
# What a shared history formed, and what rests on it: the ledger's traits and the corrections that
# ended some of them, the behavior checks still open, and the intent in force. They are classes of
# their own rather than part of `profile_version`, so a trait that moved costs a reread of what
# rested on it and not of every stored proposal there is (decision 10).
LEDGER = ("traits", "corrections", "predictions", "intent")
# Every class a manifest of this version can carry. `time` stands for the validity check, and
# `sources` for a citable source that moved without the stored proposal citing it.
CLASSES = (*MIND_STATE, *LEDGER, "plans", "procedures", "habits", "graph", "topics", "works", "shares",
           "pending", "dialogue", "session", "sources", "time")
# Judgment type -> classes it does not rest on. History is shown no mood, wishes, plans, methods,
# timing, session or ledger; a receipt settlement cannot touch concerns or rhythm.
IRRELEVANT = {
    HISTORY: frozenset({*MIND_STATE, *LEDGER, "plans", "procedures", "time", "session"}),
    DELIVERY: frozenset({"concerns", "rhythm"}),
}
# Judgment type -> the only known classes it rests on (its root evidence is always compared).
RELEVANT_ONLY = {MAINTENANCE: frozenset({"session"})}

# Class -> entry fields whose change is bookkeeping: compare-and-swap tokens, the moment a curve was
# last re-anchored and review times. The values that move along a curve are not class content at
# all: they are kept under `time.values`, and validity bounds how far they may have moved.
BOOKKEEPING = {
    "dimensions": ("at",),
    "plans": ("plan_revision",),
    "graph": ("revision",), "works": ("revision",), "shares": ("revision",),
    "pending": ("cursor", "through_seq", "next_review", "revision"),
}
# Class -> entry fields that say who wrote the object last. The write set is compared on these.
VERSION = {
    "dimensions": ("event_id",), "rhythm": ("event_id",), "assessment": ("event_id",),
    "plans": ("plan_revision", "status", "revision", "state", "decision_id"),
}
SETTINGS_SHOWN = ("semantic", "operational_lanes", "semantic_actions", "autonomous_plans", "creative_execution",
                  "procedure_learning", "usage_reinforcement", "event_lifecycle", "graph", "sharing",
                  "associations", "auto_volumes")
POLICY_STATE = ("profile_version", "action_policy", "autonomy", "continuity")
# A proposal can only write these under a version it was shown. One it names without having been
# shown it cannot be proven untouched, so it is never reused without a question.
MUST_BE_SHOWN = frozenset({"desires", "concerns", "plans", "procedures", "graph", "shares"})


def switches(conn, scope):
    return {name: optimized(conn, scope, name) for name in (REBASE, REUSE, REVALIDATION)}


def judgment_type(stimulus):
    """The kind of judgment an attempt makes. It selects the dependency row, so it is derived from
    the host's own stimulus and never from anything the model returned."""
    if stimulus in {"memory-backfill", "memory-enrichment"}:
        return HISTORY
    return stimulus or "interaction"


def relevant(judgment, name):
    """Unregistered judgment types and unregistered classes are relevant."""
    only = RELEVANT_ONLY.get(judgment)
    if only is not None:
        return name in only or name not in CLASSES
    return name not in IRRELEVANT.get(judgment, ())


def attempt_bound(provider):
    """Seconds one ordinary attempt may take: its request timeout and the host's margin. The queue
    lease uses the same bound, so no attempt commits a judgment older than this."""
    return max(180, float(getattr(provider, "timeout", 90)) + 90)


def _reference(ref):
    return [ref["source_id"], ref["hash"], ref["revision"]]


def _shown(value, skipped=("revision", "updated_at")):
    return digest({k: v for k, v in value.items() if k not in skipped})


def _owner(conn, scope):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_runtime_events'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(MAX(seq),0) FROM mind_runtime_events WHERE scope=? AND kind='owner-message' "
                        "AND COALESCE(json_extract(data,'$.historical'),0)=0", (scope,)).fetchone()[0]


def _moment(value):
    """A shown time as an aware datetime, or None when it cannot bound anything."""
    try:
        moment = timestamp(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return moment if moment.tzinfo else None


def _mind_state(view, shown):
    """(classes, values) of the mind-state objects the model was shown.

    A class entry holds identifiers, versions, enumerated states and curve parameters; `values` holds
    the numbers that move along those curves as time passes. Text stays in the state and its history.
    """
    classes, values = {}, {}
    if "dimensions" in shown:
        classes["dimensions"], values["dimensions"] = {}, {}
        for name in shown["dimensions"]:
            entry, motive = view["dimensions"][name], view["dimensions"][name].get("motivation") or {}
            classes["dimensions"][name] = {
                "event_id": entry.get("event_id"), "target": entry.get("target"), "baseline": entry.get("baseline"),
                "half_life_hours": entry.get("half_life_hours"), "needs_review": entry.get("needs_review"),
                "motivation": [motive.get("target"), motive.get("half_life_minutes"), motive.get("episode_id")] if motive else None,
                "at": entry.get("observed_at")}
            values["dimensions"][name] = entry.get("projected_value")
    if "desires" in shown:
        chosen = {d["id"] for d in shown["desires"]}
        classes["desires"] = {"#window": {"shown": len(chosen), "total": (shown.get("desire_window") or {}).get("total", 0)}}
        for desire in view.get("desires", []):
            if desire["id"] in chosen:
                wait = desire.get("contact_wait") or {}
                classes["desires"][desire["id"]] = {
                    "revision": desire.get("revision"), "status": desire.get("status"), "kind": desire.get("kind"),
                    "strength": desire.get("strength"), "expires_at": desire.get("expires_at"),
                    "expired": desire.get("expired"), "needs_review": desire.get("needs_review"),
                    "concern_needs_review": desire.get("concern_needs_review"),
                    "wait": [wait.get("condition"), wait.get("retry_at")] if wait else None,
                    "exploration_id": desire.get("exploration_id")}
    features = (view.get("continuity") or {}).get("features") or {}
    if "concerns" in shown:
        chosen = {c["id"] for c in shown["concerns"]}
        classes["concerns"] = {"#window": {"shown": len(chosen), "total": (shown.get("concern_window") or {}).get("total", 0),
                                           "enabled": bool(features.get("concerns"))}}
        values["concerns"] = {}
        for concern in view.get("concerns", []):
            if concern["id"] in chosen:
                classes["concerns"][concern["id"]] = {
                    "revision": concern.get("revision"), "status": concern.get("status"), "kind": concern.get("kind"),
                    "needs_review": concern.get("needs_review"), "request": (concern.get("owner_request") or {}).get("status")}
                values["concerns"][concern["id"]] = concern.get("intensity")
    if "exploration_decisions" in shown:
        classes["decisions"] = {d["exploration_id"]: {"revision": d.get("revision"), "decision": d.get("decision"),
                                                      "needs_review": d.get("needs_review")}
                                for d in shown["exploration_decisions"] or []}
    if "rhythm" in shown:
        rhythm = shown["rhythm"] or {}
        classes["rhythm"] = {"rhythm": {
            "status": rhythm.get("status"), "phase": rhythm.get("phase"), "proposed_phase": rhythm.get("proposed_phase"),
            "target": rhythm.get("target"), "half_life_minutes": rhythm.get("half_life_minutes"),
            "event_id": rhythm.get("event_id"), "needs_review": rhythm.get("needs_review"),
            "interactions": (rhythm.get("interactions") or {}).get("fingerprint")}}
        values["rhythm"] = rhythm.get("alertness")
    if "appraisal_summary" in shown:
        summary = shown["appraisal_summary"] or {}
        classes["assessment"] = {"assessment": {"event_id": summary.get("event_id"), "needs_review": summary.get("needs_review"),
                                                "enabled": bool(features.get("interpretation"))}}
    if "interaction_timing" in shown:
        timing = shown["interaction_timing"] or {}
        classes["timing"] = {"timing": {k: timing.get(k) for k in ("last_owner_message_at", "last_proactive_accepted_at", "awaiting_reply")}}
        values["timing"] = {k: timing.get(k) for k in ("owner_silence_seconds", "unanswered_contact_seconds")}
    if "interaction_style" in shown:
        # Its band follows the flirtation score, so it can flip with no write at all.
        style = shown["interaction_style"] or {}
        classes["style"] = {"style": {k: style.get(k) for k in ("band", "event_id", "needs_review")}}
    return classes, values


def _ledger(conn, mind, view, clock):
    """The four stage-4 classes, each from the switch that produced it.

    `traits` and `corrections` are read off the projection the model was really shown; `predictions`
    and `intent` come from the modules that own them, because what a manifest needs of those — the
    revision, and whether the thing still holds — is deliberately not in the projection. With every
    switch off nothing here is produced at all, and the manifest is the one it was.
    """
    from .autonomy_schema import optimized
    from .traits import manifest_entries as ledger_entries
    found, scope = {}, mind.scope.key()
    traits, corrections = ledger_entries(view)
    if traits:
        found["traits"] = traits
    if corrections:
        found["corrections"] = corrections
    if optimized(conn, scope, "behavior_chain"):
        from .behavior_chain import manifest_entries as chain_entries
        found["predictions"] = chain_entries(conn, mind)
    if optimized(conn, scope, "expression_intent"):
        from .expression_intent import manifest_entries as intent_entries
        continuity = view.get("continuity") or {}
        # The same two facts `_continuity_view` reads before it decides whether an intent may shape
        # the wording at all. Two attempts derive it the same way, which is what a comparison needs.
        found["intent"] = intent_entries(
            conn, mind, clock, persona=view.get("persona_contract"),
            continuity_active=bool((continuity.get("features") or {}).get("expression"))
            and continuity.get("activation", "active") == "active")
    return {name: entry for name, entry in found.items() if entry}


def _boundaries(view, shown_state, context, clock):
    """Every future time boundary the model was shown: desire expiries and retries, step windows,
    review times and the next edges of the quiet hours."""
    found = []
    chosen = {d["id"] for d in shown_state.get("desires", [])}
    for desire in view.get("desires", []):
        if desire["id"] in chosen and desire.get("status") in {"wanted", "waiting", "in_progress"}:
            found.append(["desire-expiry", desire["id"], desire.get("expires_at")])
            found.append(["desire-retry", desire["id"], (desire.get("contact_wait") or {}).get("retry_at")])
    for plan in ((context.get("autonomy_context") or {}).get("plans") or {}).get("plans", []):
        found.append(["plan-review", plan["id"], plan.get("next_review_at")])
        for step in plan.get("steps", []):
            key = plan["id"] + "/" + step["id"]
            found.append(["step-window-opens", key, step.get("not_before")])
            found.append(["step-window-closes", key, step.get("not_after")])
            found.append(["decision-review", key, (step.get("decision") or {}).get("next_review_at")])
    if isinstance(context.get("memory_context"), dict):
        found.append(["memory-review", "memory", context["memory_context"].get("next_review")])
    contact, now = shown_state.get("contact") or {}, _moment(clock)
    if now and type(contact.get("quiet_start")) is int and type(contact.get("quiet_end")) is int:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(contact.get("timezone") or "Asia/Singapore"))
        for edge in ("quiet_start", "quiet_end"):
            at = local.replace(hour=contact[edge] % 24, minute=0, second=0, microsecond=0)
            found.append(["quiet-hours", edge, (at if at > local else at + timedelta(days=1)).isoformat()])
    future = [b for b in found if _moment(b[2]) and now and _moment(b[2]) > now]
    return sorted(future, key=lambda b: (_moment(b[2]), b[0], b[1]))[:40]


def build(jobs, provider, data, context, *, refs, targets, sources, view, lane, settings, isolation):
    """The manifest of the context `context`, as the model will be shown it."""
    from .appraisal import appraisal_context
    mind, scope = jobs.mind, jobs.mind.scope.key()
    shown = appraisal_context(context)
    shown_state = shown.get("state") if isinstance(shown.get("state"), dict) else {}
    memory = context.get("memory_context") if isinstance(context.get("memory_context"), dict) else None
    shown_memory = shown.get("memory_context") if memory else {}
    clock = (context.get("clock") or {}).get("current_time") or mind.clock()
    judgment = judgment_type(data.get("stimulus"))
    classes, values = _mind_state(view, shown_state)
    autonomy = context.get("autonomy_context") or {}
    if autonomy:
        classes["plans"] = {}
        recorded = (data.get("plan_view") or {}).get("plans", {})
        for plan in (autonomy.get("plans") or {}).get("plans", []):
            classes["plans"][plan["id"]] = {"plan_revision": plan.get("revision"), "status": plan.get("status"),
                                            "needs_review": plan.get("needs_review")}
            waiting = {s["id"]: s.get("waiting_reason") for s in plan.get("steps", [])}
            for step_id, step in (recorded.get(plan["id"]) or {}).get("steps", {}).items():
                classes["plans"][plan["id"] + "/" + step_id] = {**step, "waiting_reason": waiting.get(step_id)}
        classes["procedures"] = {p["id"]: {"revision": p.get("revision"), "status": p.get("status"), "executable": p.get("executable")}
                                 for p in (autonomy.get("procedures") or {}).get("procedures", [])}
    habits = (memory or {}).get("conversation_habits") or (jobs.memory.habits.read() if judgment != MAINTENANCE else None)
    if habits:
        classes["habits"] = {"habits": {"revision": habits.get("revision"), "preferences": digest(habits.get("preferences"))}}
    if memory:
        classes["graph"] = {n["id"]: {"revision": n.get("revision"), "kind": n.get("kind"), "needs_review": n.get("needs_review"),
                                      "shown": _shown(n)} for n in shown_memory.get("graph_candidates", [])}
        classes["topics"] = {t["id"]: {"revision": t.get("revision"), "members": [[m["id"], m.get("revision")] for m in t.get("members", [])]}
                             for t in shown_memory.get("topic_candidates", [])}
        for kind in ("works", "shares"):
            classes[kind] = {n["id"]: {"revision": n.get("revision"), "needs_review": n.get("needs_review"), "shown": _shown(n)}
                             for n in shown_memory.get(kind, [])}
        classes["pending"] = {"pending": {"seqs": [e.get("seq") for e in memory.get("pending_events", [])],
                                          "cursor": memory.get("cursor"), "through_seq": memory.get("through_seq"),
                                          "next_review": memory.get("next_review"), "revision": memory.get("revision")}}
    classes["dialogue"] = {str(item.get("seq")): {"id": item.get("id"), "kind": item.get("kind"), "source_id": item.get("source_id"),
                                                  "delivery": item.get("delivery_state")} for item in context.get("recent_dialogue") or []}
    session = context.get("session_context")
    if isinstance(session, dict):
        classes["session"] = {"session": {
            "id": session.get("id"), "generation": (session.get("binding") or {}).get("generation"),
            "compaction": (session.get("lastCompaction") or {}).get("id"),
            "evidence": [[e.get("id"), e.get("at"), bool(e.get("needsReview")), bool(e.get("resolved"))] for e in session.get("evidence", [])],
            "recent": [e.get("id") for e in session.get("recent", [])]}}
    with mind.engine.db.connect() as conn:
        epoch = _owner(conn, scope)
        classes.update(_ledger(conn, mind, view, clock))
    bound = attempt_bound(provider)
    boundaries = _boundaries(view, shown_state, context, clock)
    limit = timestamp(clock) + timedelta(seconds=bound)
    valid_until = min([limit, *(_moment(b[2]) for b in boundaries[:1])]).isoformat()
    profile = provider.request_profile(context) if hasattr(provider, "request_profile") else {}
    capabilities = jobs.exploration_capabilities
    if context.get("effective_memory_use"):
        values["reinforcement"] = digest(context["effective_memory_use"])
    policy = {
        "agent_version": view.get("agent_version"), "enqueued_agent_version": data.get("agent_version"),
        "configuration": capabilities.get("version") or (view.get("continuity") or {}).get("version")
                         or (view.get("action_policy") or {}).get("version"),
        "profile_version": view.get("profile_version"), "persona": view.get("persona_contract"),
        **{name: digest(view.get(name)) for name in ("action_policy", "autonomy", "continuity", "contact", "exploration")},
        "capabilities": digest(capabilities), "settings": {k: settings.get(k) for k in SETTINGS_SHOWN},
        "section_isolation": bool(isolation), "operational": bool(context.get("operational_only")),
        "environment": digest(autonomy.get("execution_environment")) if autonomy else None,
        "definitions": digest(shown.get("definitions")),
    }
    return {
        "version": MANIFEST_VERSION,
        "judgment": {"type": judgment, "lane": lane, "stimulus": data.get("stimulus")},
        "built_at": clock, "valid_until": valid_until,
        # What frames the request is compared like policy. The digest of the context proves two
        # requests equal and nothing more: the clock alone makes it differ from one attempt to the next.
        "request": {**{name: profile.get(name) for name in ("system", "schema", "model", "parameters")}, "context": digest(dumps(shown))},
        "policy": policy,
        "owner": {"latest_owner_seq": (memory or {}).get("latest_owner_seq", epoch), "owner_epoch": epoch,
                  "last_owner_message_at": (view.get("interaction_timing") or {}).get("last_owner_message_at")},
        "time": {"clock": clock, "bound_seconds": bound, "boundaries": boundaries, "values": values},
        "state_revision": view.get("revision"),
        "roots": {r["record_id"]: _reference(r) for r in refs},
        "targets": {t["record_id"]: [*_reference(t), t.get("exploration_id")] for t in targets},
        "sources": {r["record_id"]: _reference(r) for r in sources},
        "classes": classes,
    }


def record(jobs, provider, data, context, flags, **facts):
    """Build and store the manifest of this attempt, unless every switch that reads it is off.

    A fault here never fails an appraisal: without a manifest the attempt behaves as it did
    before, with nothing to reuse and the previous rebase."""
    data.pop("manifest", None)
    if not any(flags.values()):
        return None
    try:
        # Through JSON once, so this attempt compares it exactly as a later attempt will read it.
        built = json.loads(dumps(build(jobs, provider, data, context, **facts)))
        data["manifest"] = store(jobs.engine, jobs.mind.scope.key(), built)
        return built
    except Exception as error:  # noqa: BLE001 - an optimization's own fault; only its class is recorded
        jobs.engine.db.metric("appraisal_manifest_failed", 1, {"error": type(error).__name__})
        return None


def store(engine, scope, manifest):
    """Content addressed twice over: the same manifest is one row however many attempts recorded
    it, and each class is a row of its own that every manifest showing the same objects shares."""
    parts = {"sources": manifest["sources"], **{"classes/" + name: value for name, value in manifest["classes"].items()}}
    bodies = {name: dumps(value) for name, value in parts.items()}
    keys = {name: digest([name, body]) for name, body in bodies.items()}
    body = dumps({**manifest, "sources": keys["sources"],
                  "classes": {name: keys["classes/" + name] for name in manifest["classes"]}})
    key = digest(body)
    at, valid = manifest["built_at"], manifest["valid_until"]
    cutoff = (timestamp(at) - timedelta(days=RETENTION_DAYS)).isoformat()
    with engine.db.connect(write=True) as conn:
        for statement in SCHEMA.strip().split(";\n"):
            # One at a time: executescript() would commit the transaction this write belongs to.
            conn.execute(statement)
        for name, part in bodies.items():
            # A shared part lives as long as the newest manifest that names it.
            conn.execute("INSERT INTO mind_appraisal_manifests VALUES(?,?,?,?,?,?,?) ON CONFLICT(digest) DO UPDATE SET built_at=excluded.built_at",
                         (keys[name], scope, name, PART, at, valid, part))
        conn.execute("INSERT OR IGNORE INTO mind_appraisal_manifests VALUES(?,?,?,?,?,?,?)",
                     (key, scope, manifest["judgment"]["type"], manifest["judgment"]["lane"], at, valid, body))
        # A manifest serves the retries of one evaluation and its audit; nothing reads one this old.
        conn.execute("DELETE FROM mind_appraisal_manifests WHERE scope=? AND built_at<?", (scope, cutoff))
    return key


def load(engine, scope, key):
    """The manifest with its parts resolved, or None when it (or any part of it) is gone."""
    if not key:
        return None
    with engine.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_appraisal_manifests'").fetchone():
            return None

        def read(digest_key):
            row = conn.execute("SELECT data FROM mind_appraisal_manifests WHERE digest=? AND scope=?", (digest_key, scope)).fetchone()
            return json.loads(row[0]) if row else None
        manifest = read(key)
        if manifest is None:
            return None
        manifest["sources"] = read(manifest["sources"])
        manifest["classes"] = {name: read(part) for name, part in manifest["classes"].items()}
    if manifest["sources"] is None or any(part is None for part in manifest["classes"].values()):
        return None
    return manifest


def expired(manifest, now):
    return timestamp(now) >= timestamp(manifest["valid_until"])


def framing(manifest):
    """Policy, persona, configuration and what frames the request: a change of any of it changes the
    prompt itself, so nothing judged under the old one is reused or revalidated."""
    return [manifest.get("policy"), {k: v for k, v in (manifest.get("request") or {}).items() if k != "context"}]


def _without(name, entry, fields):
    skipped = fields.get(name, ())
    return {k: v for k, v in entry.items() if k not in skipped} if isinstance(entry, dict) else entry


def _version(name, entry):
    fields = VERSION.get(name, ("revision",))
    return [entry.get(k) for k in fields] if isinstance(entry, dict) else entry


def compare(old, new):
    """Every object whose entry differs between two manifests, class by class.

    `relevant`: more than bookkeeping moved, or the object entered or left the view.
    `version`: someone wrote the object, which matters when the stored proposal writes it too.
    """
    changes = []
    for name in sorted(set(old.get("classes", {})) | set(new.get("classes", {}))):
        before, after = old["classes"].get(name) or {}, new["classes"].get(name) or {}
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            appeared = a is None or b is None
            changes.append({"class": name, "object": key, "before": a, "after": b,
                            "relevant": appeared or _without(name, a, BOOKKEEPING) != _without(name, b, BOOKKEEPING),
                            "version": appeared or _version(name, a) != _version(name, b)})
    return changes


def write_set(proposal):
    """(class, object) of everything a proposal writes under a version of its own. `proposal` is a dump."""
    memory = proposal.get("memory") or {}
    graph = memory.get("graph") or {}
    written = {("dimensions", name) for name in [*(proposal.get("values") or {}), *(proposal.get("motivations") or {})]}
    written |= {("desires", u["desire_id"]) for u in proposal.get("wish_updates") or []}
    written |= {("concerns", c["concern_id"]) for c in proposal.get("concerns") or [] if c.get("concern_id")}
    written |= {("decisions", s["exploration_id"]) for s in proposal.get("sharing") or []}
    written |= {("plans", c["id"]) for c in proposal.get("plan_changes") or [] if c.get("id")}
    # A decision on a plan this very proposal creates names it by key: nobody else has a version of it.
    created = {c.get("key") for c in proposal.get("plan_changes") or [] if c.get("action") == "create"}
    written |= {("plans", d["plan_id"] + "/" + d["step_id"]) for d in proposal.get("action_decisions") or []
                if d["plan_id"] not in created}
    written |= {("procedures", c["id"]) for c in proposal.get("procedure_candidates") or [] if c.get("id")}
    # A trait decision fences itself on the revision it was taken against, exactly as a plan change
    # does. One that names no trait is a first proposal: nobody else has a version of it yet.
    written |= {("traits", d["trait_id"]) for d in proposal.get("trait_decisions") or [] if d.get("trait_id")}
    written |= {("graph", n["id"]) for n in graph.get("nodes") or [] if n.get("id")}
    for route in memory.get("event_routes") or []:
        written |= {("graph", identifier) for identifier in (route.get("event_id"), route.get("thread_id")) if identifier}
    written |= {("shares", d["share_id"]) for d in memory.get("disclosures") or []}
    for mapping in (memory.get("coverage") or {}).get("mappings") or []:
        written.add(("shares", mapping["share_id"]))
        written |= {("graph", r["unit_id"]) for r in mapping.get("references") or []}
    if proposal.get("rhythm"):
        written.add(("rhythm", "rhythm"))
    if proposal.get("habits"):
        written.add(("habits", "habits"))
    return written


def _written(shown, state):
    """(class, what the model saw, what the state holds now), read from a raw state inside the commit
    transaction. Only writes are compared here: time and freshness predicates are not recomputed,
    because an ordinary attempt accepts them as they stood when its context was built."""
    rows = []
    if "dimensions" in shown:
        def motive(entry):
            value = (entry or {}).get("motivation") or {}
            return [value.get("target"), value.get("half_life_minutes"), value.get("episode_id")] if value else None
        rows.append(("dimensions",
                     {k: [e.get("event_id"), e.get("target"), e.get("baseline"), e.get("half_life_hours"), e.get("motivation")]
                      for k, e in shown["dimensions"].items()},
                     {k: [(state["dimensions"].get(k) or {}).get(f) for f in ("event_id", "target", "baseline", "half_life_hours")]
                         + [motive(state["dimensions"].get(k))] for k in shown["dimensions"]}))
    if "desires" in shown:
        seen = {k: [e.get("revision"), e.get("status")] for k, e in shown["desires"].items() if k != "#window"}
        # The total the model was shown counts the finished wishes that have been moved out of the
        # document as well as the ones still in it, so this side counts them the same way. Read
        # from the state's own count rather than from the archive, because this runs inside the
        # commit transaction and the number has to be the one this revision carries.
        rows.append(("desires", {"#total": shown["desires"]["#window"]["total"], **seen},
                     {"#total": len(state.get("desires", {})) + ((state.get("desire_archive") or {}).get("count") or 0),
                      **{k: [(state["desires"].get(k) or {}).get(f) for f in ("revision", "status")] for k in seen}}))
    if "concerns" in shown and shown["concerns"]["#window"].get("enabled"):
        seen = {k: e.get("revision") for k, e in shown["concerns"].items() if k != "#window"}
        rows.append(("concerns", {"#total": shown["concerns"]["#window"]["total"], **seen},
                     {"#total": len(state.get("concerns", {})),
                      **{k: (state.get("concerns", {}).get(k) or {}).get("revision") for k in seen}}))
    if "decisions" in shown:
        rows.append(("decisions", {k: e.get("revision") for k, e in shown["decisions"].items()},
                     {k: (state.get("exploration_decisions", {}).get(k) or {}).get("revision") for k in shown["decisions"]}))
    if "rhythm" in shown and shown["rhythm"]["rhythm"].get("status") != "disabled":
        rows.append(("rhythm", shown["rhythm"]["rhythm"].get("event_id"), (state.get("rhythm") or {}).get("event_id")))
    if "assessment" in shown and shown["assessment"]["assessment"].get("enabled"):
        rows.append(("assessment", shown["assessment"]["assessment"].get("event_id"), (state.get("last_assessment") or {}).get("event_id")))
    return rows


def _persona(mind):
    from eventmem.core.persona import load_persona, persona_metadata
    try:
        return persona_metadata(load_persona(mind.engine, mind.scope))
    except ValueError:
        return "needs-review"


def rebase(mind, manifest, before, state, proposal, *, historical):
    """Whether a proposal judged on `before` may commit on `state`, whose revision has moved.

    The write set is narrowed to what the proposal writes: the dimensions it scores and the wishes
    and concerns it updates. The read set is everything of the mind state this judgment type was
    shown, compared by the same relevance the manifests are compared by across attempts.
    """
    if _persona(mind) != manifest["policy"].get("persona"):
        # The prompt itself would differ now: nothing judged under the old contract is carried over.
        return False
    if historical:
        return state.get("profile_version") == before.get("profile_version")
    if manifest.get("state_revision") != before.get("revision"):
        # The state moved between the view the model was given and this baseline: nothing is proven.
        return False
    if any(state.get(k) != before.get(k) for k in POLICY_STATE):
        return False
    written = [*proposal.values, *proposal.motivations]
    if any((state["dimensions"].get(k) or {}).get("event_id") != (before["dimensions"].get(k) or {}).get("event_id") for k in written):
        return False
    if any(state["desires"].get(u.desire_id) != before["desires"].get(u.desire_id) for u in proposal.wish_updates):
        return False
    for concern in proposal.concerns:
        identifier = getattr(concern, "concern_id", None)
        if identifier and state.get("concerns", {}).get(identifier) != before.get("concerns", {}).get(identifier):
            return False
    judgment = manifest["judgment"]["type"]
    return all(seen == current for name, seen, current in _written(manifest.get("classes", {}), state) if relevant(judgment, name))
