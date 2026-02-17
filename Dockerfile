FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Minimal OS deps for TLS + building wheels when needed
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl build-essential \
  && rm -rf /var/lib/apt/lists/*

# Install uv (project uses uv.lock)
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

# Install dependencies into a local venv at /app/.venv
RUN uv sync --frozen --no-dev

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Copy source
COPY server ./server
COPY supabase ./supabase

# Ensure "nexow" import path exists (repo uses symlink locally)
RUN ln -s server nexow

EXPOSE 8000

# Default to running the API; compose overrides for the worker.
CMD ["uvicorn", "nexow.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
