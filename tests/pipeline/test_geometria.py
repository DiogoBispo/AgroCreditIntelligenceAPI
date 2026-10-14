import shapely
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon

from agrocredit.pipeline.geometria import sanitizar

QUADRADO = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


def test_poligono_valido_vira_multipolygon() -> None:
    res = sanitizar(QUADRADO)
    assert isinstance(res.geom, MultiPolygon)
    assert res.geom.area == QUADRADO.area


def test_remove_coordenada_z() -> None:
    res = sanitizar(Polygon([(0, 0, 5), (1, 0, 5), (1, 1, 5), (0, 1, 5)]))
    assert res.geom is not None and not res.geom.has_z


def test_repara_gravata_bowtie() -> None:
    gravata = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    res = sanitizar(gravata)
    assert res.geom is not None and res.geom.is_valid
    assert len(res.geom.geoms) == 2


def test_achata_colecao_aninhada() -> None:
    colecao = GeometryCollection([LineString([(0, 0), (1, 1)]), GeometryCollection([QUADRADO])])
    res = sanitizar(colecao)
    assert res.geom is not None and res.geom.area == 1


def test_rejeita_geometria_sem_area() -> None:
    assert sanitizar(Point(0, 0)).motivo == "tipo_nao_poligonal"
    assert sanitizar(None).motivo == "geometria_ausente"


def test_guarda_de_area_no_reparo() -> None:
    # Gravata assimétrica: o reparo "cria" ~8% de área. Na Fase 0, os 2 casos reais barrados
    # teriam inventado +26% e +68% em relação à área declarada no SICAR.
    res = sanitizar(Polygon([(0, 0), (10, 10), (10, 0), (0, 2)]))
    assert res.geom is None
    assert res.motivo == "reparo_alterou_area"


def test_partes_sobrepostas_sao_unidas() -> None:
    a = shapely.box(0, 0, 2, 2)
    b = shapely.box(1, 1, 3, 3)
    res = sanitizar(MultiPolygon([a, b]))
    assert res.geom is not None and res.geom.is_valid
    assert abs(res.geom.area - 7) < 1e-9
