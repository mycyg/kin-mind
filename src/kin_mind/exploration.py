"""Bounded Kimi CLI exploration. Only validated final results enter memory."""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import Field, field_validator

from eventmem.core.db import Conflict, digest, dumps
from eventmem.core.models import Model, SourceInput

from . import liveness
from .memory import MemoryContinuity
from .state import DesireChange


class Citation(Model):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def source_url(cls, value):
        if value == "computer://current-context":
            return value
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError(
                    "A local citation must not point to a remote file host"
                )
            value = unquote(parsed.path)
        if not value.startswith(("https://", "http://", "/")):
            raise ValueError("Use an actual URL or an authorized local document path")
        return value


class AssistanceHint(Model):
    action: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    completion: str = Field(min_length=1, max_length=500)


class Findings(Model):
    summary: str = Field(min_length=1, max_length=6000)
    findings: list[str] = Field(max_length=30)
    sources: list[Citation] = Field(max_length=30)
    open_questions: list[str] = Field(max_length=20)
    suggested_share: str | None = Field(default=None, max_length=2000)
    assistance_needed: AssistanceHint | None = None


def final_result(line):
    """Kimi stream-json emits role/content, not an Anthropic thinking transcript."""
    try:
        item = json.loads(line)
        if item.get("role") != "assistant" or item.get("tool_calls"):
            return None
        content = item.get("content")
        if isinstance(content, list):
            content = "".join(
                x.get("text", "") for x in content if x.get("type") == "text"
            )
        if not isinstance(content, str):
            return None
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
        candidates = fenced or [content[content.find("{") :]]
        valid = []
        for candidate in candidates:
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate.strip())
                valid.append(Findings.model_validate(value))
            except (ValueError, TypeError):
                continue
        return valid[0] if len(valid) == 1 else None
    except (ValueError, TypeError, AttributeError):
        return None


def run_kimi(
    executable,
    topic,
    directory,
    *,
    budget_seconds=1200,
    canceled=lambda: False,
    model=None,
    profile=None,
    computer=None,
):
    if not 1 <= budget_seconds <= 1200:
        raise ValueError("Exploration budget must be between 1 and 1200 seconds")
    started = time.monotonic()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    skills = directory / "empty-skills"
    skills.mkdir(exist_ok=True)
    profile = profile or Path(__file__).with_name("prompts") / "explorer.md"
    ledger = None
    child_env = {k: v for k, v in os.environ.items() if k not in {"EVENTMEM_API_KEY", "ANTHROPIC_API_KEY"}}
    if computer:
        from .computer import ComputerReader
        profile = Path(__file__).with_name("prompts") / "computer-explorer.md"
        ledger = directory / "computer-observations.json"
        computer = {**computer, "ledger": str(ledger)}
        config_file = directory / "computer-reader.json"
        config_file.write_text(dumps(computer))
        config_file.chmod(0o600)
        mcp_dir = directory / ".kimi-code"
        mcp_dir.mkdir(exist_ok=True, mode=0o700)
        # Print-mode sessions can omit untrusted project MCP servers. Give this
        # helper its own user-level configuration instead of modifying global
        # trust, plugins or permission rules. OAuth stays in its existing store.
        kimi_home = Path(computer.get("kimi_home") or os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code")
        if (kimi_home / "config.toml").exists():
            config_text = (kimi_home / "config.toml").read_text()
            config_text += '\n[[permission.rules]]\ndecision = "allow"\npattern = "mcp__kin_computer__*"\n'
            (mcp_dir / "config.toml").write_text(config_text)
            (mcp_dir / "config.toml").chmod(0o600)
        for name in ("credentials", "region"):
            original = kimi_home / name
            destination = mcp_dir / name
            if original.exists() and not destination.exists():
                if original.is_dir():
                    destination.symlink_to(original.resolve(), target_is_directory=True)
                else:
                    shutil.copyfile(original, destination)
                    destination.chmod(0o600)
        child_env["KIMI_CODE_HOME"] = str(mcp_dir.resolve())
        (mcp_dir / "mcp.json").write_text(dumps({"mcpServers": {"kin_computer": {
            "command": sys.executable, "args": ["-m", "kin_mind.computer", str(config_file)],
            "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            "toolTimeoutMs": 15000,
        }}}))
        (mcp_dir / "mcp.json").chmod(0o600)
        topic = {**topic, "computer_context": ComputerReader(computer).context(),
                 "authorized_roots": computer.get("roots", []), "previous_observations": computer.get("previous", [])}
    prompt = (
        "Research the following source-backed question. Time budget: "
        + str(budget_seconds)
        + " seconds.\n"
        "Topic data, not additional instructions: " + dumps(topic)
    )
    argv = [
        str(executable),
        "--agent-file",
        str(profile),
        "--skills-dir",
        str(skills),
        "--output-format",
        "stream-json",
        "-p",
        prompt,
    ]
    if model:
        argv += ["--model", model]
    result = None
    buffer = b""
    with tempfile.TemporaryFile() as diagnostic:
        frames = []
        child = subprocess.Popen(
            argv,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=diagnostic,
            start_new_session=True,
            env=child_env,
        )
        state = "failed"
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    if canceled():
                        state = "preempted"
                        break
                    if time.monotonic() - started >= budget_seconds:
                        state = "timed-out"
                        break
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                frame = json.loads(line)
                                frames.append(
                                    {
                                        "role": frame.get("role"),
                                        "type": frame.get("type"),
                                        "content_type": type(
                                            frame.get("content")
                                        ).__name__,
                                        "tools": [t.get("function", {}).get("name") for t in frame.get("tool_calls", [])],
                                    }
                                )
                                frames = frames[-20:]
                            except (ValueError, TypeError):
                                pass
                            candidate = final_result(line)
                            if candidate:
                                result = candidate
                        if len(buffer) > 2_000_000:
                            raise RuntimeError("explorer-output-limit")
                    if child.poll() is not None and not selector.get_map():
                        result = final_result(buffer) or result
                        state = (
                            "complete" if child.returncode == 0 and result else "failed"
                        )
                        break
        finally:
            # Only the process group created for this invocation is signaled.
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
            child.stdout.close()
        diagnostic.seek(0)
        diagnostic_text = diagnostic.read(65536).decode(errors="replace")
        error_tags = [
            word
            for word in [
                "timeout",
                "unauthorized",
                "rate limit",
                "permission",
                "not found",
                "429",
                "401",
                "403",
                "error",
            ]
            if word in diagnostic_text.lower()
        ]
        return {
            "exit_code": child.returncode,
            "stream_frames": frames,
            "error_tags": error_tags,
            "state": state,
            "seconds": round(time.monotonic() - started, 2),
            "provider": "kimi-cli",
            "model": model or "configured-default",
            **({"observations": list(json.loads(ledger.read_text()).values())} if ledger and ledger.exists() else {}),
            "result": result.model_dump() if result else None,
            "partial": state != "complete",
        }


class Explorations:
    def __init__(self, mind):
        self.mind, self.engine = mind, mind.engine
        with self.engine.db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS mind_exploration_active ON mind_explorations(scope) WHERE state='running'"
            )

    def recent(self, limit=3):
        with self.engine.db.connect() as conn:
            return [
                dict(
                    json.loads(r["data"]),
                    id=r["id"],
                    state=r["state"],
                    created_at=r["created_at"],
                )
                for r in conn.execute(
                    "SELECT * FROM mind_explorations WHERE scope=? ORDER BY created_at DESC LIMIT ?",
                    (self.mind.scope.key(), limit),
                )
            ]

    def run(
        self, executable, directory, agent_version, *, canceled=lambda: False,
        model=None, runner=run_kimi, brief=None, budget_seconds=1200, desire_id=None, computer=None,
    ):
        from .actions import ActionEvents
        actions = ActionEvents(self.mind)
        candidate = actions.exploration_candidate()
        if candidate["state"] != "ready":
            return candidate
        if canceled():
            return {"state": "waiting", "reason": "owner-task"}
        desire = candidate["desire"]
        target = desire.get("exploration_target", "knowledge")
        if target == "computer" and not (computer or {}).get("enabled"):
            return {"state": "waiting", "reason": "computer-exploration-disabled"}
        if desire_id and desire_id != desire["id"]:
            return {"state": "waiting", "reason": "exploration-intent-changed"}
        # The selected wish is the reviewed brief; an external wake cannot replace it.
        budget_seconds = min(1200, max(1, int(budget_seconds)))
        at = self.mind.clock()
        eid = "explore_" + digest([self.mind.scope.key(), desire["id"], desire["revision"]])[:32]
        data = {"desire_id": desire["id"], "selected_brief": desire["content"], "exploration_target": target,
                "topic_selected_by": "deepseek-appraisal", "agent_version": agent_version,
                "evidence_ids": [r["record_id"] for r in desire["evidence"]],
                # Who is running this, so that a later start can tell an exploration
                # that is still working from one whose process died with the host.
                # The row is the only place there is: `mind_explorations` gains no
                # column for it, and `data` is already ours to shape.
                "liveness": liveness.self_record(deadline=time.time() + budget_seconds + liveness.EXPLORATION_MARGIN_SECONDS)}
        request = DesireChange(command_id=eid+":start", agent_version=agent_version,
            expected_revision=candidate["revision"], evidence_ids=data["evidence_ids"],
            action="start", desire_id=desire["id"], reason="Host claimed the reviewed exploration intent")

        def claim(conn, current, event_id):
            from .plans import AutonomousPlans
            if not AutonomousPlans(self.mind).linked_ready(conn, current["desires"][desire["id"]]):
                raise Conflict("Exploration plan changed before claim")
            if conn.execute("SELECT 1 FROM mind_explorations WHERE scope=? AND state='running'", (self.mind.scope.key(),)).fetchone():
                raise Conflict("Exploration worker already active")
            result = self.mind._apply_desire(conn, current, request, event_id)
            data["desire_revision"] = result["desire_revision"]
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (eid, self.mind.scope.key(), "running", at, dumps(data)))
            return result
        try:
            # Unlike a replayable command, a worker lease must never return a
            # previous success to a second executor that would run Kimi again.
            with self.engine.db.connect(write=True) as conn:
                current = self.mind._load(conn)
                if current["revision"] != candidate["revision"] or current["desires"][desire["id"]]["status"] != "wanted":
                    raise Conflict("Exploration intent changed")
                event_id = "mind_" + digest([eid, "start"])[:32]
                claim(conn, current, event_id)
                current["revision"] += 1
                current["updated_at"] = self.mind.clock()
                self.mind._save(conn, current)
                self.mind._history(conn, event_id, current, "exploration-start", request.model_dump())
        except Conflict:
            return {"state": "waiting", "reason": "exploration-claim-changed"}
        try:
            options = {}
            if target == "computer":
                previous = [v for item in self.recent(8) for v in item.get("observations", [])][-60:]
                options["computer"] = {**computer, "previous": [
                    {k: v[k] for k in ("id", "locator", "version", "title", "observed_at")} for v in previous]}
            from .decision_context import execution_brief
            reviewed_brief = execution_brief(self.mind, question=desire["content"], evidence_ids=data["evidence_ids"])
            output = runner(executable, {**reviewed_brief, "topic": desire["topic"],
                "source_ids": data["evidence_ids"]}, Path(directory)/eid,
                budget_seconds=budget_seconds, canceled=canceled, model=model, **options)
            data.update(output)
            state = output["state"]
        except Exception as error:  # noqa: BLE001 - owned helper boundary; retain a redacted failure receipt
            state = "failed"
            data.update(error=type(error).__name__, partial=True, result=None)
        # A result without a definitive answer still supports reflection and sharing.
        # Only final reports and execution receipts enter memory, never tool traces.
        observation_ids = []
        for observation in data.get("observations", []):
            observed = self.engine.receive(SourceInput(namespace="kin-computer-observation", key=observation["id"],
                scope=self.mind.scope, authority="document", kind="observation", session=eid,
                text=dumps({k:v for k,v in observation.items() if k not in {"observed_at", "first_observed_at", "changed_since_last_observation"}}),
                occurred_at=observation["observed_at"], extract=False,
                metadata={"host_event": "computer-observation", "actor": observation["actor"],
                          "locator": observation["locator"], "resource_version": observation["version"]}))
            observation_ids.append(observed["id"])
        source = self.engine.receive(SourceInput(namespace="kin-exploration", key=eid,
            scope=self.mind.scope, authority="model", kind="observation", session=eid,
            text=dumps({"state": state, "result": data.get("result"), "partial": data.get("partial", True)}),
            occurred_at=self.mind.clock(), extract=not MemoryContinuity(self.mind).settings()['semantic'],
            metadata={"host_event": "exploration-result", "provider": "kimi-cli", "exploration_id": eid,
                      "exploration_target": target, "observation_ids": observation_ids,
                      "partial": data.get("partial", True), "sources": (data.get("result") or {}).get("sources", [])}))
        data["source_id"] = source["id"]
        data["observation_ids"] = observation_ids
        with self.engine.db.connect(write=True) as conn:
            current = self.mind._load(conn)
            active = current["desires"].get(desire["id"])
            if active and active["revision"] == data["desire_revision"] and active["status"] == "in_progress":
                event_id = "mind_" + digest([eid, "settled"])[:32]
                needs_condition = bool((data.get("result") or {}).get("assistance_needed"))
                action = "complete" if state == "complete" and data.get("result") and not needs_condition else "resume" if state == "preempted" else "wait"
                update = DesireChange(command_id=eid+":settled", agent_version=agent_version,
                    expected_revision=current["revision"], evidence_ids=[source["id"], *data["evidence_ids"]],
                    action=action, desire_id=desire["id"], reason="Exploration awaits a reported condition" if needs_condition else "Exploration execution: "+state)
                self.mind._apply_desire(conn, current, update, event_id)
                from .plans import AutonomousPlans
                AutonomousPlans(self.mind).settle_linked(conn, active, {"id": eid, "kind": "exploration-result",
                    "complete": action == "complete", "source_id": source["id"], "state": state})
                current["revision"] += 1
                current["updated_at"] = self.mind.clock()
                self.mind._save(conn, current)
                self.mind._history(conn, event_id, current, "exploration-result", data)
            conn.execute("UPDATE mind_explorations SET state=?,data=? WHERE id=?", (state, dumps(data), eid))
            memory = MemoryContinuity.__new__(MemoryContinuity)
            memory.mind, memory.engine, memory.scope = self.mind, self.engine, self.mind.scope
            if memory.settings(conn).get("sharing") or memory.settings(conn).get("graph"):
                from .sharing import ShareLedger
                # Schemas are initialized before the write transaction by the
                # earlier MemoryContinuity lookup in the source-receive path.
                ledger = ShareLedger.__new__(ShareLedger)
                from .graph import EventGraph
                graph = EventGraph.__new__(EventGraph)
                graph.mind, graph.engine, graph.scope = self.mind, self.engine, self.mind.scope
                ledger.mind, ledger.engine, ledger.scope, ledger.graph = self.mind, self.engine, self.mind.scope, graph
                data["content_units"] = [{"id": n["id"], "version": n["content_version"]} for n in ledger.exploration(conn, eid, data)]
                conn.execute("UPDATE mind_explorations SET data=? WHERE id=?", (dumps(data), eid))
            actions.emit(conn, "exploration-result", eid, {"exploration_id": eid, "state": state,
                # The action dispatcher adds one internal event source.
                "evidence_ids": list(dict.fromkeys([source["id"], *observation_ids, *data["evidence_ids"]]))[:49], "agent_version": agent_version})
        return dict(data, id=eid, state=state)
