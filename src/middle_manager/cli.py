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
from middle_manager.lifecycle import TASK_STATUSES


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
    if not args.skip_tools:
        print("provisioning companion tools (treehouse, lavish-axi, gh-axi)…")
        _print_provision(_provision(force=False))
    return 0


def _provision(*, force: bool, names: list[str] | None = None) -> list:
    from middle_manager.paths import ensure_layout
    from middle_manager.setup.provision import provision_all

    ensure_layout()
    return provision_all(names=names, force=force)


def _print_provision(results: list) -> None:
    glyph = {"installed": "+", "present": "=", "skipped": "-", "failed": "x"}
    for r in results:
        ver = f" {r.version}" if r.version else ""
        detail = f" — {r.detail}" if r.detail else ""
        print(f"  [{glyph.get(r.status, '?')}] {r.name}{ver}: {r.status}{detail}")


def _cmd_tools(args: argparse.Namespace) -> int:
    if args.tools_cmd == "install":
        results = _provision(force=args.force, names=args.names or None)
        _print_provision(results)
        return 0 if all(r.status != "failed" for r in results) else 1
    if args.tools_cmd == "status":
        from middle_manager.setup.discovery import discover

        for c in discover()["companions"]:
            state = "available" if c["available"] else "missing"
            ver = f" {c['version']}" if c.get("version") else ""
            detail = f" ({c['detail']})" if c.get("detail") else ""
            print(f"  {c['name']:<11}{ver:<10} {state}{detail}")
        return 0
    print("no tools subcommand given", file=sys.stderr)
    return 2


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
        if args.config_cmd == "claude":
            from middle_manager.config import default_claude_allowed_tools, save_config

            cfg = load_config()
            if args.reset:
                cfg.claude.allowed_tools = default_claude_allowed_tools()
            if args.allowed_tools is not None:
                cfg.claude.allowed_tools = [
                    part.strip() for part in args.allowed_tools.split(",") if part.strip()
                ]
            save_config(cfg)
            from middle_manager import health as _health

            print(_health.describe_claude_tools(_health.claude_tool_profile()))
            return 0
        if args.config_cmd == "health":
            from middle_manager.config import save_config

            cfg = load_config()
            if args.quiet_minutes is not None:
                cfg.health.quiet_minutes = args.quiet_minutes
            if args.plan_minutes is not None:
                cfg.health.plan_minutes = args.plan_minutes
            save_config(cfg)
            print(
                f"worker health: flag a worker after {cfg.health.quiet_minutes}m of silence, "
                f"or {cfg.health.plan_minutes}m without a plan"
            )
            return 0
        if args.config_cmd == "assessments":
            cfg = load_config()
            policy = cfg.assessments
            for name in (
                "enabled",
                "completed_runs",
                "max_days",
                "cooldown_days",
                "minimum_runs",
                "failure_trigger_count",
                "max_actions",
            ):
                value = getattr(args, name, None)
                if value is not None:
                    setattr(policy, name, value)
            from middle_manager.config import save_config

            save_config(cfg)
            print(
                "assessment cadence: "
                f"{'on' if policy.enabled else 'off'}, every {policy.completed_runs} completed "
                f"runs or {policy.max_days} days, {policy.cooldown_days}-day cooldown"
            )
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
    from middle_manager import preflight
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    # Preflight first: refuse before any task state exists, and say why plainly.
    if args.brief and args.instructions:
        print("dispatch: pass --brief <file> or --instructions, not both", file=sys.stderr)
        return 1
    try:
        instructions = preflight.read_brief(args.brief) if args.brief else (args.instructions or "")
        preflight.check_disk()
    except preflight.PreflightError as exc:
        print(f"dispatch refused: {exc}", file=sys.stderr)
        return 1

    client = SupervisorClient()
    try:
        resp = client.dispatch_task(
            repo=args.repo,
            title=args.title,
            instructions=instructions,
            provider=args.provider,
            model=args.model,
            reasoning=args.reasoning,
            run_id=args.run_id,
            base=args.base,
        )
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"dispatch failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"dispatched task {resp['task_id']} in run {resp['run_id']} (branch {resp['branch']})")
    if args.brief:
        archived = preflight.archive_brief(args.repo, int(resp["task_id"]), instructions)
        print(f"brief archived at {archived}")
    return 0


def _cmd_worktree(args: argparse.Namespace) -> int:
    from middle_manager.worktree.reclaim import human_bytes, list_worktrees, prune

    if args.worktree_cmd == "list":
        entries = list_worktrees(args.repo)
        if args.json:
            print(json.dumps([vars(e) for e in entries], indent=2))
            return 0
        if not entries:
            print("no leased worktrees")
            return 0
        print(f"{'SLOT':<14}{'TASK':<7}{'STATUS':<14}{'STATE':<10}{'SIZE':>10}  BRANCH")
        for e in entries:
            state = "gone" if not e.exists else ("dirty" if e.dirty else "clean")
            if e.exists and not e.dirty and e.unpushed_commits > 0:
                state = f"+{e.unpushed_commits} local"
            task = str(e.task_id) if e.task_id else "-"
            print(
                f"{e.slot:<14}{task:<7}{(e.task_status or '?'):<14}{state:<10}"
                f"{human_bytes(e.size_bytes):>10}  {e.branch}"
            )
        total = sum(e.size_bytes for e in entries)
        free = sum(e.size_bytes for e in entries if e.reclaimable)
        print(f"{len(entries)} slot(s), {human_bytes(total)} on disk, {human_bytes(free)} prunable")
        return 0

    res = prune(args.repo, dry_run=args.dry_run)
    verb = "would remove" if res["dry_run"] else "removed"
    for r in res["removed"]:
        print(f"  {verb} {r['path']} ({human_bytes(r['size_bytes'])}) — {r['reason']}")
    for r in res["skipped"]:
        print(f"  kept {r['path']} ({human_bytes(r['size_bytes'])}) — {r['reason']}")
    print(
        f"{verb} {len(res['removed'])} worktree(s), "
        f"{human_bytes(res['reclaimed_bytes'])} reclaimed; "
        f"{len(res['skipped'])} kept ({human_bytes(res['held_bytes'])})"
    )
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.wait_actionable(args.run_id, timeout=args.timeout, after_seq=args.after_seq)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if resp.get("timed_out"):
        print(f"run {args.run_id}: no actionable state within {args.timeout}s")
        return 0
    print(json.dumps(resp, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Non-blocking snapshot of a run: task states, actionable events, usage.

    Reads the supervisor's live state (workers run in the background daemon) and
    returns immediately — it never waits. Use this to track progress inside a turn
    instead of the blocking ``mm wait``.
    """
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        snap = client.run_status(args.run_id)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not snap.get("ok", True) or "run" not in snap:
        print(f"run {args.run_id}: {snap.get('error', 'not found')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    run = snap["run"]
    print(f'run {run["id"]} "{run.get("title", "")}" — {run.get("status", "?")}')
    for t in snap.get("tasks", []):
        prov = t.get("provider") or "?"
        print(f'  task {t["id"]} "{t.get("title", "")}" [{prov}] {t.get("status", "?")}')
    actionable = snap.get("actionable", [])
    if actionable:
        marks = ", ".join(f"{e['kind']}(seq {e['seq']})" for e in actionable)
        print(f"actionable: {marks}")
    else:
        print("actionable: none")
    usage = snap.get("usage") or {}
    if usage:
        print(f"usage: in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    """Snapshot a task, or run one of the lifecycle bookkeeping commands."""
    from middle_manager.lifecycle import LifecycleError, close_task, set_task_status

    try:
        if args.task_cmd == "close":
            res = close_task(args.task_id, args.reason)
            lease = res.get("lease") or {}
            print(
                f"task {args.task_id}: {res['previous_status']} -> closed "
                f"({res['reason']}); {lease.get('note', 'no lease on record')}"
            )
            return 0
        if args.task_cmd == "set-status":
            res = set_task_status(args.task_id, args.status, args.note)
            note = f" — {args.note}" if args.note else ""
            print(f"task {args.task_id}: {res['from']} -> {res['to']}{note}")
            return 0
    except LifecycleError as exc:
        print(f"task {args.task_cmd}: {exc}", file=sys.stderr)
        return 1
    return _cmd_task_show(args)


def _cmd_task_show(args: argparse.Namespace) -> int:
    """Non-blocking snapshot of a single task. Returns immediately; never waits."""
    from middle_manager.supervisor.client import SupervisorClient, SupervisorUnavailable

    client = SupervisorClient()
    try:
        resp = client.task_status(args.task_id)
    except SupervisorUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    task = resp.get("task")
    if not task:
        print(f"task {args.task_id}: {resp.get('error', 'not found')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(task, indent=2))
        return 0
    prov = task.get("provider") or "?"
    model = task.get("model")
    label = f"{prov}/{model}" if model else prov
    line = f'task {task["id"]} "{task.get("title", "")}" [{label}] {task.get("status", "?")}'
    if task.get("branch"):
        line += f"  branch {task['branch']}"
    print(line)
    from middle_manager import progress

    print(f"progress: {progress.describe(progress.latest(args.task_id))}")
    return 0


def _cmd_lease(args: argparse.Namespace) -> int:
    from middle_manager.lifecycle import LifecycleError, release_task_lease

    try:
        res = release_task_lease(args.task_id, reason=args.reason, remove_branch=args.remove_branch)
    except LifecycleError as exc:
        print(f"lease release: {exc}", file=sys.stderr)
        return 1
    print(f"task {args.task_id}: {res['note']}")
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
    print(
        f"resuming task {args.task_id} (session {resp.get('resumed_session')}) — "
        f"status {resp.get('status', 'in_progress')}"
    )
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
    status = resp.get("status")
    line = f"steer task {args.task_id}: mode={resp.get('mode')}"
    if status:
        line += f" status={status}"
    print(f"{line} {resp.get('note', '')}".rstrip())
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
            from middle_manager import progress

            bundle = build_bundle(args.task_id)
            # The worker's own report comes first, in full: judging a diff without
            # the evidence the worker filed is how a complete report was once read
            # as "missing".
            reports = progress.history(args.task_id)
            latest = progress.latest(args.task_id)
            if latest is not None:
                print(
                    f"worker report ({latest['phase']}, {len(reports)} report(s); "
                    f"full log: mm progress {args.task_id}):"
                )
                print(latest["note"] or "(no note)")
                print()
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
    from middle_manager.delivery import DeliveryError, deliver, record_merged

    try:
        if args.merged:
            res = record_merged(args.task_id, args.merged)
            print(f"task {res.task_id}: {res.note}")
            return 0
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


def _cmd_memory(args: argparse.Namespace) -> int:
    from middle_manager import memory
    from middle_manager.repos import list_repos

    if args.memory_cmd == "init":
        memory.ensure_memory_layout()
        seeded = [
            memory.seed_repo_memory(
                r["name"], origin=r.get("origin"), default_branch=r.get("default_branch")
            )
            for r in list_repos()
        ]
        print(f"memory ready at {memory.memory_dir()} ({len(seeded)} repo(s) seeded)")
        return 0
    if args.memory_cmd == "path":
        print(memory.repo_dir(args.repo) if args.repo else memory.memory_dir())
        return 0
    if args.memory_cmd == "show":
        memory.ensure_memory_layout()
        if args.repo:
            notes, tasks = memory.repo_notes_path(args.repo), memory.repo_tasks_path(args.repo)
            if not notes.exists():
                print(f"no memory for repo {args.repo!r} yet", file=sys.stderr)
                return 1
            from middle_manager import progress

            print("# ---- progress (from `mm progress`) ----")
            print(progress.render_repo_log(args.repo))
            for path in (notes, tasks):
                if path.exists():
                    print(f"# ---- {path} ----")
                    print(path.read_text())
            return 0
        for path in (
            memory.board_path(),
            memory.preferences_path(),
            memory.relationships_path(),
            memory.improvements_path(),
        ):
            print(f"# ---- {path} ----")
            print(path.read_text())
        return 0
    print("no memory subcommand given", file=sys.stderr)
    return 2


def _cmd_hook(args: argparse.Namespace) -> int:
    from middle_manager.hooks import handle_hook_stdin

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    result = handle_hook_stdin(args.event, raw)
    print(json.dumps(result))
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    """Write the snapshot to `.mm/memory/handoff.md` and print the short pickup prompt."""
    from middle_manager.handoff import build_handoff

    result = build_handoff()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for warning in result["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(result["prompt"])
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Worker health: alive / quiet (possibly stuck) / dead, per in-flight task."""
    from datetime import timedelta

    from middle_manager import health

    quiet_after = (
        timedelta(minutes=args.quiet_minutes)
        if args.quiet_minutes is not None
        else health.quiet_threshold()
    )
    entries = health.check(quiet_after=quiet_after)
    tools = health.claude_tool_profile()
    if args.json:
        print(json.dumps({"workers": entries, "claude_tools": tools}, indent=2))
        return 0
    print(health.describe_claude_tools(tools))
    if not entries:
        print("no workers in flight")
        return 0 if tools["ok"] else 1
    for e in entries:
        print(health.describe(e))
    troubled = [e for e in entries if e["verdict"] != "alive"]
    return 1 if troubled or not tools["ok"] else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """The team heartbeat: one relayable line of state per tick, on a cadence."""
    from middle_manager import watch

    try:
        return watch.run(
            interval=args.interval,
            once=args.once,
            as_json=args.json,
            exit_when_idle=args.exit_when_idle,
        )
    except KeyboardInterrupt:
        return 0


def _cmd_todo(args: argparse.Namespace) -> int:
    """The manager's intent ledger: what's next and what's waiting on whom."""
    from middle_manager import board

    try:
        if args.todo_cmd == "add":
            todo_id = board.add(
                args.text, run_id=args.run_id, task_id=args.task_id, blocked_on=args.blocked_on
            )
            print(f"todo #{todo_id} added")
            return 0
        if args.todo_cmd == "done":
            for todo_id in args.ids:
                board.done(todo_id)
            print("done: " + ", ".join(f"#{i}" for i in args.ids))
            return 0
        if args.todo_cmd == "drop":
            for todo_id in args.ids:
                board.drop(todo_id)
            print("dropped: " + ", ".join(f"#{i}" for i in args.ids))
            return 0
        if args.todo_cmd == "reopen":
            board.reopen(args.id)
            print(f"todo #{args.id} reopened")
            return 0
        if args.todo_cmd == "block":
            board.block(args.id, args.on)
            print(f"todo #{args.id} waiting on {args.on}")
            return 0
        if args.todo_cmd == "unblock":
            board.unblock(args.id)
            print(f"todo #{args.id} unblocked")
            return 0
        if args.todo_cmd == "edit":
            board.edit(args.id, args.text)
            print(f"todo #{args.id} updated")
            return 0
        if args.todo_cmd == "list":
            from middle_manager.state import init_db, store

            conn = init_db()
            status = None if args.all else "open"
            rows = [
                board.todo_dict(r)
                for r in store.list_todos(conn, status=status, run_id=args.run_id)
            ]
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("no open todos" if not args.all else "no todos")
                return 0
            for t in rows:
                mark = {"open": "[ ]", "done": "[x]", "dropped": "[-]"}.get(t["status"], "[?]")
                print(f"{mark} {board.todo_line(t)[2:]}")
            return 0
    except board.TodoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("no todo subcommand given", file=sys.stderr)
    return 2


def _cmd_board(args: argparse.Namespace) -> int:
    """Render the work board from the ledger + live state (and refresh the projection)."""
    from middle_manager import board

    text = board.write_board() if args.write else board.render_board()
    print(text, end="")
    return 0


def _cmd_progress(args: argparse.Namespace) -> int:
    """A worker's structured progress report (also usable by the manager to inspect)."""
    from middle_manager import progress

    if args.phase is None:
        if args.history:
            entries = progress.history(args.task_id)
            if args.json:
                print(json.dumps(entries, indent=2))
                return 0
            if not entries:
                print(f"task {args.task_id}: no progress reported")
            for e in entries:
                print(f"{e['at']} · {progress.describe(e)}")
            return 0
        latest = progress.latest(args.task_id)
        if args.json:
            print(json.dumps(latest, indent=2))
            return 0
        print(f"task {args.task_id}: {progress.describe(latest)}")
        return 0
    try:
        progress.record(args.task_id, phase=args.phase, note=args.note or "")
    except progress.ProgressError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"task {args.task_id}: {args.phase} recorded")
    return 0


def _cmd_reflect(args: argparse.Namespace) -> int:
    """A worker's end-of-task reflection: its self-assessment and its assessment of the manager."""
    from middle_manager import reflections

    if args.self_note is None and args.manager_note is None:
        entries = reflections.history(args.task_id)
        if args.json:
            print(json.dumps(entries, indent=2))
            return 0
        print(reflections.render(entries, heading=f"# task {args.task_id} — reflections"), end="")
        return 0
    try:
        reflections.record(args.task_id, self_note=args.self_note, manager_note=args.manager_note)
    except reflections.ReflectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"task {args.task_id}: reflection recorded")
    return 0


def _cmd_assessment(args: argparse.Namespace) -> int:
    from middle_manager import assessments
    from middle_manager.state import init_db

    conn = init_db()
    try:
        if args.assessment_cmd == "tick":
            cycle = assessments.ensure_due(conn, force=args.force)
            if args.json:
                print(json.dumps(cycle, indent=2))
            elif cycle is None:
                print("no assessment due")
            else:
                print(f"assessment {cycle['id']} {cycle['status']} (trigger: {cycle['trigger']})")
            return 0
        if args.assessment_cmd == "status":
            row = assessments.latest_cycle(conn)
            if row is None:
                print("no assessments yet")
                return 0
            cycle = assessments.cycle_dict(conn, int(row["id"]))
            if args.json:
                print(json.dumps(cycle, indent=2))
            else:
                print(
                    f"assessment {cycle['id']}: {cycle['status']} "
                    f"(trigger: {cycle['trigger']}, actions: {len(cycle['actions'])})"
                )
            return 0
        if args.assessment_cmd == "show":
            cycle_id = args.id
            if cycle_id is None:
                row = assessments.latest_cycle(conn)
                if row is None:
                    print("no assessments yet")
                    return 0
                cycle_id = int(row["id"])
            cycle = assessments.cycle_dict(conn, cycle_id)
            if args.json:
                print(json.dumps(cycle, indent=2))
            else:
                print(assessments.assessment_prompt(cycle))
            return 0
        if args.assessment_cmd == "complete":
            actions: list[dict] = []
            if args.action_json:
                decoded = json.loads(args.action_json)
                if not isinstance(decoded, list) or not all(isinstance(x, dict) for x in decoded):
                    raise assessments.AssessmentError(
                        "--action-json must be a JSON list of objects"
                    )
                actions = decoded
            cycle = assessments.complete(
                args.id,
                summary=args.summary,
                strengths=args.strength,
                weaknesses=args.weakness,
                actions=actions,
                conn=conn,
            )
            print(
                f"assessment {cycle['id']} recorded; align {len(cycle['actions'])} "
                "proposed actions with the user"
            )
            return 0
        if args.assessment_cmd == "align":
            cycle = assessments.align(
                args.id, decision=args.decision, notes=args.notes or "", conn=conn
            )
            print(f"assessment {cycle['id']} {cycle['status']} ({args.decision})")
            return 0
    except (assessments.AssessmentError, json.JSONDecodeError) as exc:
        print(f"assessment failed: {exc}", file=sys.stderr)
        return 1
    return 2


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
    try:
        from middle_manager import board
        from middle_manager.state import init_db, store

        conn = init_db()
        marks = ",".join("?" for _ in (*board.IN_FLIGHT, *board.NEEDS_ME))
        counts = dict(
            conn.execute(
                f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) GROUP BY status",
                (*board.IN_FLIGHT, *board.NEEDS_ME),
            ).fetchall()
        )
        running = sum(counts.get(k, 0) for k in board.IN_FLIGHT)
        waiting = sum(counts.get(k, 0) for k in board.NEEDS_ME)
        print(f"tasks:   {running} in flight, {waiting} waiting on me")
        nxt = board.next_steps(conn)
        blocked = board.waiting(conn)
        print(f"todos:   {len(nxt)} next, {len(blocked)} waiting")
        for t in nxt[:3]:
            print(f"  next:  #{t['id']} {t['text']}")
        _ = store
    except Exception:  # noqa: BLE001 - status must never crash
        pass
    try:
        from middle_manager import assessments
        from middle_manager.state import init_db

        row = assessments.latest_cycle(init_db())
        if row is not None and row["status"] in assessments.OPEN_STATUSES:
            print(f"review:  assessment {row['id']} {row['status']}")
    except Exception:  # noqa: BLE001 - status must never crash
        pass
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
    setup.add_argument(
        "--skip-tools",
        dest="skip_tools",
        action="store_true",
        help="do not provision companion tools during setup",
    )
    _add_profile_flags(setup)
    setup.set_defaults(func=_cmd_setup)

    tools = sub.add_parser("tools", help="provision and inspect companion tools")
    tsub = tools.add_subparsers(dest="tools_cmd", required=True)
    tinstall = tsub.add_parser("install", help="download/verify pinned companions into .mm/tools")
    tinstall.add_argument("names", nargs="*", help="specific companions (default: all)")
    tinstall.add_argument("--force", action="store_true", help="reinstall even if present")
    tsub.add_parser("status", help="show companion availability")
    tools.set_defaults(func=_cmd_tools)

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
    assessments_cfg = csub.add_parser("assessments", help="change review cadence")
    assessments_cfg.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    assessments_cfg.add_argument("--disabled", dest="enabled", action="store_false")
    assessments_cfg.add_argument("--completed-runs", type=int, default=None)
    assessments_cfg.add_argument("--max-days", type=int, default=None)
    assessments_cfg.add_argument("--cooldown-days", type=int, default=None)
    assessments_cfg.add_argument("--minimum-runs", type=int, default=None)
    assessments_cfg.add_argument("--failure-trigger-count", type=int, default=None)
    assessments_cfg.add_argument("--max-actions", type=int, default=None)
    claude_cfg = csub.add_parser("claude", help="the tool profile Claude workers launch with")
    claude_cfg.add_argument(
        "--allowed-tools",
        dest="allowed_tools",
        default=None,
        help="comma-separated Claude Code tool patterns, e.g. 'Read,Edit,Bash(git:*)'",
    )
    claude_cfg.add_argument(
        "--reset", action="store_true", help="restore the documented default profile"
    )

    health_cfg = csub.add_parser("health", help="change the worker quiet threshold")
    health_cfg.add_argument("--quiet-minutes", dest="quiet_minutes", type=int, default=None)
    health_cfg.add_argument("--plan-minutes", dest="plan_minutes", type=int, default=None)
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
        "--brief",
        default=None,
        help=(
            "read the task instructions from this file; refused if missing or empty, "
            "and archived under .mm/briefs/<repo>/task-<id>.md after dispatch"
        ),
    )
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
    dispatch.add_argument(
        "--base",
        default=None,
        help=(
            "start the worker from origin/<branch> instead of the repo base (stacked "
            "PRs); recorded on the task so `mm deliver` opens the PR against it"
        ),
    )
    dispatch.set_defaults(func=_cmd_dispatch)

    worktree = sub.add_parser("worktree", help="inspect and reclaim leased worktrees")
    wtsub = worktree.add_subparsers(dest="worktree_cmd", required=True)
    wtlist = wtsub.add_parser("list", help="slot, task, status, branch, dirty/clean, size")
    wtlist.add_argument("--repo", default=None, help="only this registered repo")
    wtlist.add_argument("--json", action="store_true", help="machine-readable output")
    wtprune = wtsub.add_parser(
        "prune", help="remove the worktrees of finished tasks that hold nothing unique"
    )
    wtprune.add_argument("--repo", default=None, help="only this registered repo")
    wtprune.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report what would go without touching anything",
    )
    worktree.set_defaults(func=_cmd_worktree)

    wait = sub.add_parser(
        "wait",
        help="BLOCKING long-poll until a run is actionable (scripts/tests; "
        "use --timeout 0 for a non-blocking drain). Prefer `mm run` in a live turn.",
    )
    wait.add_argument("run_id", type=int)
    wait.add_argument("--timeout", type=float, default=30.0)
    wait.add_argument(
        "--after-seq",
        dest="after_seq",
        type=int,
        default=0,
        help="only report actionable events with seq greater than this (a cursor)",
    )
    wait.set_defaults(func=_cmd_wait)

    run = sub.add_parser(
        "run", help="non-blocking snapshot of a run (task states, actionable events, usage)"
    )
    run.add_argument("run_id", type=int)
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.set_defaults(func=_cmd_run)

    task = sub.add_parser("task", help="inspect a task, or close/repair its bookkeeping")
    tksub = task.add_subparsers(dest="task_cmd", required=True)
    tkshow = tksub.add_parser("show", help="non-blocking snapshot of a single task")
    tkshow.add_argument("task_id", type=int)
    tkshow.add_argument("--json", action="store_true", help="machine-readable output")
    tkclose = tksub.add_parser(
        "close", help="end a task that will not be delivered, and give its slot back"
    )
    tkclose.add_argument("task_id", type=int)
    tkclose.add_argument("--reason", required=True, help="why this task is over")
    tkset = tksub.add_parser("set-status", help="repair a task's status, with a note")
    tkset.add_argument("task_id", type=int)
    tkset.add_argument("status", choices=list(TASK_STATUSES))
    tkset.add_argument("--note", default=None, help="why the status was corrected")
    task.set_defaults(func=_cmd_task)

    lease = sub.add_parser("lease", help="worktree lease bookkeeping")
    lsub = lease.add_subparsers(dest="lease_cmd", required=True)
    lrelease = lsub.add_parser("release", help="release a stuck lease and free the slot")
    lrelease.add_argument("task_id", type=int)
    lrelease.add_argument("--reason", default="released by hand", help="why it was released")
    lrelease.add_argument(
        "--remove-branch",
        dest="remove_branch",
        action="store_true",
        help="also delete the lease branch (only when its commits are safe elsewhere)",
    )
    lease.set_defaults(func=_cmd_lease)

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
    deliver.add_argument(
        "--merged",
        default=None,
        metavar="SHA",
        help="record delivery against a commit already merged upstream; pushes nothing",
    )
    deliver.set_defaults(func=_cmd_deliver)

    artifact = sub.add_parser("artifact", help="write a Lavish HTML review artifact")
    artifact.add_argument("run_id", type=int)
    artifact.add_argument("--title", required=True)
    artifact.add_argument("--sections", default=None, help="JSON list of {heading,body,options}")
    artifact.set_defaults(func=_cmd_artifact)

    feedback = sub.add_parser("feedback", help="read a Lavish artifact's feedback sidecar")
    feedback.add_argument("artifact_id", type=int)
    feedback.set_defaults(func=_cmd_feedback)

    memory = sub.add_parser(
        "memory", help="durable memory under .mm/memory (per-instance + per-repo)"
    )
    msub = memory.add_subparsers(dest="memory_cmd", required=True)
    msub.add_parser("init", help="scaffold instance memory + seed a dir per registered repo")
    mpath = msub.add_parser("path", help="print the memory dir, or a repo's memory dir")
    mpath.add_argument("--repo", default=None, help="print this repo's memory directory")
    mshow = msub.add_parser(
        "show", help="print instance preferences+relationships, or a repo's memory"
    )
    mshow.add_argument("--repo", default=None, help="show this repo's notes + tasks instead")
    memory.set_defaults(func=_cmd_memory)

    todo = sub.add_parser("todo", help="the manager's intent ledger: next steps and waits")
    tsub = todo.add_subparsers(dest="todo_cmd", required=True)
    tadd = tsub.add_parser("add", help="record a next step")
    tadd.add_argument("text")
    tadd.add_argument("--run", dest="run_id", type=int, default=None)
    tadd.add_argument("--task", dest="task_id", type=int, default=None)
    tadd.add_argument(
        "--blocked-on",
        dest="blocked_on",
        default=None,
        help="user[:what] | review | task:<id> | access[:what]",
    )
    tdone = tsub.add_parser("done", help="mark todo(s) done")
    tdone.add_argument("ids", type=int, nargs="+")
    tdrop = tsub.add_parser("drop", help="drop todo(s) without doing them")
    tdrop.add_argument("ids", type=int, nargs="+")
    treopen = tsub.add_parser("reopen", help="reopen a done/dropped todo")
    treopen.add_argument("id", type=int)
    tblock = tsub.add_parser("block", help="mark a todo as waiting on someone/something")
    tblock.add_argument("id", type=int)
    tblock.add_argument("--on", required=True, help="user[:what] | review | task:<id> | access")
    tunblock = tsub.add_parser("unblock", help="clear a todo's wait")
    tunblock.add_argument("id", type=int)
    tedit = tsub.add_parser("edit", help="rewrite a todo's text")
    tedit.add_argument("id", type=int)
    tedit.add_argument("text")
    tlist = tsub.add_parser("list", help="list open todos (or all)")
    tlist.add_argument("--all", action="store_true")
    tlist.add_argument("--run", dest="run_id", type=int, default=None)
    tlist.add_argument("--json", action="store_true")
    todo.set_defaults(func=_cmd_todo)

    board_cmd = sub.add_parser("board", help="render the work board from ledger + live state")
    board_cmd.add_argument("--write", action="store_true", help="also refresh .mm/memory/tasks.md")
    board_cmd.set_defaults(func=_cmd_board)

    prog = sub.add_parser(
        "progress", help="worker progress report (record with --phase; inspect without)"
    )
    prog.add_argument("task_id", type=int)
    prog.add_argument("--phase", choices=["plan", "implement", "test", "review", "blocked", "done"])
    prog.add_argument("--note", default="")
    prog.add_argument("--history", action="store_true", help="show every report, oldest last")
    prog.add_argument("--json", action="store_true")
    prog.set_defaults(func=_cmd_progress)

    refl = sub.add_parser(
        "reflect",
        help="worker reflection: --self (own assessment) and/or --manager (assessment of the "
        "manager); without either, show the task's reflections",
    )
    refl.add_argument("task_id", type=int)
    refl.add_argument(
        "--self", dest="self_note", default=None, help="what you learned, what went well or badly"
    )
    refl.add_argument(
        "--manager",
        dest="manager_note",
        default=None,
        help="how the brief, scope, steering, and review served you",
    )
    refl.add_argument("--json", action="store_true")
    refl.set_defaults(func=_cmd_reflect)

    handoff = sub.add_parser(
        "handoff",
        help="write the snapshot to .mm/memory/handoff.md and print a short pickup prompt",
    )
    handoff.add_argument("--json", action="store_true", help="machine-readable output")
    handoff.set_defaults(func=_cmd_handoff)

    watch_cmd = sub.add_parser(
        "watch",
        help=(
            "team heartbeat: print one line of state (in-flight workers, tasks waiting "
            "on you, the pull requests delivered work sits in with their CI verdict, "
            "new events) now and every --interval seconds; relay each tick"
        ),
    )
    watch_cmd.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help="seconds between ticks (default 300 — the 5-minute check-in)",
    )
    watch_cmd.add_argument("--once", action="store_true", help="print one tick and exit")
    watch_cmd.add_argument("--json", action="store_true", help="machine-readable ticks")
    watch_cmd.add_argument(
        "--exit-when-idle",
        dest="exit_when_idle",
        action="store_true",
        help=(
            "for scripts: exit on the second idle tick instead of going quiet and "
            "waiting for the team to have news again"
        ),
    )
    watch_cmd.set_defaults(func=_cmd_watch)

    health_cmd = sub.add_parser("health", help="are in-flight workers alive and talking?")
    health_cmd.add_argument(
        "--quiet-minutes",
        dest="quiet_minutes",
        type=int,
        default=None,
        help="override the configured silence threshold for this check",
    )
    health_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    health_cmd.set_defaults(func=_cmd_health)

    hook = sub.add_parser("hook", help="ingest a provider lifecycle hook (payload on stdin)")
    hook.add_argument("event", help="lifecycle event name (e.g. stop, session-start)")
    hook.set_defaults(func=_cmd_hook)

    assessment = sub.add_parser(
        "assessment", help="inspect and record proactive manager performance reviews"
    )
    asub = assessment.add_subparsers(dest="assessment_cmd", required=True)
    atick = asub.add_parser("tick", help="prepare a cycle if policy says one is due")
    atick.add_argument("--force", action="store_true", help="prepare a cycle regardless of cadence")
    atick.add_argument("--json", action="store_true")
    astatus = asub.add_parser("status", help="show the latest cycle")
    astatus.add_argument("--json", action="store_true")
    ashow = asub.add_parser("show", help="show the assessment prompt or state")
    ashow.add_argument("id", type=int, nargs="?", default=None)
    ashow.add_argument("--json", action="store_true")
    acomplete = asub.add_parser("complete", help="record findings and proposed experiments")
    acomplete.add_argument("id", type=int)
    acomplete.add_argument("--summary", required=True)
    acomplete.add_argument("--strength", action="append", default=[])
    acomplete.add_argument("--weakness", action="append", default=[])
    acomplete.add_argument(
        "--action-json",
        default=None,
        help=(
            "JSON actions with description/category/observation/likely_cause/"
            "baseline/target/measurement"
        ),
    )
    aalign = asub.add_parser("align", help="record the user's decision on the plan")
    aalign.add_argument("id", type=int)
    aalign.add_argument("--decision", choices=["approved", "revised", "dismissed"], required=True)
    aalign.add_argument("--notes", default=None)
    assessment.set_defaults(func=_cmd_assessment)

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


def _normalize_argv(argv: list[str]) -> list[str]:
    """Keep ``mm task <id>`` working now that ``task`` has subcommands."""
    if len(argv) >= 2 and argv[0] == "task" and argv[1].isdigit():
        return [argv[0], "show", *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(list(argv if argv is not None else sys.argv[1:])))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
