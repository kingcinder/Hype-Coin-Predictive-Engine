"""Engine shutdown must stop a Streamlit worker that outlived its launcher."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

from engine.run import _kill_ui_group, _terminate_ui


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _wait_for_port(port: int, *, open_: bool, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _accepts(port) is open_:
            return True
        time.sleep(0.05)
    return _accepts(port) is open_


def test_kill_ui_group_frees_the_port_without_waiting() -> None:
    """The forced-exit path must SIGKILL the UI group even with a dead leader."""
    port = _free_port()
    worker = (
        "import socket,time\n"
        "s=socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen()\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{worker!r}])",
        ],
        start_new_session=True,
    )
    pgid = os.getpgid(leader.pid)
    try:
        leader.wait(timeout=10)
        assert _wait_for_port(port, open_=True), "worker never bound the UI port"

        _kill_ui_group(leader, pgid)

        assert _wait_for_port(port, open_=False), "forced kill left the UI port bound"
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_terminate_ui_kills_orphaned_worker_holding_the_port() -> None:
    """A dead `python -m streamlit` leader must not spare its worker group.

    The old shutdown skipped the group signal when ``ui.poll()`` reported the
    leader gone, leaving the worker bound to the UI port and blocking the next
    ``./start-engine.sh``.
    """
    port = _free_port()
    worker = (
        "import socket,time\n"
        "s=socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {port}))\n"
        "s.listen()\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{worker!r}])",
        ],
        start_new_session=True,
    )
    pgid = os.getpgid(leader.pid)
    try:
        leader.wait(timeout=10)
        assert _wait_for_port(port, open_=True), "worker never bound the UI port"

        _terminate_ui(leader, pgid)

        assert _wait_for_port(port, open_=False), "orphaned worker kept the UI port"
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
