UV ?= uv
OWNER_DSN ?= postgresql://agrocredit_owner:dev_only_owner@localhost:5433/agrocredit_db
ETL_DSN ?= postgresql://agrocredit_etl:dev_only_etl@localhost:5433/agrocredit_db

.PHONY: setup db-up db-down db-reset migrate test test-unit lint typecheck security check pipeline run token \
	chaves-dev up tools fontes

setup:  ## Instala dependências (inclui pipeline)
	$(UV) sync --all-extras

db-up:  ## Sobe o PostGIS local (porta 5433)
	docker compose up -d --wait db

db-down:
	docker compose down

db-reset:  ## Apaga o volume e recria o banco do zero
	docker compose down -v
	docker compose up -d --wait db
	$(MAKE) migrate

migrate:
	MIGRATIONS_DATABASE_URL=$(OWNER_DSN) $(UV) run python -m agrocredit.infrastructure.db.migrate

pipeline:  ## Carrega todas as fontes automatizáveis (SICAR primeiro: as camadas usam seus envelopes)
	for f in sicar ibama embargos_mg mte cnuc cnfp quilombolas prodes ti rfb; do ETL_DATABASE_URL=$(ETL_DSN) $(UV) run python -m agrocredit.pipeline run $$f || exit 1; done

run:  ## Sobe a API local na porta 8000 (chaves de desenvolvimento em .secrets/)
	@[ -f .secrets/jwks.json ] || $(UV) run python -m agrocredit.core.dev_keys gerar --dir .secrets
	AGROCREDIT_DATABASE_URL=postgresql://agrocredit_api:dev_only_api@localhost:5433/agrocredit_db \
	AGROCREDIT_HMAC_PEPPER=dev_only_pepper_nao_usar_em_producao_0123456789 \
	AGROCREDIT_JWT_JWKS="$$(cat .secrets/jwks.json)" \
	AGROCREDIT_JWT_ISSUER=https://auth.agrocredit.local AGROCREDIT_JWT_AUDIENCE=agrocredit-api \
	$(UV) run uvicorn --factory agrocredit.presentation.api.app:create_app --port 8000

chaves-dev:  ## Gera o par de chaves JWT de desenvolvimento em .secrets/
	@[ -f .secrets/jwks.json ] || $(UV) run python -m agrocredit.core.dev_keys gerar --dir .secrets

up: chaves-dev  ## Sobe banco e API em containers (http://localhost:8000)
	docker compose up -d --build --wait db api

tools:  ## Roda um módulo na imagem de ferramentas: make tools CMD="agrocredit.pipeline run ibama"
	HOST_UID=$$(id -u) HOST_GID=$$(id -g) docker compose run --rm tools $(CMD)

fontes:  ## Defasagem das bases (saída 0 OK, 1 alerta P2, 2 crítico P1)
	ETL_DATABASE_URL=$(ETL_DSN) $(UV) run python -m agrocredit.manutencao fontes

token:  ## Emite token de desenvolvimento: make token TENANT=coop-demo
	@$(UV) run python -m agrocredit.core.dev_keys token --dir .secrets --tenant $(TENANT)

test:  ## Todos os testes (integração exige db-up)
	$(UV) run pytest

test-unit:  ## Só testes sem banco
	$(UV) run pytest -m "not integration"

lint:
	$(UV) run ruff format --check src tests
	$(UV) run ruff check src tests

typecheck:
	$(UV) run mypy

security:
	$(UV) run bandit -q -r src -ll
	$(UV) run pip-audit

check: lint typecheck security test  ## Gates da SPEC §7.2
