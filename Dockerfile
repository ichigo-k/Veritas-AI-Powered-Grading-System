# syntax=docker/dockerfile:1

FROM python:3.12-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim AS runtime

RUN groupadd -r grader && useradd -r -g grader -d /app -s /sbin/nologin grader

RUN apt-get update \
     && apt-get install -y --no-install-recommends libpq5 curl \
     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=deps /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY manage.py ./
COPY verion_ai_grader/ ./verion_ai_grader/
COPY grader/ ./grader/
COPY auth_keys/ ./auth_keys/
COPY admin_console/ ./admin_console/
COPY deploy/entrypoint.sh ./entrypoint.sh

RUN DJANGO_SECRET_KEY=build-placeholder \
     DATABASE_URL=postgresql://x:x@localhost/x \
     python manage.py collectstatic --noinput 2>/dev/null || true

RUN chmod +x /app/entrypoint.sh \
     && chown -R grader:grader /app

USER grader

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
     CMD curl -sf http://localhost:${PORT:-8000}/api/health/ || exit 1

CMD ["/app/entrypoint.sh"]
