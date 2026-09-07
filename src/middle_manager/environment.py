"""The per-repository environment block a worker is handed at dispatch.

Between 2026-09-01 and 09-07, 48 of 102 worker reflections lost a cycle to a fact
about the environment rather than to the task: a pre-push hook that runs the full
suite and denies the push (21), an evidence directory under ``/private/tmp`` that
the worker's shell cannot reach (21), a shared Postgres container recreated by a
sibling worktree mid-run (12), and a 10,000-test run killed by the worker's tool
timeout at about 77 percent, leaving the database poisoned for the next run. The
private-database recipe (a per-task compose project and port, ``make`` variable
overrides, a root ``.env`` pin) existed in repo notes and in
``mm task env set <id> compose_project=...``, but nothing put it in front of the
worker; briefs restated environment facts by hand and drifted.

So the facts live on the repository (``mm repo set <name> --compose-stack ...
--db-port-base ... --push-hook-runs-full-suite ... --local-gate ... --evidence-dir
...``) and the runtime renders them, once, identically, for every dispatch:

- the evidence directory is inside the worktree and excluded from git through the
  repository's ``info/exclude`` (shared by every worktree of the base clone), so a
  worker's shell can always reach it and ``mm review show`` always lists it;
- a repository that declares a compose stack gets ``compose_project=task_<n>`` and
  a host port derived from ``db_port_base`` assigned at dispatch and recorded with
  the task-env mechanism the teardown on deliver/close/prune already reads;
- a repository whose pre-push hook runs the full suite tells the worker to stop at
  the code-level gates and file its done note with the head SHA — the harness's
  push-on-behalf path (:func:`middle_manager.turn_end.rescue_unpushed`) then
  publishes the lease branch, exactly as it does for any done-but-unpushed turn.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from middle_manager import compose
from middle_manager.state import store

HEADING = "Environment for this repository"

#: Where receipts land when a repository names nothing else: inside the worktree,
#: so the worker's shell reaches it, and ignored by git, so it never ships.
DEFAULT_EVIDENCE_DIR = ".mm-evidence"
#: Who runs the full suite when a repository names nobody.
DEFAULT_FULL_SUITE_OWNER = "ci"
#: The task-scoped key holding the database host port assigned at dispatch.
DB_PORT_KEY = "db_port"
#: The variable name the recipe uses for that port. Compose files read it from
#: the environment or the root ``.env``; Makefiles take it as a variable override.
DB_PORT_VARIABLE = "DB_PORT"
#: The columns ``mm repo set`` owns for this block.
REPO_COLUMNS = (
    "compose_stack",
    "db_port_base",
    "push_hook_runs_full_suite",
    "local_gate",
    "full_suite_owner",
    "evidence_dir",
)

MAX_PORT = 65535
MIN_PORT_BASE = 1024
_TRUE_WORDS = frozenset({"1", "yes", "y", "true", "on"})
_FALSE_WORDS = frozenset({"0", "no", "n", "false", "off", ""})


class RepoEnvironmentError(Exception):
    """A repo environment setting was refused."""


def _cell(row, column: str):
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(column) if isinstance(row, dict) else None
    return row[column] if column in keys else None  # noqa: SIM118 - sqlite3.Row


def parse_bool(value: str | bool | None) -> bool | None:
    """``yes``/``no`` and friends to a bool; None (or "") clears the setting."""
    if value is None or isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return None
    raise RepoEnvironmentError(f"expected yes or no, got {value!r}")


def parse_compose_stack(value: str | None) -> str | None:
    """``yes`` (the repository's default compose file), ``no`` (clear), or a file path."""
    if value is None:
        return None
    word = value.strip()
    if word.lower() in _FALSE_WORDS:
        return None
    if word.lower() in _TRUE_WORDS:
        return "yes"
    return word


def parse_port_base(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RepoEnvironmentError(f"--db-port-base wants a port number, got {value!r}") from exc
    if port == 0:
        return None
    if not MIN_PORT_BASE <= port < MAX_PORT:
        raise RepoEnvironmentError(
            f"--db-port-base must be between {MIN_PORT_BASE} and {MAX_PORT - 1}, got {port}"
        )
    return port


@dataclass
class RepoEnvironment:
    """What a repository has declared about its environment, defaults filled in."""

    repo: str
    compose_stack: str | None = None
    db_port_base: int | None = None
    push_hook_runs_full_suite: bool = False
    local_gate: str | None = None
    full_suite_owner: str = DEFAULT_FULL_SUITE_OWNER
    evidence_dir: str = DEFAULT_EVIDENCE_DIR

    @property
    def compose_file(self) -> str | None:
        """The compose file named, or None when the repository's default applies."""
        if not self.compose_stack or self.compose_stack == "yes":
            return None
        return self.compose_stack

    def describe(self) -> list[str]:
        """One line per fact, the way ``mm repo set <name>`` prints them."""
        if not self.compose_stack:
            stack = "none declared"
        else:
            stack = self.compose_file or "the repository's default compose file"
        port = (
            f"from {self.db_port_base} + task id"
            if self.db_port_base
            else "not assigned (set --db-port-base)"
        )
        return [
            f"compose stack: {stack}; database port {port}",
            "push hook runs the full suite: "
            + (
                "yes (workers stop at code-level gates; the harness pushes)"
                if self.push_hook_runs_full_suite
                else "no"
            ),
            f"local gate: {self.local_gate or 'not set (the brief names the suite)'}; "
            f"full suite owner: {self.full_suite_owner}",
            f"evidence directory: {self.evidence_dir} (inside each worktree, excluded from git)",
        ]


def for_repo(row) -> RepoEnvironment:
    """A registered repository's environment, from its row. Unset reads as the default."""
    name = _cell(row, "name") or ""
    evidence = str(_cell(row, "evidence_dir") or "").strip()
    owner = str(_cell(row, "full_suite_owner") or "").strip()
    gate = str(_cell(row, "local_gate") or "").strip()
    stack = str(_cell(row, "compose_stack") or "").strip()
    base = _cell(row, "db_port_base")
    return RepoEnvironment(
        repo=name,
        compose_stack=stack or None,
        db_port_base=int(base) if base else None,
        push_hook_runs_full_suite=bool(_cell(row, "push_hook_runs_full_suite")),
        local_gate=gate or None,
        full_suite_owner=owner or DEFAULT_FULL_SUITE_OWNER,
        evidence_dir=evidence.strip("/") or DEFAULT_EVIDENCE_DIR,
    )


def set_fields(conn: sqlite3.Connection, name: str, **values) -> None:
    """Store the environment columns ``mm repo set`` passed. Only named ones change."""
    fields = {}
    for key, value in values.items():
        if key not in REPO_COLUMNS:
            raise RepoEnvironmentError(f"{key!r} is not an environment setting")
        if value is None:
            continue
        if key == "compose_stack":
            fields[key] = parse_compose_stack(value)
        elif key == "db_port_base":
            fields[key] = parse_port_base(value)
        elif key == "push_hook_runs_full_suite":
            parsed = parse_bool(value)
            fields[key] = 1 if parsed else None
        elif key == "evidence_dir":
            fields[key] = str(value).strip().strip("/") or None
        else:
            fields[key] = str(value).strip() or None
    if fields:
        store.update_repo_fields(conn, name, **fields)


# --------------------------------------------------------------------------- #
# Per-task assignment
# --------------------------------------------------------------------------- #


def compose_project_for(task_id: int) -> str:
    """The per-task compose project: the shape teardown recognises as the task's own."""
    return f"task_{task_id}"


def derive_port(base: int | None, task_id: int) -> int | None:
    """A host port for this task: ``base + task_id``, wrapped back into range.

    Deterministic (same task, same port, every dispatch and every resume) and
    unique per task as long as fewer than ``65535 - base`` tasks are in flight
    at once, which is every machine this runs on.
    """
    if not base:
        return None
    span = MAX_PORT - base
    return base + (task_id % (span + 1))


@dataclass
class PreparedEnvironment:
    """What dispatch settled for one task, rendered and recorded."""

    block: str
    evidence_path: str
    compose_project: str | None = None
    db_port: int | None = None


def ensure_excluded(worktree: str | Path, evidence_dir: str) -> bool:
    """Add ``evidence_dir`` to the repository's ``info/exclude`` so it never ships.

    ``info/exclude`` lives in the common git dir, so one line covers the base clone
    and every worktree leased off it. Idempotent; a worktree git cannot read is
    left alone (the block still names the directory, and the auto-commit's own
    exclusion list holds it back regardless).
    """
    pattern = f"/{evidence_dir.strip('/')}/"
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        target = Path(proc.stdout.strip())
        if not target.is_absolute():
            target = Path(worktree) / target
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if pattern in existing.splitlines():
            return True
        with target.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{pattern}\n")
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def prepare(
    conn: sqlite3.Connection,
    repo_row,
    *,
    task_id: int,
    worktree: str,
    branch: str | None,
) -> PreparedEnvironment:
    """Settle the environment for a task at dispatch and render its block.

    A repository with a compose stack gets its project and port recorded on the
    task here (``source="dispatch"``), through the same keys ``mm task env set``
    writes and teardown reads. Nothing here raises for a repository that declared
    nothing: every task gets at least the evidence directory and the local/CI
    split.
    """
    env = for_repo(repo_row)
    project = port = None
    if env.compose_stack:
        project = compose.record_project(
            task_id, compose_project_for(task_id), source="dispatch", conn=conn
        )
        port = derive_port(env.db_port_base, task_id)
        if port is not None:
            store.set_task_env(conn, task_id, DB_PORT_KEY, str(port), source="dispatch")
    ensure_excluded(worktree, env.evidence_dir)
    evidence_path = str(Path(worktree) / env.evidence_dir)
    block = render(
        env,
        task_id=task_id,
        evidence_path=evidence_path,
        compose_project=project,
        db_port=port,
        branch=branch,
    )
    return PreparedEnvironment(
        block=block, evidence_path=evidence_path, compose_project=project, db_port=port
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(
    env: RepoEnvironment,
    *,
    task_id: int,
    evidence_path: str,
    compose_project: str | None = None,
    db_port: int | None = None,
    branch: str | None = None,
) -> str:
    """The block itself. Markdown, one bullet per fact, no prose to drift."""
    lines = [f"## {HEADING}", ""]
    lines.append(
        f"- **Evidence directory: `{evidence_path}/`** — inside your worktree and excluded "
        "from git. Create it with your own commands and write every receipt there (gate "
        "output, screenshots, dumps), then name the paths in your done report; "
        "`mm review show` lists that directory for the reviewer. Never write receipts "
        "under `/private/tmp`: that path is not reachable from your shell, and tests "
        "never write receipts at all (they use `tmp_path`)."
    )
    gate = f"`{env.local_gate}`" if env.local_gate else "the scoped suite the brief names"
    owner = "CI" if env.full_suite_owner.lower() == "ci" else env.full_suite_owner
    lines.append(
        f"- **Local gate: {gate}.** That is what you run before reporting done. The full "
        f"suite belongs to {owner}; do not run it here — a run that outlasts your tool "
        "timeout is killed part-way and leaves the database poisoned for the next run."
    )
    if compose_project:
        port_text = (
            f" and host port **{db_port}** (`{DB_PORT_VARIABLE}`)" if db_port is not None else ""
        )
        file_flag = f" -f {env.compose_file}" if env.compose_file else ""
        overrides = f"COMPOSE_PROJECT_NAME={compose_project}"
        pin = [f"COMPOSE_PROJECT_NAME={compose_project}"]
        if db_port is not None:
            overrides += f" {DB_PORT_VARIABLE}={db_port}"
            pin.append(f"{DB_PORT_VARIABLE}={db_port}")
        lines.append(
            f"- **Private database stack: compose project `{compose_project}`{port_text}.** "
            "This task owns that stack and nothing else; the shared container other "
            "worktrees use is not yours to start, stop, or recreate. Every `make` or "
            "`docker compose` call that touches the database carries these values:"
        )
        lines.append(f"    - `make <target> {overrides}` (make variable overrides);")
        lines.append(
            f"    - `docker compose -p {compose_project}{file_flag} up -d` "
            "(and the same `-p` on every other compose command);"
        )
        lines.append(
            "    - pin them in the worktree's root `.env` so anything that reads it agrees: "
            + ", ".join(f"`{line}`" for line in pin)
            + "."
        )
        lines.append(
            "  Never run `docker compose down` on any other project name. The harness takes "
            "this stack down when the task is delivered or closed."
        )
    if env.push_hook_runs_full_suite:
        target = f"`{branch}`" if branch else "your lease branch"
        lines.append(
            "- **Push hook: this repository's pre-push hook runs the full suite, so do not "
            "push.** This replaces the push line in the command rules above. Stop at the "
            "code-level gates (the local gate, lint, format), commit, and file your done "
            f'report with `mm progress {task_id} --phase done --note "..."` naming the '
            f"head SHA from `git rev-parse HEAD`. The harness then pushes {target} on your "
            f"behalf and records it; `mm task push {task_id}` is the same push by hand."
        )
    return "\n".join(lines) + "\n"


def evidence_path_for(repo_row, worktree: str | None) -> str | None:
    """Where this task's receipts are, for the reviewer. None without a worktree."""
    if not worktree:
        return None
    return str(Path(worktree) / for_repo(repo_row).evidence_dir)
