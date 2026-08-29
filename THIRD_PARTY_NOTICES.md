# Third-party notices

Middle Manager provisions the following companion tools as pinned, locally
managed dependencies. Their source trees are not vendored into this repository;
they are provisioned into the gitignored `.mm/tools/` directory by `mm setup`.
Upstream license texts and notices will be committed here once versions are
pinned in [`tools.lock`](tools.lock) (see milestone M2/M4).

- **Treehouse** — durable, identity-bound Git worktree leases. License: MIT.
- **gh-axi** — token-efficient GitHub operations for agents. License: MIT.
- **lavish-axi** — local HTML review surfaces and structured feedback. License: MIT.

The runtime Python control plane itself has zero third-party package
dependencies. Development uses Ruff and pytest (see `pyproject.toml`).
