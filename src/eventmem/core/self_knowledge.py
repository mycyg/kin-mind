"""Versioned self-claims and prospective, source-backed behavioral checks.

Entries use ordinary memory records, revisions and dependencies. A role declaration
is an explicit agreement; a hypothesis remains an inference regardless of its score.
Scores describe recorded assessments, not an independent verification of the assessor.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .db import Conflict, Missing, digest
from .models import Model, RecordInput, Scope, now
from .retrieval import tokens


class ClaimInput(Model):
    command_id: str = Field(min_length=1, max_length=200)
    aspect: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=1000)
    agent_version: str = Field(min_length=1, max_length=200)
    claim: str = Field(min_length=1, max_length=4000)
    basis: Literal["role", "hypothesis"] = "hypothesis"
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    supersedes: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("command_id", "aspect", "context", "agent_version", "claim")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank value is required")
        return value.strip()

    @model_validator(mode="after")
    def replacement(self):
        if bool(self.supersedes) != (self.expected_revision is not None):
            raise ValueError("A replacement requires the previous id and revision")
        return self


class SupersessionInput(Model):
    command_id: str = Field(min_length=1, max_length=200)
    old_id: str
    new_id: str
    # Both claims are compared and swapped: a chain is repaired against the state it was read in.
    expected_revisions: dict[str, int] = Field(min_length=2, max_length=2)


# The reason stored on the repaired revision. A chain repair has no text of its own to quote.
SUPERSESSION_REASON = "A later claim on the same aspect and context replaces this one"


class PredictionInput(Model):
    command_id: str = Field(min_length=1, max_length=200)
    claim_id: str
    expected_revision: int = Field(ge=1)
    case_id: str = Field(min_length=1, max_length=200)
    behavior: str = Field(min_length=1, max_length=2000)
    information: str = Field(min_length=1, max_length=2000)
    probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    generic_probability: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )

    _nonblank = field_validator("command_id", "case_id", "behavior", "information")(
        ClaimInput.nonblank.__func__
    )


class AssessmentInput(Model):
    command_id: str = Field(min_length=1, max_length=200)
    prediction_id: str
    expected_revision: int = Field(ge=1)
    outcome: bool | None
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    note: str = Field(min_length=1, max_length=2000)

    _nonblank = field_validator("command_id", "note")(ClaimInput.nonblank.__func__)


def metadata(record):
    return record.get("attributes", {}).get("self_knowledge", {})


class SelfKnowledge:
    def __init__(self, engine, scope: Scope, clock=now):
        # The clock is injectable because ordering is the whole point of a prospective check: a
        # host that replays a chain must be able to say which moment came first.
        self.engine, self.scope, self.clock = engine, scope, clock

    def _get(self, conn, rid, entry=None):
        row = self.engine._get(conn, rid)
        if row["scope"] != self.scope.model_dump():
            raise Conflict("Self-knowledge evidence belongs to another scope")
        if entry and metadata(row).get("entry") != entry:
            raise Conflict(f"Expected a self-knowledge {entry}")
        return row

    def _evidence(self, conn, ids, *, authorities=None, after=None):
        sources, snapshots, hashes = set(), {}, set()
        stamp = self.clock()
        for rid in sorted(set(ids)):
            row = self._get(conn, rid)
            if row["status"] != "active" or not self._current(row, stamp):
                raise Conflict("Evidence must be active and current")
            if authorities and (row["generated"] or row["confirmation"] == "inferred"):
                raise Conflict(
                    "Use an explicit report or operation, not a model interpretation"
                )
            if not row["source_ids"]:
                raise Conflict("Evidence requires a retained source")
            snapshots[rid] = row["revision"]
            for sid in row["source_ids"]:
                source = conn.execute(
                    "SELECT * FROM sources WHERE id=? AND deleted=0", (sid,)
                ).fetchone()
                if not source:
                    raise Missing(sid)
                if source["scope"] != self.scope.key():
                    raise Conflict("Source scope differs")
                authority = json.loads(source["data"])["authority"]
                if authorities and authority not in authorities:
                    raise Conflict(
                        "Evidence does not have the required source authority"
                    )
                if after and source["occurred_at"] <= after:
                    # The same moment is not later: evidence recorded with the prediction was
                    # available to whoever made it, and tests nothing.
                    raise Conflict("Outcome evidence must be later than the prediction")
                if source["occurred_at"] > stamp:
                    raise Conflict("Evidence has not occurred yet")
                sources.add(sid)
                hashes.add(source["hash"])
        return sorted(sources), snapshots, sorted(hashes)

    def _within(self, conn, name, request, run):
        """The command, in a transaction the caller owns, so one appraisal can record a hypothesis
        and its predictions beside everything else it commits, and refuse them together."""
        payload = {"scope": self.scope.model_dump(), "request": request.model_dump()}
        key = "self:" + name + ":" + digest([self.scope.key(), request.command_id])
        result = self.engine.command(conn, key, payload, lambda: run(conn))
        # A receipt replay reports current state and cannot resurrect deletion.
        return self._get(conn, result["id"])

    def _command(self, name, request, run):
        with self.engine.db.connect(write=True) as conn:
            return self._within(conn, name, request, run)

    def claim(self, request: ClaimInput, *, compat=None):
        return self._command("claim", request, self._claiming(request, compat))

    def claim_in(self, conn, request: ClaimInput, *, compat=None):
        """`claim()` inside the caller's transaction."""
        return self._within(conn, "claim", request, self._claiming(request, compat))

    def _claiming(self, request, compat):
        def run(conn):
            sources, snapshots, _ = self._evidence(
                conn,
                request.evidence_ids,
                authorities={"explicit"} if request.basis == "role" else None,
            )
            old = None
            if request.supersedes:
                old = self._get(conn, request.supersedes, "claim")
                prior = metadata(old)
                if old["revision"] != request.expected_revision:
                    raise Conflict("Self-claim revision changed")
                if old["status"] not in {"active", "unverified"}:
                    raise Conflict("Only a current claim can be replaced")
                if any(
                    prior[key].casefold() != getattr(request, key).casefold()
                    for key in ("aspect", "context", "basis")
                ):
                    raise Conflict(
                        "Replacement must keep the same aspect, context and basis"
                    )
            info = {
                "entry": "claim",
                "basis": request.basis,
                "aspect": request.aspect,
                "context": request.context,
                "agent_version": request.agent_version,
                "evidence_revisions": snapshots,
                "supersedes": request.supersedes,
                # The host's stamp of the configuration this was made under, when it keeps one.
                # Absent on everything written before the stamp existed, and through the tools.
                **({"compat": compat} if compat else {}),
            }
            row = self.engine._insert(
                conn,
                RecordInput(
                    kind="self_narrative",
                    title=request.aspect,
                    content=request.claim,
                    scope=self.scope,
                    # The host's clock decides when an entry of this chain became true, so a replay
                    # orders a claim, its predictions and their evidence the way they happened.
                    valid_from=self.clock(),
                    source_ids=sources,
                    evidence_ids=request.evidence_ids,
                    generated=request.basis == "hypothesis",
                    confirmation="inferred"
                    if request.basis == "hypothesis"
                    else "explicit",
                    status="unverified" if request.basis == "hypothesis" else "active",
                    attributes={"self_knowledge": info},
                ),
            )
            if old:
                old["status"] = "superseded"
                old["valid_until"] = row["valid_from"]
                old["attributes"]["superseded_by"] = row["id"]
                self.engine._save_revision(
                    conn, old, "self_claim_replaced", request.claim
                )
                self.engine._relation(conn, old["id"], "superseded_by", row["id"], {})
            self.engine.db.bump(conn)
            return row

        return run

    def link_supersession(self, old_id, new_id, expected_revisions, command_id):
        """Repeat what `claim()` does to the claim it replaces, without inserting a record.

        A chain repaired after the fact has no new claim to make and no current explicit
        evidence to offer: both claims are already stored. Calling `claim()` would mint a
        duplicate of the later one and refuse anything whose evidence has since moved on.
        """
        request = SupersessionInput(command_id=command_id, old_id=old_id, new_id=new_id,
                                    expected_revisions={str(k): int(v) for k, v in dict(expected_revisions).items()})

        def run(conn):
            if request.old_id == request.new_id:
                raise Conflict("A claim cannot supersede itself", kind="semantic", code="supersession-invalid")
            old, new = self._get(conn, request.old_id, "claim"), self._get(conn, request.new_id, "claim")
            for row in (old, new):
                if request.expected_revisions.get(row["id"]) != row["revision"]:
                    raise Conflict("Self-claim revisions changed before the supersession", kind="runtime",
                                   code="supersession-revision-changed", target=row["id"],
                                   expected=request.expected_revisions.get(row["id"]), actual=row["revision"])
            prior, later = metadata(old), metadata(new)
            if prior.get("basis") != "role" or later.get("basis") != "role":
                raise Conflict("Only role claims are chained by a supersession", kind="semantic", code="supersession-invalid")
            if any(prior[key].casefold() != later[key].casefold() for key in ("aspect", "context", "basis")):
                raise Conflict("A supersession keeps the same aspect, context and basis", kind="semantic", code="supersession-invalid")
            if old["status"] not in {"active", "unverified"}:
                raise Conflict("Only a current claim can be superseded", kind="semantic",
                               code="supersession-invalid", target=old["id"], actual=old["status"])
            if new["valid_from"] <= old["valid_from"]:
                # `valid_until` must be later than `valid_from`; a tie is left to the caller.
                raise Conflict("A superseding claim must be valid after the one it replaces", kind="semantic",
                               code="supersession-invalid", target=old["id"])
            old["status"] = "superseded"
            old["valid_until"] = new["valid_from"]
            old["attributes"]["superseded_by"] = new["id"]
            self.engine._save_revision(conn, old, "self_claim_replaced", SUPERSESSION_REASON)
            self.engine._relation(conn, old["id"], "superseded_by", new["id"], {})
            self.engine.db.bump(conn)
            return {"state": "linked", "old_id": old["id"], "new_id": new["id"], "revision": old["revision"]}

        payload = {"scope": self.scope.model_dump(), "request": request.model_dump()}
        key = "self:supersession:" + digest([self.scope.key(), request.command_id])
        with self.engine.db.connect(write=True) as conn:
            return self.engine.command(conn, key, payload, lambda: run(conn))

    def predict(self, request: PredictionInput, *, compat=None, window_hours=None):
        return self._command("prediction", request, self._predicting(request, compat, window_hours))

    def predict_in(self, conn, request: PredictionInput, *, compat=None, window_hours=None):
        """`predict()` inside the caller's transaction. `window_hours` is the host's own note of
        when the behavior was expected to show; the caller, not the model, decides what it means."""
        return self._within(conn, "prediction", request, self._predicting(request, compat, window_hours))

    def _predicting(self, request, compat, window_hours):
        def run(conn):
            claim = self._get(conn, request.claim_id, "claim")
            info = metadata(claim)
            if claim["revision"] != request.expected_revision:
                raise Conflict("Self-claim revision changed")
            if claim["status"] not in {"active", "unverified"}:
                raise Conflict("A prediction requires a current claim")
            if not self._fresh(conn, claim):
                raise Conflict("Self-claim evidence changed")
            # One forecast per case per configuration. With a stamp the configuration is what the
            # host says decides behavior; without one it stays the agent version, as it was.
            field, value = ("compat.key", compat["key"]) if compat else ("agent_version", info["agent_version"])
            duplicate = conn.execute(
                "SELECT 1 FROM records WHERE scope=? AND deleted=0 "
                "AND json_extract(data,'$.attributes.self_knowledge.entry')='prediction' "
                "AND json_extract(data,'$.attributes.self_knowledge.case_id')=? "
                f"AND json_extract(data,'$.attributes.self_knowledge.{field}')=?",
                (self.scope.key(), request.case_id, value),
            ).fetchone()
            if duplicate:
                raise Conflict(
                    "This case already has a prediction for the agent version"
                )
            row = self.engine._insert(
                conn,
                RecordInput(
                    kind="prediction",
                    title=request.behavior[:1000],
                    content=request.behavior,
                    scope=self.scope,
                    valid_from=self.clock(),
                    source_ids=claim["source_ids"],
                    evidence_ids=[claim["id"]],
                    generated=True,
                    confirmation="inferred",
                    attributes={
                        "self_knowledge": {
                            "entry": "prediction",
                            "claim_id": claim["id"],
                            "claim_revision": claim["revision"],
                            "aspect": info["aspect"],
                            "context": info["context"],
                            "agent_version": info["agent_version"],
                            "case_id": request.case_id,
                            "information": request.information,
                            "probability": request.probability,
                            "generic_probability": request.generic_probability,
                            **({"compat": compat} if compat else {}),
                            **({"test_window_hours": window_hours} if window_hours else {}),
                        }
                    },
                ),
            )
            self.engine.db.bump(conn)
            return row

        return run

    def assess(self, request: AssessmentInput, *, compat=None):
        return self._command("assessment", request, self._assessing(request, compat))

    def assess_in(self, conn, request: AssessmentInput, *, compat=None):
        """`assess()` inside the caller's transaction."""
        return self._within(conn, "assessment", request, self._assessing(request, compat))

    def _assessing(self, request, compat):
        def run(conn):
            prediction = self._get(conn, request.prediction_id, "prediction")
            if (
                prediction["revision"] != request.expected_revision
                or prediction["revision"] != 1
            ):
                raise Conflict(
                    "A revised forecast cannot be scored as a prospective prediction"
                )
            if prediction["status"] != "active":
                raise Conflict("Prediction is not active")
            sources, snapshots, hashes = self._evidence(
                conn,
                request.evidence_ids,
                authorities={"explicit", "operation"},
                # When the forecast was made, by the clock that made it, not by storage time.
                after=prediction["valid_from"],
            )
            existing = conn.execute(
                "SELECT 1 FROM records WHERE scope=? AND deleted=0 "
                "AND json_extract(data,'$.attributes.self_knowledge.entry')='assessment' "
                "AND json_extract(data,'$.attributes.self_knowledge.prediction_id')=?",
                (self.scope.key(), prediction["id"]),
            ).fetchone()
            if existing:
                raise Conflict(
                    "Prediction already assessed; use the existing record's correction history"
                )
            info = metadata(prediction)
            row = self.engine._insert(
                conn,
                RecordInput(
                    kind="observation",
                    title=prediction["title"],
                    content=request.note,
                    scope=self.scope,
                    valid_from=self.clock(),
                    source_ids=sources,
                    evidence_ids=[prediction["id"], *request.evidence_ids],
                    # Recording a reported outcome is not independent validation.
                    generated=True,
                    confirmation="inferred",
                    attributes={
                        "self_knowledge": {
                            "entry": "assessment",
                            "prediction_id": prediction["id"],
                            "prediction_revision": prediction["revision"],
                            "aspect": info["aspect"],
                            "context": info["context"],
                            "agent_version": info["agent_version"],
                            "case_id": info["case_id"],
                            "outcome": request.outcome,
                            "evidence_revisions": snapshots,
                            "source_hashes": hashes,
                            **({"compat": compat} if compat else {}),
                        }
                    },
                ),
            )
            self.engine.db.bump(conn)
            return row

        return run

    @staticmethod
    def _current(row, stamp):
        return (
            row["valid_from"] <= stamp
            and (not row["valid_until"] or row["valid_until"] > stamp)
            and not row["attributes"].get("stale_evidence")
            and not row["attributes"].get("analysis_pending")
        )

    def _fresh(self, conn, row):
        stamp = self.clock()
        for rid, revision in metadata(row).get("evidence_revisions", {}).items():
            try:
                evidence = self._get(conn, rid)
            except Missing:
                return False
            if (
                evidence["revision"] != revision
                or evidence["status"] != "active"
                or not self._current(evidence, stamp)
            ):
                return False
        return self._current(row, stamp)

    def view(
        self,
        *,
        agent_version=None,
        history=False,
        aspect=None,
        context=None,
        limit=50,
        budget=2000,
    ):
        if not history and not (agent_version and agent_version.strip()):
            raise ValueError(
                "Current self-knowledge requires an explicit agent_version"
            )
        if not 1 <= limit <= 100 or not 128 <= budget <= 32000:
            raise ValueError("Use limit 1..100 and text budget 128..32000")
        clauses = [
            "scope=?",
            "deleted=0",
            "json_extract(data,'$.attributes.self_knowledge.entry') IS NOT NULL",
        ]
        args = [self.scope.key()]
        for field, value in (
            ("agent_version", agent_version),
            ("aspect", aspect),
            ("context", context),
        ):
            if value:
                clauses.append(
                    f"json_extract(data,'$.attributes.self_knowledge.{field}')=?"
                )
                args.append(value)
        if not history:
            clauses.append("status IN ('active','unverified')")
        with self.engine.db.connect() as conn:
            rows = conn.execute(
                "SELECT data FROM records WHERE "
                + " AND ".join(clauses)
                + " ORDER BY received_at DESC,id LIMIT ?",
                [*args, limit + 1],
            ).fetchall()
            records = [json.loads(r[0]) for r in rows[:limit]]
            items, lines, scored, seen_hashes = [], [], {}, {}
            omitted = 0
            for row in records:
                info = metadata(row)
                fresh = self._fresh(conn, row)
                if not history and not fresh:
                    continue
                label = info.get("basis", info["entry"])
                detail = ""
                if info["entry"] == "prediction":
                    detail = f" P(self)={info['probability']}; P(generic)={info['generic_probability']}."
                elif info["entry"] == "assessment":
                    detail = f" Reported outcome={info['outcome']}."
                prefix = f"[{row['id']} {label} {row['status']} {row['confirmation']} {info['agent_version']}] "
                line = prefix + row["content"] + detail
                if tokens("\n".join([*lines, line])) > budget:
                    line = prefix + f"hint; read {row['read_url']}"
                if tokens("\n".join([*lines, line])) > budget:
                    omitted += 1
                    continue
                lines.append(line)
                items.append(
                    {
                        "id": row["id"],
                        "revision": row["revision"],
                        "status": row["status"],
                        "confirmation": row["confirmation"],
                        "generated": row["generated"],
                        "source_ids": row["source_ids"],
                        "read_url": row["read_url"],
                        "fresh_evidence": fresh,
                        "self_knowledge": info,
                    }
                )
                if (
                    info["entry"] != "assessment"
                    or info["outcome"] is None
                    or not fresh
                    or row["status"] != "active"
                ):
                    continue
                try:
                    prediction = self._get(conn, info["prediction_id"], "prediction")
                except Missing:
                    continue
                # Keep the pre-outcome forecast fixed, including after corrections.
                if (
                    prediction["revision"] != info["prediction_revision"]
                    or prediction["status"] != "active"
                ):
                    continue
                hashes = set(info["source_hashes"])
                version = info["agent_version"]
                seen = seen_hashes.setdefault(version, set())
                if hashes & seen:
                    continue
                seen.update(hashes)
                p = metadata(prediction)
                scored.setdefault(version, []).append(
                    (p["probability"], p["generic_probability"], int(info["outcome"]))
                )
        if agent_version:
            scored.setdefault(agent_version, [])
        text = "\n".join(lines)
        return {
            "items": items,
            "text": text,
            "tokens": tokens(text),
            "budget": budget,
            "has_more": len(rows) > limit,
            "omitted_for_budget": omitted,
            "instruction_authority": "data",
            "calibration": {
                "scope": "returned_assessments",
                "by_agent_version": {
                    version: self._scores(cases) for version, cases in scored.items()
                },
            },
        }

    @staticmethod
    def _scores(cases):
        paired = [(p, g, y) for p, g, y in cases if g is not None]

        def mean(values):
            return sum(values) / len(values) if values else None

        return {
            "assessed_cases": len(cases),
            "reported_brier": mean([(p - y) ** 2 for p, _, y in cases]),
            "paired_cases": len(paired),
            "paired_self_brier": mean([(p - y) ** 2 for p, _, y in paired]),
            "paired_generic_brier": mean([(g - y) ** 2 for _, g, y in paired]),
            "paired_self_advantage": mean(
                [(g - y) ** 2 - (p - y) ** 2 for p, g, y in paired]
            ),
        }
