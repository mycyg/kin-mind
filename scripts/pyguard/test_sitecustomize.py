"""The guard refuses what reaches a protected root, remaps imports, and leaves the rest alone."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent
# Runs inside a child that has the guard; every path comes from HERMETIC_ROOT. The service
# manager's name is assembled at run time: the parent refuses to start a child that names it.
PROBE = r"""
import json, os, shutil, signal, socket, sqlite3, subprocess, sys
root = os.environ['HERMETIC_ROOT']; guarded = root + '/protected'; free = root + '/free'; out = {}
def attempt(name, work):
    try:
        out[name] = work() or 'ok'
    except Exception as error:
        out[name] = 'blocked' if str(error).startswith('hermetic: blocked') else f'error: {error!r}'
attempt('write', lambda: open(guarded + '/a', 'w'))
attempt('throughSymlink', lambda: open(free + '/link/b', 'w'))
attempt('replace', lambda: (open(free + '/c', 'w').close(), os.replace(free + '/c', guarded + '/c')))
attempt('makedirs', lambda: os.makedirs(guarded + '/d/e'))
attempt('rmtree', lambda: shutil.rmtree(guarded))
attempt('sqliteReadOnly', lambda: sqlite3.connect(f'file:{guarded}/memory.sqlite3?mode=ro', uri=True))
attempt('read', lambda: open(guarded + '/memory.sqlite3', 'rb').close())
attempt('temp', lambda: open(free + '/g', 'w').close())
attempt('sqliteTemp', lambda: sqlite3.connect(free + '/ok.sqlite3').close())
attempt('spawnEnv', lambda: subprocess.run(['/usr/bin/true'], env={**os.environ, 'EXTRA': guarded}))
attempt('spawnFree', lambda: str(subprocess.run(['/usr/bin/true']).returncode))
attempt('serviceManager', lambda: subprocess.run(['launch' + 'ctl', 'list']))
def grandchild():
    run = subprocess.run([sys.executable, '-c', "import os; open(os.environ['HERMETIC_ROOT'] + '/protected/h', 'w')"],
                         env={'PATH': os.environ['PATH'], 'HERMETIC_ROOT': root}, capture_output=True, text=True)
    return 'blocked' if run.returncode and 'hermetic: blocked' in run.stderr else 'unguarded'
attempt('grandchild', grandchild)
def remap():
    sys.path.insert(0, root + '/prod')
    import where
    return where.WHERE
attempt('remap', remap)
attempt('network', lambda: socket.create_connection(('192.0.2.1', 9), timeout=1))
attempt('busyPort', lambda: socket.create_connection(('127.0.0.1', int(os.environ['BUSY_PORT'])), timeout=1).close())
def loopback():
    server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen()
    socket.create_connection(server.getsockname(), timeout=1).close(); server.close()
attempt('loopback', loopback)
attempt('signal', lambda: os.kill(int(os.environ['PARENT_PID']), signal.SIGCONT))
attempt('probe', lambda: os.kill(int(os.environ['PARENT_PID']), 0))
print(json.dumps(out))
"""


def test_guard_blocks_only_protected_roots(tmp_path):
    root = tmp_path.resolve()
    for name in ("protected", "free", "prod", "dev"):
        (root / name).mkdir()
    (root / "free" / "link").symlink_to(root / "protected")
    for where in ("prod", "dev"):
        (root / where / "where.py").write_text(f"WHERE = {where!r}\n")
    sqlite3.connect(root / "protected" / "memory.sqlite3").close()
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(GUARD), "PYTHONDONTWRITEBYTECODE": "1",
           "HERMETIC_ROOT": str(root), "KIN_PROTECTED_ROOTS": str(root / "protected"),
           "KIN_REMAP": f"{root / 'prod'}={root / 'dev'}", "KIN_BLOCK_NETWORK": "1",
           "KIN_BLOCKED_PORTS": str(busy.getsockname()[1]), "BUSY_PORT": str(busy.getsockname()[1]),
           "KIN_PROTECTED_PIDS": str(os.getpid()), "PARENT_PID": str(os.getpid())}
    try:
        run = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, check=False)
    finally:
        busy.close()
    assert json.loads(run.stdout) == {
        "write": "blocked", "throughSymlink": "blocked", "replace": "blocked", "makedirs": "blocked",
        "rmtree": "blocked", "sqliteReadOnly": "blocked", "read": "ok", "temp": "ok", "sqliteTemp": "ok",
        "spawnEnv": "blocked", "spawnFree": "0", "serviceManager": "blocked", "grandchild": "blocked",
        "remap": "dev", "network": "blocked", "busyPort": "blocked", "loopback": "ok", "signal": "blocked",
        "probe": "ok"}, run.stderr
    assert run.returncode == 3, "caught violations still fail the process"
    assert "hermetic: 11 blocked operation(s)" in run.stderr
    assert sorted(os.listdir(root / "protected")) == ["memory.sqlite3"]

