"""Caso de uso completo contra o PostGIS real, como papel da API.

Dados sintéticos publicados com os nomes reais das fontes, no banco isolado de testes (conftest), num ponto
do oceano.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import shapely

from agrocredit.application.use_cases.check_credit_compliance import Contexto, TenantNaoAutorizado, verificar
from agrocredit.core.security import Identidade
from agrocredit.domain.enums import CreditStatus, RuleResult as R
from agrocredit.domain.ruleset import carregar
from agrocredit.domain.validators import parse_document
from agrocredit.infrastructure.db.repositories import auditoria
from agrocredit.pipeline import gold
from tests.integration.conftest import API_DSN, ETL_DSN, limpar_bases

pytestmark = pytest.mark.integration

HEX = "AB" * 16
CAR_A = f"GO-5218805-{HEX}"  # ativo, com embargos sobrepostos
CAR_SEM_GEOM = f"GO-5218805-{'CD' * 16}"  # geometria irrecuperável na fonte
CAR_INEXISTENTE = f"GO-5218805-{'EF' * 16}"
CAR_FORA = f"GO-5208707-{'AB' * 16}"  # Goiânia: fora da cobertura
CNPJ = parse_document("11222333000181")
CPF = parse_document("52998224725")
TENANT = "teste-coop-a"
VERSAO = "20260101000000"

IMOVEL = shapely.box(-31.0, -20.0, -30.99, -19.99)  # ~116 ha, no Atlântico
E1 = shapely.box(-31.0, -20.0, -30.995, -19.99)  # metade oeste
E2 = shapely.box(-30.997, -20.0, -30.994, -19.99)  # sobrepõe E1
UNIAO_HA = Decimal("69.8")  # 60% do imóvel; a soma E1 + E2 daria ~93


def _ewkb(g: shapely.Geometry) -> bytes:
    return bytes(shapely.to_wkb(shapely.set_srid(g, 4674), include_srid=True))


def _multi(g: shapely.Geometry) -> bytes:
    return _ewkb(shapely.MultiPolygon([g]))


def _cnpj_filial(raiz: str, ordem: str) -> str:
    base = [int(c) for c in raiz + ordem]
    for pesos in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        resto = sum(v * p for v, p in zip(base, pesos, strict=True)) % 11
        base.append(0 if resto < 2 else 11 - resto)
    return "".join(map(str, base))


async def _publicar(
    base: str, fonte: str, colunas: tuple[str, ...], registros: list[tuple[object, ...]]
) -> None:
    carga = gold.Carga(base, fonte, VERSAO, date.today(), colunas, registros, [])
    await gold.carregar(ETL_DSN, carga)
    await gold.publicar(ETL_DSN, base, fonte, VERSAO)


@pytest.fixture(scope="module")
async def cenario(migrated: str) -> AsyncIterator[None]:
    await limpar_bases(migrated)
    await _publicar(
        "imoveis_car",
        "SICAR",
        (
            "codigo_car",
            "codigo_ibge",
            "status_cadastro",
            "area_declarada_ha",
            "geometria",
            "municipio",
            "uf",
            "problema_geometria",
        ),
        [
            (CAR_A, 5218805, "ATIVO", Decimal("116"), _multi(IMOVEL), "Rio Verde", "GO", None),
            (CAR_SEM_GEOM, 5218805, "ATIVO", Decimal("50"), None, "Rio Verde", "GO", "reparo_alterou_area"),
        ],
    )
    cols = (
        "orgao",
        "id_embargo",
        "numero_processo",
        "situacao",
        "tipo_geom_origem",
        "geometria",
        "ponto",
        "tipo_documento",
        "documento_autuado",
        "data_embargo",
    )
    centro = _ewkb(shapely.Point(-30.995, -19.995))
    filial = _cnpj_filial("11222333", "0002")
    await _publicar(
        "embargos",
        "IBAMA",
        cols,
        [
            (
                "IBAMA",
                "T1",
                "PROC-T1",
                "VIGENTE",
                "POLIGONO",
                _multi(E1),
                None,
                None,
                None,
                date(2024, 3, 15),
            ),
            ("IBAMA", "T2", "PROC-T2", "VIGENTE", "POLIGONO", _multi(E2), None, None, None, date(2024, 5, 1)),
            ("IBAMA", "T3", "PROC-T3", "DESEMBARGADO", "POLIGONO", _multi(IMOVEL), None, None, None, None),
            (
                "IBAMA",
                "T4",
                "PROC-T4",
                "VIGENTE",
                "PONTO",
                None,
                centro,
                None,
                None,
                None,
            ),  # atividade, ponto no imóvel
            ("IBAMA", "T5", "PROC-T5", "VIGENTE", "NENHUMA", None, None, "CNPJ", filial, date(2023, 1, 1)),
        ],
    )
    await _publicar(
        "mte",
        "MTE",
        ("tipo_documento", "documento", "cnpj_raiz", "nome_infrator", "data_inclusao"),
        [("CPF", CPF.valor, None, None, date(2025, 4, 9))],
    )
    conn = await asyncpg.connect(migrated)
    await conn.execute(
        "INSERT INTO core_geo.tenant_config (client_tenant_id) VALUES ($1), ('teste-coop-b')", TENANT
    )
    await conn.close()
    yield
    await limpar_bases(migrated)


@pytest.fixture
async def api(cenario: None) -> AsyncIterator[asyncpg.Connection]:
    """Conexão da API numa transação desfeita: a auditoria é somente inserção e não pode ser limpa."""
    conn = await asyncpg.connect(API_DSN)
    tx = conn.transaction(isolation="repeatable_read")  # mesmo isolamento do caso de uso
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _ctx(tenant: str = TENANT) -> Contexto:
    return Contexto(
        request_id=str(uuid4()),
        identidade=Identidade(tenant, "teste"),
        hmac_pepper=b"p" * 32,
        hmac_key_version=1,
        rulesets=carregar(),
    )


async def test_impedimentos_por_geometria_documento_e_ponto(api: asyncpg.Connection) -> None:
    res = await verificar(api, CNPJ, CAR_A, _ctx())
    checks = {c.rule_id: c for c in res.checks}

    geo = checks["R-EMB-GEO"]
    assert geo.result is R.IMPEDIDO and geo.detail is not None
    # União das interseções, não soma (E1 e E2 se sobrepõem)
    assert abs(Decimal(geo.detail["overlap_ha"]) - UNIAO_HA) < Decimal("0.5")
    assert {e["process_number"] for e in geo.detail["embargoes"]} == {"PROC-T1", "PROC-T2"}  # T3 desembargado
    assert geo.detail["embargoes"][0]["embargo_date"] in {"2024-03-15", "2024-05-01"}

    assert checks["R-EMB-DOC"].result is R.IMPEDIDO  # embargo na filial (mesma raiz de CNPJ)
    assert checks["R-EMB-SEMGEO"].result is R.ALERTA  # ponto do embargo T4 dentro do imóvel
    assert res.decision.credit_status is CreditStatus.IMPEDIMENTO_IDENTIFICADO
    assert res.input.document_masked == "11.222.333/****-**"
    assert res.owner_link_verified is False
    assert res.property is not None and abs(res.property.geodesic_area_ha - Decimal(116)) < 2  # type: ignore[operator]


async def test_mte_por_cpf(api: asyncpg.Connection) -> None:
    res = await verificar(api, CPF, CAR_A, _ctx())
    checks = {c.rule_id: c for c in res.checks}
    assert checks["R-MTE"].result is R.IMPEDIDO
    assert checks["R-RFB"].result is R.NAO_VERIFICADO
    assert res.cadastral_status.rfb_status == "NAO_DISPONIVEL"


async def test_car_com_geometria_irrecuperavel(api: asyncpg.Connection) -> None:
    res = await verificar(api, CPF, CAR_SEM_GEOM, _ctx())
    checks = {c.rule_id: c for c in res.checks}
    assert checks["R-CAR"].result is R.OK  # existe: não é "CAR não encontrado"
    assert checks["R-EMB-GEO"].result is R.NAO_VERIFICADO
    assert res.property is not None and res.property.geometry_problem == "reparo_alterou_area"


async def test_car_inexistente_em_municipio_coberto(api: asyncpg.Connection) -> None:
    res = await verificar(api, CNPJ, CAR_INEXISTENTE, _ctx())
    assert {c.rule_id: c.result for c in res.checks}["R-CAR"] is R.IMPEDIDO
    assert res.property is None


async def test_car_fora_da_cobertura(api: asyncpg.Connection) -> None:
    res = await verificar(api, CNPJ, CAR_FORA, _ctx())
    assert res.decision.credit_status is CreditStatus.FORA_DE_COBERTURA


async def test_tenant_desconhecido(api: asyncpg.Connection) -> None:
    with pytest.raises(TenantNaoAutorizado):
        await verificar(api, CNPJ, CAR_A, _ctx("tenant-inexistente"))


async def test_auditoria_pseudonimizada_e_isolada_por_tenant(api: asyncpg.Connection) -> None:
    res = await verificar(api, CPF, CAR_A, _ctx())
    propria = await auditoria.consultar(api, res.analysis_id, TENANT)
    assert propria is not None and propria["credit_status"] == res.decision.credit_status.value
    assert propria["document_masked"] == "***.982.247-**"
    assert CPF.valor not in str(dict(propria))  # CPF nunca em claro na auditoria
    assert propria["dataset_versions"]  # versões das bases usadas no parecer
    assert await auditoria.consultar(api, res.analysis_id, "teste-coop-b") is None  # BOLA
