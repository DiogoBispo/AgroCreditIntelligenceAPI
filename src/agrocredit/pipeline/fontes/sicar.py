"""SICAR: imóveis rurais via WFS oficial (sem captcha). Ver documents/FASE0_FONTES.md §2."""

import re
import urllib.parse
import urllib.request
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import geopandas as gpd
import shapely
from shapely.geometry.base import BaseGeometry

from agrocredit.pipeline.bronze import USER_AGENT, Manifesto, baixar
from agrocredit.pipeline.geometria import sanitizar
from agrocredit.pipeline.gold import Carga, Quarentena

FONTE = "SICAR"
WFS = "https://geoserver.car.gov.br/geoserver/sicar/wfs"
PAGINA = 10_000
CAMPOS_OBRIGATORIOS = {"cod_imovel", "status_imovel", "area", "cod_municipio_ibge", "municipio", "uf"}
STATUS = {"AT": "ATIVO", "PE": "PENDENTE", "SU": "SUSPENSO", "CA": "CANCELADO"}
CAR_REGEX = re.compile(r"^[A-Z]{2}-\d{7}-[A-F0-9]{32}$")
COLUNAS = (
    "codigo_car",
    "codigo_ibge",
    "status_cadastro",
    "area_declarada_ha",
    "geometria",
    "municipio",
    "uf",
    "problema_geometria",
    "tipo_imovel",
    "modulos_fiscais",
)


def _filtro(codigo_ibge: int, uf: str) -> str:
    # O código IBGE embutido no CAR nem sempre é o município real (0,1–0,4%): traz os dois casos
    return f"cod_municipio_ibge={codigo_ibge} OR cod_imovel LIKE '{uf}-{codigo_ibge}-%'"


def _url(uf: str, cql: str, **extra: str) -> str:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"sicar:sicar_imoveis_{uf.lower()}",
        "CQL_FILTER": cql,
        **extra,
    }
    return f"{WFS}?{urllib.parse.urlencode(params)}"


def _contar(uf: str, cql: str) -> int:
    # URL montada a partir da constante https WFS
    req = urllib.request.Request(_url(uf, cql, resultType="hits"), headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310
        m = re.search(rb'numberMatched="(\d+)"', resp.read())
    if not m:
        raise RuntimeError("WFS do SICAR não informou numberMatched")
    return int(m.group(1))


def baixar_sicar(escopo: Sequence[tuple[int, str]], diretorio: Path, versao: str) -> Manifesto:
    """Um arquivo GeoJSON por (município, página). `escopo` = [(codigo_ibge, uf), ...]."""
    manifesto = Manifesto(FONTE, versao)
    for codigo_ibge, uf in escopo:
        cql = _filtro(codigo_ibge, uf)
        total = _contar(uf, cql)
        for inicio in range(0, max(total, 1), PAGINA):
            url = _url(
                uf,
                cql,
                outputFormat="application/json",
                sortBy="cod_imovel",
                count=str(PAGINA),
                startIndex=str(inicio),
            )
            manifesto.arquivos.append(baixar(url, diretorio / f"{codigo_ibge}_{inicio:07d}.json"))
    manifesto.salvar(diretorio)
    return manifesto


def _ler(arquivo: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(arquivo)
    if gdf.empty:
        return gdf
    faltando = CAMPOS_OBRIGATORIOS - set(gdf.columns)
    if faltando:
        raise ValueError(f"Schema drift no SICAR ({arquivo.name}): colunas ausentes {sorted(faltando)}")
    if gdf.crs is None:
        raise ValueError(f"{arquivo.name} sem CRS: não presumir projeção")
    return gdf.to_crs(4674) if gdf.crs.to_epsg() != 4674 else gdf


def _registro(props: dict[str, Any], geom: BaseGeometry | None) -> tuple[tuple[Any, ...] | None, str | None]:
    """Devolve (registro, motivo). Geometria irrecuperável: registro mantido SEM geometria + motivo."""
    codigo = str(props["cod_imovel"] or "").replace(".", "").strip().upper()
    if not CAR_REGEX.match(codigo):
        return None, "codigo_car_invalido"
    status = STATUS.get(str(props["status_imovel"] or "").strip().upper())
    if status is None:
        return None, "status_desconhecido"
    area = Decimal(str(props["area"])) if props["area"] is not None else None
    geo = sanitizar(geom, float(area) if area else None)
    ewkb = bytes(shapely.to_wkb(shapely.set_srid(geo.geom, 4674), include_srid=True)) if geo.geom else None
    municipio = (int(props["cod_municipio_ibge"]), str(props["municipio"]), str(props["uf"]))
    tipo = str(props.get("tipo_imovel") or "").strip().upper() or None
    mf_bruto = props.get("m_fiscal")
    mf = None if mf_bruto is None or mf_bruto != mf_bruto else Decimal(str(mf_bruto))  # NaN != NaN
    registro = (codigo, municipio[0], status, area, ewkb, municipio[1], municipio[2], geo.motivo, tipo, mf)
    return registro, f"geometria_descartada:{geo.motivo}" if geo.motivo else None


def transformar_sicar(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    registros: dict[str, tuple[Any, ...]] = {}
    quarentena: Quarentena = []
    for arquivo in sorted(diretorio.glob("*.json")):
        if arquivo.name == "manifest.json":
            continue
        gdf = _ler(arquivo)
        if gdf.empty:
            continue
        atributos = gdf.drop(columns=gdf.geometry.name).to_dict("records")
        for props, geom in zip(atributos, gdf.geometry.array, strict=True):
            reg, motivo = _registro({str(k): v for k, v in props.items()}, geom)
            if motivo:
                payload = {"arquivo": arquivo.name, "wkt": geom.wkt if geom is not None else None}
                quarentena.append((str(props["cod_imovel"]), motivo, payload))
            if reg is None:
                continue
            registros[reg[0]] = reg  # um CAR pode vir em duas páginas/filtros: deduplica pelo código
    return Carga(
        base="imoveis_car",
        fonte=FONTE,
        versao=manifesto.versao,
        source_as_of=manifesto.as_of,
        colunas=COLUNAS,
        registros=list(registros.values()),
        quarentena=quarentena,
        manifesto=manifesto,
    )
