"""Transformação Bronze → carga com arquivos sintéticos no formato real das fontes (Fase 0)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import shapely

from agrocredit.pipeline.bronze import ArquivoBaixado, Manifesto
from agrocredit.pipeline.fontes.ibama import CAMPOS, transformar_ibama
from agrocredit.pipeline.fontes.sicar import transformar_sicar

CAR = "GO-5218805-72819485728194857281948572819485"
QUADRADO = [[[-50.9, -17.8], [-50.89, -17.8], [-50.89, -17.79], [-50.9, -17.79], [-50.9, -17.8]]]
GRAVATA_ASSIMETRICA = [[[0, 0], [10, 10], [10, 0], [0, 2], [0, 0]]]


def _manifesto(d: Path, fonte: str, arquivo: str) -> None:
    Manifesto(
        fonte,
        "20261013000000",
        [ArquivoBaixado("https://x", arquivo, "0" * 64, 1, "2026-10-13T00:00:00+00:00")],
    ).salvar(d)


def _feature(
    codigo: str, status: str, coords: Any, mun: int = 5218805, nome: str = "Rio Verde"
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "cod_imovel": codigo,
            "status_imovel": status,
            "area": 117.35,
            "cod_municipio_ibge": mun,
            "municipio": nome,
            "uf": "GO",
        },
        "geometry": {"type": "MultiPolygon", "coordinates": [coords]},
    }


def _geojson(path: Path, features: list[dict[str, Any]]) -> None:
    crs = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4674"}}
    path.write_text(json.dumps({"type": "FeatureCollection", "crs": crs, "features": features}))


# SICAR --------------------------------------------------------------------------------


def test_sicar_transforma_e_deduplica(tmp_path: Path) -> None:
    _manifesto(tmp_path, "SICAR", "5218805_0000000.json")
    outro = CAR.replace("7281", "AAAA", 1)
    _geojson(
        tmp_path / "5218805_0000000.json",
        [
            _feature(CAR, "AT", QUADRADO),
            _feature(
                outro, "SU", QUADRADO, mun=5211909, nome="Jataí"
            ),  # código de Rio Verde, imóvel em Jataí
        ],
    )
    _geojson(tmp_path / "5218805_0010000.json", [_feature(CAR, "AT", QUADRADO)])  # repetido em outra página
    carga = transformar_sicar(tmp_path)
    por_codigo = {r[0]: r for r in carga.registros}
    assert len(carga.registros) == 2
    assert por_codigo[CAR][2] == "ATIVO"
    # Município real vem de cod_municipio_ibge, não do código do CAR
    assert por_codigo[outro][1] == 5211909 and por_codigo[outro][5] == "Jataí"
    assert por_codigo[outro][2] == "SUSPENSO"
    geom = shapely.from_wkb(por_codigo[CAR][4])
    assert shapely.get_srid(geom) == 4674 and geom.geom_type == "MultiPolygon"
    assert carga.source_as_of == date(2026, 10, 13)


def test_sicar_quarentena_e_imovel_sem_geometria(tmp_path: Path) -> None:
    _manifesto(tmp_path, "SICAR", "a.json")
    gravata = CAR.replace("7281", "BBBB", 1)
    _geojson(
        tmp_path / "a.json",
        [
            _feature("GO-5218805-XYZ", "AT", QUADRADO),
            _feature(CAR, "ZZ", QUADRADO),
            _feature(gravata, "AT", GRAVATA_ASSIMETRICA),
        ],
    )
    carga = transformar_sicar(tmp_path)
    motivos = sorted(q[1] for q in carga.quarentena)
    assert motivos == [
        "codigo_car_invalido",
        "geometria_descartada:reparo_alterou_area",
        "status_desconhecido",
    ]
    # O CAR com geometria irrecuperável continua na base, sem geometria
    assert len(carga.registros) == 1
    reg = carga.registros[0]
    assert reg[0] == gravata and reg[4] is None and reg[7] == "reparo_alterou_area"


# IBAMA --------------------------------------------------------------------------------

POLIGONO_WKT = "POLYGON ((-50.9 -17.8, -50.89 -17.8, -50.89 -17.79, -50.9 -17.79, -50.9 -17.8))"


def _linha(**v: str) -> dict[str, str]:
    base = dict.fromkeys(CAMPOS, "")
    base.update(
        SEQ_TAD="1",
        DES_STATUS_FORMULARIO="Lavrado",
        SIT_CANCELADO="N",
        NUM_PROCESSO="02001.000001/2024-01",
        CPF_CNPJ_EMBARGADO="52998224725",
        DAT_EMBARGO="2024-03-15 10:00:00",
        COD_MUNICIPIO="5218805",
        NUM_LONGITUDE_TAD="-50.895",
        NUM_LATITUDE_TAD="-17.795",
        GEOM_AREA_EMBARGADA=POLIGONO_WKT,
        ULTIMA_ATUALIZACAO_RELATORIO="2026-10-13 00:52:41",
    )
    base.update(v)
    return base


def _csv(path: Path, linhas: list[dict[str, str]]) -> None:
    cab = list(CAMPOS)

    def q(x: str) -> str:
        return '"' + x.replace('"', '""') + '"'

    texto = ";".join(q(c) for c in cab) + "\n"
    texto += "".join(";".join(q(linha[c]) for c in cab) + "\n" for linha in linhas)
    path.write_text(texto, encoding="utf-8-sig")  # a fonte vem com BOM


def test_ibama_classifica_situacao_e_preserva_embargo(tmp_path: Path) -> None:
    _manifesto(tmp_path, "IBAMA", "termo_de_embargo.csv")
    _csv(
        tmp_path / "termo_de_embargo.csv",
        [
            _linha(SEQ_TAD="1"),
            _linha(SEQ_TAD="2", SIT_DESEMBARGO="S"),
            _linha(SEQ_TAD="3", DES_STATUS_FORMULARIO="Cancelado", SIT_CANCELADO="S"),
            _linha(SEQ_TAD="", DES_STATUS_FORMULARIO=""),
            # Atividade embargada, sem polígono, com data absurda e documento inválido
            _linha(
                SEQ_TAD="5",
                GEOM_AREA_EMBARGADA="",
                DAT_EMBARGO="2925-09-25 15:10:00",
                CPF_CNPJ_EMBARGADO="52998224724",
            ),
            # Polígono irrecuperável e ponto fora do Brasil
            _linha(
                SEQ_TAD="6",
                GEOM_AREA_EMBARGADA="POLYGON ((0 0, 10 10, 10 0, 0 2, 0 0))",
                NUM_LONGITUDE_TAD="38.3",
                NUM_LATITUDE_TAD="4.8",
            ),
            _linha(SEQ_TAD="1"),  # duplicado
        ],
    )
    carga = transformar_ibama(tmp_path, hoje=date(2026, 10, 13))
    reg = {r[1]: dict(zip(carga.colunas, r, strict=True)) for r in carga.registros}
    assert set(reg) == {"1", "2", "5", "6"}  # cancelado não é embargo; sem SEQ_TAD vai para quarentena
    assert reg["1"]["situacao"] == "VIGENTE" and reg["2"]["situacao"] == "DESEMBARGADO"
    assert reg["1"]["tipo_documento"] == "CPF" and reg["1"]["documento_autuado"] == "52998224725"
    assert reg["1"]["documento_mascarado"] == "***.982.247-**"
    assert reg["1"]["tipo_geom_origem"] == "POLIGONO" and reg["1"]["ponto"] is not None
    assert reg["5"]["tipo_geom_origem"] == "PONTO" and reg["5"]["data_embargo"] is None
    assert reg["5"]["documento_autuado"] is None
    assert reg["6"]["tipo_geom_origem"] == "NENHUMA"  # polígono descartado e ponto fora do Brasil
    motivos = sorted(q[1] for q in carga.quarentena)
    assert motivos == [
        "data_embargo_invalida_mantido_sem_data",
        "documento_invalido_mantido_sem_documento",
        "poligono_descartado:reparo_alterou_area",
        "seq_tad_duplicado",
        "status_ou_seq_tad_ausente",
    ]
    assert carga.source_as_of == date(2026, 10, 13)
