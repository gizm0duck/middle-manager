# Middle Manager control-plane developer targets.
# All Python runs go through uv, respecting versions pinned by pyproject/uv.lock.

.PHONY: help fmt lint test test-live probe clean

help:
	@echo "Targets:"
	@echo "  make fmt        Format with ruff"
	@echo "  make lint       Lint with ruff"
	@echo "  make test       Run hermetic tests (no provider spawning)"
	@echo "  make test-live  Run opt-in live probe tests (spawns Claude/Codex)"
	@echo "  make probe      Run the live interrupt/resume probe matrix and write docs/provider-capabilities.md"

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

test:
	uv run pytest

test-live:
	uv run pytest -m live

# Live probes: spawn real Claude/Codex CLIs in isolated temp repos and record
# a fail-closed capability matrix. Requires authenticated harnesses.
probe:
	uv run python -m middle_manager.probes run

clean:
	rm -rf .venv .pytest_cache **/__pycache__
