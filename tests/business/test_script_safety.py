"""Operator scripts refuse what they must not touch: replays and fixtures run on copies, a release
carries its own version and runs on the environment its lock pins (T1-07, T1-10, E3-10, T1-14, T1-02)."""

import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eventmem.core import Engine, SourceInput
from eventmem.core.models import Scope
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_wheel  # noqa: E402
import python_env  # noqa: E402
import release_version  # noqa: E402
from live_root import refuse_existing_store, refuse_live  # noqa: E402

KIN = Scope(project="personal", persona="Kin")


def held(tmp_path, marker):
    root = tmp_path / "held"
    root.mkdir()
    (root / marker).write_text("synthetic")
    return root


def snapshot(tmp_path):
    engine = Engine(tmp_path / "snapshot")
    ids = []
    for i in range(3):
        source = engine.receive(SourceInput(namespace="replay", key=str(i), scope=KIN, kind="fact",
                                            authority="explicit", text=f"合成回放记录 {i}：花园的阳光。"))
        ids.append(engine.source(source["id"])["record_ids"][0])
    mind = Mind(engine, KIN)
    mind.initialize(agent_version="replay-v1", evidence_ids=[ids[0]])
    MemoryContinuity(mind).configure({"records": True, "context": True})
    return engine, ids


def stored_settings(root):
    with Engine(root).db.connect() as conn:
        return (conn.execute("SELECT key,data FROM settings ORDER BY key").fetchall(),
                conn.execute("SELECT scope,data FROM mind_memory_config ORDER BY scope").fetchall())


@pytest.mark.parametrize("marker", ["local-token", "service.pid"])
def test_a_root_a_service_holds_is_refused(tmp_path, marker):
    with pytest.raises(SystemExit, match="a service holds it"):
        refuse_live(held(tmp_path, marker))


def test_the_live_root_and_everything_below_it_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_MEMORY_ROOT", str(tmp_path / "memory"))
    for root in (tmp_path / "memory", tmp_path / "memory" / "exports"):
        with pytest.raises(SystemExit, match="live memory root"):
            refuse_live(root)
    assert refuse_live(tmp_path / "copy") == (tmp_path / "copy").resolve()


def test_a_replay_on_a_held_root_stops_before_opening_it(tmp_path):
    root = held(tmp_path, "local-token")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"frozen_before_retrieval": True, "cases": []}))
    run = subprocess.run([sys.executable, str(SCRIPTS / "lifecycle_replay.py"), "--root", str(root),
                          "--cases", str(cases), "--output", str(tmp_path / "out.json"),
                          "--scope", json.dumps(KIN.model_dump())], capture_output=True, text=True)
    assert run.returncode != 0 and "refusing" in run.stderr
    assert sorted(p.name for p in root.iterdir()) == ["local-token"]


def test_a_replay_keeps_its_settings_in_process(tmp_path, monkeypatch):
    from lifecycle_replay import evaluate, replay_settings

    engine, ids = snapshot(tmp_path)
    before = stored_settings(engine.db.root)
    monkeypatch.setattr(MemoryContinuity, "settings", MemoryContinuity.settings)
    replay_settings(engine, adaptive_recall=True, embedding_env="KIN_REPLAY_EMBEDDING_TOKEN")
    assert engine.settings("models")["embedding"]["api_key_env"] == "KIN_REPLAY_EMBEDDING_TOKEN"
    assert MemoryContinuity(Mind(engine, KIN)).settings()["adaptive_recall"] is True
    cases = [{"id": f"case-{i}", "category": "synthetic", "critical": i == 0, "query": "花园的阳光",
              "expected_ids": [ids[i % 3]]} for i in range(3)]
    for legacy in (True, False):
        assert evaluate(engine, KIN, cases, legacy=legacy)["cases"] == 3
    assert stored_settings(engine.db.root) == before


def test_the_fixture_builds_only_a_new_store(tmp_path):
    from w4_light_fixture import build

    engine, _ = snapshot(tmp_path)
    database = engine.db.path
    size, changed = database.stat().st_size, database.stat().st_mtime_ns
    with pytest.raises(SystemExit, match="already holds a store"):
        build(engine.db.root, seed=1, anchor=datetime(2026, 9, 1, tzinfo=timezone.utc), scale=0.01)
    assert (database.stat().st_size, database.stat().st_mtime_ns) == (size, changed)
    with pytest.raises(SystemExit, match="a service holds it"):
        refuse_existing_store(held(tmp_path, "service.pid"))


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def test_a_changed_package_must_carry_a_new_version(tmp_path):
    root = tmp_path / "repo"
    (root / "src/kin_mind").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "kin-mind"\nversion = "0.1.0"\n\n[project.scripts]\nx = "y"\n')
    (root / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.1.0"\n\n'
                                  '[[package]]\nname = "kin-mind"\nversion = "0.1.0"\nsource = { editable = "." }\n')
    (root / "src/kin_mind/state.py").write_text("A = 1\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "release")
    assert release_version.check(root, "HEAD") is None
    (root / "src/kin_mind/state.py").write_text("A = 2\n")
    assert "--bump" in release_version.check(root, "HEAD")
    assert release_version.main(["--bump"], root=root) == 0
    assert '[project]\nname = "kin-mind"\nversion = "0.1.1"\n\n[project.scripts]' in (root / "pyproject.toml").read_text()
    # The lock's own entry moves with it; a dependency that shares the number does not.
    assert (root / "uv.lock").read_text() == ('version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.1.0"\n\n'
                                              '[[package]]\nname = "kin-mind"\nversion = "0.1.1"\nsource = { editable = "." }\n')
    assert release_version.check(root, "HEAD") is None
    assert release_version.bumped((0, 1, 1), "minor") == (0, 2, 0)


def test_the_wheel_check_takes_only_this_tree(tmp_path):
    root = tmp_path / "tree"
    files = {"eventmem/core/engine.py": "E = 1\n", "eventmem/sdk/__init__.pyi": "", "eventmem/sdk/py.typed": "",
             "kin_mind/state.py": "S = 1\n", "kin_mind/profile.py": "P = 1\n"}
    for name, text in files.items():
        (root / "src" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "src" / name).write_text(text)
    (root / "pyproject.toml").write_text('[project]\nname = "kin-mind"\nversion = "0.1.0"\n')

    def wheel(name, changes=None):
        path = root / "dist" / name
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for key, text in {**files, "eventmem/web/index.html": "", "eventmem/web/assets/a.js": "",
                              **(changes or {})}.items():
                archive.writestr(key, text)
        return path

    check_wheel.main([str(wheel("kin_mind-0.1.0-py3-none-any.whl"))], root=root)
    with pytest.raises(AssertionError, match="stale"):
        check_wheel.main([str(wheel("old.whl", {"kin_mind/state.py": "S = 0\n"}))], root=root)
    with pytest.raises(SystemExit, match="found 2"):
        wheel("kin_mind-0.1.0-py2-none-any.whl")
        check_wheel.main([], root=root)


def env(python):
    """A marker environment for one Python, the rest of it this machine's."""
    return {**python_env.environment(), "python_full_version": python, "python_version": ".".join(python.split(".")[:2]),
            "sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"}


# The shape uv writes: numpy locked twice, each version for its own Pythons.
LOCK = {"requires-python": ">=3.10", "package": [
    {"name": "kin-mind", "version": "0.1.0", "source": {"editable": "."},
     "dependencies": [{"name": "httpx"}, {"name": "colorama", "marker": "sys_platform == 'win32'"},
                      {"name": "numpy", "version": "2.2.6", "marker": "python_full_version < '3.11'"},
                      {"name": "numpy", "version": "2.5.3", "marker": "python_full_version >= '3.12'"}]},
    {"name": "httpx", "version": "0.28.1", "dependencies": [{"name": "idna"}]},
    {"name": "idna", "version": "3.19"},
    {"name": "colorama", "version": "0.4.6"},
    {"name": "numpy", "version": "2.2.6", "resolution-markers": ["python_full_version < '3.11'"]},
    {"name": "numpy", "version": "2.5.3", "resolution-markers": ["python_full_version >= '3.12'"]},
]}


def test_the_environment_must_be_the_one_the_lock_pins():
    same = {"kin-mind": "9.9.9", "httpx": "0.28.1", "idna": "3.19", "numpy": "2.5.3", "pip": "25.0"}
    # The project's installed copy, and what the lock does not name, are not its business.
    assert python_env.differences(LOCK, same, env("3.13.1"), extras=()) == []
    assert python_env.differences(LOCK, {**same, "idna": "3.20"}, env("3.13.1"), extras=()) == ["idna 3.20 (lock 3.19)"]
    # A runtime dependency followed through the lock must be there; one guarded by a marker
    # this interpreter does not meet need not be.
    without = {name: version for name, version in same.items() if name != "idna"}
    assert python_env.differences(LOCK, without, env("3.13.1"), extras=()) == ["idna missing (lock 3.19)"]
    assert "colorama" not in python_env.Lock(LOCK).resolve(env("3.13.1"), set())
    # A Python the lock does not support is not its environment, whatever is installed.
    assert python_env.differences(LOCK, same, env("3.9.18"), extras=()) == \
        ["Python 3.9.18 is outside the lock's requires-python >=3.10"]


def test_a_version_locked_for_another_python_does_not_pass():
    """CR2-OPS-08: numpy 2.2.6 is locked for Pythons before 3.11 only. Before, any version the lock
    named anywhere passed; now the edge this Python takes decides."""
    same = {"httpx": "0.28.1", "idna": "3.19"}
    for python, installed, found in (("3.13.1", "2.2.6", ["numpy 2.2.6 (lock 2.5.3)"]), ("3.10.12", "2.2.6", []),
                                     ("3.10.12", "2.5.3", ["numpy 2.5.3 (lock 2.2.6)"]),
                                     # Python 3.11 gets no numpy from this lock at all.
                                     ("3.11.9", "2.5.3", ["numpy 2.5.3 (not locked for this interpreter beside these extras)"])):
        assert python_env.differences(LOCK, {**same, "numpy": installed}, env(python), extras=()) == found


# local-embedding and media are declared in conflict and pull different typer versions; uv marks
# each edge with the side it belongs to, as it does in the core's lock.
LE, MEDIA = "extra-8-kin-mind-local-embedding", "extra-8-kin-mind-media"
BRANCHES = {
    "conflicts": [[{"package": "kin-mind", "extra": "local-embedding"}, {"package": "kin-mind", "extra": "media"}]],
    "package": [
        {"name": "kin-mind", "version": "0.1.0", "source": {"editable": "."}, "dependencies": [{"name": "httpx"}],
         "optional-dependencies": {
             "local-embedding": [{"name": "sentence-transformers"}, {"name": "typer", "version": "0.27.2"}],
             "media": [{"name": "docling"}]}},
        {"name": "httpx", "version": "0.28.1"},
        {"name": "sentence-transformers", "version": "5.7.0",
         "dependencies": [{"name": "typer", "version": "0.27.2", "marker": f"extra == '{LE}'"}]},
        {"name": "docling", "version": "2.60.0",
         "dependencies": [{"name": "typer", "version": "0.26.8", "marker": f"extra == '{MEDIA}' or extra != '{LE}'"}]},
        {"name": "typer", "version": "0.26.8"},
        {"name": "typer", "version": "0.27.2"},
    ]}
LOCAL = {"httpx": "0.28.1", "sentence-transformers": "5.7.0", "typer": "0.27.2"}


def test_the_chosen_extras_must_be_installed_whole():
    """CR2-MEM-03: a deployment that chose local-embedding without its dependencies is not the
    environment the lock pins for it. Before, optional dependencies were never followed."""
    assert python_env.differences(BRANCHES, LOCAL, env("3.13.1"), extras=["local-embedding"]) == []
    without = {name: version for name, version in LOCAL.items() if name != "sentence-transformers"}
    assert python_env.differences(BRANCHES, without, env("3.13.1"), extras=["local-embedding"]) == \
        ["sentence-transformers missing (lock 5.7.0)"]
    # Without the extra chosen, nothing of it is required.
    assert python_env.differences(BRANCHES, without, env("3.13.1"), extras=()) == []


def test_the_chosen_side_of_a_conflict_decides_the_versions():
    """CR2-MEM-03: typer 0.26.8 is in the lock, but on the media side only. Before, a version
    anywhere in the lock passed."""
    assert python_env.differences(BRANCHES, {**LOCAL, "typer": "0.26.8"}, env("3.13.1"), extras=["local-embedding"]) == \
        ["typer 0.26.8 (lock 0.27.2)"]
    media = {"httpx": "0.28.1", "docling": "2.60.0", "typer": "0.26.8"}
    assert python_env.differences(BRANCHES, media, env("3.13.1"), extras=["media"]) == []
    assert python_env.differences(BRANCHES, {**media, "typer": "0.27.2"}, env("3.13.1"), extras=["media"]) == \
        ["typer 0.27.2 (lock 0.26.8)"]
    # What only the other side installs does not belong beside the chosen extras.
    assert python_env.differences(BRANCHES, {**LOCAL, "docling": "2.60.0"}, env("3.13.1"), extras=["local-embedding"]) == \
        ["docling 2.60.0 (not locked for this interpreter beside these extras)"]
    # With neither side chosen, typer from either is one the lock selects beside them; others are not.
    for typer, found in (("0.27.2", []), ("0.26.8", []), ("0.25.0", ["typer 0.25.0 (lock 0.26.8, 0.27.2)"])):
        assert python_env.differences(BRANCHES, {"httpx": "0.28.1", "typer": typer}, env("3.13.1"), extras=()) == found
    # Extras the lock keeps apart, or does not have, decide nothing.
    with pytest.raises(python_env.Undecided, match="local-embedding and media are declared in conflict"):
        python_env.differences(BRANCHES, LOCAL, env("3.13.1"), extras=["local-embedding", "media"])
    with pytest.raises(python_env.Undecided, match="no extra 'gpu'"):
        python_env.differences(BRANCHES, LOCAL, env("3.13.1"), extras=["gpu"])


def test_an_extra_that_names_the_projects_own_extras_brings_them_in():
    root = BRANCHES["package"][0]
    lock = {**BRANCHES, "package": [
        {**root, "optional-dependencies": {**root["optional-dependencies"],
                                           "embed": [{"name": "kin-mind", "extra": ["local-embedding"]}]}},
        *BRANCHES["package"][1:]]}
    found, taken = python_env.check(lock, LOCAL, env("3.13.1"), extras=["embed"])
    assert (found, taken["extras"]) == ([], ["embed", "local-embedding"])
    assert python_env.differences(lock, {**LOCAL, "typer": "0.26.8"}, env("3.13.1"), extras=["embed"]) == \
        ["typer 0.26.8 (lock 0.27.2)"]
    with pytest.raises(python_env.Undecided, match="declared in conflict"):
        python_env.differences(lock, LOCAL, env("3.13.1"), extras=["embed", "media"])


@pytest.mark.parametrize("marker", ["python_full_version >= ", "python_flavour == 'cpython'",
                                    "(sys_platform == 'darwin'", "sys_platform = 'darwin'", "extra == 'socks'"])
def test_a_marker_that_cannot_be_read_stops_the_check(tmp_path, capsys, marker):
    """Before, such an edge counted as not needed. Now nothing is decided, even when the edge is
    one this interpreter would not take."""
    lock = {"package": [
        {"name": "kin-mind", "version": "0.1.0", "source": {"editable": "."},
         "dependencies": [{"name": "httpx"}, {"name": "colorama", "marker": marker}]},
        {"name": "httpx", "version": "0.28.1"}, {"name": "colorama", "version": "0.4.6"}]}
    with pytest.raises(python_env.Undecided, match="marker"):
        python_env.differences(lock, {"httpx": "0.28.1"}, env("3.13.1"), extras=())
    path = tmp_path / "uv.lock"
    path.write_text('version = 1\n\n[[package]]\nname = "kin-mind"\nversion = "0.1.0"\nsource = { editable = "." }\n'
                    'dependencies = [\n    { name = "colorama", marker = "' + marker.replace('"', '\\"') + '" },\n]\n\n'
                    '[[package]]\nname = "colorama"\nversion = "0.4.6"\n')
    assert python_env.main(["--extras", "", str(path)]) == 2
    assert "cannot decide" in (err := capsys.readouterr().err) and "marker" in err


def test_the_repository_lock_resolves_each_deployment_to_one_branch():
    """Every marker in the committed uv.lock parses and production's extras are extras of it. On
    each Python it supports, a deployment resolves to one version per package: local-embedding to
    a typer the media side does not take, and one numpy for the interpreter."""
    import tomllib

    data = tomllib.loads((SCRIPTS.parent / "uv.lock").read_text())
    lock = python_env.Lock(data)
    production = lock.choose(python_env.PRODUCTION_EXTRAS)
    for python in ("3.10.12", "3.11.9", "3.12.8", "3.13.1", "3.14.0"):
        closure = lock.resolve(env(python), production)
        assert "docling" not in closure and lock.resolve(env(python), set())
        assert closure["typer"]["version"] != lock.resolve(env(python), lock.choose(["all"]))["typer"]["version"]
        assert lock.selectable(env(python), production)["numpy"] == {closure["numpy"]["version"]}
        with pytest.raises(python_env.Undecided, match="declared in conflict"):
            lock.resolve(env(python), lock.choose(["local-embedding", "media"]))


def lock_of(tmp_path, name, version, extras=None):
    """A uv.lock of one package, required by the project and by each extra in `extras`, which maps
    an extra to further (name, version) it requires."""
    path = tmp_path / "uv.lock"
    sections = "".join(f'{extra} = [\n    {{ name = "{name}" }},\n'
                       + "".join(f'    {{ name = "{more}" }},\n' for more, _ in requires) + "]\n"
                       for extra, requires in (extras or {}).items())
    others = "".join(f'\n[[package]]\nname = "{more}"\nversion = "{pinned}"\n'
                     for requires in (extras or {}).values() for more, pinned in requires)
    path.write_text(f'version = 1\n\n[[package]]\nname = "kin-mind"\nversion = "0.1.0"\nsource = {{ editable = "." }}\n'
                    f'dependencies = [\n    {{ name = "{name}" }},\n]\n'
                    + (f"\n[package.optional-dependencies]\n{sections}" if extras else "")
                    + f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n' + others)
    return path


def test_the_environment_check_reads_this_interpreter(tmp_path, capsys):
    import importlib.metadata as metadata

    have = python_env.installed()
    name = "pydantic" if "pydantic" in have else sorted(have)[0]
    assert python_env.main(["--extras", "", str(lock_of(tmp_path, name, metadata.version(name)))]) == 0
    assert python_env.main(["--extras", "", str(lock_of(tmp_path, name, "0.0.0.dev0"))]) == 1
    assert f"{name} {metadata.version(name)} (lock 0.0.0.dev0)" in capsys.readouterr().err


def test_production_extras_are_checked_unless_others_are_named(tmp_path, capsys):
    """CR2-MEM-03: without --extras the check holds the interpreter to what production installs."""
    have = python_env.installed()
    name = "pydantic" if "pydantic" in have else sorted(have)[0]
    extras = {extra: [] for extra in python_env.PRODUCTION_EXTRAS}
    extras["local-embedding"] = [("kin-absent-package", "1.0")]
    path = lock_of(tmp_path, name, have[name], extras)
    assert python_env.main([str(path)]) == 1
    assert "kin-absent-package missing (lock 1.0)" in capsys.readouterr().err
    assert python_env.main(["--extras", "graph,vector", str(path)]) == 0
    assert "extras graph, vector" in capsys.readouterr().out
