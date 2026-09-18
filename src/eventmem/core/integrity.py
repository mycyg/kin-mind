"""Which copy of the source this process is actually running.

A deployment says where the code lives. Nothing until now checked that the
interpreter agreed. An editable install left behind in a virtual environment puts
a second, older copy of a package on `sys.path`, and the current working directory
sits ahead of everything the deployment controls; either one is enough for a
process to import code that was replaced days ago. The failure leaves no trace at
all: the host starts, the store opens, the wrong module answers every call, and
the only symptom is that a fix which was deployed appears not to have been.

So the first thing an entry point does is ask where `eventmem` and `kin_mind`
resolved from, and refuse to go on when either one is outside the configured
source root. The refusal names both paths, because the only useful question after
such a message is which two directories disagreed.

The check is blunt in one direction and careful in the other. It fails closed: a
module that cannot be resolved at all, a root that is not there, a verdict this
module does not recognise -- each refuses, because a self-check that waves work
through when it is confused is not a check. It also must not refuse a deployment
that is in fact correct, since that costs the same downtime as the fault it
prevents. That is why both sides are `resolve()`d and, when the strings still
disagree, compared with `samefile`: a symlinked root, a case-insensitive volume
and a directory mounted twice each produce two spellings of one directory, and
none of them is a foreign install.

Three ways past it, and no others. `KIN_ALLOW_FOREIGN_SOURCE=1` starts anyway, for
an operator deliberately running from somewhere else. A root that is not
configured skips the check, because there is then nothing to compare against. And
the `eventmem` command line only warns: it is a tool an operator points at
whatever checkout they like, and a refusal there would be theatre.

The interpreter is the other half of the same question, and the half that cost two
hours: a configuration named a Python that had been deleted, every spawn failed
once a minute, and nothing anywhere wrote it down, because the process that could
have noticed was the one process that had already started. So the configured
interpreter is checked too -- and only reported, never refused. Whatever finds
that fault is by definition still running, and stopping it would take the report
down along with the thing being reported.

Nothing here imports the modules it is asked about, and nothing here imports the
rest of this package. The check has to run before the code it is checking.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# The environment holds both halves of the contract: where the code is supposed to
# be when a caller has no configuration of its own, and the one way to start with
# it somewhere else.
ROOT_ENV = "KIN_SOURCE_ROOT"
ALLOW_ENV = "KIN_ALLOW_FOREIGN_SOURCE"

# The two packages a deployment of this repository consists of. They ship
# together, so a process that resolves them to different trees has already found
# the fault this module exists for.
MODULES = ("eventmem", "kin_mind")

NOT_CONFIGURED = "not-configured"
VERIFIED = "verified"
FOREIGN = "foreign"
# What the interpreter named by the configuration turned out to be.
USABLE = "usable"
MISSING = "missing"
NOT_A_FILE = "not-a-file"
NOT_EXECUTABLE = "not-executable"
UNREADABLE = "unreadable"
# The two answers that are not worth a word. Anything else, including a state this
# module does not recognise, is said out loud -- it never stops a process, so there
# is no cost to erring towards saying it.
INTERPRETER_QUIET = frozenset({NOT_CONFIGURED, USABLE})
# The check itself broke. Not a pass: a check that could not run has not run.
UNVERIFIABLE = "unverifiable"
# Written as the set of verdicts that permit a start, never as a test for the ones
# that do not. A report from an older version, or one a caller assembled itself,
# then stops the process instead of being waved through.
STARTABLE = frozenset({NOT_CONFIGURED, VERIFIED})

# EX_CONFIG. The deployment is wrong, not the request, and no retry will help.
EXIT_CODE = 78

REFUSAL = ("Refusing to start: this process would import code from outside the configured "
           "source root, so the deployed code and the running code are not the same.")
CAUTION = ("Warning: this command is importing code from outside the configured source root, "
           "so the deployed code and the running code are not the same.")
ESCAPE = f"Set {ALLOW_ENV}=1 to start anyway, or remove the other copy from the environment."
OUTSIDE = "  <- outside the source root"

# Said in the report itself, because the obvious misreading of a clean answer here
# is that it describes this process -- which would be worth nothing, since this
# process demonstrably started.
INTERPRETER_SUBJECT = ("the interpreter the configuration names for the next worker the host "
                       "spawns, not the one running this check")
UNUSABLE = ("Warning: the interpreter named by the configuration cannot be run, so every worker "
            "the host spawns from now on will fail to start. This process is already running and "
            "is not affected by it, which is why this is a warning and not a refusal.")


def _environ(environ):
    return os.environ if environ is None else environ


def _allowed(environ=None):
    """Exactly `1`, and nothing else.

    An escape hatch that also answers to `true`, `yes` or `0` is an escape hatch
    that opens by accident, and this one disables the check that stands between a
    deployment and running the wrong code for a week.
    """
    return _environ(environ).get(ALLOW_ENV) == "1"


def _origin(name):
    """Where `name` is, or would be, imported from -- without importing it.

    A module this process has already imported answers for itself: that file is
    what is running, whatever the search path would say now. Anything else is put
    to the import machinery, which resolves it exactly as an import would, the
    current working directory included, and still executes nothing.
    """
    module = sys.modules.get(name)
    if module is not None:
        return getattr(module, "__file__", None), "imported"
    try:
        spec = importlib.util.find_spec(name)
    except Exception:  # noqa: BLE001 - a name that cannot be resolved is not a name that passes
        # A parent package that is itself broken, a name that is not a module, an
        # entry on the path the machinery chokes on. Every one of them is a failure
        # to establish where the code is, and this module's answer to that is no.
        return None, "unresolvable"
    if spec is None:
        return None, "not-found"
    if not spec.origin or spec.origin in {"namespace", "built-in", "frozen"}:
        # A namespace package has no file, so there is nothing to place under a
        # root -- which is itself the answer, and it is not a pass.
        return None, "no-file"
    return spec.origin, "resolved"


def _cwd():
    try:
        return os.getcwd()
    except OSError:
        return None


def _resolved(path):
    if path is None:
        return None
    try:
        return Path(path).resolve()
    except OSError:
        # A symlink loop, or a path the filesystem refuses to walk. Unknown, and
        # unknown never passes.
        return None


def within(root, path):
    """Whether `path` lies inside `root`, asking the filesystem when the strings
    disagree.

    Both arrive resolved, so an ordinary symlinked root is already settled by the
    comparison itself. What resolution does not settle is a directory with two
    valid spellings -- a case-insensitive volume, a tree reachable through two
    mounts -- and there the only authority is `samefile`, which compares the
    filesystem's own identity for the two paths rather than their text.
    """
    if path == root or root in path.parents:
        return True
    for parent in (path, *path.parents):
        try:
            if parent.samefile(root):
                return True
        except OSError:
            # Either end may be absent. A path that is not there is not the root,
            # so keep walking up: a file's parents can exist where the file does
            # not, and a root that exists nowhere ends this loop at False.
            continue
    return False


def shadow_entries(root, modules=MODULES):
    """Every other copy of these modules that is reachable on `sys.path`.

    A shadow only bites when it comes first, so a process can be importing the
    right code today and the wrong code tomorrow after one change to `PYTHONPATH`
    or one command run from another directory. That is worth saying out loud
    before it happens, which is why this is collected even when the verdict is
    clean and reported by `operational-status`.
    """
    seen, found = set(), []
    for entry in sys.path:
        # An empty entry means the current working directory, which the import
        # machinery resolves when an import happens rather than at start-up -- and
        # which a process can be left without, if the directory it started in was
        # removed underneath it.
        directory = _resolved(entry or _cwd())
        if directory is None or directory in seen:
            continue
        seen.add(directory)
        if root is not None and within(root, directory):
            continue
        held = [name for name in modules
                if (directory / name / "__init__.py").exists() or (directory / f"{name}.py").exists()]
        if held:
            found.append({"path": str(directory), "modules": held})
    return found


def verify_source_root(source_root=None, *, modules=MODULES, environ=None, shadows=True):
    """Report where each module resolved from and whether that is the deployment.

    Never raises for a foreign source and never exits: the verdict is the return
    value, so a caller decides what a refusal means. `refuses` reads it, `message`
    renders it and `enforce_source_root` acts on it.

    An explicit `source_root` wins; otherwise the environment is asked. Neither
    one configured is the documented skip.
    """
    environ = _environ(environ)
    configured = source_root or environ.get(ROOT_ENV) or None
    allowed = _allowed(environ)
    if configured is None:
        return {"verdict": NOT_CONFIGURED, "allowed": allowed, "source_root": None,
                "modules": [], "shadows": []}
    root = _resolved(configured)
    placed = []
    for name in modules:
        origin, how = _origin(name)
        path = _resolved(origin) if origin else None
        placed.append({"module": name, "path": str(path) if path else None, "origin": how,
                       "inside": bool(root is not None and path is not None and within(root, path))})
    # `placed` is tested before the conjunction over it: an empty list satisfies
    # `all` and would otherwise turn "nothing was checked" into "everything passed".
    return {"verdict": VERIFIED if placed and all(entry["inside"] for entry in placed) else FOREIGN,
            "allowed": allowed, "source_root": str(root if root is not None else configured),
            "modules": placed, "shadows": shadow_entries(root, modules) if shadows else []}


def refuses(report):
    """Whether this report must stop a process."""
    if report.get("allowed"):
        return False
    return report.get("verdict") not in STARTABLE


def message(report, *, refusing=True):
    """The static text a refusal prints: one fixed sentence, then the paths.

    Nothing from a request or a store reaches it. What it adds to the sentence is
    the three directories an operator needs and cannot otherwise see -- the root
    the deployment configured, where each module came from instead, and any other
    copy still reachable on the path.
    """
    lines = [REFUSAL if refusing else CAUTION, f"  source_root: {report.get('source_root')}"]
    for entry in report.get("modules") or []:
        lines.append(f"  {entry['module']}: {entry['path'] or 'not found'}"
                     + ("" if entry.get("inside") else OUTSIDE))
    for shadow in report.get("shadows") or []:
        lines.append(f"  also reachable: {shadow['path']} ({', '.join(shadow['modules'])})")
    lines.append(ESCAPE)
    return "\n".join(lines)


def enforce_source_root(source_root=None, *, warn_only=False, stream=None, **kwargs):
    """The one call an entry point makes. Returns the report; may not return.

    A refusal is a `SystemExit`, not an ordinary exception, and that is the whole
    point of it. The host's top-level handler turns exceptions into a result
    document and still exits zero, so an exception raised here would be reported
    as one more failed action and forgotten -- the check would have failed open at
    exactly the moment it mattered. `SystemExit` passes through that handler
    untouched and leaves a non-zero status behind.

    For the same reason nothing else this function does is allowed to raise past
    it. A check that broke has not passed, so its own failure refuses too, and a
    stderr that will not take the message still exits.
    """
    try:
        report = verify_source_root(source_root, **kwargs)
    except Exception as error:  # noqa: BLE001 - a check that could not run has not passed
        environ = _environ(kwargs.get("environ"))
        configured = source_root or environ.get(ROOT_ENV) or None
        report = {"verdict": NOT_CONFIGURED if configured is None else UNVERIFIABLE,
                  "allowed": _allowed(environ), "source_root": configured,
                  "modules": [], "shadows": [], "error": type(error).__name__}
    if not refuses(report):
        return report
    try:
        print(message(report, refusing=not warn_only),
              file=stream if stream is not None else sys.stderr, flush=True)
    except Exception:  # noqa: BLE001, S110 - saying why is worth less than not starting
        pass
    if warn_only:
        return report
    raise SystemExit(EXIT_CODE)


def _same_file(one, other):
    if one is None or other is None:
        return None
    if one == other:
        return True
    try:
        return one.samefile(other)
    except OSError:
        return False


def _same_path(one, other):
    """Two interpreters are the same interpreter only if they are the same path.

    Not `samefile`, which is right everywhere else in this module and wrong here.
    A virtual environment's `bin/python` is usually a link to a shared base build,
    so two entirely different environments -- different packages, different
    installed code -- answer `samefile` with yes. The environment is the thing that
    differs, and the path is what names it.
    """
    if one is None or other is None:
        return None
    return Path(one) == Path(other)


def verify_interpreter(python=None, *, running=None):
    """Whether the interpreter the configuration names can actually be run.

    The other half of the same question, and the half that was missing. A source
    root answers "is the running code the deployed code"; this answers "is there
    anything to run it with at all". A configuration naming an interpreter that is
    not there fails every spawn, one a minute, with nothing written down anywhere
    -- the host keeps its own process, so it never notices that it has stopped
    being able to start any others.

    The subject has to be stated, because a clean answer here is easy to misread
    as being about this process, and about this process it would be worthless: the
    check is running, so its own interpreter plainly exists. What is being asked
    about is the value the host will hand to the **next** spawn.

    Nothing here refuses. A process that has found this is the only one still able
    to say so, and stopping it would take the report down with the fault.
    """
    running = running if running is not None else sys.executable
    report = {"subject": INTERPRETER_SUBJECT, "configured": str(python) if python else None,
              "resolved": None, "state": NOT_CONFIGURED, "usable": False,
              "running": str(running) if running else None, "running_is_configured": None,
              "shares_base_interpreter": None}
    if not python:
        return report
    path = Path(python)
    try:
        if not path.exists():
            # A symlink whose target is gone answers False here, which is the right
            # answer: the name is there and nothing runs.
            state = MISSING
        elif not path.is_file():
            state = NOT_A_FILE
        elif not os.access(path, os.X_OK):
            state = NOT_EXECUTABLE
        else:
            state = USABLE
    except OSError:
        state = UNREADABLE
    resolved = _resolved(path)
    report.update(resolved=str(resolved) if resolved else None, state=state, usable=state == USABLE,
                  # Whether the configuration names the interpreter this process is
                  # in fact running under. A usable interpreter that is not this one
                  # means something started this process from a path the
                  # configuration does not name -- which is how a corrected
                  # configuration leaves a caller still spawning the old environment
                  # from a path hardcoded somewhere else.
                  running_is_configured=_same_path(python, running),
                  # And, when they differ, whether they are two environments over one
                  # Python build or two unrelated installations. The first is the
                  # ordinary shape of that fault and the second is a stranger one.
                  shares_base_interpreter=_same_file(resolved, _resolved(running)))
    return report


def interpreter_warns(report):
    """Whether this report is worth a word. Unknown states are."""
    return report.get("state") not in INTERPRETER_QUIET


def warn_interpreter(python=None, *, stream=None, **kwargs):
    """Say it and carry on. Returns the report; never exits, never raises."""
    try:
        report = verify_interpreter(python, **kwargs)
    except Exception as error:  # noqa: BLE001 - a check that could not run says so and stops there
        report = {"subject": INTERPRETER_SUBJECT, "configured": str(python) if python else None,
                  "resolved": None, "state": UNREADABLE if python else NOT_CONFIGURED,
                  "usable": False, "running": None, "running_is_configured": None,
                  "shares_base_interpreter": None, "error": type(error).__name__}
    if not interpreter_warns(report):
        return report
    try:
        print(interpreter_message(report), file=stream if stream is not None else sys.stderr,
              flush=True)
    except Exception:  # noqa: BLE001, S110 - a warning that cannot be printed is still not a failure
        pass
    return report


def interpreter_message(report):
    """The static warning: one fixed sentence, then what the configuration said and
    what is actually there."""
    return "\n".join([UNUSABLE,
                      f"  configured: {report.get('configured')}",
                      f"  state: {report.get('state')}",
                      f"  running now: {report.get('running')}"])
