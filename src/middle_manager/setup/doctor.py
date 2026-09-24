"""`mm doctor` — read-only diagnostics of the environment and config."""

from __future__ import annotations

import json
import re

from middle_manager.config import ConfigError, load_config
from middle_manager.paths import config_path, db_path, mm_home
from middle_manager.providers.capability import LOCAL, record_source, recorded_version
from middle_manager.setup.discovery import discover, usable_harnesses

_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def _installed_version(version_str: str | None) -> str | None:
    if not version_str:
        return None
    m = _SEMVER.search(version_str)
    return m.group(0) if m else None


def _capability_drift(report: dict) -> list[dict]:
    """Compare each provider's recorded probe version with the installed CLI.

    A mismatch means the capability record no longer applies and the provider
    should be re-probed (`python -m middle_manager.probes run`) before trusting
    interrupt/resume steering. ``source`` names which record answered so the
    reader knows whether a local probe or the tracked file is in play.
    """
    out: list[dict] = []
    for h in report["harnesses"]:
        provider = h["name"]
        recorded = recorded_version(provider)
        if recorded is None:
            continue
        installed = _installed_version(h.get("version"))
        drift = installed is not None and installed != recorded
        out.append(
            {
                "provider": provider,
                "recorded": recorded,
                "installed": installed,
                "drift": drift,
                "source": record_source(provider),
            }
        )
    return out


def _schema_status() -> dict:
    from middle_manager.state.db import SCHEMA_VERSION

    status: dict = {
        "path": str(db_path()),
        "present": db_path().exists(),
        "expected": SCHEMA_VERSION,
    }
    if db_path().exists():
        try:
            from middle_manager.state import init_db
            from middle_manager.state.db import schema_version

            conn = init_db()
            status["version"] = schema_version(conn)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
            status["error"] = str(exc)
    return status


def _repo_forges() -> list[dict]:
    """Every registered repo and whether delivery has somewhere to open a PR.

    A repo registered from a local path before forge URLs existed has none: the
    worker pushes, and ``mm deliver`` has no forge to open the pull request
    against — which is exactly the hand-push that cost a delivery on 2026-09-04.
    """
    try:
        from middle_manager.repos import list_repos

        return [
            {
                "name": r["name"],
                "forge_url": r.get("forge_url"),
                "origin": r.get("origin"),
                "stale_heads": _stale_heads(r),
            }
            for r in list_repos()
        ]
    except Exception:  # noqa: BLE001 - diagnostics must not crash
        return []


def _stale_heads(repo: dict) -> dict:
    """Remote HEADs in a base clone that name a branch other than the recorded default.

    Read locally, never from the network. ``gh stack link`` without ``--base`` reads
    ``origin/HEAD``; on 2026-09-23 it named a stale feature branch.
    """
    from middle_manager.repos import FORGE_REMOTE, remote_head

    default = repo.get("default_branch")
    path = repo.get("local_path")
    if not default or not path:
        return {}
    stale = {}
    for remote in ("origin", FORGE_REMOTE):
        head = remote_head(path, remote)
        if head and head != default:
            stale[remote] = head
    return stale


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
        "state_db": _schema_status(),
        "environment": report,
        "usable_harnesses": usable_harnesses(report),
        "capability_drift": _capability_drift(report),
        "repos": _repo_forges(),
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
    drift = data.get("capability_drift", [])
    if drift:
        lines.append("")
        lines.append("Provider capability matrix:")
        for d in drift:
            installed = d["installed"] or "?"
            origin = "local probe" if d.get("source") == LOCAL else "tracked record"
            if d["drift"]:
                lines.append(
                    f"  {d['provider']:<8} DRIFT — {origin} says {d['recorded']}, "
                    f"installed {installed}; re-probe before trusting steering"
                )
            else:
                lines.append(f"  {d['provider']:<8} ok — {origin}, probed at {d['recorded']}")
    repos = data.get("repos", [])
    if repos:
        lines.append("")
        lines.append("Registered repositories:")
        for r in repos:
            if r.get("forge_url"):
                lines.append(f"  {r['name']:<24} forge {r['forge_url']}")
            else:
                lines.append(
                    f"  {r['name']:<24} NO FORGE — deliver cannot open pull requests; "
                    "re-register it with `mm repo add <path> --forge-url <url>`"
                )
            for remote, head in (r.get("stale_heads") or {}).items():
                lines.append(
                    f"  {r['name']:<24} {remote}/HEAD names {head}, not the default branch "
                    f"on record; run `mm repo sync {r['name']}`"
                )
    db = data["state_db"]
    if db.get("present") and db.get("version") is not None:
        mark = "ok" if db.get("version") == db.get("expected") else "MISMATCH"
        lines.append("")
        lines.append(f"State DB schema: v{db['version']} (expected v{db['expected']}) [{mark}]")
    lines.append("")
    usable = data["usable_harnesses"]
    lines.append(f"Usable harnesses: {', '.join(usable) if usable else 'none'}")
    return "\n".join(lines)


def run_doctor(as_json: bool = False) -> str:
    data = collect()
    return json.dumps(data, indent=2) if as_json else render_text(data)
