FROM python:3.12.14-slim-bookworm AS base
ARG GIT_REVISION=local
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PM_DATA_DIR=/data PM_GIT_REVISION=${GIT_REVISION}
WORKDIR /app
RUN pip install --no-cache-dir uv==0.8.22
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home pm && mkdir /data && chown pm:pm /data
USER pm
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3)"]
ENTRYPOINT ["/app/.venv/bin/pm-nautilus"]
CMD ["--host", "0.0.0.0"]
FROM base AS verify
USER root
COPY tests ./tests
RUN uv sync --frozen --extra dev
RUN .venv/bin/ruff check src tests && .venv/bin/pytest -q
USER pm
