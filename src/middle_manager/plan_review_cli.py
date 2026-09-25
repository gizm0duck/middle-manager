"""``mm review plan``: the command surface for :mod:`middle_manager.plan_review`.

Kept out of ``cli.py`` (already far past the source-line ceiling) so the parser
and the printing sit next to each other.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def add_parser(rvsub) -> None:
    p = rvsub.add_parser(
        "plan",
        help="record a fact-checked review of the task's newest plan note",
        description=(
            "Review the worker's newest plan note. Verify every load-bearing claim first and "
            "record each with --checked; the message or findings reach the worker on resume."
        ),
    )
    p.add_argument("task_id", type=int)
    p.add_argument(
        "--checked",
        action="append",
        default=[],
        metavar="'CLAIM => EVIDENCE'",
        help=(
            "a claim from the plan and the evidence you checked it against (a path:line you "
            "read, a command and its result, a query); repeatable, at least one required"
        ),
    )
    verdict = p.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true", help="approve the plan")
    verdict.add_argument(
        "--request-changes",
        dest="request_changes",
        action="store_true",
        help="send the plan back with --findings",
    )
    p.add_argument("--message", default=None, help="what the worker hears with an approval")
    p.add_argument(
        "--findings",
        default=None,
        metavar="FILE",
        help="a file holding what the plan must change (required with --request-changes)",
    )


def run(args: argparse.Namespace) -> int:
    from middle_manager import plan_review

    try:
        findings = ""
        if args.request_changes:
            if not args.findings:
                raise plan_review.PlanReviewError("--request-changes needs --findings <file>")
            path = Path(args.findings)
            if not path.is_file():
                raise plan_review.PlanReviewError(f"findings file {args.findings} not found")
            findings = path.read_text(encoding="utf-8")
        elif args.findings:
            raise plan_review.PlanReviewError("--findings goes with --request-changes")
        if args.message and not args.approve:
            raise plan_review.PlanReviewError("--message goes with --approve")
        res = plan_review.record(
            args.task_id,
            checked=args.checked,
            verdict="approved" if args.approve else "changes_requested",
            message=args.message or "",
            findings=findings,
        )
    except plan_review.PlanReviewError as exc:
        print(f"plan review: {exc}", file=sys.stderr)
        return 1
    what = "approved" if args.approve else "sent back"
    print(
        f"task {args.task_id}'s plan note (progress event {res['progress_event_id']}) {what}; "
        f"the worker hears it on the next `mm resume {args.task_id}`"
    )
    print()
    print(plan_review.render(res["row"]))
    return 0


def show_block(task_id: int) -> str:
    """What ``mm review show`` prints about the plan: pending, and the newest record."""
    from middle_manager import plan_review
    from middle_manager.state import init_db

    conn = init_db()
    out = []
    refused = plan_review.refusal(conn, task_id)
    if refused:
        out.append(f"plan gate: {refused}")
    rows = plan_review.reviews_for(conn, task_id)
    if rows:
        if out:
            out.append("")
        out.append(plan_review.render(rows[0]))
        if len(rows) > 1:
            out.append("")
            out.append(f"({len(rows) - 1} earlier plan review(s) on record)")
    return "\n".join(out)
