"""Test-only guard, loaded as `sitecustomize` when this directory is on PYTHONPATH.

Everything comes from the environment, and nothing happens when it is unset:
  KIN_PROTECTED_ROOTS="root1:root2:..."  writes, SQLite opens (any mode, reads included) and
                                         child processes that reach these roots raise
                                         `hermetic: blocked ...`
  KIN_REMAP="prodPrefix=devPrefix;..."   import path entries below prodPrefix load from devPrefix
  KIN_BLOCK_NETWORK=1                    connections to anything but loopback raise
  KIN_BLOCKED_PORTS="8319,..."           loopback ports that belong to something else (a live
                                         service); connections to them raise too
  KIN_PROTECTED_PIDS="1,..."             processes that must not be signalled (all that ran
                                         before the tests); signal 0 still probes them
Paths are compared after resolving symlinks. A child started with its own environment gets
the guard carried into it. Blocked operations are reported on stderr at exit and make the
exit status non-zero, so an `except OSError: pass` cannot hide one. Another sitecustomize
further along the path still runs. The Node half is ../hermetic-preload.mjs.
"""

import os
import sys

_ENV = os.environ
_HERE = os.path.dirname(os.path.abspath(__file__))
_EXEMPT = {"KIN_REMAP", "KIN_PROTECTED_ROOTS"}
_fold = str.lower if sys.platform in ("darwin", "win32") else (lambda s: s)
_blocked = []
_roots = []
_pairs = []


class HermeticViolation(PermissionError):
    pass


def _trim(path):
    return path.rstrip("/") or "/"


def _parse_roots(value):
    roots = {}
    for raw in filter(None, (s.strip() for s in value.split(":"))):
        root = _trim(os.path.abspath(raw))
        for spelling in (root, _trim(os.path.realpath(root))):
            roots.setdefault(_fold(spelling), spelling)
    return list(roots.items())


def _parse_remap(value):
    pairs = {}
    for entry in value.split(";"):
        source, sep, target = entry.partition("=")
        source, target = _trim(source.strip()), _trim(target.strip())
        if sep and source and target:
            pairs.setdefault(source, target)
    return sorted(pairs.items(), key=lambda pair: -len(pair[0]))


def _violate(message):
    error = HermeticViolation("hermetic: blocked " + message)
    _blocked.append(str(error))
    raise error


def _inside(path):
    folded = _fold(path)
    return any(folded == root or folded.startswith(root if root == "/" else root + "/") for root, _ in _roots)


def _mentions(text):
    folded = _fold(text)
    for root, shown in _roots:
        at = folded.find(root)
        while at >= 0:
            after = folded[at + len(root):at + len(root) + 1]
            if not after or not (after.isalnum() or after in "_.-"):
                return shown
            at = folded.find(root, at + 1)
    return None


def _check(op, target, follow=True, dir_fd=None):
    if target is None or isinstance(target, int):
        return
    try:
        path = os.fsdecode(target)
    except TypeError:
        return
    if dir_fd is not None and not os.path.isabs(path):
        return
    absolute = os.path.abspath(path)
    if follow:
        real = os.path.realpath(absolute)
    else:
        real = os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))
    if _inside(absolute) or _inside(real):
        _violate(f"{op} on protected path {path}")


def _wrap(owner, name, checks):
    """checks: (position, keyword, follow, dir_fd keyword) for each path argument."""
    original = getattr(owner, name, None)
    if original is None:
        return

    def guarded(*args, **kwargs):
        for position, keyword, follow, fd_key in checks:
            value = args[position] if position < len(args) else kwargs.get(keyword)
            if kwargs.get("follow_symlinks") is False:
                follow = False
            _check(name, value, follow, kwargs.get(fd_key) if fd_key else None)
        return original(*args, **kwargs)

    guarded.__name__ = getattr(original, "__name__", name)
    guarded.__doc__ = getattr(original, "__doc__", None)
    setattr(owner, name, guarded)


def _install_files():
    import builtins
    import io
    import shutil
    import sqlite3
    import sqlite3.dbapi2

    real_open = builtins.open

    def open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            _check("open", file)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = io.open = open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    real_os_open = os.open

    def os_open(path, flags, *args, **kwargs):
        if flags & write_flags:
            _check("os.open", path, dir_fd=kwargs.get("dir_fd"))
        return real_os_open(path, flags, *args, **kwargs)

    os.open = os_open
    for name in ("mkdir", "makedirs"):
        _wrap(os, name, [(0, "path" if name == "mkdir" else "name", True, "dir_fd")])
    for name in ("remove", "unlink", "rmdir"):
        _wrap(os, name, [(0, "path", False, "dir_fd")])
    for name in ("rename", "replace"):
        _wrap(os, name, [(0, "src", False, "src_dir_fd"), (1, "dst", False, "dst_dir_fd")])
    _wrap(os, "symlink", [(1, "dst", False, "dir_fd")])
    _wrap(os, "link", [(0, "src", True, "src_dir_fd"), (1, "dst", False, "dst_dir_fd")])
    for name in ("chmod", "chown", "utime"):
        _wrap(os, name, [(0, "path", True, "dir_fd")])
    _wrap(os, "truncate", [(0, "path", True, None)])
    _wrap(shutil, "rmtree", [(0, "path", False, "dir_fd")])
    for name in ("copyfile", "copy", "copy2", "copytree", "copymode", "copystat"):
        _wrap(shutil, name, [(1, "dst", True, None)])
    _wrap(shutil, "move", [(0, "src", False, None), (1, "dst", True, None)])

    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        target = _sqlite_path(database)
        if target is not None:
            _check("sqlite3.connect", target)
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = sqlite3.dbapi2.connect = connect


def _sqlite_path(database):
    try:
        name = os.fsdecode(database)
    except TypeError:
        return None
    if name in ("", ":memory:"):
        return None
    if not name.startswith("file:"):
        return name
    from urllib.parse import unquote

    rest, _, query = name[5:].partition("?")
    if "mode=memory" in query:
        return None
    rest = rest.partition("#")[0]
    if rest.startswith("//"):
        rest = "/" + rest[2:].partition("/")[2]
    rest = unquote(rest)
    return None if rest in ("", ":memory:") else rest


_LAUNCHCTL = None


def _inspect_spawn(op, strings, executable=None, cwd=None, env=None, shell=False):
    import re
    import shutil

    global _LAUNCHCTL
    if _LAUNCHCTL is None:
        _LAUNCHCTL = re.compile(r"(?:^|[\s;&|()`'\"=/])launchctl(?=$|[\s;&|()`'\"])")
    texts = [os.fsdecode(s) for s in strings if isinstance(s, (str, bytes, os.PathLike))]
    if executable is not None:
        texts.insert(0, os.fsdecode(executable))
    for text in texts:
        if _LAUNCHCTL.search(text) or os.path.basename(text) == "launchctl":
            _violate(f"{op} of launchctl")
    if not _roots:
        return
    for text in texts:
        root = _mentions(text)
        if root:
            _violate(f"{op} on protected path {root}")
        if text.startswith("/"):
            _check(op, text)
    child_env = _ENV if env is None else env
    command = texts[0] if texts else ""
    if not shell and command and "/" not in command:
        path = child_env.get("PATH") if isinstance(child_env.get("PATH", ""), str) else None
        found = shutil.which(command, path=path)
        if found:
            _check(op, found)
    if cwd is not None:
        _check(op, cwd)
    for key, value in child_env.items():
        if key in _EXEMPT or not isinstance(value, str):
            continue
        root = _mentions(value)
        if root:
            _violate(f"{op} on protected path {root} (env {key})")


def _merged(separator, own, inherited):
    seen = []
    for item in (own or "").split(separator) + (inherited or "").split(separator):
        if item and item not in seen:
            seen.append(item)
    return separator.join(seen)


def _carry(env):
    if env is None or any(not isinstance(key, str) for key in env):
        return env
    out = dict(env)
    if _ENV.get("KIN_PROTECTED_ROOTS"):
        out["KIN_PROTECTED_ROOTS"] = _merged(":", out.get("KIN_PROTECTED_ROOTS"), _ENV["KIN_PROTECTED_ROOTS"])
    if _ENV.get("KIN_REMAP"):
        out["KIN_REMAP"] = _merged(";", out.get("KIN_REMAP"), _ENV["KIN_REMAP"])
    if _ENV.get("KIN_BLOCK_NETWORK") == "1":
        out["KIN_BLOCK_NETWORK"] = "1"
    if _ENV.get("PYTHONDONTWRITEBYTECODE"):
        out.setdefault("PYTHONDONTWRITEBYTECODE", _ENV["PYTHONDONTWRITEBYTECODE"])
    for key in ("KIN_BLOCKED_PORTS", "KIN_PROTECTED_PIDS"):
        if _ENV.get(key):
            out[key] = _merged(",", out.get(key), _ENV[key])
    if _HERE not in out.get("PYTHONPATH", "").split(os.pathsep):
        out["PYTHONPATH"] = os.pathsep.join(filter(None, (_HERE, out.get("PYTHONPATH"))))
    preload = os.path.join(os.path.dirname(_HERE), "hermetic-preload.mjs")
    if os.path.exists(preload):
        from pathlib import Path

        url = Path(preload).as_uri()
        if url not in out.get("NODE_OPTIONS", ""):
            out["NODE_OPTIONS"] = f"{out.get('NODE_OPTIONS', '')} --import={url}".strip()
    return out


def _popen_fields(args, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None,
                  preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None, *rest, **kwargs):
    return args, executable, shell, cwd, env


def _install_spawns():
    import subprocess

    real_init = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        argv, executable, shell, cwd, env = _popen_fields(*args, **kwargs)
        strings = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)
        _inspect_spawn("spawn", strings, executable, cwd, env, shell)
        if env is not None:
            if "env" in kwargs:
                kwargs["env"] = _carry(env)
            else:
                args = args[:10] + (_carry(env),) + args[11:]
        real_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    real_system = os.system

    def system(command):
        _inspect_spawn("os.system", [command], shell=True)
        return real_system(command)

    os.system = system
    for name, has_env in (("execv", False), ("execve", True), ("posix_spawn", True), ("posix_spawnp", True)):
        original = getattr(os, name, None)
        if original is None:
            continue

        def guarded(path, argv, *rest, _name=name, _original=original, _has_env=has_env, **kwargs):
            env = rest[0] if _has_env and rest else kwargs.get("env")
            _inspect_spawn(_name, [path, *argv], env=env)
            if _has_env and rest:
                rest = (_carry(rest[0]),) + rest[1:]
            return _original(path, argv, *rest, **kwargs)

        guarded.__name__ = name
        setattr(os, name, guarded)


def _install_network(offline, ports):
    import socket

    def loopback(host):
        if host in ("localhost", ""):
            return True
        import ipaddress

        try:
            ip = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        return ip.is_loopback or ip.is_unspecified or bool(mapped and mapped.is_loopback)

    def inspect(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None):
            _check("connect", address)
        elif sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            host = address[0].decode() if isinstance(address[0], bytes) else str(address[0])
            if not loopback(host):
                if offline:
                    _violate(f"network connection to {host}:{address[1]}")
            elif address[1] in ports:
                _violate(f"network connection to {host}:{address[1]}, a port another process was already serving")

    for name in ("connect", "connect_ex"):
        original = getattr(socket.socket, name)

        def guarded(self, address, _original=original):
            inspect(self, address)
            return _original(self, address)

        guarded.__name__ = name
        setattr(socket.socket, name, guarded)


def _install_signals(pids):
    for name in ("kill", "killpg"):
        original = getattr(os, name, None)
        if original is None:
            continue

        def guarded(pid, sig, _original=original):
            if sig != 0 and abs(pid) in pids and abs(pid) != os.getpid():
                _violate(f"signal {sig} to pid {pid}, which was running before the tests")
            return _original(pid, sig)

        guarded.__name__ = name
        setattr(os, name, guarded)


def _install_remap():
    from importlib import machinery

    details = [(machinery.ExtensionFileLoader, machinery.EXTENSION_SUFFIXES),
               (machinery.SourceFileLoader, machinery.SOURCE_SUFFIXES),
               (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES)]

    def hook(entry):
        path = os.fsdecode(entry)
        for source, target in _pairs:
            if path == source or path.startswith(source + "/"):
                moved = target + path[len(source):]
                if os.path.isdir(moved):
                    return machinery.FileFinder(moved, *details)
        raise ImportError("not a remapped path")

    sys.path_hooks.insert(0, hook)
    sys.path_importer_cache.clear()


def _report():
    if not _blocked:
        return
    import contextlib

    with contextlib.suppress(Exception):
        sys.stdout.flush()
    lines = "".join(f"  {message}\n" for message in _blocked)
    os.write(2, f"hermetic: {len(_blocked)} blocked operation(s) in pid {os.getpid()}\n{lines}".encode())
    os._exit(3)


def _chain():
    """Run the sitecustomize this one shadows, if the path holds another."""
    import importlib.machinery
    import importlib.util

    here = os.path.realpath(_HERE)
    rest = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec and spec.loader:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def _install():
    global _roots, _pairs
    _roots = _parse_roots(_ENV.get("KIN_PROTECTED_ROOTS", ""))
    _pairs = _parse_remap(_ENV.get("KIN_REMAP", ""))
    if _pairs:
        _install_remap()
    if _roots:
        _install_files()
    if _roots or _pairs:
        _install_spawns()
    offline = _ENV.get("KIN_BLOCK_NETWORK") == "1"
    ports = {int(port) for port in _ENV.get("KIN_BLOCKED_PORTS", "").split(",") if port.strip().isdigit()}
    if offline or ports or _roots:
        _install_network(offline, ports)
    pids = {int(pid) for pid in _ENV.get("KIN_PROTECTED_PIDS", "").split(",") if pid.strip().isdigit()}
    if pids:
        _install_signals(pids)
    if _roots or _pairs or offline or ports or pids:
        import atexit

        atexit.register(_report)


if not getattr(sys, "_kin_hermetic", False):
    sys._kin_hermetic = True
    _install()
_chain()
