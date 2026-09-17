"""With the audited sections switched off, the appraisal is the appraisal it was: the three suites
that exercise it, collected again here and run unchanged.

Every configuration these suites create carries the six switches as an explicit false, which is how
an operator turns them off. Nothing else is patched: the same tests and the same assertions, and no
request may offer a section on the way.
"""
import pytest
from test_appraisal_retry_policy import *  # the suites themselves, collected again here
from test_section_isolation import *
from test_stage1_integration import *

from kin_mind import appraisal, memory

pytest_plugins = ("test_memory_continuity",)

SWITCHES = tuple(dict.fromkeys(appraisal.AUDIT_SECTION_SWITCH.values()))


@pytest.fixture(autouse=True)
def switches_off(monkeypatch):
    for name in SWITCHES:
        # configure() stores its defaults with whatever it is given, so every store these suites set
        # up carries the switches as an explicit false.
        monkeypatch.setitem(memory.DEFAULTS, name, False)
    offered, original = [], appraisal.offered_sections

    def watched(stimulus, enabled):
        offered.append(original(stimulus, enabled))
        return offered[-1]

    monkeypatch.setattr(appraisal, "offered_sections", watched)
    yield
    # Nothing was offered to any request, on any lane, in any of these runs.
    assert set(offered) <= {()}


def test_the_switches_really_are_off_in_these_runs(env):
    assert all(env.memory.settings()[name] is False for name in SWITCHES)
    with env.mind.engine.db.connect() as conn:
        assert appraisal.audit_switches(conn, env.mind.scope.key()) == set()
    job = env.enqueue("owner-chat")
    result, _ = run(env, lambda shown, context: Appraisal(reason="A real owner message", values={"mood": 61},
        next_move=appraisal.NextMove(move="reply", reason="A synthetic audit")), job_id=job)
    # An audited section a provider returns anyway is blanked, not refused and not stored.
    assert result["state"] == "complete" and "rejected_sections" not in env.job(job)
    assert not set(env.job(job)["proposed_result"]) & set(appraisal.AUDIT_SECTIONS)
    assert env.mind.read()["dimensions"]["mood"]["value"] == 61
