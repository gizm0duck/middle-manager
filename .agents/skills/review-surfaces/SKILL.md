---
name: review-surfaces
description: >-
  How Middle Manager gives the user a visual review surface (plans, diffs,
  comparisons, connected decisions) and collects their feedback without freezing
  the conversation. Use when you want to show the user something richer than prose
  and get annotations back. Mechanics live in the bundled `lavish-review` script and
  in `mm`; this skill owns the judgment — when to use a surface and how to run the
  loop.
user-invocable: false
---

# review-surfaces

When a plan, comparison, or set of connected decisions is easier to *see* than to
read, put it on a review surface and hand control back to the user. The cardinal
rule: **never hold your turn open waiting for their feedback.** A review is a
hand-off — you open the surface, step back, and pick up their notes later.

## Pick a surface

- **Rich (preferred when available): `lavish-axi`.** An interactive HTML surface the
  user can annotate. Drive it with the bundled script, never by calling
  `lavish-axi poll` yourself (that long-polls and will freeze your turn):

  ```bash
  scripts/lavish-review open  <file>   # opens the surface + backgrounds the poller
  # ... end your turn; let the user annotate ...
  scripts/lavish-review drain <file>   # read whatever they've left (non-blocking)
  scripts/lavish-review stop  <file>   # when you're done with the surface
  ```

  Author the HTML yourself first. Learn `lavish-axi`'s current authoring surface
  live (`lavish-axi --help`, `lavish-axi design`, `lavish-axi playbook <id>`) — do
  not assume flags. Advanced authoring is fine; just never foreground the poll.

- **Local fallback (always available): `mm`.** If `lavish-axi`/Node is missing, use
  the built-in surface: `mm artifact <run_id> --title ... --sections '[...]'` to
  write it, then `mm feedback <artifact_id>` to read the sidecar. Same shape:
  produce, hand back, drain later.

## The loop

1. Produce the surface (Lavish HTML, or `mm artifact`).
2. `lavish-review open <file>` (or just tell the user where the artifact is).
3. **Return to the user in one line** — "It's up; annotate it and tell me when
   you're ready." Then end your turn. Do not block.
4. When they say they've left notes (or on your next natural turn), `drain` /
   `mm feedback` to collect annotations, act on them, and report the outcome.
5. `stop` the poller when the review is finished.

## Boundaries

- **Plumbing is the script's job, not yours.** Don't hand-roll `lavish-axi poll`,
  `&`, `nohup`, or pidfiles inline — call `lavish-review`.
- **A frozen prompt is never acceptable.** The only thing you legitimately block on
  is the runtime's own `mm wait`, which returns on the next actionable event.
- Keep the machinery backstage in what you tell the user: "I've put the plan up for
  you to mark up" — not the command you ran.
