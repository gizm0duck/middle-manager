---
name: bitbucket-twg
description: >-
  How to work a Bitbucket Cloud repository through Atlassian's TWG CLI
  (`twg bb`) instead of `gh`: the JSON invocation contract, error and auth
  handling, the gh→twg command map, and where Bitbucket behaves differently
  from GitHub (short hashes, statuses across old commits, merge checks, no
  native stacks, no update-branch, no `gh api`). Use whenever mm is configured
  with `forge = bitbucket`, a brief or task touches a bitbucket.org repository,
  or someone asks to "check the PR", "open a PR", "merge" or "check CI" on
  Bitbucket.
user-invocable: true
---

# Bitbucket through `twg bb`

## When this applies

Only when this install is configured for Bitbucket: `mm config show` has
`[forge] kind = "bitbucket"` (set with `mm setup --forge bitbucket` or
`mm config forge --forge bitbucket`). GitHub with `gh` is the default and a
`bitbucket.org` remote alone never switches it. With the forge set, `mm deliver`,
`mm watch` and `mm stack merge` already drive `twg` for you. Use this skill when you
read or act on a Bitbucket pull request yourself, or brief a worker who will.

Bitbucket **Cloud** only. `twg bb` does not reach Bitbucket Server or Data Center.

## The invocation contract

Always run it this way in scripts and by hand:

```bash
twg --output json --output-summary none bb <command> ... --repo WORKSPACE/REPO
```

- `--output json --output-summary none` makes stdout the JSON envelope itself.
  Without them, `TWG_AGENT_DEFAULTS=1` (set by twg's own agent skill) prints a YAML
  summary pointing at a file instead. Unset `TWG_AGENT_DEFAULTS` for scripts.
- Always pass `--repo WORKSPACE/REPO` explicitly. Without it twg guesses from the
  checkout's git remotes, and in a worktree whose `origin` is a local path that
  guess can be wrong.
- **Success:** exit 0, stdout is
  `{"apiVersion": "v2", "command": ..., "data": ..., "meta": ...}`. `data` is
  Bitbucket's REST v2 object, passed through untouched.
- **Failure:** non-zero exit, stdout is
  `{"ok": false, "error": {"code", "kind", "statusCode", "message", "summary", "remediation": {"command", "env"}}}`.
  Relay `error.summary` and `error.remediation.command`. Don't relay stderr: it's
  the same error rendered for humans.
- **Exit 77 / `AUTH_INVALID`** means Bitbucket auth is missing or invalid. Stop
  and tell the user to run `twg login` and then `twg setup bitbucket` themselves.
  **Never ask for, read, print, store or pass a token** (`TWG_TOKEN`,
  `TWG_BBC_TOKEN`, `TWG_USER`), and never put one on a command line. twg owns
  credentials. `mm doctor` reports whether Bitbucket access is ready.

## gh → twg

| What | gh | twg bb |
|---|---|---|
| Find the PR for a branch | `gh pr list --head B` | `pull-requests query --source B --state OPEN` (one state per call: OPEN, MERGED, DECLINED, SUPERSEDED) |
| Read a PR | `gh pr view N --json ...` | `pull-requests get N` (`data` is the PR) |
| CI on a PR | `gh pr checks N` | `pull-requests get N --statuses` (`data._statuses`) |
| Can it merge / required checks | `gh pr checks --required`, `mergeStateStatus` | `pull-requests mergeability N` |
| Open a PR | `gh pr create` | `pull-requests create --title T --source B --dest BASE --description-file F` |
| Retarget | `gh pr edit N --base X` | `pull-requests update --pull-request N --dest X` |
| Merge | `gh pr merge N --merge` | `pull-requests merge --pull-request N --merge-strategy merge_commit --wait` |
| Default branch | `gh repo view --json defaultBranchRef` | `repo get` → `data.mainbranch.name` |
| Pipelines | `gh run list` | `pipeline query --branch B` |
| Stack link / merge | `gh stack link`, `gh stack merge` | none: merge layer by layer |
| Update branch | `gh pr update-branch N` | none: `git merge` the base in and push normally |
| Raw API | `gh api ...` | none: answer from git (see below) |
| Issues | `gh issue ...` | none: Bitbucket issues are not in twg; use Jira (`twg jira`) |

`scripts/twg_pr_state.py` combines the CI read and the mergeability read into one
verdict:

```bash
uv run .agents/skills/bitbucket-twg/scripts/twg_pr_state.py --repo WORKSPACE/REPO 237
# {"pr": 237, "state": "OPEN", "head": "49f3b57d59e8", "checks": "pass", "failing": [],
#  "pending": [], "blocking": ["minimum_approvals: FAILED"], "mergeable": false}
```

## Where Bitbucket differs

- **Short hashes.** `source.commit.hash` and `merge_commit.hash` are 12-character
  short hashes. Expand one with `git fetch` and `git rev-parse <short>^{commit}`
  before you record or compare it. Never hand mm a short SHA as a merge commit.
- **Statuses span commits.** `_statuses` holds statuses for every commit ever
  pushed to the PR, each with a full 40-character `commit.hash`. Count only those
  whose hash starts with the head's short hash. A red build on an old push means
  nothing.
- **Build states:** `SUCCESSFUL` is pass, `FAILED` and `STOPPED` are fail,
  `INPROGRESS` is pending.
- **Merge checks, not required checks.** `mergeability` returns every check with
  `status` (PASSED, FAILED, PENDING or UNKNOWN), `required` and `blocking`. A PR can
  merge now only when no check has `blocking: true`. `UNKNOWN` means not evaluated,
  never passed. The build gates are `standard_merge_check` with `check.kind`
  `minimum_successful_builds`, `failed_builds` or `in_progress_builds`. Approval
  gates (`minimum_approvals`, `minimum_default_reviewer_approvals`) block too: that
  needs a human reviewer, not another CI run.
- **No native stacks.** A stack merges one layer at a time: merge the bottom,
  retarget the next layer onto the default branch, wait for its CI, repeat. That
  is one CI run per layer. `mm stack merge` does this for you and says so.
- **No update-branch.** To bring a layer up to date, merge its base into it with
  git in a worktree and push normally. Never force-push.
- **No `gh api`.** Answer "does B contain A" from git:
  `git fetch` then `git merge-base --is-ancestor A B`.
- **Merging:** always `--merge-strategy merge_commit --wait`. The repository's
  default strategy may be squash, and a squash rewrites the commits the next layer
  is built on. `--wait` returns either an async task record
  (`{"task_status": "SUCCESS", "merge_result": <PR>}`) or
  `{"ok": true, "status": "merged", "pullRequest": <PR>}`. Read the merged PR from
  whichever one you get.
- **No mergedAt.** A merged PR has no merge timestamp; `updated_on` is the closest.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Exit 77, `AUTH_INVALID` | No Bitbucket token. The user runs `twg setup bitbucket` (and `twg login` for Atlassian). Do not work around it. |
| A YAML block with `output_files:` instead of JSON | `TWG_AGENT_DEFAULTS=1` is in the environment. Pass `--output-summary none` and unset it. |
| `twg: command not found` in a detached process | The installer puts twg in `~/.local/bin`. Set `mm config forge --twg-path ~/.local/bin/twg`. |
| "Missing required context" | No `--repo WORKSPACE/REPO`, and twg could not infer one from the remotes. Pass it. |
| A PR shows `fail` but CI is green | You counted statuses from an older commit. Filter to the head. |
| `mergeable: false` with green checks | A blocking merge check that isn't CI: approvals, unresolved tasks, or your own permission. Read `blocking`. |
| `unknown command` | Your twg is older than the command you used. Run `twg doctor`, then `twg upgrade`. |
