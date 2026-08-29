"""The ``mm`` control-plane CLI.

Deterministic policy enforcement lives here and in the modules it calls; the
manager agent never enforces authority or ceilings itself. Subcommands are added
as milestones land. M1 provides: setup, config, doctor, repo, status, version.
"""

from __future__ import annotations

import argparse
import json
import os
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


def _cmd_supervisor(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable
    from middle_manager.supervisor.server import SupervisorServer, default_socket_path

    if args.supervisor_cmd == "serve":
        server = SupervisorServer()
        print(f"supervisor listening on {server.socket_path} (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
            print("supervisor stopped")
        return 0

    client = SupervisorClient()
    try:
        if args.supervisor_cmd == "status":
            resp = client.ping()
            print(f"supervisor up (pid {resp.get('pid')}) at {default_socket_path()}")
            return 0
        if args.supervisor_cmd == "stop":
            client.shutdown()
            print("supervisor shutdown requested")
            return 0
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.dispatch_task(
            repo=args.repo,
            title=args.title,
            instructions=args.instructions or "",
            provider=args.provider,
            model=args.model,
            reasoning=args.reasoning,
            run_id=args.run_id,
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"dispatch failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"dispatched task {resp['task_id']} in run {resp['run_id']} (branch {resp['branch']})")
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.wait_actionable(args.run_id, timeout=args.timeout)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if resp.get("timed_out"):
        print(f"run {args.run_id}: no actionable state within {args.timeout}s")
        return 0
    print(json.dumps(resp, indent=2))
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.resume_task(args.task_id, message=args.message)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"resume failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"resuming task {args.task_id} (session {resp.get('resumed_session')})")
    return 0


def _cmd_steer(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.steer_task(args.task_id, args.message)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"steer failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"steer task {args.task_id}: mode={resp.get('mode')} {resp.get('note', '')}")
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.answer_question(
            args.task_id, args.answer, scope=args.scope, rationale=args.rationale
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"answer failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(
        f"recorded decision {resp.get('decision_id')} ({args.scope}); resuming task {args.task_id}"
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    from middle_manager.review import ReviewError, build_bundle, is_approved_at_head, record_review

    try:
        if args.review_cmd == "show":
            bundle = build_bundle(args.task_id)
            print(
                f"task {bundle.task_id}: {bundle.base_sha[:8]}..{bundle.head_sha[:8]} "
                f"({bundle.files_changed} file(s))"
            )
            print(bundle.diffstat or "(no changes)")
            return 0
        if args.review_cmd == "approve":
            res = record_review(args.task_id, "approved", args.findings or "")
            print(f"approved task {args.task_id} at {res['head_sha'][:8]}")
            return 0
        if args.review_cmd == "request-changes":
            res = record_review(args.task_id, "changes_requested", args.findings or "")
            print(f"requested changes on task {args.task_id} at {res['head_sha'][:8]}")
            return 0
        if args.review_cmd == "status":
            approved, reason = is_approved_at_head(args.task_id)
            print(f"task {args.task_id}: {'APPROVED' if approved else 'NOT approved'} — {reason}")
            return 0 if approved else 1
    except ReviewError as exc:
        print(f"review error: {exc}", file=sys.stderr)
        return 1
    print("no review subcommand given", file=sys.stderr)
    return 2


def _cmd_deliver(args: argparse.Namespace) -> int:
    from middle_manager.delivery import DeliveryError, deliver

    try:
        res = deliver(
            args.task_id,
            push=not args.no_push,
            open_pr=not args.no_pr,
            remote=args.remote,
            base=args.base,
        )
    except DeliveryError as exc:
        print(f"delivery refused: {exc}", file=sys.stderr)
        return 1
    print(f"task {res.task_id}: {res.note}")
    if res.pr_url:
        print(f"PR: {res.pr_url}")
    return 0


def _cmd_artifact(args: argparse.Namespace) -> int:
    import json as _json

    from middle_manager.artifacts import ArtifactError, create_artifact

    try:
        sections = _json.loads(args.sections) if args.sections else []
        art = create_artifact(args.run_id, args.title, sections)
    except (ArtifactError, ValueError) as exc:
        print(f"artifact error: {exc}", file=sys.stderr)
        return 1
    print(f"artifact {art.id} written to {art.path}")
    print(f"feedback sidecar: {art.feedback_path}")
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    from middle_manager.artifacts import ArtifactError, read_feedback

    try:
        result = read_feedback(args.artifact_id)
    except ArtifactError as exc:
        print(f"feedback error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    from middle_manager.hooks import handle_hook_stdin

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    result = handle_hook_stdin(args.event, raw)
    print(json.dumps(result))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    from middle_manager.crossrepo import DependencyError, rollout_plan

    try:
        plan = rollout_plan(args.run_id)
    except DependencyError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 1
    if plan.missing:
        for task, dep in plan.missing:
            print(f"task {task} depends on {dep}, which is not in this run", file=sys.stderr)
    if plan.cycles:
        for cycle in plan.cycles:
            print(f"dependency cycle among tasks: {cycle}", file=sys.stderr)
    print("rollout order: " + " -> ".join(str(t) for t in plan.order))
    return 0 if plan.ok else 1


def _cmd_decision(args: argparse.Namespace) -> int:
    from middle_manager import decisions
    from middle_manager.state import init_db

    conn = init_db()
    if args.decision_cmd == "list":
        rows = decisions.list_decisions(conn, include_inactive=args.all)
        if not rows:
            print("no decisions recorded")
            return 0
        for d in rows:
            flags = []
            if d["superseded_by"]:
                flags.append(f"superseded->{d['superseded_by']}")
            if d["invalidated"]:
                flags.append("invalidated")
            tag = f" [{', '.join(flags)}]" if flags else ""
            print(f"{d['id']:>3} ({d['scope']}) {d['question']!r} -> {d['answer']!r}{tag}")
        return 0
    if args.decision_cmd == "invalidate":
        decisions.invalidate(conn, args.id)
        print(f"invalidated decision {args.id}")
        return 0
    if args.decision_cmd == "forget":
        decisions.forget(conn, args.id)
        print(f"forgot decision {args.id}")
        return 0
    print("no decision subcommand given", file=sys.stderr)
    return 2


def _cmd_usage(args: argparse.Namespace) -> int:
    from middle_manager.state import init_db, store

    conn = init_db()
    if store.get_run(conn, args.run_id) is None:
        print(f"run {args.run_id} not found", file=sys.stderr)
        return 1
    rows = store.usage_by_profile(conn, args.run_id)
    totals = store.usage_totals(conn, args.run_id)
    if not rows:
        print(f"run {args.run_id}: no usage recorded")
        return 0
    print(f"run {args.run_id} usage by profile:")
    for r in rows:
        model = r["model"] or "(default)"
        reasoning = r["reasoning"] or "-"
        print(
            f"  {r['provider']}/{model}@{reasoning}: "
            f"{r['input_tokens']} in / {r['output_tokens']} out ({r['calls']} call(s))"
        )
    print(f"total: {totals['input_tokens']} in / {totals['output_tokens']} out")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.reconcile()
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    findings = resp.get("reconciled", [])
    if not findings:
        print("nothing to reconcile")
    else:
        for f in findings:
            print(f"task {f['task_id']}: {f['action']}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from middle_manager.config import ConfigError, load_config
    from middle_manager.manager import ManagerLaunchError, build_launch, start

    try:
        config = load_config()
    except ConfigError:
        config = None

    provider = args.provider
    if provider is None and config is None:
        # Unconfigured and no override: pick a detected, authenticated harness so
        # the manager can launch and walk the user through setup conversationally.
        from middle_manager.setup.discovery import discover, usable_harnesses

        usable = usable_harnesses(discover())
        if not usable:
            print(
                "no authenticated Claude/Codex harness detected; install one and "
                "sign in, then run `mm start` (see `mm doctor`)",
                file=sys.stderr,
            )
            return 1
        provider = usable[0]

    try:
        launch = build_launch(
            config=config,
            provider=provider,
            model=args.model,
            reasoning=args.reasoning,
            objective=args.objective,
        )
    except ManagerLaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"provider: {launch.provider}")
        print(f"model:    {launch.model or '(harness default)'}")
        print(f"cwd:      {launch.cwd}")
        print("PATH:     " + launch.env["PATH"].split(os.pathsep)[0] + " (prepended)")
        print("argv:     " + " ".join(launch.argv))
        return 0

    try:
        start(launch)  # replaces this process; does not return on success
    except ManagerLaunchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


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

    supervisor = sub.add_parser("supervisor", help="run and control the supervisor")
    ssub = supervisor.add_subparsers(dest="supervisor_cmd", required=True)
    ssub.add_parser("serve", help="run the supervisor (blocking)")
    ssub.add_parser("status", help="ping a running supervisor")
    ssub.add_parser("stop", help="ask a running supervisor to shut down")
    supervisor.set_defaults(func=_cmd_supervisor)

    dispatch = sub.add_parser("dispatch", help="dispatch a task to a registered repo")
    dispatch.add_argument("--repo", required=True, help="registered repository name")
    dispatch.add_argument("--title", required=True, help="task title/objective")
    dispatch.add_argument("--instructions", default=None, help="task instructions")
    dispatch.add_argument(
        "--provider",
        default="fake",
        choices=["fake", "claude", "codex"],
        help="worker provider",
    )
    dispatch.add_argument("--model", default=None, help="worker model (clamped to ceiling)")
    dispatch.add_argument("--reasoning", default=None, help="worker reasoning (clamped to ceiling)")
    dispatch.add_argument(
        "--run-id", dest="run_id", type=int, default=None, help="attach to an existing run"
    )
    dispatch.set_defaults(func=_cmd_dispatch)

    wait = sub.add_parser("wait", help="wait until a run reaches an actionable state")
    wait.add_argument("run_id", type=int)
    wait.add_argument("--timeout", type=float, default=30.0)
    wait.set_defaults(func=_cmd_wait)

    resume = sub.add_parser("resume", help="resume a task's provider session")
    resume.add_argument("task_id", type=int)
    resume.add_argument("--message", default=None, help="answer/steer message")
    resume.set_defaults(func=_cmd_resume)

    steer = sub.add_parser("steer", help="steer a task (checkpoint or capability-gated interrupt)")
    steer.add_argument("task_id", type=int)
    steer.add_argument("--message", required=True, help="steer message")
    steer.set_defaults(func=_cmd_steer)

    answer = sub.add_parser("answer", help="answer a blocked task and record a durable decision")
    answer.add_argument("task_id", type=int)
    answer.add_argument("--answer", required=True, help="the answer to the pending question")
    answer.add_argument(
        "--scope",
        default="run",
        choices=["task", "run", "global"],
        help="how widely the decision applies (default: run)",
    )
    answer.add_argument("--rationale", default=None, help="why this answer was chosen")
    answer.set_defaults(func=_cmd_answer)

    review = sub.add_parser("review", help="review a task's work (exact-HEAD gate)")
    rvsub = review.add_subparsers(dest="review_cmd", required=True)
    for name in ("show", "approve", "request-changes", "status"):
        p = rvsub.add_parser(name)
        p.add_argument("task_id", type=int)
        if name in ("approve", "request-changes"):
            p.add_argument("--findings", default=None, help="review notes")
    review.set_defaults(func=_cmd_review)

    deliver = sub.add_parser("deliver", help="push and open a PR (requires approval at head)")
    deliver.add_argument("task_id", type=int)
    deliver.add_argument("--no-push", dest="no_push", action="store_true")
    deliver.add_argument("--no-pr", dest="no_pr", action="store_true")
    deliver.add_argument("--remote", default="origin")
    deliver.add_argument("--base", default=None, help="PR base branch")
    deliver.set_defaults(func=_cmd_deliver)

    artifact = sub.add_parser("artifact", help="write a Lavish HTML review artifact")
    artifact.add_argument("run_id", type=int)
    artifact.add_argument("--title", required=True)
    artifact.add_argument("--sections", default=None, help="JSON list of {heading,body,options}")
    artifact.set_defaults(func=_cmd_artifact)

    feedback = sub.add_parser("feedback", help="read a Lavish artifact's feedback sidecar")
    feedback.add_argument("artifact_id", type=int)
    feedback.set_defaults(func=_cmd_feedback)

    hook = sub.add_parser("hook", help="ingest a provider lifecycle hook (payload on stdin)")
    hook.add_argument("event", help="lifecycle event name (e.g. stop, session-start)")
    hook.set_defaults(func=_cmd_hook)

    plan = sub.add_parser("plan", help="compute cross-repo rollout order for a run")
    plan.add_argument("run_id", type=int)
    plan.set_defaults(func=_cmd_plan)

    decision = sub.add_parser("decision", help="inspect and manage durable decisions")
    dsub = decision.add_subparsers(dest="decision_cmd", required=True)
    dlist = dsub.add_parser("list", help="list decisions")
    dlist.add_argument("--all", action="store_true", help="include superseded/invalidated")
    dinval = dsub.add_parser("invalidate", help="mark a decision no longer valid")
    dinval.add_argument("id", type=int)
    dforget = dsub.add_parser("forget", help="hard-delete a decision")
    dforget.add_argument("id", type=int)
    decision.set_defaults(func=_cmd_decision)

    usage = sub.add_parser("usage", help="report model usage for a run")
    usage.add_argument("run_id", type=int)
    usage.set_defaults(func=_cmd_usage)

    reconcile = sub.add_parser("reconcile", help="recover half-alive runners")
    reconcile.set_defaults(func=_cmd_reconcile)

    status = sub.add_parser("status", help="show a compact runtime summary")
    status.set_defaults(func=_cmd_status)

    for name in ("start", "manager"):
        start = sub.add_parser(
            name,
            help="talk to the manager: launch the harness and let it drive mm for you",
        )
        start.add_argument(
            "objective",
            nargs="?",
            default=None,
            help="optional objective to hand the manager as the first message",
        )
        start.add_argument(
            "--provider",
            default=None,
            choices=["claude", "codex"],
            help="enter as this harness (default: configured manager provider)",
        )
        start.add_argument("--model", default=None, help="override the manager model")
        start.add_argument("--reasoning", default=None, help="override the manager reasoning")
        start.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="print the harness invocation instead of launching it",
        )
        start.set_defaults(func=_cmd_start)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
