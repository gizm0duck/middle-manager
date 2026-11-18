# Bitbucket / twg fixtures

Captured 2026-11-17 from Bitbucket Cloud's REST API v2 for the public repository
`atlassian/forge-bitbucket-related-prs` (anonymous reads), then trimmed to the
fields mm reads. These are real responses, not guesses.

twg 1.4.5 passes Bitbucket's objects through untouched inside its JSON envelope
(`{apiVersion, command, data, meta}`), so each file is the REST payload wrapped
the way `twg --output json --output-summary none bb ...` prints it:

- `query_*.json`: `pull-requests query`. `data` is a list of pull requests.
- `get_<id>_statuses.json`: `pull-requests get <id> --statuses`. `data` is the
  pull request plus `_statuses` (Bitbucket commit-status records, full 40-char
  `commit.hash`).
- `mergeability_<id>.json`: `pull-requests mergeability <id>`. `data` is the list
  of merge checks. `current_user_permission_check` fails only because the
  capture was anonymous.
- `repo_get.json`: `repo get`. `data.mainbranch.name` is the default branch.

PR 237 is open; PR 244 is merged. Both PRs' `source.commit.hash` and
`merge_commit.hash` values are the 12-character short hashes Bitbucket returns.
