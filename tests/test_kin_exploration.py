import json

import pytest

from kin_mind.exploration import final_result, run_kimi


def test_only_final_results_cross_boundary():
    assert (
        final_result(
            json.dumps(
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "private"}],
                }
            )
        )
        is None
    )
    assert final_result(json.dumps({"role": "tool", "content": "not a result"})) is None
    answer = {
        "summary": "A sourced finding",
        "findings": ["One finding"],
        "sources": [{"url": "https://example.com", "title": "Synthetic source"}],
        "open_questions": [],
        "suggested_share": None,
    }
    assert (
        final_result(
            json.dumps({"role": "assistant", "content": json.dumps(answer)})
        ).summary
        == "A sourced finding"
    )
    assert (
        final_result(
            json.dumps(
                {"role": "assistant", "tool_calls": [{}], "content": json.dumps(answer)}
            )
        )
        is None
    )


def test_owned_child_timeout_and_preemption(tmp_path):
    fake = tmp_path / "fake-kimi"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    fake.chmod(0o700)
    result = run_kimi(
        fake, {"question": "synthetic"}, tmp_path / "job", budget_seconds=1
    )
    assert (
        result["state"] == "timed-out"
        and result["seconds"] < 5
        and result["result"] is None
    )
    result = run_kimi(fake, {}, tmp_path / "preempt", canceled=lambda: True)
    assert result["state"] == "preempted"


def test_stream_final_json_and_no_raw_transcript(tmp_path):
    fake = tmp_path / "fake-kimi"
    payload = {
        "summary": "Useful",
        "findings": [],
        "sources": [],
        "open_questions": [],
        "suggested_share": None,
    }
    fake.write_text(
        '#!/usr/bin/env python3\nimport json\nprint(json.dumps({"role":"assistant","content":'
        + repr(json.dumps(payload))
        + "}))\n"
    )
    fake.chmod(0o700)
    result = run_kimi(fake, {}, tmp_path / "job")
    assert result["state"] == "complete" and result["result"]["summary"] == "Useful"
    assert not any(x.name.endswith(".jsonl") for x in (tmp_path / "job").iterdir())


def test_exploration_result_is_persisted_once_and_readable(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.exploration import Explorations
    from kin_mind.state import DesireChange, Mind

    engine = Engine(tmp_path / "memory")
    scope = Scope(persona="synthetic-explorer")
    mind = Mind(engine, scope)
    source = engine.receive(
        SourceInput(
            namespace="synthetic",
            key="question",
            scope=scope,
            text="Explore a source-backed question",
        )
    )["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[source])
    mind.manage_desire(
        DesireChange(
            command_id="explore-wish",
            agent_version="test-v1",
            expected_revision=1,
            evidence_ids=[source],
            action="create",
            content="Research a question",
            topic="synthetic",
            kind="explore",
            strength=80,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            completion="A sourced report",
            reason="A documented question",
        )
    )

    def runner(*args, **kwargs):
        assert kwargs["budget_seconds"] == 1200
        return {
            "state": "complete",
            "provider": "kimi-cli",
            "partial": False,
            "result": {
                "summary": "Synthetic finding",
                "findings": ["A result"],
                "sources": [
                    {"url": "https://example.com", "title": "Synthetic source"}
                ],
                "open_questions": [],
                "suggested_share": None,
            },
        }

    explorer = Explorations(mind)
    result = explorer.run("fake", tmp_path / "jobs", "test-v1", runner=runner)
    assert result["state"] == "complete"
    assert explorer.recent()[0]["result"]["summary"] == "Synthetic finding"
    assert mind.read()["desires"][0]["status"] == "completed"
    again = explorer.run(
        "fake",
        tmp_path / "jobs",
        "test-v1",
        runner=lambda *a, **k: pytest.fail("Duplicate exploration"),
    )
    assert again["state"] == "waiting"


def test_native_kimi_file_uri_citation():
    from kin_mind.exploration import Citation

    assert (
        Citation(url="file:///tmp/a%20b.py", title="Synthetic code").url
        == "/tmp/a b.py"
    )
    with pytest.raises(ValueError):
        Citation(url="file://remote-host/private", title="Not local")


def test_final_json_may_follow_brief_delivery_prose():
    payload = {
        "summary": "Synthetic final finding",
        "findings": [],
        "sources": [{"url": "file:///tmp/source.py", "title": "Synthetic source"}],
        "open_questions": [],
        "suggested_share": None,
    }
    text = "Research is complete.\n\n```json\n" + json.dumps(payload) + "\n```"
    result = final_result(json.dumps({"role": "assistant", "content": text}))
    assert (
        result.summary == "Synthetic final finding"
        and result.sources[0].url == "/tmp/source.py"
    )
