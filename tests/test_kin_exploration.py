import pytest


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
            "executor": "codex-cli",
            "provider": "deepseek",
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


def test_local_file_uri_citation():
    from kin_mind.exploration import Citation

    assert (
        Citation(url="file:///tmp/a%20b.py", title="Synthetic code").url
        == "/tmp/a b.py"
    )
    with pytest.raises(ValueError):
        Citation(url="file://remote-host/private", title="Not local")


def test_memory_locator_citation_names_supplied_evidence():
    from kin_mind.exploration import Citation

    assert Citation(url="memory://src_abc123", title="Supplied evidence").url == "memory://src_abc123"
    with pytest.raises(ValueError):
        Citation(url="memory://", title="Empty")
    with pytest.raises(ValueError):
        Citation(url="memory://../../etc/passwd", title="Escape")
