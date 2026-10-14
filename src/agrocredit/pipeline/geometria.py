"""Protocolo geométrico (DATA_PIPELINE §3): 2D, válida, MultiPolygon, guarda de área no reparo."""

from dataclasses import dataclass

import shapely
from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry

LIMITE_MUDANCA_AREA = 0.01  # 1%
_GEOD = Geod(ellps="GRS80")  # elipsoide do SIRGAS 2000


@dataclass(frozen=True, slots=True)
class GeomResult:
    geom: MultiPolygon | None
    motivo: str | None  # preenchido quando geom é None


def area_geodesica_ha(geom: BaseGeometry) -> float:
    area_m2, _ = _GEOD.geometry_area_perimeter(geom)
    return abs(area_m2) / 10_000


def _partes_poligonais(geom: BaseGeometry) -> list[Polygon]:
    """Achata qualquer coleção, devolvendo apenas polígonos não vazios."""
    if isinstance(geom, Polygon):
        return [] if geom.is_empty else [geom]
    if isinstance(geom, BaseMultipartGeometry):
        partes: list[Polygon] = []
        for g in geom.geoms:
            partes.extend(_partes_poligonais(g))
        return partes
    return []


def _reparar(geom: BaseGeometry) -> MultiPolygon | None:
    # "structure" une partes sobrepostas e subtrai buracos; o padrão ("linework") transforma a
    # sobreposição entre partes em buraco e some com a área comum
    partes = _partes_poligonais(shapely.make_valid(geom, method="structure", keep_collapsed=False))
    if not partes:
        return None
    reparada = MultiPolygon(partes)
    if reparada.is_valid:
        return reparada
    # Partes válidas isoladas podem se sobrepor: a união resolve
    partes = _partes_poligonais(shapely.union_all(partes))
    return MultiPolygon(partes) if partes else None


def _area_mudou(original: BaseGeometry, reparada: MultiPolygon, area_referencia_ha: float | None) -> bool:
    """Guarda contra reparo que inventa ou some com área.

    Com área de referência da fonte (declarada), compara a área geodésica reparada com ela. Sem referência,
    só vale para polígono simples: em multipartes sobrepostas, a área "antes" conta a sobreposição duas
    vezes e a união (menor) é o reparo correto.
    """
    if area_referencia_ha and area_referencia_ha > 0:
        return (
            abs(area_geodesica_ha(reparada) - area_referencia_ha) / area_referencia_ha > LIMITE_MUDANCA_AREA
        )
    if isinstance(original, Polygon) and original.area > 0:
        return abs(reparada.area - original.area) / original.area > LIMITE_MUDANCA_AREA
    return False


def sanitizar(geom: BaseGeometry | None, area_referencia_ha: float | None = None) -> GeomResult:
    """Devolve MultiPolygon 2D válido ou o motivo da rejeição. Nunca simplifica."""
    if geom is None or geom.is_empty:
        return GeomResult(None, "geometria_ausente")
    geom = shapely.force_2d(geom)
    if geom.is_valid:
        partes = _partes_poligonais(geom)
        if not partes:
            return GeomResult(None, "tipo_nao_poligonal")
        return GeomResult(MultiPolygon(partes), None)

    reparada = _reparar(geom)
    if reparada is None:
        return GeomResult(None, "geometria_colapsada")
    if _area_mudou(geom, reparada, area_referencia_ha):
        return GeomResult(None, "reparo_alterou_area")
    return GeomResult(reparada, None)
