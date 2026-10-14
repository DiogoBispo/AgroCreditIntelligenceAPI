from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest
import shapely

from agrocredit.pipeline import gold
from tests.integration.conftest import ETL_DSN, limpar_bases

pytestmark = pytest.mark.integration

COLUNAS = ("tipo_documento", "documento", "cnpj_raiz", "nome_infrator", "data_inclusao")


def _ewkb(geom: shapely.Geometry) -> bytes:
    return bytes(shapely.to_wkb(shapely.set_srid(geom, 4674), include_srid=True))


def _carga(fonte: str, versao: str, docs: list[str]) -> gold.Carga:
    return gold.Carga(
        base="mte",
        fonte=fonte,
        versao=versao,
        source_as_of=date(2026, 10, 6),
        colunas=COLUNAS,
        registros=[("CPF", d, None, None, date(2025, 1, 1)) for d in docs],
        quarentena=[("x", "documento_invalido", {"linha": "..."})],
    )


@pytest.fixture
async def etl(migrated: str) -> AsyncIterator[str]:
    await limpar_bases(migrated)
    yield ETL_DSN
    await limpar_bases(migrated)


async def _docs_na_view(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SET ROLE role_api")
        return {r["documento"] for r in await conn.fetch("SELECT documento FROM core_geo.mte_current")}
    finally:
        await conn.close()


async def test_publica_e_api_le_a_view(etl: str, migrated: str) -> None:
    await gold.carregar(etl, _carga("TESTE_A", "20260101000000", ["52998224725"]))
    await gold.publicar(etl, "mte", "TESTE_A", "20260101000000")
    assert "52998224725" in await _docs_na_view(migrated)


async def test_view_une_a_ultima_versao_de_cada_fonte(etl: str, migrated: str) -> None:
    await gold.carregar(etl, _carga("TESTE_A", "20260101000000", ["52998224725"]))
    await gold.publicar(etl, "mte", "TESTE_A", "20260101000000")
    await gold.carregar(etl, _carga("TESTE_B", "20260101000000", ["12345678909"]))
    await gold.publicar(etl, "mte", "TESTE_B", "20260101000000")
    # Nova versão da fonte A substitui só a fonte A
    await gold.carregar(etl, _carga("TESTE_A", "20260102000000", ["11144477735"]))
    await gold.publicar(etl, "mte", "TESTE_A", "20260102000000")
    docs = await _docs_na_view(migrated)
    assert {"11144477735", "12345678909"} <= docs
    assert "52998224725" not in docs  # registro que saiu da fonte sai da base (ADR-007)


async def test_variacao_de_volume_bloqueia_e_aprovacao_libera(etl: str, migrated: str) -> None:
    await gold.carregar(
        etl, _carga("TESTE_A", "20260101000000", ["52998224725", "12345678909", "11144477735"])
    )
    await gold.publicar(etl, "mte", "TESTE_A", "20260101000000")
    await gold.carregar(etl, _carga("TESTE_A", "20260102000000", ["52998224725"]))
    with pytest.raises(gold.PublicacaoBloqueada, match="volume variou"):
        await gold.publicar(etl, "mte", "TESTE_A", "20260102000000")
    assert len(await _docs_na_view(migrated) & {"12345678909", "11144477735"}) == 2  # versão anterior segue

    with pytest.raises(ValueError, match="exige"):
        await gold.publicar(etl, "mte", "TESTE_A", "20260102000000", aprovado_por="fulano")
    await gold.publicar(
        etl, "mte", "TESTE_A", "20260102000000", aprovado_por="fulano", motivo="MTE excluiu nomes no prazo"
    )
    assert "12345678909" not in await _docs_na_view(migrated)

    conn = await asyncpg.connect(migrated)
    row = await conn.fetchrow(
        "SELECT aprovado_por, motivo_aprovacao, bloqueio, registros, quarentena"
        " FROM core_geo.dataset_versions WHERE fonte = 'TESTE_A' AND versao = '20260102000000'"
    )
    await conn.close()
    assert row["aprovado_por"] == "fulano" and row["bloqueio"] is None
    assert (row["registros"], row["quarentena"]) == (1, 1)


async def test_geometria_ewkb_e_area_gerada(etl: str, migrated: str) -> None:
    poligono = shapely.MultiPolygon([shapely.box(-50.9, -17.8, -50.89, -17.79)])
    carga = gold.Carga(
        base="embargos",
        fonte="TESTE_A",
        versao="20260101000000",
        source_as_of=date(2026, 10, 13),
        colunas=("orgao", "id_embargo", "situacao", "tipo_geom_origem", "geometria", "ponto"),
        registros=[
            (
                "IBAMA",
                "TESTE-1",
                "VIGENTE",
                "POLIGONO",
                _ewkb(poligono),
                _ewkb(shapely.Point(-50.895, -17.795)),
            )
        ],
        quarentena=[],
    )
    await gold.carregar(etl, carga)
    await gold.publicar(etl, "embargos", "TESTE_A", "20260101000000")
    conn = await asyncpg.connect(migrated)
    await conn.execute("SET ROLE role_api")
    area = await conn.fetchval(
        "SELECT ST_Area(geometria::geography) / 10000 FROM core_geo.embargos_current"
        " WHERE id_embargo = 'TESTE-1'"
    )
    await conn.close()
    assert 117 < area < 118


def test_identificadores_validados() -> None:
    with pytest.raises(ValueError):
        gold.validar_identificadores("mte", "x; DROP TABLE y", "20260101000000")
    with pytest.raises(ValueError):
        gold.validar_identificadores("pg_catalog", "MTE", "20260101000000")
    with pytest.raises(ValueError):
        gold.validar_identificadores("mte", "MTE", "2026")
