"""The fake worker subprocess.

Emits a normalized JSONL event stream on stdout and performs a real commit in its
leased worktree, so the runner/spool/supervisor path is exercised end to end
without any model. Behavior is driven by instruction markers:

- ``ASK:<question>`` -> emit a blocked/question result and stop (no commit).
- ``FAIL`` -> emit no result event and exit non-zero (failure path).
- otherwise -> write a file, commit, and emit a completed result with the head SHA.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec)

    session_id = spec.get("resume_session_id") or uuid.uuid4().hex
    _emit({"type": "session", "session_id": session_id})
    _emit({"type": "assistant", "session_id": session_id, "text": f"starting {spec['title']}"})

    instructions = spec.get("instructions", "")
    worktree = spec["worktree_path"]

    if "FAIL" in instructions:
        _emit({"type": "error", "session_id": session_id, "text": "instructed to fail"})
        return 3

    if instructions.startswith("ASK:"):
        question = instructions[len("ASK:") :].strip() or "need direction"
        _emit(
            {
                "type": "result",
                "session_id": session_id,
                "status": "blocked",
                "question": question,
                "summary": "worker is blocked on a question",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }
        )
        return 0

    # Normal path: make a real change and commit it in the leased worktree.
    steer = spec.get("steer_message")
    fname = f"mm-fake-{spec['task_id']}.txt"
    with open(f"{worktree}/{fname}", "a", encoding="utf-8") as fh:
        fh.write(f"work for task {spec['task_id']}: {spec['title']}\n")
        if steer:
            fh.write(f"steer: {steer}\n")
    _git(["add", "."], cwd=worktree)
    _git(
        [
            "-c",
            "user.name=MM Fake",
            "-c",
            "user.email=fake@mm.local",
            "commit",
            "-qm",
            f"task {spec['task_id']}: {spec['title']}",
        ],
        cwd=worktree,
    )
    head = _git(["rev-parse", "HEAD"], cwd=worktree)
    _emit({"type": "progress", "session_id": session_id, "text": "committed change"})
    time.sleep(0.05)
    _emit(
        {
            "type": "result",
            "session_id": session_id,
            "status": "completed",
            "summary": f"implemented {spec['title']}",
            "head_sha": head,
            "usage": {"input_tokens": 250, "output_tokens": 40},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
