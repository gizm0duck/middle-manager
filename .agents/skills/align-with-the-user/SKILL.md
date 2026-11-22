---
name: align-with-the-user
description: >-
  When and how to get the user's decisions so it happens the same way every time:
  a plan for them to review, or more than two things to align on, goes on a Lavish
  review surface they can annotate and answer, never a list of questions in the
  chat. Use whenever you are about to ask the user to approve, choose, rank, scope,
  or sign off on something, when you have put together a plan or proposal for
  review, and when the user asks to "align", "review the plan", "walk me through
  the options" or "what do you need from me".
user-invocable: true
---

# align-with-the-user

Alignment is the moment the user's judgment enters the work. It should look the
same every time, so the user always knows where to answer and nothing they say
gets lost in scrollback.

## The rule

Count the things you need from the user: each decision, open question, choice
between options, or plan they must approve is one thing.

| What you have | Where it goes |
| --- | --- |
| A plan or proposal for the user to review, of any size | A Lavish review surface |
| More than two things to align on | A Lavish review surface |
| One or two questions with clear options | The chat, each question standing on its own |
| A status report, an outcome, a finished answer | The chat |

Don't split a larger set across several chat turns to stay under the line. When
the count is borderline, use the surface.

Lavish beats any page or artifact tool the harness offers for this, because the
user's answers come back into the session (`lavish-review drain`) instead of
staying in a browser tab.

## Before you build it

- **Decide what is yours to decide.** Only put a question on the surface when the
  answer is genuinely the user's: irreversible, outward-facing, a product or scope
  call, or something you could not resolve from the code, the docs or durable
  memory. Decide the rest yourself and list it on the surface under "Decided — tell
  me if any of these is wrong", so the user can still overturn it.
- **Recommend.** Every decision carries your recommendation and the reason, with
  the recommended option first and preselected. The user should be able to agree
  with everything in one pass.
- **Describe, don't cite.** Say what each thing is. Never use a bare label like
  "1B", "§5" or "rev3" (the `review-surfaces` skill has the full rule).

## Build and hand off

Load the `review-surfaces` skill and follow it for the mechanics. In short:

1. `lavish-review new <file> --title "…"` scaffolds the surface in the
   installation's design.
2. Fill it in this order: what you need from the user and by when, the
   recommendation in a sentence, one card per decision (what it is, the options,
   your pick and why, what it unblocks, a *Queue answer* form), then the decided
   list, then the evidence, folded.
3. `lavish-review open <file>`, then tell the user in one line that it's up, with
   its URL, and end your turn. Never hold the turn open for the answer.
4. On their next message, or when they say they've answered, `lavish-review drain
   <file>`, act on every answer, and report what you did. Don't ask again in the
   chat what is already on the surface.
5. `lavish-review stop <file>` once the alignment is settled.

## When Lavish isn't there

If `lavish-axi` is missing, use mm's local surface (`mm artifact` and
`mm feedback`). If neither works, ask in the chat as a numbered list, each item
standing on its own with your recommendation, and say why the surface wasn't
available so the cause gets fixed.
