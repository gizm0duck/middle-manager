---
name: improve-middle-manager
description: >-
  The conventions a change to Middle Manager itself must meet before its pull
  request is opened, and the same list a maintainer applies before merging one:
  gitleaks installed and the commit hooks on, no private information from the
  contributor's installation (repositories, people, companies, hosts, home paths),
  one focused outcome per pull request on current main, commit titles that state
  the outcome with a body that says why, tests for every behavior change, the
  safety model untouched, docs and skills kept true, and `make contribution-check`
  green. Use whenever you change mm's code, docs, skills or hooks for a pull
  request, file a GitHub issue against mm (including a performance review's action
  items), when the user asks to "contribute", "upstream", "open a PR against mm"
  or "improve mm", and when reviewing someone else's mm pull request or issue. A pull
  request that fails any rule here is rejected, not merged with fixes promised.
user-invocable: true
---

# improve-middle-manager

Middle Manager is public. The people improving it run it on private code, so every
contribution starts in an installation full of names that must not leave it. A pull
request is accepted only when it meets every rule below. Maintainers apply this same
list when deciding whether to merge, and a pull request that misses one is rejected,
however good the idea. The contributor fixes it and opens it again. Nobody merges on
a promise to fix it later.

Read [`AGENTS.md`](../../../AGENTS.md) (the development contract) first. This skill
is the gate on top of it, not a replacement.

## 1. Before the first commit

- **gitleaks is installed** (`brew install gitleaks`, or its install guide). The
  `pre-commit` hook refuses every commit without it.
- **The hooks are on.** `git config --get core.hooksPath` prints `.githooks`.
  `./bin/install` and `make hooks` set it. The hooks run gitleaks and
  `mm hygiene check` on every commit's changes and message.
- **Never `git commit --no-verify`.** A refusal means the change is wrong. Fix the
  change. A gitleaks false positive goes in `.gitleaks.toml` with a comment saying
  why it is safe. A line that must keep a hygiene match carries `hygiene: allow`,
  and the pull request says why.
- **Start from current main** in a branch of your own. Don't build on unmerged
  local commits that hold private context.

## 2. Nothing private leaves the installation

The hygiene check knows the installation's registered repositories, their owners
and non-public hosts, the git user, the home directory, and the terms in
`<MM_HOME>/hygiene/private-terms.txt`. It can't know everything. Add your
company, product, team and ticket-prefix names to that file before you start.
Then hold every line you write to the rule:

- No name of a repository you manage, its owner or workspace, or its host.
- No company, product, team, channel or ticket name. No person, email or account.
- No local path, hostname, IP, internal URL, log excerpt, or command output copied
  from your installation.
- Describe what happened instead: "a large monorepo", "a forge with tens of
  thousands of refs", "a repository that tracks a file named FETCH_HEAD". Dates
  and measurements are welcome ("on 2026-11-20 a one-commit push uploaded
  123 MB"). They are evidence, and they name nothing.
- Fixtures use invented names (`acmecorp`, `secret-billing-service`) and
  reserved domains (`example.com`, `.test`).

This covers code, comments, docstrings, tests, docs, skills, commit messages, the
pull request title and body, review replies, and GitHub issues, including the
action items a performance review turns into issues. Text that never goes through
a commit hook is checked by hand before it's posted:

```bash
./bin/mm hygiene check --text-file pr-body.md --text-file issue.md
```

## 3. One outcome per pull request

- A pull request delivers one outcome a user would notice, with the tests and docs
  it needs. An unrelated cleanup is its own pull request.
- Related changes that must land in order are a stack, as the runtime contract's
  "Stacked pull requests" section describes. They aren't one pull request mixing
  several outcomes.
- Squash local work-in-progress into commits a reviewer can read. Each commit
  builds and passes on its own. Once a commit is pushed, don't amend or force-push
  it. Add a new commit instead.

## 4. Commit messages and the pull request body

- **The title states the outcome that is now true**, in plain English and sentence
  case: "Pushes and deliveries from shallow clones stay small and fast", not
  "fix push", "feat: shallow" or a ticket key.
- **The body says why, with evidence:** what went wrong or was missing, how you
  know (a measurement, a reproduction, a probe of the real tool), what changed, and
  what deliberately stays the same. Wrap at about 80 columns.
- **Agent-written commits carry the co-author trailer** the harness gives you.
- **The pull request body** has: Why, What changed, Verification (the exact
  commands and their results), and, when anything is unproven, Not verified. No
  private information (section 2).

## 5. Tests prove behavior

- Every behavior change has a test that fails without it. A bug fix has a test
  that reproduces the bug.
- Where git or the filesystem is the point, tests use real git in temp dirs, the
  way `tests/test_stack_composition.py` does. They never use the network, a provider,
  or your installation's state; `MM_HOME` points at a temp dir (the `mm_home`
  fixture).
- A test never writes outside pytest's `tmp_path`.
- Adding or renaming a test module: `uv run pytest --collect-only -q` shows zero
  errors.
- A flaky test is reported with both runs, never deleted or loosened to pass.

## 6. The safety model is not the thing being optimized

Authority is enforced in code: model and spend ceilings, the exact-HEAD review gate,
the scope fence around `.mm/repos/`, merge authority, and the hooks themselves. A
pull request that weakens, bypasses or makes any of these optional is rejected
unless the user who owns that decision asked for exactly that, and its body says
so in its first paragraph. "Faster" or "fewer prompts" is never the reason.

## 7. Keep the contracts true

- Behavior an agent relies on changes in the same pull request as its doc:
  [`docs/manager-runtime.md`](../../../docs/manager-runtime.md) for the runtime
  manager, `AGENTS.md` for development, the skill that teaches it, and `README.md`
  for anything a user sees.
- A new skill lives in `.agents/skills/<name>/SKILL.md`, with the relative symlink
  `.claude/skills/<name> -> ../../.agents/skills/<name>`, and frontmatter with a
  `name` and a `description` that says when to use it.
- No trackers, journals or review sidecars in the repository. The record of a
  change is its commits and its pull request.
- Python goes through `uv`, and dependency changes update `uv.lock` (with
  `uv lock`). Companion tools are pinned in `tools.lock`.

## 8. The mechanical gate

```bash
make contribution-check BASE=origin/main
```

This runs ruff lint and the format check, the test collection check, the test
files the branch adds or changes, `mm hygiene check` over every commit and message
since `BASE`, and gitleaks over the same commits. It must pass on the exact head
you push. Paste the summary into Verification.

The whole suite is slow, and one of its tests is load-sensitive, so it isn't the
default. When a change reaches past the files its tests cover (a shared helper,
the supervisor, the state schema), name the other test files that exercise it with
`TESTS="tests/a.py tests/b.py"`, or run `TESTS=all`, and say which you ran.

## Reviewing someone else's pull request

Check out the pull request's head and run `make contribution-check
BASE=<merge-base>`. Then read the diff against sections 2 to 7. **Reject** (request
changes, don't merge) when any of these holds, and name the rule:

1. `make contribution-check` fails, or the hooks were skipped: a commit is
   unscanned, or a `hygiene: allow` has no stated reason.
2. Any private information, even one name in a test fixture, commit message, or
   the pull request's own description. The
   contributor rewrites it before the pull request is reopened. Don't merge it and
   scrub it afterward, because a merge publishes it.
3. More than one outcome, or unrelated churn.
4. A commit title that isn't an outcome sentence, or a body with no why.
5. A behavior change without a test, or a test that touches the network, a provider
   or real installation state.
6. A weakened safety boundary without the owner's explicit request.
7. A behavior change whose runtime contract, development contract, skill or README
   is now wrong.

An issue that carries private information is edited to remove it, or closed and
filed again without it, as soon as it's seen. Don't wait for its author. It is
already public.

Approve only when none of these holds. Thank the contributor and say what was good.
When you reject, be just as specific: name the rule, the file and line, and the
smallest change that would meet it.
