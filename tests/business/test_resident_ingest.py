"""CR4-MM-05: `ingest` is a resident action, and a resident action only stores. Whatever the request
asks -- no deferral, a `read` purpose with adaptive recall on -- it prepares no context: that is
`memory-context`'s, a worker of its own under the model lanes' admission."""
from kin_mind import host
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from test_kin_exploration_codex import exploration_world, host_config


def request(key):
    return {"id": key, "text": "上次说的那本书叫什么来着", "at": "2026-09-25T00:00:00+00:00", "channel": "feishu",
            "purpose": "read"}


def test_ingest_stores_the_input_and_prepares_no_context(tmp_path, monkeypatch):
    _, mind, _ = exploration_world(tmp_path)
    MemoryContinuity(mind).configure({"records": True, "semantic": True, "context": True, "adaptive_recall": True})
    config = host_config(tmp_path, mind, session_id="session-1")

    def no_build(*_args, **_kwargs):
        raise AssertionError("the resident worker prepares no context")
    monkeypatch.setattr(Contexts, "build", no_build)
    answer = host.dispatch(config, "ingest", request("owner-1"))
    assert answer["memory_enabled"] is True and answer["source_id"] and answer["appraisal"]["id"]
    # With the context layer off it answers with the state view, as before, and still builds nothing.
    MemoryContinuity(mind).configure({"context": False})
    plain = host.dispatch(config, "ingest", request("owner-2"))
    assert plain["memory_context"] == {"state": "disabled", "text": "", "tokens": 0}
    assert plain["state"]["dimensions"] and plain["source_id"]
