"""`mm review show` puts the worker's own report, in full, above the diff."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from middle_manager import cli, progress
from middle_manager.state import init_db, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_HOME", str(tmp_path / ".mm"))
    return init_db()


def test_review_show_prints_the_full_latest_report_first(home, monkeypatch, capsys) -> None:
    conn = home
    run_id = store.create_run(conn, "ship it")
    task_id = store.add_task(conn, run_id=run_id, title="build")
    long_note = "Verification:\n" + "\n".join(f"- check {i}: PASS" for i in range(40))
    progress.record(task_id, phase="implement", note="halfway", conn=conn)
    progress.record(task_id, phase="done", note=long_note, conn=conn)

    import middle_manager.review as review_mod

    monkeypatch.setattr(
        review_mod,
        "build_bundle",
        lambda tid: SimpleNamespace(
            task_id=tid, base_sha="a" * 40, head_sha="b" * 40, files_changed=2, diffstat="2 files"
        ),
    )
    assert cli.main(["review", "show", str(task_id)]) == 0
    out = capsys.readouterr().out
    assert "worker report (done, 2 report(s)" in out, out
    assert out.index("worker report (done, 2 report(s)") < out.index("2 files")
    assert "- check 39: PASS" in out  # the whole note, not a truncated line
    assert f"mm progress {task_id}" in out
