"""`mm doctor` — read-only diagnostics of the environment and config."""

from __future__ import annotations

import json

from middle_manager.config import ConfigError, load_config
from middle_manager.paths import config_path, db_path, mm_home
from middle_manager.setup.discovery import discover, usable_harnesses


def collect() -> dict:
    report = discover()
    home = mm_home()
    cfg_status: dict = {"path": str(config_path()), "present": config_path().exists()}
    if config_path().exists():
        try:
            cfg = load_config()
            cfg_status["valid"] = True
            cfg_status["manager"] = f"{cfg.manager.provider}/{cfg.manager.model}"
            cfg_status["worker_ceiling"] = f"{cfg.worker.provider}/{cfg.worker.max_model}"
        except ConfigError as exc:
            cfg_status["valid"] = False
            cfg_status["error"] = str(exc)
    return {
        "mm_home": str(home),
        "config": cfg_status,
        "state_db": {"path": str(db_path()), "present": db_path().exists()},
        "environment": report,
        "usable_harnesses": usable_harnesses(report),
    }


def render_text(data: dict) -> str:
    lines: list[str] = []
    lines.append(f"mm home:   {data['mm_home']}")
    cfg = data["config"]
    if cfg["present"]:
        state = "valid" if cfg.get("valid") else f"INVALID ({cfg.get('error')})"
        extra = ""
        if cfg.get("valid"):
            extra = f" — manager {cfg['manager']}, worker ceiling {cfg['worker_ceiling']}"
        lines.append(f"config:    {cfg['path']} [{state}]{extra}")
    else:
        lines.append(f"config:    {cfg['path']} [missing — run `mm setup`]")
    lines.append("")
    lines.append("Harnesses:")
    for h in data["environment"]["harnesses"]:
        mark = "ready" if h["available"] else "unavailable"
        v = h.get("version") or "?"
        detail = f" — {h['detail']}" if h.get("detail") else ""
        lines.append(f"  {h['name']:<8} [{mark}] {v}{detail}")
    lines.append("")
    lines.append("Requirements:")
    for r in data["environment"]["requirements"]:
        mark = "ok" if r["available"] else "MISSING"
        v = r.get("version") or ""
        detail = f" — {r['detail']}" if not r["available"] and r.get("detail") else ""
        lines.append(f"  {r['name']:<8} [{mark}] {v}{detail}")
    lines.append("")
    lines.append("Companion tools:")
    for c in data["environment"]["companions"]:
        mark = "present" if c["available"] else "not provisioned"
        lines.append(f"  {c['name']:<12} [{mark}]")
    lines.append("")
    usable = data["usable_harnesses"]
    lines.append(f"Usable harnesses: {', '.join(usable) if usable else 'none'}")
    return "\n".join(lines)


def run_doctor(as_json: bool = False) -> str:
    data = collect()
    return json.dumps(data, indent=2) if as_json else render_text(data)
