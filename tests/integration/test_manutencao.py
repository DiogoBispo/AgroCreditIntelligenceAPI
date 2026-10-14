"""Tarefas de manutenção contra o PostGIS real, com os papéis de produção."""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import polars as pl
import pytest
import shapely

from agrocredit.core.security import hash_documento
from agrocredit.domain.validators import parse_document
from agrocredit.manutencao import tarefas
from agrocredit.pipeline import gold
from tests.integration.conftest import ETL_DSN, MAINT_DSN, limpar_bases

pytestmark = pytest.mark.integration


async def _conectar(dsn: str, papel: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn)
    await conn.execute(f"SET ROLE {papel}")
    return conn


@pytest.fixture
async def limpo(migrated: str) -> AsyncIterator[str]:
    await limpar_bases(migrated)
    yield migrated
    await limpar_bases(migrated)


async def test_verificar_fontes_classifica_niveis(migrated: str) -> None:
    conn = await _conectar(ETL_DSN, "role_etl")
    try:
        situacoes = {s.fonte: s for s in await tarefas.verificar_fontes(conn, hoje=date(2026, 10, 14))}
    finally:
        await conn.close()
    if "CNUC" in situacoes:  # base de ago/2025 com limite de 240 dias
        assert situacoes["CNUC"].nivel == "P1"
    assert all(s.nivel in {"OK", "P2", "P1"} for s in situacoes.values())
    assert not any(f.startswith("TESTE") for f in situacoes)


async def test_expurgo_exporta_confere_e_remove(migrated: str, tmp_path: Path) -> None:
    maint = await _conectar(MAINT_DSN, "role_maint")
    owner = await asyncpg.connect(migrated)
    try:
        await maint.execute("SELECT audit.criar_particoes('2019-01-01', 1)")
        await owner.execute(
            "INSERT INTO audit.credit_analysis_log (executed_at, request_id, client_tenant_id, document_hash,"
            " hmac_key_version, document_masked, car_code, ruleset_version, dataset_versions,"
            " tolerance_applied, rule_results, credit_status)"
            " VALUES ('2019-01-15', 'r', 'teste-expurgo', repeat('a', 64), 1,"
            " '***', 'GO-X', 'v', '{}', '{}', '[]', 'INCONCLUSIVO')"
        )
        antes = await owner.fetchval("SELECT count(*) FROM audit.credit_analysis_log_2019_01")
        feitas = await tarefas.expurgar_auditoria(maint, tmp_path, hoje=date(2026, 10, 14))
        assert [f.particao for f in feitas] == ["credit_analysis_log_2019_01"]
        df = pl.read_parquet(feitas[0].arquivo)
        assert df.height == antes and "teste-expurgo" in df["client_tenant_id"].to_list()
        assert (
            (tmp_path / "credit_analysis_log_2019_01.parquet.sha256").read_text().startswith(feitas[0].sha256)
        )
        assert await owner.fetchval("SELECT to_regclass('audit.credit_analysis_log_2019_01')") is None
        # Partições dentro da retenção ficam
        assert await owner.fetchval("SELECT to_regclass('audit.credit_analysis_log_2026_10')") is not None
    finally:
        await maint.close()
        await owner.close()


async def _publicar(fonte: str, versao: str, n: int) -> None:
    poligono = shapely.MultiPolygon([shapely.box(-31.0, -20.0, -30.99, -19.99)])
    ewkb = bytes(shapely.to_wkb(shapely.set_srid(poligono, 4674), include_srid=True))
    registros = [("TESTE", f"{versao}-{i}", "VIGENTE", "POLIGONO", ewkb) for i in range(n)]
    carga = gold.Carga(
        "embargos",
        fonte,
        versao,
        date(2026, 10, 1),
        ("orgao", "id_embargo", "situacao", "tipo_geom_origem", "geometria"),
        registros,
        [],
    )
    await gold.carregar(ETL_DSN, carga)
    await gold.publicar(ETL_DSN, "embargos", fonte, versao)


async def test_arquivar_versoes_preserva_a_publicada(limpo: str, tmp_path: Path) -> None:
    for i, versao in enumerate(("20260101000000", "20260102000000", "20260103000000")):
        await _publicar("TESTE_ARQ", versao, 10 + i)
    etl = await _conectar(ETL_DSN, "role_etl")
    try:
        feitas = await tarefas.arquivar_versoes(etl, tmp_path, manter=1, fontes={"TESTE_ARQ"})
        assert sorted(f.versao for f in feitas) == ["20260101000000", "20260102000000"]
        df = pl.read_parquet(feitas[0].arquivo)
        assert df.height == 10
        assert shapely.get_srid(shapely.from_wkb(df["geometria"][0])) == 4674  # geometria reproduzível
        assert await etl.fetchval("SELECT to_regclass('core_geo.embargos_teste_arq_v20260101000000')") is None
        # A versão em uso segue publicada e acessível
        n = await etl.fetchval("SELECT count(*) FROM core_geo.embargos_current WHERE orgao = 'TESTE'")
        assert n == 12
        arquivadas = await etl.fetchval(
            "SELECT count(*) FROM core_geo.dataset_versions"
            " WHERE fonte = 'TESTE_ARQ' AND arquivado_em IS NOT NULL"
        )
        assert arquivadas == 2
    finally:
        await etl.close()


async def test_busca_do_titular_atravessa_rotacao_de_chave(migrated: str) -> None:
    doc = parse_document("52998224725")
    chaves = {1: b"a" * 32, 2: b"b" * 32}
    owner = await asyncpg.connect(migrated)
    tx = owner.transaction()
    await tx.start()
    try:
        for versao, tenant in ((1, "teste-titular-antigo"), (2, "teste-titular-novo")):
            await owner.execute(
                "INSERT INTO audit.credit_analysis_log (request_id, client_tenant_id, document_hash,"
                " hmac_key_version, document_masked, car_code, ruleset_version, dataset_versions,"
                " tolerance_applied, rule_results, credit_status)"
                " VALUES ('r', $1, $2, $3, '***.982.247-**', 'GO-X', 'v', '{}', '{}', '[]', 'INCONCLUSIVO')",
                tenant,
                hash_documento(doc, chaves[versao]),
                versao,
            )
        await owner.execute("SET LOCAL ROLE role_maint")
        achadas = await tarefas.analises_do_titular(owner, doc, chaves)
        assert {a["client_tenant_id"] for a in achadas} == {"teste-titular-antigo", "teste-titular-novo"}
        so_nova = await tarefas.analises_do_titular(owner, doc, {2: chaves[2]})
        assert [a["client_tenant_id"] for a in so_nova] == ["teste-titular-novo"]
    finally:
        await tx.rollback()
        await owner.close()
