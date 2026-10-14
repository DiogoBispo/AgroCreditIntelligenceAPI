import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from agrocredit.infrastructure.db.migrate import apply_migrations
from agrocredit.pipeline import gold

# Banco próprio dos testes, recriado a cada sessão: os testes nunca dependem dos dados reais carregados
# no banco de desenvolvimento (nem os alteram), e o CI parte de um servidor vazio.
ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://agrocredit_owner:dev_only_owner@localhost:5433/agrocredit_db"
)
BANCO_TESTE = "agrocredit_test"


def _dsn(usuario: str | None = None, senha: str | None = None) -> str:
    u = urlsplit(ADMIN_DSN)
    netloc = u.netloc if usuario is None else f"{usuario}:{senha}@{u.hostname}:{u.port or 5432}"
    return urlunsplit((u.scheme, netloc, f"/{BANCO_TESTE}", u.query, u.fragment))


OWNER_DSN = _dsn()
API_DSN = _dsn("agrocredit_api", os.environ.get("API_DB_PASSWORD", "dev_only_api"))
ETL_DSN = _dsn("agrocredit_etl", os.environ.get("ETL_DB_PASSWORD", "dev_only_etl"))
MAINT_DSN = _dsn("agrocredit_maint", os.environ.get("MAINT_DB_PASSWORD", "dev_only_maint"))


@pytest.fixture(scope="session")
async def migrated() -> str:
    try:
        admin = await asyncpg.connect(ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostGIS indisponível ({exc}); rode `make db-up`")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {BANCO_TESTE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {BANCO_TESTE}")
    finally:
        await admin.close()
    await apply_migrations(OWNER_DSN)
    return OWNER_DSN


@pytest.fixture
async def conn(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """Conexão do dono dentro de uma transação desfeita ao fim de cada teste."""
    c = await asyncpg.connect(migrated)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def limpar_bases(owner_dsn: str) -> None:
    """Remove todas as versões publicadas, tenants e quarentena (o banco de testes é isolado)."""
    conn = await asyncpg.connect(owner_dsn)
    try:
        versoes = await conn.fetch(
            "SELECT base, fonte, versao FROM core_geo.dataset_versions WHERE base IS NOT NULL"
        )
        await conn.execute("DELETE FROM core_geo.dataset_versions")
        await conn.execute("DELETE FROM raw_ingest.quarantine_records")
        await conn.execute("DELETE FROM raw_ingest.sources_manifest")
        await conn.execute("DELETE FROM core_geo.tenant_config")
        await conn.execute("SET ROLE role_etl")
        for base in sorted(gold.BASES):
            await gold._recriar_view(conn, base)
        for v in versoes:
            await conn.execute(
                f"DROP TABLE IF EXISTS {gold.tabela_versao(v['base'], v['fonte'], v['versao'])}"
            )
    finally:
        await conn.close()
