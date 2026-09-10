"""Run a long-lived `mm` process detached from the harness that started it.

On 2026-09-09 the harness killed the supervisor and `mm watch` under memory
pressure, orphaning three live workers; six resumes were needed in that window,
three for that one event, and `mm watch` went the same way twice more the next
day until it was relaunched under `nohup` (issue #80). A harness-tracked
background task is the first thing the harness reclaims. So the supervisor and
the watch can be started *detached*: a double fork with ``setsid`` puts the
process in its own session with no controlling terminal and no parent the
harness tracks, stdio goes to a log under ``.mm/run/``, and the pid is written
where ``mm supervisor status`` and ``mm watch --stop`` can find it.

The parent does not return until the child says it is ready (or has refused),
so ``mm supervisor start`` printing a pid means a supervisor that has already
taken the owner lock and bound its socket.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from middle_manager.paths import run_dir


class DetachError(Exception):
    """The detached process refused or never reported ready; nothing is running."""


def log_path(name: str) -> Path:
    return run_dir() / f"{name}.log"


def pid_path(name: str) -> Path:
    return run_dir() / f"{name}.pid"


def detach(
    name: str,
    body: Callable[[Callable[[str], None]], None],
    *,
    ready_timeout: float = 15.0,
) -> tuple[int, Path]:
    """Fork ``body`` into a detached process; return its pid and log once it reports ready.

    ``body`` receives a ``ready`` callable and must call it once — with ``"ok"``
    when it is serving, or with ``"refused: <why>"`` (then return or raise) when
    it cannot — before doing anything long-lived. The grandchild's stdout and
    stderr are appended to ``<MM_HOME>/run/<name>.log``; its pid is written to
    ``<MM_HOME>/run/<name>.pid``.
    """
    run_dir().mkdir(parents=True, exist_ok=True)
    log = log_path(name)
    read_end, write_end = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        # Child: leave the session the harness tracks, then fork again so the
        # process that serves can never reacquire a controlling terminal.
        os.close(read_end)
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        _redirect_stdio(log)
        owns_pid_file = False
        try:

            def ready(message: str) -> None:
                nonlocal owns_pid_file
                if message.startswith("ok"):
                    # Only a process that is actually serving claims the pid
                    # file; one that was refused must not touch the owner's.
                    pid_path(name).write_text(str(os.getpid()), encoding="utf-8")
                    owns_pid_file = True
                with contextlib.suppress(OSError):
                    os.write(write_end, f"{message}\n".encode())
                    os.close(write_end)

            body(ready)
        except BaseException as exc:  # noqa: BLE001 - the parent reads the reason
            with contextlib.suppress(OSError):
                os.write(write_end, f"refused: {exc}\n".encode())
            _release_pid_file(name, owns_pid_file)
            os._exit(1)
        _release_pid_file(name, owns_pid_file)
        os._exit(0)

    # Parent: reap the intermediate child, then wait for the grandchild's word.
    os.close(write_end)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
    message = _read_line(read_end, ready_timeout)
    os.close(read_end)
    if message is None:
        raise DetachError(f"{name} did not report ready within {ready_timeout:.0f}s; see {log}")
    if not message.startswith("ok"):
        raise DetachError(message)
    started = _read_pid(name)
    if started is None:
        raise DetachError(f"{name} reported ready but wrote no pid file; see {log}")
    return started, log


def _release_pid_file(name: str, owned: bool) -> None:
    if not owned or _read_pid(name) != os.getpid():
        return
    with contextlib.suppress(OSError):
        pid_path(name).unlink()


def _redirect_stdio(log: Path) -> None:
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = os.fdopen(2, "w", buffering=1)


def _read_line(fd: int, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    buffer = b""
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            break
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buffer += chunk
        if b"\n" in buffer:
            break
    line = buffer.split(b"\n", 1)[0].decode(errors="replace").strip()
    return line or None


def _read_pid(name: str) -> int | None:
    try:
        return int(pid_path(name).read_text(encoding="utf-8").strip() or 0) or None
    except (OSError, ValueError):
        return None


def running_pid(name: str) -> int | None:
    """The detached process's pid if its pid file names a live process, else None."""
    pid = _read_pid(name)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def stop(name: str, *, grace: float = 5.0) -> int | None:
    """Ask the detached process to stop; return the pid it had, or None if none ran."""
    pid = running_pid(name)
    if pid is None:
        with contextlib.suppress(OSError):
            pid_path(name).unlink()
        return None
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and running_pid(name) is not None:
        time.sleep(0.05)
    if running_pid(name) is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        pid_path(name).unlink()
    return pid
