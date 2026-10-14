# Duas imagens (SPEC-001 §7.1: multi-stage, mínima, usuário não-root, sem .env):
#   docker build --target api   -t agrocredit-api .     (padrão)
#   docker build --target tools -t agrocredit-tools .   pipeline + manutenção + migrations

FROM python:3.12-slim-bookworm AS base-build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./

FROM base-build AS build-api
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM base-build AS build-tools
RUN uv sync --frozen --no-dev --all-extras --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --all-extras --no-editable

FROM python:3.12-slim-bookworm AS runtime
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agrocredit
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH \
    AGROCREDIT_RULESET=/app/rules/mcr-2-9.yaml
COPY rules ./rules

FROM runtime AS tools
# pdftotext (poppler-utils) para o PDF do MTE
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build-tools /app/.venv /app/.venv
COPY migrations ./migrations
ENV AGROCREDIT_DATA=/app/data AGROCREDIT_MIGRATIONS=/app/migrations
USER 10001
ENTRYPOINT ["python", "-m"]
CMD ["agrocredit.manutencao", "--help"]

FROM runtime AS api
COPY --from=build-api /app/.venv /app/.venv
USER 10001
EXPOSE 8000
# Segredos (AGROCREDIT_DATABASE_URL, AGROCREDIT_HMAC_PEPPER, AGROCREDIT_JWT_*) vêm do orquestrador
CMD ["uvicorn", "--factory", "agrocredit.presentation.api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
