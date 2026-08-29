"""Smoke test for the bundled `lavish-review` script.

Uses a fake `lavish-axi` on PATH (whose `poll` writes a line then sleeps) to prove
the script backgrounds the poll, drains the sidecar non-blockingly, and stops the
poller — without a real tool and without ever blocking the caller.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "review-surfaces"
    / "scripts"
    / "lavish-review"
)

FAKE_LAVISH = """#!/usr/bin/env bash
if [ "$1" = "poll" ]; then
  echo "annotation: tighten the header"
  sleep 30
fi
exit 0
"""


@pytest.fixture
def env_with_fake_lavish(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "lavish-axi"
    fake.write_text(FAKE_LAVISH)
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return env


def _run(args, env, cwd):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_open_backgrounds_poll_and_drain_reads_it(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "map.html"
    surface.write_text("<html></html>")
    env, cwd = env_with_fake_lavish, str(tmp_path)

    try:
        opened = _run(["open", str(surface)], env, cwd)
        # open returns immediately (well under the 10s timeout) and hands the turn back.
        assert opened.returncode == 0, opened.stderr
        assert "background" in opened.stdout
        assert "hand the turn back" in opened.stdout

        # Poller writes asynchronously; give it a beat, then drain non-blockingly.
        annotation_seen = False
        for _ in range(20):
            drained = _run(["drain", str(surface)], env, cwd)
            if "annotation: tighten the header" in drained.stdout:
                annotation_seen = True
                break
            time.sleep(0.1)
        assert annotation_seen, "drain never surfaced the backgrounded feedback"

        stopped = _run(["stop", str(surface)], env, cwd)
        assert "poller stopped" in stopped.stdout
    finally:
        pidf = surface.with_name(f".{surface.name}.pid")
        if pidf.exists():
            with contextlib.suppress(ValueError, ProcessLookupError, OSError):
                os.kill(int(pidf.read_text().strip()), 15)


def test_drain_before_open_is_empty_and_nonblocking(tmp_path, env_with_fake_lavish):
    surface = tmp_path / "plan.html"
    drained = _run(["drain", str(surface)], env_with_fake_lavish, str(tmp_path))
    assert drained.returncode == 0
    assert "no feedback yet" in drained.stdout


def test_missing_tool_points_at_local_surface(tmp_path):
    surface = tmp_path / "plan.html"
    surface.write_text("x")
    # Minimal PATH with no lavish-axi.
    env = dict(os.environ, PATH="/usr/bin:/bin")
    result = _run(["open", str(surface)], env, str(tmp_path))
    assert result.returncode == 1
    assert "mm artifact" in result.stderr
