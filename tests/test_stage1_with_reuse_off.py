"""Stage 2 WP4 with its three switches off is stage 1: the stage-1 suites, run again unchanged.

`manifest_rebase`, `appraisal_reuse` and `appraisal_revalidation` are written as an explicit false into
every configuration these suites create, which is how an operator turns them off. Nothing else is
patched: the same tests, the same assertions, and no manifest may be recorded on the way.
"""
import pytest
from test_appraisal_retry_policy import *  # the three stage-1 suites themselves, collected again here
from test_section_isolation import *
from test_stage1_integration import *

from kin_mind import manifest as manifests
from kin_mind import memory, revalidation

pytest_plugins = ("test_memory_continuity",)

SWITCHES = (manifests.REBASE, manifests.REUSE, manifests.REVALIDATION)


@pytest.fixture(autouse=True)
def switches_off(monkeypatch):
    for name in SWITCHES:
        # configure() stores its defaults with whatever it is given, so every store these suites set up
        # carries the three switches as an explicit false.
        monkeypatch.setitem(memory.DEFAULTS, name, False)
    used = []
    for module, function in ((manifests, "store"), (revalidation, "remember"), (revalidation, "resume")):
        original = getattr(module, function)
        monkeypatch.setattr(module, function, lambda *a, _original=original, _name=function, **k: (used.append(_name), _original(*a, **k))[1])
    yield
    # No manifest was stored, nothing was kept for reuse, and no stored proposal was ever resumed
    # (the historical seed of stage 1 is the one exception, and it takes its stage-1 path).
    assert [name for name in used if name != "resume"] == []


def test_the_switches_really_are_off_in_these_runs(system):
    from kin_mind.autonomy_schema import optimized
    mind, memory_store, *_ = system
    assert all(memory_store.settings()[name] is False for name in SWITCHES)
    with mind.engine.db.connect() as conn:
        assert manifests.switches(conn, mind.scope.key()) == {name: False for name in SWITCHES}
        assert not any(optimized(conn, mind.scope.key(), name) for name in SWITCHES)
