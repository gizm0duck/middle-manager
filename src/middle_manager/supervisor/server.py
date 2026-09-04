"""Unix-domain-socket transport for the supervisor.

A thin request/response server around :class:`Supervisor`. Each connection is
handled in its own thread so a blocking ``wait_actionable`` never stalls other
control requests. Startable in a background thread (tests) or blocking (CLI).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import tempfile
import threading
import time

from middle_manager.paths import ensure_layout, mm_home, run_dir
from middle_manager.supervisor.core import Supervisor, SupervisorError
from middle_manager.supervisor.protocol import encode, read_request


def default_socket_path() -> str:
    # AF_UNIX paths are limited to ~104 bytes on macOS, so a deep .mm path under a
    # temp dir overflows. Derive a short, stable path from the .mm home instead.
    key = hashlib.sha1(str(mm_home().resolve()).encode()).hexdigest()[:10]
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    return os.path.join(base, f"mm-{key}.sock")


class SupervisorServer:
    def __init__(self, socket_path: str | None = None) -> None:
        ensure_layout()
        self.socket_path = socket_path or default_socket_path()
        self.supervisor = Supervisor()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_assessment_tick = 0.0
        self._last_health_tick = 0.0
        self._quiet_flagged: set[int] = set()
        self._plan_flagged: set[int] = set()

    def start_background(self) -> None:
        self._bind()
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._bind()
        self._serve_loop()

    def _bind(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        # Persist the listening pid for `mm supervisor status` / reconcile.
        with open(run_dir() / "supervisor.pid", "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            self._tick_assessments()
            self._tick_health()
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        self._cleanup()

    def _tick_assessments(self) -> None:
        """Prepare due reviews while the supervisor is alive, without a model call."""
        now = time.monotonic()
        if now - self._last_assessment_tick < 60:
            return
        self._last_assessment_tick = now
        try:
            from middle_manager.assessments import ensure_due

            ensure_due()
        except Exception:  # noqa: BLE001 - maintenance must never take down dispatch
            return

    def _tick_health(self) -> None:
        """Poll in-flight workers; flag the ones we haven't heard from in a while."""
        now = time.monotonic()
        if now - self._last_health_tick < 60:
            return
        self._last_health_tick = now
        try:
            from middle_manager import health
            from middle_manager.state import init_db

            conn = init_db()
            health.flag_quiet_workers(
                conn,
                quiet_after=health.quiet_threshold(),
                already_flagged=self._quiet_flagged,
            )
            health.flag_missing_plans(
                conn, grace=health.plan_grace(), already_flagged=self._plan_flagged
            )
        except Exception:  # noqa: BLE001 - maintenance must never take down dispatch
            return

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r") as rf:
            try:
                request = read_request(rf)
            except (ValueError, OSError):
                conn.sendall(encode({"ok": False, "error": "bad request"}))
                return
            if not request:
                return
            response = self._dispatch(request)
            with contextlib.suppress(OSError):
                conn.sendall(encode(response))

    def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        sup = self.supervisor
        try:
            if cmd == "ping":
                return {"ok": True, "pid": os.getpid()}
            if cmd == "dispatch_task":
                return {
                    "ok": True,
                    **sup.dispatch_task(
                        repo=request["repo"],
                        title=request["title"],
                        instructions=request.get("instructions", ""),
                        provider=request.get("provider", "fake"),
                        run_id=request.get("run_id"),
                        model=request.get("model"),
                        reasoning=request.get("reasoning"),
                        base=request.get("base"),
                        stack_on=request.get("stack_on"),
                    ),
                }
            if cmd == "task_status":
                return {"ok": True, "task": sup.task_status(int(request["task_id"]))}
            if cmd == "run_status":
                return {"ok": True, **sup.run_status(int(request["run_id"]))}
            if cmd == "wait_actionable":
                return {
                    "ok": True,
                    **sup.wait_actionable(
                        int(request["run_id"]),
                        float(request.get("timeout", 30.0)),
                        int(request.get("after_seq", 0) or 0),
                    ),
                }
            if cmd == "resume_task":
                return {
                    "ok": True,
                    **sup.resume_task(int(request["task_id"]), request.get("message")),
                }
            if cmd == "steer_task":
                return {"ok": True, **sup.steer_task(int(request["task_id"]), request["message"])}
            if cmd == "answer_question":
                return {
                    "ok": True,
                    **sup.answer_question(
                        int(request["task_id"]),
                        request["answer"],
                        scope=request.get("scope", "run"),
                        rationale=request.get("rationale"),
                    ),
                }
            if cmd == "reconcile":
                return {"ok": True, **sup.reconcile()}
            if cmd == "shutdown":
                sup.shutdown()
                self._stop.set()
                return {"ok": True, "shutdown": True}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except (SupervisorError, KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        for path in (self.socket_path, str(run_dir() / "supervisor.pid")):
            if os.path.exists(path):
                with contextlib.suppress(OSError):
                    os.unlink(path)
