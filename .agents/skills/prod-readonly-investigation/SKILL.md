---
name: prod-readonly-investigation
description: >-
  How to answer a question from production data without relitigating access
  every time: use the repo's sanctioned read-only path (logs, the telemetry
  store, a read-only investigation job), never a primary database credential,
  never a hand-built tunnel, and know up front which tables the read-only role
  can see and which settings rule lets you run it. Use whenever the user asks
  what production is doing, which workspace/tenant/record something is, or to
  "check prod", "look at the logs", "query the database" or "find out why".
user-invocable: true
---

# prod-readonly-investigation

Production questions keep costing a round of "which credential, which host, am I
allowed". The answer is written down per repository; this skill is the order to use
it in. (2026-09-24: an hour went to a private-IP DSN, a missing proxy and three
permission denials before the sanctioned read-only job turned up.)

## 0. Read the repo's investigation notes first

`.mm/memory/repos/<repo>/notes.md`, section **Production investigation**. It names:
the cloud project, the log source, the telemetry project, the read-only job (or
replica) and its role, the tables that role can and cannot read, and the exact
permission rule the harness needs. If the section is missing, finding those facts
is the first deliverable; write them there before answering.

## 1. Cheapest source that can answer, in this order

1. **Telemetry** (Logfire or equivalent) for traces, spans, call volumes, latency.
   Remember exports can be dropped (quota, sampling): a missing trace is not proof
   nothing happened; check the service logs before concluding.
2. **Service logs** (`gcloud logging read …`) for requests, status codes, errors,
   and anything the app logs but does not trace.
3. **The read-only investigation job** for database rows. Never the service's own
   database URL, never a primary or admin credential, never a new tunnel, public IP
   or proxy against a private database.

## 2. Running the read-only job

- Override only the job's arguments; keep its image, role and network. Start every
  script with `set default_transaction_read_only = on` and run `SELECT`s only, with
  a `LIMIT`. Print JSON lines, one per query, and never print a credential or a
  connection string.
- Put the script in the session scratchpad, run it with `--wait`, then read its
  output from the execution's logs.
- If the role cannot read a table (for example people or membership tables), that
  is a privacy boundary, not a bug: answer from what it can read, say what it could
  not, and name who can look it up (an admin view, the owner). Do not look for
  another credential that can.

## 3. When the harness blocks a production read

Auto mode refuses production reads, secret access and job execution that no settings
rule allows, and it refuses edits to the harness's own permission settings even when
the user asks. Do not retry the same call or route around it. Tell the user once,
precisely:
- the exact allow rule (from the notes), for `/permissions` or
  `.claude/settings.local.json` `permissions.allow`;
- what the call reads and that it is read-only;
- that you will continue as soon as it is added.
Then do the rest of the task that does not depend on it.

## 4. Report

What you queried (source, window, filters), the numbers, what the sources could not
show and why, and the one decision the finding puts to the user. Update the notes
section if anything about the path changed.
