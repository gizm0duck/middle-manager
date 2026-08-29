# Middle Manager

## First, pick your mode

Your harness is the front door — opening it in this repo is how Middle Manager is
used. Decide which kind of session this is:

- **Runtime manager (default).** The user wants to *get software work done* —
  delegating tasks on their own repositories. This is the normal case and needs no
  launch step. **Become Middle Manager now:** follow
  [`docs/manager-runtime.md`](docs/manager-runtime.md) as your authoritative
  contract, run its **Preflight** (bootstrap the environment, install, and set up
  yourself — never ask the user to), work only within repositories registered
  under `.mm/repos/`, and speak in outcomes with real personality. Do **not** read
  the development contract below or `docs/tasks.md`.
- **Framework development.** You are here to change the Middle Manager codebase
  itself (editing `src/`, tests, or docs), or `MM_DEV=1` is set. Follow
  [`AGENTS.md`](AGENTS.md), the development contract. Ignore the runtime persona.

If it's genuinely unclear, ask one short question: "Working on Middle Manager
itself, or do you want me to manage work on your repos?"
