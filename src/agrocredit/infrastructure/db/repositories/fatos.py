"""Monta os Fatos do motor de regras a partir das views publicadas (core_geo.*_current).

Todas as consultas rodam numa transação somente leitura REPEATABLE READ: um snapshot consistente mesmo
se o ETL publicar uma versão no meio da requisição.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from agrocredit.domain.enums import Camada, CarStatus, TipoDocumento
from agrocredit.domain.facts import (
    EmbargoDocumental,
    EmbargoHit,
    Empresa,
    Fatos,
    Imovel,
    RegistroMte,
    Sobreposicao,
    SobreposicaoTerritorial,
)
from agrocredit.domain.validators import Documento, car_codigo_ibge
from agrocredit.infrastructure.db.tipos import Conexao

ORGAOS_FEDERAIS = ["IBAMA", "ICMBIO"]
_CEM = Decimal(100)

_SQL_IMOVEL = """
SELECT codigo_car, codigo_ibge, uf, municipio, status_cadastro, area_declarada_ha, area_geodesica_ha,
       problema_geometria, tipo_imovel, modulos_fiscais
FROM core_geo.imoveis_car_current WHERE codigo_car = $1
"""

_SQL_COBERTURA = "SELECT em_cobertura FROM core_geo.municipios WHERE codigo_ibge = $1"

# União das interseções por esfera: embargos sobrepostos não contam duas vezes (ADR-002).
# Interseções sem área (só encostam na divisa) são descartadas.
_SQL_EMBARGOS_GEO = """
WITH imovel AS (
    SELECT geometria FROM core_geo.imoveis_car_current WHERE codigo_car = $1 AND geometria IS NOT NULL
),
inter AS (
    SELECT CASE WHEN e.orgao = ANY($2::text[]) THEN 'FEDERAL' ELSE 'ESTADUAL' END AS esfera,
           e.orgao, e.numero_processo, e.data_infracao, e.data_embargo,
           ST_Intersection(i.geometria, e.geometria) AS g
    FROM imovel i
    JOIN core_geo.embargos_current e
      ON e.situacao = 'VIGENTE' AND e.geometria IS NOT NULL AND ST_Intersects(i.geometria, e.geometria)
),
hits AS (SELECT * FROM inter WHERE ST_Area(g) > 0)
SELECT esfera,
       (ST_Area(ST_Union(g)::geography) / 10000)::numeric(14, 4) AS area_ha,
       json_agg(json_build_object(
           'orgao', orgao, 'processo', numero_processo, 'data_infracao', data_infracao,
           'data_embargo', data_embargo, 'ha', round((ST_Area(g::geography) / 10000)::numeric, 4)
       ) ORDER BY ST_Area(g) DESC) AS hits
FROM hits GROUP BY esfera
"""

_SQL_EMBARGOS_PONTO = """
SELECT count(*)
FROM core_geo.imoveis_car_current i
JOIN core_geo.embargos_current e
  ON e.situacao = 'VIGENTE' AND e.geometria IS NULL AND e.ponto IS NOT NULL
 AND ST_Intersects(i.geometria, e.ponto)
WHERE i.codigo_car = $1 AND i.geometria IS NOT NULL
"""

# Documento exato ou, para CNPJ, outro estabelecimento da mesma empresa (raiz)
_SQL_EMBARGOS_DOC = """
SELECT orgao, numero_processo, data_infracao, data_embargo
FROM core_geo.embargos_current
WHERE situacao = 'VIGENTE' AND tipo_documento = $1::core_geo.tipo_documento
  AND (documento_autuado = $2 OR ($3::text IS NOT NULL AND left(documento_autuado, 8) = $3))
ORDER BY data_embargo NULLS LAST
"""

_SQL_MTE = """
SELECT tipo_documento::text AS tipo, documento, data_inclusao, (documento <> $2) AS por_raiz
FROM core_geo.mte_current
WHERE (tipo_documento = $1::core_geo.tipo_documento AND documento = $2)
   OR ($3::text IS NOT NULL AND cnpj_raiz = $3)
"""

_SQL_TERRITORIAIS = """
SELECT t.camada, t.id, t.nome, t.atributos,
       (ST_Area(ST_Intersection(i.geometria, t.geometria)::geography) / 10000)::numeric(14, 4) AS overlap_ha
FROM core_geo.imoveis_car_current i
JOIN core_geo.camadas_territoriais_current t ON ST_Intersects(i.geometria, t.geometria)
WHERE i.codigo_car = $1 AND i.geometria IS NOT NULL
"""

_SQL_EMPRESA = (
    "SELECT situacao_cadastral, cnae_fiscal_principal FROM core_geo.empresas_current WHERE cnpj = $1"
)

_SQL_DATA_AS_OF = """
SELECT DISTINCT ON (fonte) fonte, source_as_of, versao
FROM core_geo.dataset_versions WHERE publicado_em IS NOT NULL
ORDER BY fonte, publicado_em DESC
"""


@dataclass(frozen=True, slots=True)
class VersoesDados:
    data_as_of: Mapping[str, date]
    versoes: Mapping[str, str]


def _pct(area: Decimal, total: Decimal | None) -> Decimal:
    if not total:
        return Decimal(0)
    return (area / total * _CEM).quantize(Decimal("0.0001"))


def _sobreposicao(row: asyncpg.Record | None, area_imovel: Decimal | None) -> Sobreposicao:
    if row is None:
        return Sobreposicao.vazia()
    hits = tuple(
        EmbargoHit(
            orgao=h["orgao"],
            numero_processo=h["processo"],
            data_infracao=date.fromisoformat(h["data_infracao"]) if h["data_infracao"] else None,
            overlap_ha=Decimal(str(h["ha"])),
            data_embargo=date.fromisoformat(h["data_embargo"]) if h["data_embargo"] else None,
        )
        for h in json.loads(row["hits"])
    )
    area = Decimal(row["area_ha"])
    return Sobreposicao(area, _pct(area, area_imovel), hits)


async def versoes_publicadas(conn: Conexao) -> VersoesDados:
    rows = await conn.fetch(_SQL_DATA_AS_OF)
    return VersoesDados(
        data_as_of={r["fonte"]: r["source_as_of"] for r in rows},
        versoes={r["fonte"]: r["versao"] for r in rows},
    )


async def _imovel(conn: Conexao, codigo_car: str) -> Imovel | None:
    row = await conn.fetchrow(_SQL_IMOVEL, codigo_car)
    if row is None:
        return None
    return Imovel(
        codigo_car=row["codigo_car"],
        codigo_ibge=row["codigo_ibge"],
        uf=row["uf"],
        municipio=row["municipio"],
        status=CarStatus(row["status_cadastro"]),
        area_declarada_ha=row["area_declarada_ha"],
        area_geodesica_ha=row["area_geodesica_ha"],
        problema_geometria=row["problema_geometria"],
        tipo_imovel=row["tipo_imovel"],
        modulos_fiscais=row["modulos_fiscais"],
    )


async def _em_cobertura(conn: Conexao, codigo_car: str, imovel: Imovel | None) -> bool:
    """CAR carregado está coberto. Se não foi encontrado, o código do CAR indica o município provável."""
    if imovel is not None:
        return True
    return bool(await conn.fetchval(_SQL_COBERTURA, car_codigo_ibge(codigo_car)))


async def _territoriais(
    conn: Conexao, codigo_car: str, area_imovel: Decimal | None
) -> tuple[SobreposicaoTerritorial, ...]:
    rows = await conn.fetch(_SQL_TERRITORIAIS, codigo_car)
    resultado = []
    for r in rows:
        atributos: Any = r["atributos"]
        resultado.append(
            SobreposicaoTerritorial(
                camada=Camada(r["camada"]),
                id=r["id"],
                nome=r["nome"],
                atributos=json.loads(atributos) if isinstance(atributos, str) else atributos,
                overlap_ha=r["overlap_ha"],
                pct=_pct(r["overlap_ha"], area_imovel),
            )
        )
    return tuple(resultado)


async def coletar_fatos(
    conn: Conexao, documento: Documento, codigo_car: str, fontes_frescas: Mapping[str, bool]
) -> Fatos:
    """Executa as consultas (dentro da transação do chamador) e devolve os fatos para o motor."""
    imovel = await _imovel(conn, codigo_car)
    area = imovel.area_geodesica_ha if imovel else None
    raiz = documento.cnpj_raiz

    geo = {r["esfera"]: r for r in await conn.fetch(_SQL_EMBARGOS_GEO, codigo_car, ORGAOS_FEDERAIS)}
    docs = await conn.fetch(_SQL_EMBARGOS_DOC, documento.tipo.value, documento.valor, raiz)
    mte = await conn.fetch(_SQL_MTE, documento.tipo.value, documento.valor, raiz)
    empresa_row = (
        await conn.fetchrow(_SQL_EMPRESA, documento.valor) if documento.tipo is TipoDocumento.CNPJ else None
    )

    return Fatos(
        documento=documento,
        codigo_car=codigo_car,
        em_cobertura=await _em_cobertura(conn, codigo_car, imovel),
        fontes_frescas=fontes_frescas,
        imovel=imovel,
        embargos_federais=_sobreposicao(geo.get("FEDERAL"), area),
        embargos_estaduais=_sobreposicao(geo.get("ESTADUAL"), area),
        embargos_documentais=tuple(
            EmbargoDocumental(r["orgao"], r["numero_processo"], r["data_infracao"], r["data_embargo"])
            for r in docs
        ),
        embargos_sem_poligono_no_imovel=await conn.fetchval(_SQL_EMBARGOS_PONTO, codigo_car),
        mte=tuple(RegistroMte(TipoDocumento(r["tipo"]), r["data_inclusao"], r["por_raiz"]) for r in mte),
        territoriais=await _territoriais(conn, codigo_car, area),
        empresa=Empresa(empresa_row["situacao_cadastral"], empresa_row["cnae_fiscal_principal"])
        if empresa_row
        else None,
    )
