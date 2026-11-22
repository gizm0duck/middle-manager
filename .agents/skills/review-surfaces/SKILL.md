---
name: review-surfaces
description: >-
  How Middle Manager gives the user a visual review surface (plans, diffs,
  comparisons, connected decisions) and collects their feedback without freezing
  the conversation. Use for every alignment the `align-with-the-user` skill sends
  here (a plan for review, or more than two things to align on), and whenever you
  want to show the user something richer than prose and get annotations back. Mechanics live in the bundled `lavish-review` script and
  in `mm`; this skill owns the judgment — when to use a surface and how to run the
  loop.
user-invocable: false
---

# review-surfaces

A plan for the user to review, or more than two things to align on, always goes on
a review surface: the `align-with-the-user` skill owns that rule and the shape of
an alignment surface, and this skill owns the mechanics. Beyond that, use a surface
whenever a comparison or set of connected decisions is easier to *see* than to
read. Either way, put it on the surface and hand control back to the user. The cardinal
rule: **never hold your turn open waiting for their feedback.** A review is a
hand-off — you open the surface, step back, and pick up their notes later.

## Pick a surface

- **Rich (preferred when available): `lavish-axi`.** An interactive HTML surface the
  user can annotate. Drive it with the bundled script, never by calling
  `lavish-axi poll` yourself (that long-polls and will freeze your turn):

  ```bash
  scripts/lavish-review new   <file> --title "…"   # scaffold from the shipped default (below)
  scripts/lavish-review open  <file>   # opens the surface + backgrounds the poller
  # ... end your turn; let the user annotate ...
  scripts/lavish-review drain <file>   # read whatever they've left (non-blocking)
  scripts/lavish-review stop  <file>   # when you're done with the surface
  ```

  Author the HTML yourself. Learn `lavish-axi`'s current authoring surface live
  (`lavish-axi --help`, `lavish-axi playbook <id>` — `input` for anything that
  collects decisions) — do not assume flags. Advanced authoring is fine; just never
  foreground the poll.

## Pick the design

Every surface uses the installation's design unless the user asks for another
look. Say which one you used when you hand the surface over.

1. **The user named a look or a design system for this surface** — use that.
2. **Otherwise, the installation's design: `lavish-review new`.** By default that
   is Middle Manager's house design, the same for every installation: one
   self-contained file with the house stylesheet (`assets/ads.css`, an
   Atlassian-style system: ADS-named tokens, light and dark, lozenges, section
   messages, dynamic tables, stat tiles, cards, native-control forms, disclosures)
   inlined into the house layout (`assets/surface.html`: header → key numbers →
   verdict → one card per decision with its own *Queue answer* form → evidence
   table → folded reference). Replace the placeholder content; keep the classes.
   An installation that wants its own look puts it in `<MM_HOME>/review-surfaces/`:
   `style.css` is appended after the house stylesheet (restyle the tokens, or
   replace any rule), and `surface.html` replaces the house layout (it places the
   stylesheet with `/*__ADS_CSS__*/` and the title with `__TITLE__`). The script
   picks them up and says which design it used.
3. **A mock of a specific product's UI** — a proposed screen, a before and after —
   renders *that product's* design system (its theme config, CSS tokens, component
   library, existing styled pages), shown inside the installation-styled surface.
   The surface around it stays in the installation's design.

Never fall back to the Tailwind/DaisyUI CDN snippet that `lavish-axi design`
suggests: the house design exists so surfaces from this manager look like one
product, render offline, and never depend on a CDN.

  Whichever source: no horizontal overflow at any nesting level (tables go inside
  `.ads-table-wrap`), native controls for choices, one queued prompt per decision
  from the form's submit — never from a radio change — and a clear *Send to Agent*
  path at the end.

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

If the browser says the agent is not listening, the background poller died:
`lavish-review open <file>` again re-arms it. When your harness offers a *tracked*
background job that wakes you on completion, prefer it over the sidecar poller and
arm `lavish-axi poll <file>` through that facility — the user's answer then
interrupts you rather than waiting for your next turn. Use the wrapper's sidecar
poller only when no completion-aware background facility exists. In either case,
hand the turn back immediately; never hold it open waiting for feedback.

## Write for someone who has not read the source

The surface is read by the user, not by you. Every card, row, badge, and option
must say what the thing *is* — "the plan step that adds per-item summaries and
links (second backend PR)", "the section of the plan on continuity" — never a bare
label like "1B", "§6.3", "rev3", "informs 1B/1D". A label may appear once, in
parentheses, after the description. Badges are the worst offender: a lozenge that
reads "gates PR 1B" is noise; "must be settled before the summaries-and-links PR
starts" is information. Read the finished surface as someone who has never opened
the plan; anything they would have to look up gets rewritten.

## Boundaries

- **Plumbing is the script's job, not yours.** Don't hand-roll `lavish-axi poll`,
  `&`, `nohup`, or pidfiles inline — call `lavish-review`.
- **A frozen prompt is never acceptable.** The only thing you legitimately block on
  is the runtime's own `mm wait`, which returns on the next actionable event.
- Keep the machinery backstage in what you tell the user: "I've put the plan up for
  you to mark up" — not the command you ran.
