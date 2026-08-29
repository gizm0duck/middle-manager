"""The ``mm`` control-plane CLI.

Deterministic policy enforcement lives here and in the modules it calls; the
manager agent never enforces authority or ceilings itself. Subcommands are added
as milestones land. M1 provides: setup, config, doctor, repo, status, version.
"""

from __future__ import annotations

import argparse
import json
import sys

from middle_manager import __version__


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"middle-manager {__version__}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from middle_manager.setup.doctor import run_doctor

    print(run_doctor(as_json=args.json))
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from middle_manager.config import ConfigError
    from middle_manager.setup.wizard import run_setup

    overrides = _collect_overrides(args)
    try:
        cfg = run_setup(non_interactive=args.non_interactive, overrides=overrides)
    except ConfigError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"configured manager {cfg.manager.provider}/{cfg.manager.model}"
        f"@{cfg.manager.reasoning}; worker ceiling {cfg.worker.provider}/"
        f"{cfg.worker.max_model}@{cfg.worker.max_reasoning}; posture "
        f"{cfg.cost_posture}"
    )
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from middle_manager.config import ConfigError, load_config
    from middle_manager.setup.wizard import config_authority, config_models

    try:
        if args.config_cmd == "show":
            cfg = load_config()
            print(json.dumps(cfg.to_dict(), indent=2))
            return 0
        if args.config_cmd == "models":
            cfg = config_models(_collect_overrides(args))
            print(
                f"updated models: manager {cfg.manager.model}, "
                f"worker ceiling {cfg.worker.max_model}"
            )
            return 0
        if args.config_cmd == "authority":
            overrides: dict = {}
            if args.allow_merge is not None:
                overrides["merge"] = args.allow_merge
            cfg = config_authority(overrides)
            print(f"merge authority: {'on' if cfg.authority.merge else 'off'}")
            return 0
    except ConfigError as exc:
        print(f"config failed: {exc}", file=sys.stderr)
        return 1
    print("no config subcommand given", file=sys.stderr)
    return 2


def _cmd_repo(args: argparse.Namespace) -> int:
    from middle_manager.repos import RepoError, add_repo, list_repos, sync_repo

    try:
        if args.repo_cmd == "add":
            added = add_repo(args.url, name=args.name)
            short_sha = added.base_sha[:8] if added.base_sha else "?"
            print(
                f"registered {added.name} from {added.origin} "
                f"(branch {added.default_branch}, base {short_sha})"
            )
            return 0
        if args.repo_cmd == "list":
            repos = list_repos()
            if not repos:
                print("no repositories registered")
                return 0
            for r in repos:
                sha = (r.get("base_sha") or "?")[:8]
                print(f"{r['name']:<24} {r['default_branch'] or '?':<12} {sha}  {r['origin']}")
            return 0
        if args.repo_cmd == "sync":
            sha = sync_repo(args.name)
            print(f"synced {args.name} to {sha[:8]}")
            return 0
    except RepoError as exc:
        print(f"repo error: {exc}", file=sys.stderr)
        return 1
    print("no repo subcommand given", file=sys.stderr)
    return 2


def _cmd_status(args: argparse.Namespace) -> int:
    from middle_manager.paths import config_path, mm_home
    from middle_manager.repos import list_repos

    print(f"mm home: {mm_home()}")
    print(f"config:  {'present' if config_path().exists() else 'missing (run mm setup)'}")
    try:
        repos = list_repos()
        print(f"repos:   {len(repos)} registered")
    except Exception:  # noqa: BLE001 - status must never crash
        print("repos:   (state not initialized)")
    return 0


def _collect_overrides(args: argparse.Namespace) -> dict:
    keys = (
        "manager_provider",
        "manager_model",
        "manager_reasoning",
        "worker_provider",
        "worker_max_model",
        "worker_max_reasoning",
        "cost_posture",
    )
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def _add_profile_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manager-provider", dest="manager_provider")
    parser.add_argument("--manager-model", dest="manager_model")
    parser.add_argument("--manager-reasoning", dest="manager_reasoning")
    parser.add_argument("--worker-provider", dest="worker_provider")
    parser.add_argument("--worker-max-model", dest="worker_max_model")
    parser.add_argument("--worker-max-reasoning", dest="worker_max_reasoning")
    parser.add_argument("--cost-posture", dest="cost_posture")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mm", description="Middle Manager control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print version").set_defaults(func=_cmd_version)

    doctor = sub.add_parser("doctor", help="environment and config diagnostics")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=_cmd_doctor)

    setup = sub.add_parser("setup", help="configure manager profile and worker ceiling")
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="write config from flags/defaults without prompting",
    )
    _add_profile_flags(setup)
    setup.set_defaults(func=_cmd_setup)

    config = sub.add_parser("config", help="inspect or change configuration")
    csub = config.add_subparsers(dest="config_cmd", required=True)
    csub.add_parser("show", help="print the current config")
    models = csub.add_parser("models", help="rerun discovery and change model profiles")
    _add_profile_flags(models)
    authority = csub.add_parser("authority", help="change standing authority")
    authority.add_argument(
        "--allow-merge",
        dest="allow_merge",
        action="store_true",
        default=None,
        help="grant standing merge authority (off by default)",
    )
    authority.add_argument("--no-allow-merge", dest="allow_merge", action="store_false")
    config.set_defaults(func=_cmd_config)

    repo = sub.add_parser("repo", help="register and sync repositories")
    rsub = repo.add_subparsers(dest="repo_cmd", required=True)
    add = rsub.add_parser("add", help="register a repository")
    add.add_argument("url", help="repository URL or local path")
    add.add_argument("--name", default=None, help="override the derived name")
    rsub.add_parser("list", help="list registered repositories")
    sync = rsub.add_parser("sync", help="fetch and update a repository base")
    sync.add_argument("name", help="registered repository name")
    repo.set_defaults(func=_cmd_repo)

    status = sub.add_parser("status", help="show a compact runtime summary")
    status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
