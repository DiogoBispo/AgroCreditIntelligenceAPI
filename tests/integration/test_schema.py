from decimal import Decimal

import asyncpg
import pytest

pytestmark = pytest.mark.integration

QUADRADO = "MULTIPOLYGON(((-50.9 -17.8,-50.89 -17.8,-50.89 -17.79,-50.9 -17.79,-50.9 -17.8)))"

AUDIT_INSERT = """
INSERT INTO audit.credit_analysis_log (request_id, client_tenant_id, document_hash, hmac_key_version,
    document_masked, car_code, ruleset_version, dataset_versions, tolerance_applied, rule_results,
    credit_status)
VALUES ('req-1', 'tenant-a', repeat('a', 64), 1, '***.982.247-**', 'GO-5218805-X', 'MCR-2-9@2026-05-12',
    '{}', '{}', '[]', 'INCONCLUSIVO')
"""


async def test_area_geodesica_calculada_pelo_banco(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "INSERT INTO core_geo.imoveis_car_template"
        " (codigo_car, codigo_ibge, status_cadastro, geometria, municipio, uf)"
        " VALUES ('GO-5218805-X', 5218805, 'ATIVO', ST_GeomFromText($1, 4674), 'Rio Verde', 'GO')",
        QUADRADO,
    )
    area = await conn.fetchval("SELECT area_geodesica_ha FROM core_geo.imoveis_car_template")
    # 0,01° x 0,01° a 17,8°S ≈ 1,06 km x 1,11 km
    assert Decimal("117") < area < Decimal("118")


@pytest.mark.parametrize(
    "tipo,doc,ok",
    [
        ("CPF", "52998224725", True),
        ("CNPJ", "12ABC34501DE35", True),
        ("CNPJ", "00052998224725", True),  # formato ok; o tipo vem explícito, sem colisão com CPF
        ("CPF", "00052998224725", False),
        ("CNPJ", "12abc34501de35", False),
        ("CNPJ", "12ABC34501DEA5", False),
    ],
)
async def test_formato_de_documento(conn: asyncpg.Connection, tipo: str, doc: str, ok: bool) -> None:
    assert await conn.fetchval("SELECT core_geo.documento_formato_ok($1, $2)", tipo, doc) is ok


async def test_embargo_poligono_exige_geometria(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO core_geo.embargos_template (orgao, id_embargo, situacao, tipo_geom_origem)"
            " VALUES ('IBAMA', '1', 'VIGENTE', 'POLIGONO')"
        )


async def test_embargo_sem_geometria_e_aceito(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "INSERT INTO core_geo.embargos_template (orgao, id_embargo, situacao, tipo_geom_origem, codigo_ibge)"
        " VALUES ('IBAMA', '2', 'VIGENTE', 'NENHUMA', 5218805)"
    )


async def test_auditoria_somente_insercao_ate_para_o_dono(conn: asyncpg.Connection) -> None:
    await conn.execute(AUDIT_INSERT)
    for sql in (
        "UPDATE audit.credit_analysis_log SET credit_status = 'X'",
        "DELETE FROM audit.credit_analysis_log",
    ):
        await conn.execute("SAVEPOINT s")
        with pytest.raises(asyncpg.RaiseError, match="somente inserção"):
            await conn.execute(sql)
        await conn.execute("ROLLBACK TO SAVEPOINT s")


async def test_auditoria_cai_na_particao_do_mes(conn: asyncpg.Connection) -> None:
    await conn.execute(AUDIT_INSERT)
    particao = await conn.fetchval(
        "SELECT tableoid::regclass::text FROM audit.credit_analysis_log WHERE request_id = 'req-1'"
    )
    assert particao.startswith("audit.credit_analysis_log_20")


async def test_auditoria_sem_particao_usa_default(conn: asyncpg.Connection) -> None:
    sql = AUDIT_INSERT.replace("credit_status)", "credit_status, executed_at)").replace(
        "'INCONCLUSIVO')", "'INCONCLUSIVO', '2031-01-15')"
    )
    await conn.execute(sql)
    particao = await conn.fetchval(
        "SELECT tableoid::regclass::text FROM audit.credit_analysis_log WHERE executed_at = '2031-01-15'"
    )
    assert particao == "audit.credit_analysis_log_default"


# Papéis -------------------------------------------------------------------------------


async def test_api_le_views_mas_nao_templates(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_api")
    await conn.fetch("SELECT * FROM core_geo.imoveis_car_current")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetch("SELECT * FROM core_geo.imoveis_car_template")


async def test_api_so_insere_na_auditoria(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_api")
    await conn.execute(AUDIT_INSERT)
    for sql in ("SELECT * FROM audit.credit_analysis_log", "DELETE FROM audit.credit_analysis_log"):
        await conn.execute("SAVEPOINT s")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(sql)
        await conn.execute("ROLLBACK TO SAVEPOINT s")


async def test_api_nao_altera_configuracao_nem_dados(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_api")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute("UPDATE core_geo.tenant_config SET tolerancia_ha = 100")


async def test_etl_nao_le_auditoria(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_etl")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.fetch("SELECT * FROM audit.credit_analysis_log")


async def test_etl_publica_nova_versao_trocando_a_view(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_etl")
    await conn.execute(
        "CREATE TABLE core_geo.mte_vteste (LIKE core_geo.mte_template INCLUDING ALL);"
        "INSERT INTO core_geo.mte_vteste VALUES ('CPF', '52998224725', NULL, 'Fulano', '2025-01-01');"
        "CREATE OR REPLACE VIEW core_geo.mte_current AS SELECT * FROM core_geo.mte_vteste;"
    )
    # A API continua com acesso após a troca (CREATE OR REPLACE preserva o GRANT)
    await conn.execute("RESET ROLE; SET LOCAL ROLE role_api")
    assert await conn.fetchval("SELECT count(*) FROM core_geo.mte_current") == 1


async def test_manutencao_cria_particoes(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE role_maint")
    await conn.execute("SELECT audit.criar_particoes('2030-01-01', 1)")
    dono = await conn.fetchval(
        "SELECT pg_get_userbyid(relowner) FROM pg_class"
        " WHERE oid = 'audit.credit_analysis_log_2030_01'::regclass"
    )
    assert dono == "role_maint"


async def test_tolerancia_padrao_do_tenant(conn: asyncpg.Connection) -> None:
    await conn.execute("INSERT INTO core_geo.tenant_config (client_tenant_id) VALUES ('t1')")
    row = await conn.fetchrow("SELECT tolerancia_ha, tolerancia_pct FROM core_geo.tenant_config")
    assert (row["tolerancia_ha"], row["tolerancia_pct"]) == (Decimal("0.1"), Decimal("0.01"))
