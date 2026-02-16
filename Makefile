.PHONY: api worker executor dev install lint format test clean

# ── Run ──────────────────────────────────────────────────

api:  ## Start the API server
	uv run uvicorn nexow.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:  ## Start the background worker
	uv run python -m nexow.worker

executor:  ## Start the WASM executor sidecar
	cd ../nexow-executor && npm run dev

dev:  ## Start API + worker + executor together (Ctrl+C stops all)
	@trap 'kill 0' EXIT; \
	uv run uvicorn nexow.api.app:app --host 0.0.0.0 --port 8000 --reload & \
	uv run python -m nexow.worker & \
	(cd ../nexow-executor && npm run dev) & \
	wait

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
