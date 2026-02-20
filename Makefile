.PHONY: api worker sandbox dev install lint format test clean

# ── Run ──────────────────────────────────────────────────

api:  ## Start the API server
	uv run uvicorn nexow.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:  ## Start the background worker
	uv run python -m nexow.worker

sandbox:  ## Start the WASM sandbox sidecar
	cd sandbox && pnpm run dev

dev:  ## Start API + worker + sandbox together (Ctrl+C stops all)
	@bash "$(CURDIR)/scripts/dev.sh"

# ── Setup ────────────────────────────────────────────────

install:  ## Install all dependencies
	uv sync

install-dev:  ## Install with dev dependencies
	uv sync --extra dev

env:  ## Create .env from sample
	@test -f .env || cp .env.sample .env && echo ".env created" || echo ".env already exists"

# ── Quality ──────────────────────────────────────────────

lint:  ## Run linter
	uv run ruff check nexow/

format:  ## Auto-format code
	uv run ruff format nexow/

test:  ## Run tests
	uv run pytest -v

# ── Cleanup ──────────────────────────────────────────────

clean:  ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info/

# ── Help ─────────────────────────────────────────────────

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
