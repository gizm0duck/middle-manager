# Middle Manager control-plane developer targets.
# All Python runs go through uv, respecting versions pinned by pyproject/uv.lock.

.PHONY: help hooks fmt lint test test-live probe clean contribution-check

help:
	@echo "Targets:"
	@echo "  make hooks      Enable the checked-in git hooks (gitleaks secret scan and mm hygiene check on commit; needs gitleaks)"
	@echo "  make contribution-check BASE=origin/main  The gate a pull request to mm must pass"
	@echo "  make fmt        Format with ruff"
	@echo "  make lint       Lint with ruff"
	@echo "  make test       Run hermetic tests (no provider spawning)"
	@echo "  make test-live  Run opt-in live probe tests (spawns Claude/Codex)"
	@echo "  make probe      Run the live interrupt/resume probe matrix into .mm/provider-capabilities.json (machine-local)"

hooks:
	git config core.hooksPath .githooks

# The gate every pull request to Middle Manager passes, run by authors before opening
# one and by maintainers before merging (see .agents/skills/improve-middle-manager).
BASE ?= origin/main
contribution-check:
	@command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks is required to contribute (brew install gitleaks)"; exit 1; }
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest --collect-only -q >/dev/null
	uv run pytest
	./bin/mm hygiene check --range $(BASE)..HEAD
	gitleaks git --log-opts="$(BASE)..HEAD" --redact --no-banner
	@echo "contribution-check: passed for $$(git rev-parse --short HEAD) against $(BASE)"

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

test:
	uv run pytest

test-live:
	uv run pytest -m live

# Live probes: spawn real Claude/Codex CLIs in isolated temp repos and record
# a fail-closed capability matrix. Requires authenticated harnesses. Writes the
# machine-local record only; add --publish to refresh the tracked files.
# PYTHONPATH mirrors bin/mm: the package is not installed into the venv, it is
# imported from src/. MM_HOME pins the machine-local record to this checkout.
probe:
	PYTHONPATH=src MM_HOME=$(CURDIR)/.mm uv run python -m middle_manager.probes run $(ARGS)

clean:
	rm -rf .venv .pytest_cache **/__pycache__
