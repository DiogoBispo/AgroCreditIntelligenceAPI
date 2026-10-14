"""Validação cruzada das sobreposições: repositório da API (PostGIS) × cálculo independente
(shapely + pyproj).

Não valida a interpretação normativa; valida que as consultas espaciais da API (filtros de situação, união
de embargos sobrepostos, percentual sobre a área geodésica, camadas territoriais) produzem os mesmos
números que um cálculo feito por outro caminho. Divergência na classificação aponta erro de consulta.
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg
import shapely
from pyproj import Geod
from shapely import STRtree

from agrocredit.domain.enums import RuleResult
from agrocredit.domain.facts import Tolerancia
from agrocredit.domain.rules.base import classificar_sobreposicao
from agrocredit.infrastructure.db.repositories.fatos import (
    _SQL_EMBARGOS_GEO,
    _SQL_TERRITORIAIS,
    ORGAOS_FEDERAIS,
)

_GEOD = Geod(ellps="GRS80")
TOLERANCIA_PADRAO = Tolerancia(Decimal("0.1"), Decimal("0.01"))
DIFERENCA_MAXIMA_HA = 0.01  # diferença de área aceitável entre os dois cálculos


def _ha(g: shapely.Geometry) -> float:
    return abs(_GEOD.geometry_area_perimeter(g)[0]) / 10_000


@dataclass
class Relatorio:
    imoveis: int = 0
    comparacoes: int = 0
    classificacao_divergente: list[dict[str, Any]] = field(default_factory=list)
    area_divergente: list[dict[str, Any]] = field(default_factory=list)
    resultados: Counter[str] = field(default_factory=Counter)
    maior_diferenca_ha: float = 0.0

    def registrar(self, chave: str, car: str, api_ha: float, ref_ha: float, area_imovel: float) -> None:
        self.comparacoes += 1
        dif = abs(api_ha - ref_ha)
        self.maior_diferenca_ha = max(self.maior_diferenca_ha, dif)
        tol = TOLERANCIA_PADRAO

        def classe(ha: float) -> RuleResult:
            pct = Decimal(str(ha / area_imovel * 100)) if area_imovel else Decimal(0)
            return classificar_sobreposicao(Decimal(str(round(ha, 4))), pct, tol)

        c_api, c_ref = classe(api_ha), classe(ref_ha)
        self.resultados[f"{chave}:{c_ref}"] += 1
        caso = {"car": car, "camada": chave, "api_ha": round(api_ha, 4), "independente_ha": round(ref_ha, 4)}
        if c_api is not c_ref:
            self.classificacao_divergente.append({**caso, "api": c_api, "independente": c_ref})
        elif dif > DIFERENCA_MAXIMA_HA:
            self.area_divergente.append(caso)


async def validar(conn: asyncpg.Connection, limite: int | None = None) -> Relatorio:
    """Compara, para cada imóvel coberto, os embargos federais e as camadas territoriais."""
    imoveis = await conn.fetch(
        "SELECT i.codigo_car, ST_AsBinary(i.geometria) AS g FROM core_geo.imoveis_car_current i"
        " JOIN core_geo.municipios m ON m.codigo_ibge = i.codigo_ibge AND m.em_cobertura"
        " WHERE i.geometria IS NOT NULL ORDER BY i.codigo_car LIMIT $1",
        limite,
    )
    embargos = await conn.fetch(
        "SELECT ST_AsBinary(geometria) AS g FROM core_geo.embargos_current"
        " WHERE situacao = 'VIGENTE' AND geometria IS NOT NULL AND orgao = ANY($1::text[])",
        ORGAOS_FEDERAIS,
    )
    camadas = await conn.fetch(
        "SELECT camada, id, ST_AsBinary(geometria) AS g FROM core_geo.camadas_territoriais_current"
    )
    geo_emb = [shapely.from_wkb(r["g"]) for r in embargos]
    arvore_emb = STRtree(geo_emb)
    geo_cam = [shapely.from_wkb(r["g"]) for r in camadas]
    arvore_cam = STRtree(geo_cam)

    rel = Relatorio()
    for im in imoveis:
        car = im["codigo_car"]
        g = shapely.from_wkb(im["g"])
        area_imovel = _ha(g)
        rel.imoveis += 1

        # Embargos federais: união das interseções (independente) × consulta da API
        inter = [g.intersection(geo_emb[i]) for i in arvore_emb.query(g, predicate="intersects")]
        inter = [x for x in inter if x.area > 0]
        ref = _ha(shapely.union_all(inter)) if inter else 0.0
        api_rows = {r["esfera"]: r for r in await conn.fetch(_SQL_EMBARGOS_GEO, car, ORGAOS_FEDERAIS)}
        api = float(api_rows["FEDERAL"]["area_ha"]) if "FEDERAL" in api_rows else 0.0
        rel.registrar("EMBARGO_FEDERAL", car, api, ref, area_imovel)

        # Camadas territoriais: área por polígono
        ref_cam = {
            (camadas[i]["camada"], camadas[i]["id"]): _ha(g.intersection(geo_cam[i]))
            for i in arvore_cam.query(g, predicate="intersects")
        }
        api_cam = {
            (r["camada"], r["id"]): float(r["overlap_ha"]) for r in await conn.fetch(_SQL_TERRITORIAIS, car)
        }
        for chave in ref_cam.keys() | api_cam.keys():
            rel.registrar(chave[0], car, api_cam.get(chave, 0.0), ref_cam.get(chave, 0.0), area_imovel)
    return rel
