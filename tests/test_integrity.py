"""The start-up self-check: the running code must be the deployed code.

Two kinds of test here, because the fault has two halves. Most of them build
packages under `tmp_path` and ask about those by name, so every branch of the
verdict is exercised without depending on how this test process happens to have
been started. The rest spawn a real interpreter with a real `PYTHONPATH` and a
real working directory, because the question the check answers -- which of two
copies would this process import -- is a question about the import machinery, and
only the import machinery can be trusted to answer it.

Both directions matter equally. A shadow that is missed leaves the fault exactly
as it was; a correct deployment that is refused is an outage caused by the check
itself. So every foreign case has a clean counterpart next to it.
"""
import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eventmem.core import integrity
from eventmem.core.integrity import (
    ALLOW_ENV,
    EXIT_CODE,
    FOREIGN,
    NOT_CONFIGURED,
    ROOT_ENV,
    VERIFIED,
    enforce_source_root,
    refuses,
    verify_interpreter,
    verify_source_root,
    warn_interpreter,
)

pytest_plugins = ("test_memory_continuity",)

# The packages the fixtures are built from. Never `eventmem` or `kin_mind`: those
# two are already imported into this process, so they would answer for themselves
# and no arrangement of `sys.path` could change what they say. The subprocess
# tests below use the real names, which is where they belong.
PAIR = ("deployed_alpha", "deployed_beta")

# What a child process runs: load the module under test straight from its file,
# so the copy being tested is never itself resolved through the path the test is
# rearranging, then report on whatever the ordinary machinery finds.
PROBE = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("integrity_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report = module.verify_source_root(sys.argv[2])
print(json.dumps({**report, "first_on_path": sys.path[0]}))
"""

INTEGRITY = Path(integrity.__file__).resolve()
# <src>/eventmem/core/integrity.py
SOURCE = INTEGRITY.parents[2]


def tree(root, modules=PAIR, body="value = 'deployed'\n"):
    """A source root holding one package per name."""
    for name in modules:
        package = root / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(body)
    # A directory that was scanned before these existed would otherwise be
    # remembered without them.
    importlib.invalidate_caches()
    return root


@pytest.fixture
def path(monkeypatch):
    """A search path the test arranges, restored afterwards. The runner's own
    entries stay behind the fixture's -- nothing there answers to these names."""
    original = list(sys.path)
    monkeypatch.setattr(sys, "path", list(original))

    def use(*directories):
        sys.path[:] = [str(directory) for directory in directories] + original
        importlib.invalidate_caches()
        return sys.path
    return use


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(ROOT_ENV, raising=False)


def child(root, *, cwd, pythonpath):
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in pythonpath)}
    environment.pop(ALLOW_ENV, None)
    environment.pop(ROOT_ENV, None)
    done = subprocess.run([sys.executable, "-c", PROBE, str(INTEGRITY), str(root)],
                          cwd=str(cwd), env=environment, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------- the verdicts

def test_a_root_that_is_not_configured_is_not_a_check(path, tmp_path):
    path(tree(tmp_path / "src"))
    report = verify_source_root(None)
    assert report["verdict"] == NOT_CONFIGURED
    assert report["source_root"] is None and report["modules"] == []
    assert not refuses(report)


def test_the_environment_supplies_a_root_when_the_caller_has_none(path, tmp_path, monkeypatch):
    source = tree(tmp_path / "src")
    path(source)
    monkeypatch.setenv(ROOT_ENV, str(source))
    assert verify_source_root(None, modules=PAIR)["verdict"] == VERIFIED
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "elsewhere"))
    assert verify_source_root(None, modules=PAIR)["verdict"] == FOREIGN
    # An argument outranks the environment; it is the deployment's own answer.
    assert verify_source_root(str(source), modules=PAIR)["verdict"] == VERIFIED


def test_a_deployment_that_is_correct_is_not_refused(path, tmp_path):
    source = tree(tmp_path / "src")
    path(source)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == VERIFIED and not refuses(report)
    assert [entry["module"] for entry in report["modules"]] == list(PAIR)
    assert all(entry["inside"] for entry in report["modules"])
    assert report["shadows"] == []


def test_a_second_copy_earlier_on_the_path_is_refused(path, tmp_path):
    source = tree(tmp_path / "src")
    # The production shape: the stale copy holds one of the two packages, not both.
    shadow = tree(tmp_path / "old-src", modules=PAIR[:1])
    path(shadow, source)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == FOREIGN and refuses(report)
    outside = [entry for entry in report["modules"] if not entry["inside"]]
    assert [entry["module"] for entry in outside] == [PAIR[0]]
    assert outside[0]["path"] == str(shadow / PAIR[0] / "__init__.py")
    # The one that resolved correctly is still named, because the useful question
    # after a refusal is which two directories disagreed.
    assert [entry["path"] for entry in report["modules"] if entry["inside"]] == \
        [str(source / PAIR[1] / "__init__.py")]


def test_a_module_that_resolves_nowhere_at_all_is_refused(path, tmp_path):
    source = tree(tmp_path / "src", modules=PAIR[:1])
    path(source)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == FOREIGN and refuses(report)
    missing = next(entry for entry in report["modules"] if entry["module"] == PAIR[1])
    assert missing == {"module": PAIR[1], "path": None, "origin": "not-found", "inside": False}


def test_a_root_that_is_configured_but_absent_is_refused(path, tmp_path):
    source = tree(tmp_path / "src")
    path(source)
    # Not the documented skip: a root that was configured and is not there means
    # the deployment cannot be confirmed, and an unconfirmed deployment stops.
    assert verify_source_root(str(tmp_path / "never-created"), modules=PAIR)["verdict"] == FOREIGN


def test_a_namespace_package_has_no_file_to_place(path, tmp_path):
    source = tmp_path / "src"
    (source / PAIR[0]).mkdir(parents=True)  # no __init__.py: a namespace package
    tree(source, modules=PAIR[1:])
    path(source)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == FOREIGN
    assert [entry["origin"] for entry in report["modules"] if entry["module"] == PAIR[0]] == ["no-file"]


def test_a_module_already_imported_answers_for_itself(path, tmp_path):
    source = tree(tmp_path / "src")
    other = tree(tmp_path / "old-src")
    path(source)
    try:
        importlib.import_module(PAIR[0])
        # The search path now offers only the other copy, but this process is
        # running the first one, and what it is running is what the check reports.
        path(other)
        report = verify_source_root(str(source), modules=PAIR[:1])
        assert report["verdict"] == VERIFIED
        assert report["modules"][0]["origin"] == "imported"
        assert verify_source_root(str(other), modules=PAIR[:1])["verdict"] == FOREIGN
    finally:
        sys.modules.pop(PAIR[0], None)


# ----------------------------------------------------------------- symlinks

def test_a_symlinked_root_is_not_a_false_alarm(path, tmp_path):
    source = tree(tmp_path / "src")
    link = tmp_path / "current"
    link.symlink_to(source, target_is_directory=True)
    path(source)
    # The deployment names the link; the modules resolved through the real path.
    assert verify_source_root(str(link), modules=PAIR)["verdict"] == VERIFIED
    # And the other way round: the path names the link, the configuration the
    # directory it points at.
    path(link)
    assert verify_source_root(str(source), modules=PAIR)["verdict"] == VERIFIED
    assert verify_source_root(str(link), modules=PAIR)["verdict"] == VERIFIED


def test_a_symlink_into_another_tree_is_still_foreign(path, tmp_path):
    source = tree(tmp_path / "src")
    elsewhere = tree(tmp_path / "old-src", modules=PAIR[:1])
    # One package of the root is a link to the stale copy. Resolution follows it,
    # which is the point: a symlink is not a way to smuggle a tree in.
    (source / PAIR[0] / "__init__.py").unlink()
    (source / PAIR[0]).rmdir()
    (source / PAIR[0]).symlink_to(elsewhere / PAIR[0], target_is_directory=True)
    path(source)
    assert verify_source_root(str(source), modules=PAIR)["verdict"] == FOREIGN


def test_two_real_spellings_of_one_directory_are_one_directory(path, tmp_path):
    """`resolve()` settles a symlink. What it cannot settle is a directory reachable
    under two names that are both real -- a case-insensitive volume, a tree mounted
    twice -- because resolution rewrites neither. There `samefile` is the only
    authority, and without it a correct deployment would be refused."""
    source = tree(tmp_path / "src")
    spelled = tmp_path / "SRC"
    if not spelled.is_dir():
        pytest.skip("this filesystem distinguishes case, so there is only one spelling")
    module = (source / PAIR[0] / "__init__.py").resolve()
    assert spelled.resolve() != source.resolve()  # the strings disagree
    assert integrity.within(spelled.resolve(), module)  # the filesystem does not
    path(source)
    assert verify_source_root(str(spelled), modules=PAIR)["verdict"] == VERIFIED


# ------------------------------------------------------------- the escape hatch

@pytest.mark.parametrize("value,opens", [("1", True), ("0", False), ("", False),
                                         ("true", False), ("yes", False), ("11", False)])
def test_the_escape_hatch_answers_to_exactly_one_value(path, tmp_path, monkeypatch, value, opens):
    source = tree(tmp_path / "src")
    path(tree(tmp_path / "old-src"))
    monkeypatch.setenv(ALLOW_ENV, value)
    report = verify_source_root(str(source), modules=PAIR)
    # The verdict never softens; only the decision made from it does.
    assert report["verdict"] == FOREIGN
    assert report["allowed"] is opens
    assert refuses(report) is not opens


def test_a_check_that_breaks_does_not_wave_the_process_through(tmp_path, monkeypatch, capsys):
    """The accident this whole module is arranged against: something inside the
    check raises, the host's handler catches it, prints it as one more failed
    action, and carries on running whatever code it happened to import."""
    def broken(*_, **__):
        raise RuntimeError("the check itself")
    monkeypatch.setattr(integrity, "verify_source_root", broken)

    with pytest.raises(SystemExit) as raised:
        enforce_source_root(str(tmp_path))
    assert raised.value.code == EXIT_CODE
    assert integrity.REFUSAL in capsys.readouterr().err
    # A deployment that never asked for the check is still not refused by it, and
    # the escape hatch still lets an operator past a check that is itself broken.
    assert enforce_source_root(None)["verdict"] == NOT_CONFIGURED
    monkeypatch.setenv(ALLOW_ENV, "1")
    assert enforce_source_root(str(tmp_path))["verdict"] == integrity.UNVERIFIABLE


def test_an_unsayable_refusal_is_still_a_refusal(path, tmp_path):
    source = tree(tmp_path / "src")
    path(tree(tmp_path / "old-src"))

    class Closed(io.StringIO):
        def write(self, _):
            raise OSError("stderr is gone")

    with pytest.raises(SystemExit):
        enforce_source_root(str(source), modules=PAIR, stream=Closed())


def test_checking_nothing_is_not_the_same_as_everything_passing(path, tmp_path):
    source = tree(tmp_path / "src")
    path(source)
    # `all` of an empty sequence is true, which is exactly the kind of accident a
    # check that gates start-up cannot afford.
    assert verify_source_root(str(source), modules=())["verdict"] == FOREIGN


def test_an_unrecognised_report_stops_the_process():
    # Whatever this module does not understand, it refuses: a report from an older
    # version, one a caller assembled by hand, one with the verdict missing.
    for report in ({}, {"verdict": None}, {"verdict": "probably-fine"}, {"modules": []}):
        assert refuses(report)


# ------------------------------------------------------------------- shadows

def test_another_copy_is_named_even_when_the_verdict_is_clean(path, tmp_path):
    source = tree(tmp_path / "src")
    stale = tree(tmp_path / "old-src", modules=PAIR[:1])
    # Correct today only because of the order: the stale copy is still installed.
    path(source, stale)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == VERIFIED
    assert report["shadows"] == [{"path": str(stale), "modules": [PAIR[0]]}]


def test_the_working_directory_counts_as_a_path_entry(path, tmp_path, monkeypatch):
    source = tree(tmp_path / "src")
    here = tree(tmp_path / "here", modules=PAIR[:1])
    monkeypatch.chdir(here)
    path("", source)
    report = verify_source_root(str(source), modules=PAIR)
    assert report["verdict"] == FOREIGN
    assert report["shadows"] == [{"path": str(here.resolve()), "modules": [PAIR[0]]}]


# ------------------------------------------------------------- what it prints

def test_the_refusal_names_the_root_and_both_modules(path, tmp_path, capsys):
    source = tree(tmp_path / "src")
    shadow = tree(tmp_path / "old-src", modules=PAIR[:1])
    path(shadow, source)
    with pytest.raises(SystemExit) as raised:
        enforce_source_root(str(source), modules=PAIR)
    assert raised.value.code == EXIT_CODE
    printed = capsys.readouterr().err
    assert integrity.REFUSAL in printed and ALLOW_ENV in printed
    assert str(source) in printed
    assert str(shadow / PAIR[0] / "__init__.py") in printed
    assert str(source / PAIR[1] / "__init__.py") in printed
    assert printed.count(integrity.OUTSIDE) == 1
    # A fixed sentence, then paths, then a fixed sentence. Nothing a request or a
    # store could have put there.
    lines = printed.splitlines()
    assert lines[0] == integrity.REFUSAL and lines[-1] == integrity.ESCAPE
    assert all(line.startswith("  ") for line in lines[1:-1])


def test_the_command_line_says_so_and_carries_on(path, tmp_path, capsys):
    source = tree(tmp_path / "src")
    path(tree(tmp_path / "old-src"))
    report = enforce_source_root(str(source), modules=PAIR, warn_only=True)
    assert report["verdict"] == FOREIGN
    printed = capsys.readouterr().err
    assert integrity.CAUTION in printed and integrity.REFUSAL not in printed


def test_a_clean_verdict_prints_nothing(path, tmp_path, capsys):
    source = tree(tmp_path / "src")
    path(source)
    assert enforce_source_root(str(source), modules=PAIR)["verdict"] == VERIFIED
    assert enforce_source_root(None, modules=PAIR)["verdict"] == NOT_CONFIGURED
    assert capsys.readouterr().err == ""


# ----------------------------------------------- a real interpreter, real paths

def test_a_shadow_install_is_caught_in_a_real_interpreter(tmp_path):
    """The production shape, with the real module names: a stale editable install
    holding `eventmem` and no `kin_mind`, ahead of the deployed source."""
    stale = tree(tmp_path / "old-src", modules=("eventmem",))
    clean = tmp_path / "cwd"
    clean.mkdir()
    report = child(SOURCE, cwd=clean, pythonpath=[stale, SOURCE])
    assert report["verdict"] == FOREIGN
    placed = {entry["module"]: entry for entry in report["modules"]}
    assert placed["eventmem"]["path"] == str(stale / "eventmem" / "__init__.py")
    assert placed["eventmem"]["inside"] is False
    assert placed["kin_mind"]["inside"] is True
    assert {"path": str(stale), "modules": ["eventmem"]} in report["shadows"]


def test_the_working_directory_shadows_a_real_interpreter_too(tmp_path):
    """The host runs with a working directory it does not own and a `PYTHONPATH`
    that points at the deployment. The working directory still comes first."""
    here = tree(tmp_path / "host-home", modules=("eventmem",))
    report = child(SOURCE, cwd=here, pythonpath=[SOURCE])
    assert report["first_on_path"] in {"", str(here)}
    assert report["verdict"] == FOREIGN
    placed = {entry["module"]: entry for entry in report["modules"]}
    assert placed["eventmem"]["path"] == str(here / "eventmem" / "__init__.py")


def test_this_very_deployment_is_not_refused(tmp_path):
    clean = tmp_path / "cwd"
    clean.mkdir()
    report = child(SOURCE, cwd=clean, pythonpath=[SOURCE])
    assert report["verdict"] == VERIFIED, report
    assert {entry["module"] for entry in report["modules"]} == {"eventmem", "kin_mind"}
    assert all(entry["inside"] for entry in report["modules"])


def test_a_symlinked_deployment_is_not_refused_in_a_real_interpreter(tmp_path):
    link = tmp_path / "current-src"
    link.symlink_to(SOURCE, target_is_directory=True)
    clean = tmp_path / "cwd"
    clean.mkdir()
    # The interpreter imports through the link and the configuration names the
    # directory, and the other way round. Neither is a foreign install.
    assert child(SOURCE, cwd=clean, pythonpath=[link])["verdict"] == VERIFIED
    assert child(link, cwd=clean, pythonpath=[SOURCE])["verdict"] == VERIFIED


def test_a_root_nobody_configured_skips_in_a_real_interpreter(tmp_path):
    stale = tree(tmp_path / "old-src", modules=("eventmem",))
    clean = tmp_path / "cwd"
    clean.mkdir()
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(stale), str(SOURCE)])}
    environment.pop(ROOT_ENV, None)
    done = subprocess.run([sys.executable, "-c", PROBE.replace("sys.argv[2]", "None"), str(INTEGRITY)],
                          cwd=str(clean), env=environment, capture_output=True, text=True,
                          timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["verdict"] == NOT_CONFIGURED


# --------------------------------------------------------- the interpreter half

def test_the_configured_interpreter_is_the_one_that_exists(tmp_path):
    report = verify_interpreter(sys.executable)
    assert report["state"] == integrity.USABLE and report["usable"] is True
    assert report["running_is_configured"] is True
    assert not integrity.interpreter_warns(report)
    # It says which interpreter it is about, so a clean answer cannot be read as a
    # remark about the process doing the asking.
    assert "not the one running this check" in report["subject"]


def test_an_interpreter_that_was_deleted_is_reported(tmp_path):
    """The two-hour outage: the configuration named a Python that had been removed,
    every spawn failed, and the process that could have said so was the one process
    that had already started."""
    gone = tmp_path / "venv" / "bin" / "python"
    report = verify_interpreter(str(gone))
    assert report["state"] == integrity.MISSING and report["usable"] is False
    assert report["configured"] == str(gone)
    assert report["running"] == sys.executable
    assert report["running_is_configured"] is False
    assert integrity.interpreter_warns(report)


def test_an_interpreter_whose_target_is_gone_is_reported(tmp_path):
    link = tmp_path / "python"
    link.symlink_to(tmp_path / "removed-during-a-cleanup")
    assert verify_interpreter(str(link))["state"] == integrity.MISSING


def test_an_interpreter_that_is_there_but_will_not_run_is_reported(tmp_path):
    blocked = tmp_path / "python"
    blocked.write_text("#!/bin/sh\n")
    blocked.chmod(0o644)
    report = verify_interpreter(str(blocked))
    assert report["state"] == integrity.NOT_EXECUTABLE and report["usable"] is False
    assert integrity.interpreter_warns(report)
    blocked.chmod(0o755)
    assert verify_interpreter(str(blocked))["state"] == integrity.USABLE


def test_a_directory_is_not_an_interpreter(tmp_path):
    assert verify_interpreter(str(tmp_path))["state"] == integrity.NOT_A_FILE


def test_no_configured_interpreter_is_nothing_to_report(tmp_path):
    for absent in (None, ""):
        report = verify_interpreter(absent)
        assert report["state"] == NOT_CONFIGURED and report["usable"] is False
        assert report["configured"] is None and report["running_is_configured"] is None
        assert not integrity.interpreter_warns(report)


def test_an_unrecognised_interpreter_state_is_still_said_out_loud():
    # It never stops anything, so there is no cost to erring towards speech.
    assert integrity.interpreter_warns({"state": "something-new"})
    assert integrity.interpreter_warns({})


def test_a_usable_interpreter_that_is_not_this_one_is_visible(tmp_path):
    """A corrected configuration does not correct a caller that spawns from a path
    of its own. The two values side by side are what shows that."""
    other = tmp_path / "python"
    other.write_text("#!/bin/sh\nexec true\n")
    other.chmod(0o755)
    report = verify_interpreter(str(other))
    assert report["state"] == integrity.USABLE
    assert report["running_is_configured"] is False
    assert report["shares_base_interpreter"] is False
    assert report["resolved"] == str(other.resolve()) != report["running"]


def test_two_environments_over_one_python_are_two_environments(tmp_path):
    """The shape the incident actually left behind: a second virtual environment
    whose `bin/python` links to the same base build. `samefile` calls those two one
    interpreter; they have different packages and different code, so the comparison
    that decides is the path, and the shared base is reported separately."""
    mine, theirs = tmp_path / "a/bin", tmp_path / "b/bin"
    for directory in (mine, theirs):
        directory.mkdir(parents=True)
        (directory / "python").symlink_to(Path(sys.executable).resolve())
    report = verify_interpreter(str(theirs / "python"), running=str(mine / "python"))
    assert report["state"] == integrity.USABLE
    assert report["running_is_configured"] is False
    assert report["shares_base_interpreter"] is True
    # And the same environment stays the same environment, link or no link.
    same = verify_interpreter(str(mine / "python"), running=str(mine / "python"))
    assert same["running_is_configured"] is True and same["shares_base_interpreter"] is True


def test_the_warning_says_what_was_configured_and_what_is_running(tmp_path, capsys):
    gone = tmp_path / "python"
    report = warn_interpreter(str(gone))
    assert report["state"] == integrity.MISSING
    printed = capsys.readouterr().err
    lines = printed.splitlines()
    assert lines[0] == integrity.UNUSABLE
    assert str(gone) in printed and integrity.MISSING in printed
    assert sys.executable in printed
    assert all(line.startswith("  ") for line in lines[1:])


def test_a_usable_or_absent_interpreter_prints_nothing(capsys):
    assert warn_interpreter(sys.executable)["usable"] is True
    assert warn_interpreter(None)["state"] == NOT_CONFIGURED
    assert capsys.readouterr().err == ""


def test_the_interpreter_check_never_stops_a_process(tmp_path, monkeypatch, capsys):
    def broken(*_, **__):
        raise RuntimeError("the check itself")
    monkeypatch.setattr(integrity, "verify_interpreter", broken)
    # Not even its own failure: whatever finds this fault is the only thing left
    # able to report it.
    report = warn_interpreter(str(tmp_path / "python"))
    assert report["state"] == integrity.UNREADABLE and report["error"] == "RuntimeError"
    assert integrity.UNUSABLE in capsys.readouterr().err


# ------------------------------------------------------------ the entry points

def host_config(tmp_path, **extra):
    credentials = tmp_path / "credentials"
    credentials.write_text("# nothing to read here\n")
    config = tmp_path / "mind-config.json"
    config.write_text(json.dumps({"credentials_file": str(credentials), "root": str(tmp_path / "db"),
                                  "scope": {"persona": "synthetic-memory"}, "agent_version": "fixture-v1",
                                  **extra}))
    return config


def test_the_host_refuses_to_start_on_a_foreign_source(tmp_path, monkeypatch, capsys):
    from kin_mind import host

    config = host_config(tmp_path, source_root=str(tmp_path / "not-where-the-code-is"))
    monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(config), "operational-status"])
    # A SystemExit, not an exception: the handler in `main` catches every exception
    # and prints it as a result, so an exception would leave the host running the
    # wrong code with an error line as the only trace.
    with pytest.raises(SystemExit) as raised:
        host.main()
    assert raised.value.code == EXIT_CODE
    printed = capsys.readouterr()
    assert integrity.REFUSAL in printed.err
    # It refused before it read the request or opened the store, so it printed no
    # result at all.
    assert printed.out == ""


def test_the_host_starts_when_the_deployment_is_the_one_configured(tmp_path, monkeypatch, capsys):
    from kin_mind import host

    config = host_config(tmp_path, source_root=str(SOURCE))
    monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(config), "synthetic-action"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(host, "dispatch", lambda *_: {"state": "dispatched"})
    host.main()
    assert json.loads(capsys.readouterr().out) == {"state": "dispatched"}


def test_the_host_says_the_interpreter_is_gone_and_still_answers(tmp_path, monkeypatch, capsys):
    from kin_mind import host

    config = host_config(tmp_path, source_root=str(SOURCE), python=str(tmp_path / "venv/bin/python"))
    monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(config), "synthetic"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(host, "dispatch", lambda *_: {"state": "dispatched"})
    host.main()
    printed = capsys.readouterr()
    # Said, and then the action ran anyway: the caller that can still report this is
    # the last one that should be stopped by it.
    assert integrity.UNUSABLE in printed.err
    assert json.loads(printed.out) == {"state": "dispatched"}


def test_the_host_without_a_configured_root_starts_anywhere(tmp_path, monkeypatch, capsys):
    from kin_mind import host

    monkeypatch.setattr(sys, "argv", ["kin_mind.host", "--config", str(host_config(tmp_path)), "synthetic"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(host, "dispatch", lambda *_: {"state": "dispatched"})
    host.main()
    assert json.loads(capsys.readouterr().out) == {"state": "dispatched"}


def test_the_status_report_names_the_source_it_is_running(system, tmp_path):
    from kin_mind.appraisal import Appraisals
    from kin_mind.operational_status import operational_status

    mind = system[0]
    Appraisals(mind)  # the queue the status report counts
    clean = operational_status(mind, {"source_root": str(SOURCE)})["source"]
    assert clean["verdict"] == VERIFIED
    assert {entry["module"] for entry in clean["modules"]} == {"eventmem", "kin_mind"}
    # No configuration, and no config argument at all: the block is still there and
    # says plainly that nothing was compared.
    assert operational_status(mind, {})["source"]["verdict"] == NOT_CONFIGURED
    assert operational_status(mind)["source"]["verdict"] == NOT_CONFIGURED
    foreign = operational_status(mind, {"source_root": str(tmp_path / "elsewhere")})["source"]
    assert foreign["verdict"] == FOREIGN
    assert [entry["path"] for entry in foreign["modules"] if entry["inside"]] == []


def test_the_status_report_names_the_interpreter_of_the_next_spawn(system, tmp_path):
    from kin_mind.appraisal import Appraisals
    from kin_mind.operational_status import operational_status

    mind = system[0]
    Appraisals(mind)
    gone = tmp_path / "venv" / "bin" / "python"
    # The one channel that reaches an operator: the host's own stderr is discarded
    # by the process that spawns it, so this block is where a deleted interpreter
    # becomes visible without anyone already suspecting it.
    reported = operational_status(mind, {"python": str(gone)})["source"]["interpreter"]
    assert reported["state"] == integrity.MISSING and reported["usable"] is False
    assert reported["configured"] == str(gone)
    assert reported["running"] == sys.executable
    assert "not the one running this check" in reported["subject"]
    working = operational_status(mind, {"python": sys.executable})["source"]["interpreter"]
    assert working["state"] == integrity.USABLE and working["running_is_configured"] is True
    assert operational_status(mind, {})["source"]["interpreter"]["state"] == NOT_CONFIGURED
