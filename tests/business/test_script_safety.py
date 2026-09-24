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


LOCK = {"package": [
    {"name": "kin-mind", "version": "0.1.0", "source": {"editable": "."},
     "dependencies": [{"name": "httpx"}, {"name": "colorama", "marker": "sys_platform == 'win32'"}]},
    {"name": "httpx", "version": "0.28.1", "dependencies": [{"name": "idna"}]},
    {"name": "idna", "version": "3.19"},
    {"name": "colorama", "version": "0.4.6"},
    {"name": "numpy", "version": "2.2.6"}, {"name": "numpy", "version": "2.5.3"},
]}


def test_the_environment_must_be_the_one_the_lock_pins():
    same = {"kin-mind": "9.9.9", "httpx": "0.28.1", "idna": "3.19", "numpy": "2.5.3", "pip": "25.0"}
    # The project's installed copy, and what the lock does not name, are not its business.
    assert python_env.differences(LOCK, same) == []
    assert python_env.differences(LOCK, {**same, "idna": "3.20"}) == ["idna 3.20 (lock 3.19)"]
    assert python_env.differences(LOCK, {**same, "numpy": "2.4.0"}) == ["numpy 2.4.0 (lock 2.2.6, 2.5.3)"]
    # A runtime dependency followed through the lock must be there; one guarded by a marker
    # this interpreter does not meet need not be.
    without = {name: version for name, version in same.items() if name != "idna"}
    assert python_env.differences(LOCK, without) == ["idna missing (lock 3.19)"]
    assert "colorama" not in python_env.closure(LOCK, "kin-mind")


def test_the_environment_check_reads_this_interpreter(tmp_path, capsys):
    import importlib.metadata as metadata

    have = python_env.installed()
    name = "pydantic" if "pydantic" in have else sorted(have)[0]
    lock = tmp_path / "uv.lock"

    def pin(version):
        lock.write_text(f'version = 1\n\n[[package]]\nname = "kin-mind"\nversion = "0.1.0"\nsource = {{ editable = "." }}\n'
                        f'dependencies = [\n    {{ name = "{name}" }},\n]\n\n[[package]]\nname = "{name}"\nversion = "{version}"\n')

    pin(metadata.version(name))
    assert python_env.main([str(lock)]) == 0
    pin("0.0.0.dev0")
    assert python_env.main([str(lock)]) == 1
    assert f"{name} {metadata.version(name)} (lock 0.0.0.dev0)" in capsys.readouterr().err
