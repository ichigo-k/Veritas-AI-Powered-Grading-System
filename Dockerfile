# syntax=docker/dockerfile:1

# ═══════════════════════════════════════════════════════════════════════════════
# verion-ai-grader — Django AI Grading Microservice
# Multi-stage build with uv for fast dependency installation.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Dependencies ────────────────────────────────────────────────────
FROM python:3.12-slim AS deps

# Install uv (standalone binary — no pip needed)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first — layer is cached unless these change.
COPY pyproject.toml uv.lock ./

# Install production dependencies into a virtual env.
# --frozen ensures the lockfile is respected exactly.
RUN uv sync --frozen --no-dev --no-install-project

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN groupadd -r grader && useradd -r -g grader -d /app -s /sbin/nologin grader

# Runtime system deps (psycopg2-binary needs libpq at runtime)
RUN apt-get update \
     && apt-get install -y --no-install-recommends libpq5 curl \
     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pull in the virtual env from deps stage
COPY --from=deps /app/.venv /app/.venv

# Make sure the venv's bin is on PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy application source
COPY manage.py ./
COPY verion_ai_grader/ ./verion_ai_grader/
COPY grader/ ./grader/
COPY auth_keys/ ./auth_keys/

# Collect static files (Django admin / DRF browsable API — harmless for API-only)
# Provide a dummy secret key just for collectstatic — the real one comes at runtime.
RUN DJANGO_SECRET_KEY=build-placeholder \
     DATABASE_URL=postgresql://x:x@localhost/x \
     python manage.py collectstatic --noinput 2>/dev/null || true

# Switch to non-root
RUN chown -R grader:grader /app
USER grader

# Expose the application port
EXPOSE 8000

# Health check — hits the unauthenticated health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
     CMD curl -sf http://localhost:8000/api/health/ || exit 1

# Run with gunicorn.
# Workers default to 4; override via GUNICORN_WORKERS env var.
# Timeout is generous (300s) because Bedrock / Ollama calls can be slow.
CMD sh -c "gunicorn verion_ai_grader.wsgi:application \
     --bind 0.0.0.0:8000 \
     --workers ${GUNICORN_WORKERS:-4} \
     --timeout ${GUNICORN_TIMEOUT:-300} \
     --graceful-timeout 30 \
     --keep-alive 5 \
     --max-requests 1000 \
     --max-requests-jitter 50 \
     --access-logfile - \
     --error-logfile -"
                                                            