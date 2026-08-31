"""The launcher addresses one instance regardless of the caller's working directory."""

from __future__ import annotations

from pathlib import Path

from middle_manager import paths

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_pins_mm_home_to_its_own_checkout() -> None:
    launcher = (ROOT / "bin" / "mm").read_text()
    # The export must derive from the launcher's ROOT and still defer to an explicit
    # MM_HOME, so tests and multi-instance setups keep working.
    assert 'export MM_HOME="${MM_HOME:-$ROOT/.mm}"' in launcher


def test_paths_honor_mm_home_over_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MM_HOME", raising=False)
    assert paths.mm_home() == tmp_path / ".mm"

    pinned = tmp_path / "elsewhere" / ".mm"
    monkeypatch.setenv("MM_HOME", str(pinned))
    assert paths.mm_home() == pinned
    assert paths.db_path() == pinned / "state.db"
