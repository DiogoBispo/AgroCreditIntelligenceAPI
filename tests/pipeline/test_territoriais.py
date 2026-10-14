"""Camadas territoriais e embargos de MG com arquivos sintéticos no formato real das fontes."""

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
import shapely

from agrocredit.pipeline.bronze import ArquivoBaixado, Manifesto
from agrocredit.pipeline.fontes import embargos_mg, territoriais
from agrocredit.pipeline.fontes.territoriais import ler_shapefile, normalizar

ENVELOPE = (-51.0, -18.0, -50.0, -17.0)
DENTRO = shapely.box(-50.9, -17.8, -50.89, -17.79)
FORA = shapely.box(-40.0, -10.0, -39.99, -9.99)


def _manifesto(d: Path, fonte: str, arquivo: str, as_of: str | None = None) -> None:
    m = Manifesto(
        fonte,
        "20261014000000",
        [ArquivoBaixado("https://x", arquivo, "0" * 64, 1, "2026-10-14T00:00:00+00:00")],
    )
    m.parametros["envelopes"] = [list(ENVELOPE)]
    m.source_as_of = as_of
    m.salvar(d)


def _shapefile_zip(d: Path, nome: str, linhas: list[dict[str, Any]], geoms: list[shapely.Geometry]) -> Path:
    gdf = gpd.GeoDataFrame(linhas, geometry=geoms, crs=4674)
    pasta = d / "shp"
    pasta.mkdir()
    gdf.to_file(pasta / f"{nome}.shp", engine="pyogrio", encoding="utf-8")
    destino = d / f"{nome}.zip"
    with zipfile.ZipFile(destino, "w") as z:
        for arq in pasta.iterdir():
            z.write(arq, arq.name)
    return destino


@pytest.mark.parametrize(
    "entrada,saida",
    [
        ("Título Parcial", "TITULO_PARCIAL"),
        ("Área de Proteção Ambiental", "AREA_DE_PROTECAO_AMBIENTAL"),
        ("TIPO B", "TIPO_B"),
        (None, None),
        (float("nan"), None),
    ],
)
def test_normalizar(entrada: Any, saida: str | None) -> None:
    assert normalizar(entrada) == saida


def test_dbf_com_utf8_truncado(tmp_path: Path) -> None:
    """O CNUC corta o texto no meio de um caractere multibyte; a leitura não pode quebrar."""
    arq = _shapefile_zip(tmp_path, "uc", [{"nome": "PROTEÇÃO"}], [DENTRO])
    with zipfile.ZipFile(arq) as z:
        dbf = bytearray(z.read("uc.dbf"))
    pos = dbf.find("PROTEÇÃO".encode())
    dbf[pos + 9] = 0x20  # corta o "Ã" (2 bytes) pela metade
    with zipfile.ZipFile(arq, "w") as z:
        for nome in ("uc.shp", "uc.shx", "uc.prj", "uc.cpg"):
            z.write(tmp_path / "shp" / nome, nome)
        z.writestr("uc.dbf", bytes(dbf))
    gdf = ler_shapefile(arq)
    assert gdf.iloc[0]["nome"].startswith("PROTEÇ")


def test_cnuc_categorias_e_escopo(tmp_path: Path) -> None:
    linhas = [
        {
            "cd_cnuc": "1",
            "uc_id": "1",
            "nome_uc": "PARQUE X",
            "categoria": "Parque",
            "grupo": "Proteção Integral",
            "esfera": "Estadual",
            "situacao": "Ativo",
            "ha_total": "117.35",
        },
        {
            "cd_cnuc": "2",
            "uc_id": "2",
            "nome_uc": "APA Y",
            "categoria": "Área de Proteção Ambiental",
            "grupo": "Uso Sustentável",
            "esfera": "Municipal",
            "situacao": "Ativo",
            "ha_total": "117.35",
        },
        {
            "cd_cnuc": "3",
            "uc_id": "3",
            "nome_uc": "LONGE",
            "categoria": "Parque",
            "grupo": "Proteção Integral",
            "esfera": "Federal",
            "situacao": "Ativo",
            "ha_total": "117",
        },
    ]
    _shapefile_zip(tmp_path, "cnuc_2025_08", linhas, [DENTRO, DENTRO, FORA])
    _manifesto(tmp_path, "CNUC", "cnuc_2025_08.zip")
    carga = territoriais.transformar_cnuc(tmp_path)
    atributos = {r[1]: json.loads(r[3]) for r in carga.registros}
    assert set(atributos) == {"1", "2"}  # fora do envelope não entra
    assert atributos["1"]["categoria"] == "PARQUE" and atributos["2"]["categoria"] == "APA"
    assert all(r[0] == "UC" for r in carga.registros)
    assert carga.source_as_of == territoriais.AS_OF_CNUC  # data do nome do arquivo, não do download


def test_arquivo_manual_usa_a_data_informada(tmp_path: Path) -> None:
    linhas = [
        {
            "cd_cnuc": "1",
            "uc_id": "1",
            "nome_uc": "P",
            "categoria": "Parque",
            "grupo": "x",
            "esfera": "x",
            "situacao": "Ativo",
            "ha_total": "117.35",
        }
    ]
    _shapefile_zip(tmp_path, "cnuc_2026_07", linhas, [DENTRO])
    _manifesto(tmp_path, "CNUC", "cnuc_2026_07.zip", as_of="2026-07-01")
    assert territoriais.transformar_cnuc(tmp_path).source_as_of == date(2026, 7, 1)


def test_quilombola_fase_normalizada(tmp_path: Path) -> None:
    linhas = [
        {
            "nr_process": "54200.1/2006",
            "nm_comunid": "C",
            "fase": "Título Parcial",
            "esfera": "FEDERAL",
            "nr_area_ha": 117.35,
        }
    ]
    _shapefile_zip(tmp_path, "quilombolas", linhas, [DENTRO])
    _manifesto(tmp_path, "INCRA", "quilombolas.zip")
    carga = territoriais.transformar_quilombolas(tmp_path)
    assert json.loads(carga.registros[0][3])["fase"] == "TITULO_PARCIAL"
    assert carga.source_as_of == date(2026, 10, 14)  # INCRA gera o arquivo na hora: data do download


def test_cnfp_tipo_e_id_estavel(tmp_path: Path) -> None:
    linhas = [
        {
            "nome": "GLEBA " + "X" * 200,
            "tipo": "TIPO B",
            "classe": "GLEBAFED",
            "categoria": "x",
            "governo": "FEDERAL",
            "estagio": "x",
            "codigo": None,
            "geocodigo": "5218805",
            "area_ha": 117.35,
        }
    ]
    _shapefile_zip(tmp_path, "cnfp", linhas, [DENTRO])
    _manifesto(tmp_path, "SFB", "cnfp.zip")
    reg = territoriais.transformar_cnfp(tmp_path).registros[0]
    assert json.loads(reg[3])["tipo"] == "B"
    assert reg[1].startswith("h:") and len(reg[1]) <= 80  # nome longo não estoura a coluna id


def test_prodes_reprojeta_partes_em_wgs84(tmp_path: Path) -> None:
    props = {
        "uuid": "u1",
        "class_name": "d2024",
        "image_date": "2024-08-10",
        "year": 2024,
        "main_class": "D",
        "source": None,
        "pub_date": "20260717",
        "area_km": 1.17,
    }
    for nome, crs in (
        ("cerrado_0.json", "urn:ogc:def:crs:EPSG::4674"),
        ("mata-atlantica_0.json", "EPSG:4326"),
    ):
        feats = [{"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(DENTRO)}]
        if "mata" in nome:
            feats = [dict(feats[0], properties={**props, "uuid": "u2"})]
        (tmp_path / nome).write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": feats,
                    "crs": {"type": "name", "properties": {"name": crs}},
                }
            )
        )
    _manifesto(tmp_path, "PRODES", "cerrado_0.json")
    carga = territoriais.transformar_prodes(tmp_path)
    assert {r[1] for r in carga.registros} == {"u1", "u2"}
    assert json.loads(carga.registros[0][3])["data_deteccao"] == "2024-08-10"
    assert carga.source_as_of == date(2026, 7, 17)


def test_embargos_mg(tmp_path: Path) -> None:
    feats = [
        {
            "type": "Feature",
            "geometry": shapely.geometry.mapping(DENTRO),
            "properties": {
                "id": 1,
                "num_ai": "726218/2026",
                "doc_fiscal": "XXX.473.786-XX",
                "data_fisca": "2026-05-05",
                "outras_pen": "Embargo",
            },
        },
        {
            "type": "Feature",
            "geometry": shapely.geometry.mapping(DENTRO),
            "properties": {
                "id": 2,
                "num_ai": "1/2026",
                "doc_fiscal": "11222333000181",
                "data_fisca": "2026-03-25",
                "outras_pen": "Embargo",
            },
        },
    ]
    crs = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4674"}}
    (tmp_path / "pagina_0000000.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": feats, "crs": crs})
    )
    _manifesto(tmp_path, "EMBARGOS_MG", "pagina_0000000.json")
    carga = embargos_mg.transformar_mg(tmp_path, hoje=date(2026, 10, 14))
    reg = {r[1]: dict(zip(carga.colunas, r, strict=True)) for r in carga.registros}
    assert reg["1"]["documento_autuado"] is None and reg["1"]["documento_mascarado"] == "XXX.473.786-XX"
    assert reg["2"]["tipo_documento"] == "CNPJ" and reg["2"]["documento_autuado"] == "11222333000181"
    assert all(r["situacao"] == "VIGENTE" and r["orgao"] == "SEMAD-MG" for r in reg.values())
    assert carga.source_as_of == date(2026, 5, 5)  # auto de infração mais recente
