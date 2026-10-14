"""Camadas territoriais: UCs (CNUC/MMA), florestas públicas (CNFP/SFB), quilombolas (INCRA) e PRODES (INPE).

Terras indígenas (FUNAI) seguem sem canal atual acessível (FASE0_FONTES.md §8).
Só entram polígonos que tocam os envelopes do escopo (municípios cobertos + margem), salvos no manifesto.
"""

import hashlib
import json
import re
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box

from agrocredit.domain.enums import Camada
from agrocredit.pipeline.bronze import USER_AGENT, Manifesto, baixar
from agrocredit.pipeline.geometria import sanitizar
from agrocredit.pipeline.gold import Carga, Quarentena

COLUNAS = ("camada", "id", "nome", "atributos", "geometria")
Envelope = tuple[float, float, float, float]

URL_CNUC = (
    "https://dados.mma.gov.br/dataset/44b6dc8a-dc82-4a84-8d95-1b0da7c85dac/resource/"
    "6ba9a557-87e8-4882-acb7-b3e0f0ea192d/download/shp_cnuc_2025_08.zip"
)
AS_OF_CNUC = date(2025, 8, 1)  # versão no nome do arquivo; as de 2026 só existem em link com login
URL_CNFP = (
    "https://mapas.florestal.gov.br/portal/sharing/rest/content/items/2b752dfbf9254920bf8990ce363918af/data"
)
AS_OF_CNFP = date(2026, 7, 23)  # "modified" do item no portal do SFB (CNFP 2025)
URL_QUILOMBOLAS = "https://certificacao.incra.gov.br/csv_shp/zip/%C3%81reas%20de%20Quilombolas.zip"
# Cópia da base da Funai sincronizada pelo IBAMA (PAMGIA, "BasesSincronizadas"): o WFS da própria Funai
# recusa downloads (403). Traz as fases que o MCR 2-9 item 7 considera (homologada, regularizada, RI).
URL_TI = (
    "https://pamgia.ibama.gov.br/server/rest/services/BasesSincronizadas/"
    "lim_terra_indigena_funai_a/MapServer/0/query"
)
PAGINA_ARCGIS = 1000
WFS_PRODES = "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-{bioma}-nb/ows"
# Biomas dos municípios do MVP (workspaces confirmados na Fase 0); ampliar ao expandir o escopo
BIOMAS_PRODES = ("cerrado", "mata-atlantica")

# CNUC traz a categoria por extenso; o ruleset usa códigos curtos
CATEGORIAS_UC = {
    "AREA_DE_PROTECAO_AMBIENTAL": "APA",
    "AREA_DE_RELEVANTE_INTERESSE_ECOLOGICO": "ARIE",
    "ESTACAO_ECOLOGICA": "ESEC",
    "FLORESTA": "FLORESTA",
    "MONUMENTO_NATURAL": "MONA",
    "PARQUE": "PARQUE",
    "REFUGIO_DE_VIDA_SILVESTRE": "RVS",
    "RESERVA_BIOLOGICA": "REBIO",
    "RESERVA_DE_DESENVOLVIMENTO_SUSTENTAVEL": "RDS",
    "RESERVA_DE_FAUNA": "REFAU",
    "RESERVA_EXTRATIVISTA": "RESEX",
    "RESERVA_PARTICULAR_DO_PATRIMONIO_NATURAL": "RPPN",
}


def normalizar(texto: Any) -> str | None:
    """'Título Parcial' → 'TITULO_PARCIAL'."""
    if texto is None or (isinstance(texto, float) and pd.isna(texto)):
        return None
    sem_acento = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]+", "_", sem_acento.upper()).strip("_") or None


def ler_shapefile(caminho: Path, layer: str | None = None) -> gpd.GeoDataFrame:
    """Lê shapefile zipado tolerando UTF-8 truncado no DBF (o CNUC corta campos no meio de um caractere).

    Lê os bytes como latin-1 (sem perda) e redecodifica como UTF-8, trocando só o caractere cortado.
    """
    kwargs: dict[str, Any] = {"encoding": "latin1"}
    if layer:
        kwargs["layer"] = layer
    gdf = gpd.read_file(f"zip://{caminho}", **kwargs)
    for col in gdf.columns:
        if col != gdf.geometry.name and pd.api.types.is_string_dtype(gdf[col]):
            gdf[col] = gdf[col].map(
                lambda v: v.encode("latin1").decode("utf-8", "replace") if isinstance(v, str) else v
            )
    if gdf.crs is None:
        raise ValueError(f"{caminho.name} sem CRS: não presumir projeção")
    resultado: gpd.GeoDataFrame = gdf.to_crs(4674) if gdf.crs.to_epsg() != 4674 else gdf
    return resultado


def _no_escopo(gdf: gpd.GeoDataFrame, envelopes: Sequence[Envelope]) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    alvo = shapely.union_all([box(*e) for e in envelopes])
    return gdf[gdf.intersects(alvo)]


def _jsonavel(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _carga(
    fonte: str,
    manifesto: Manifesto,
    as_of: date,
    camada: Camada,
    gdf: gpd.GeoDataFrame,
    chave: Callable[[dict[str, Any]], str],
    nome: Callable[[dict[str, Any]], str | None],
    atributos: Callable[[dict[str, Any]], dict[str, Any]],
    area_ref: Callable[[dict[str, Any]], float | None] = lambda _: None,
) -> Carga:
    registros: dict[str, tuple[Any, ...]] = {}
    quarentena: Quarentena = []
    props_lista = gdf.drop(columns=gdf.geometry.name).to_dict("records")
    for props_raw, geom in zip(props_lista, gdf.geometry.array, strict=True):
        props = {str(k): _jsonavel(v) for k, v in props_raw.items()}
        ident = chave(props)
        res = sanitizar(geom, area_ref(props))
        if res.geom is None:
            quarentena.append((ident, str(res.motivo), {"nome": nome(props)}))
            continue
        if ident in registros:
            quarentena.append((ident, "id_duplicado", None))
            continue
        ewkb = bytes(shapely.to_wkb(shapely.set_srid(res.geom, 4674), include_srid=True))
        registros[ident] = (
            camada.value,
            ident,
            nome(props),
            json.dumps(atributos(props), ensure_ascii=False),
            ewkb,
        )
    manifesto.source_as_of = as_of.isoformat()
    return Carga(
        base="camadas_territoriais",
        fonte=fonte,
        versao=manifesto.versao,
        source_as_of=as_of,
        colunas=COLUNAS,
        registros=list(registros.values()),
        quarentena=quarentena,
        manifesto=manifesto,
    )


def _hash(*partes: Any) -> str:
    return "h:" + hashlib.sha1("|".join(map(str, partes)).encode(), usedforsecurity=False).hexdigest()


def _envelopes(manifesto: Manifesto) -> list[Envelope]:
    return [(e[0], e[1], e[2], e[3]) for e in manifesto.parametros["envelopes"]]


def _zip(diretorio: Path) -> Path:
    zips = sorted(diretorio.glob("*.zip"))
    if len(zips) != 1:
        raise ValueError(f"Esperado 1 arquivo .zip em {diretorio}, encontrados {len(zips)}")
    return zips[0]


# Downloads ---------------------------------------------------------------------------------


def baixar_arquivo_unico(
    url: str, nome: str, fonte: str
) -> Callable[[Path, str, Sequence[Envelope]], Manifesto]:
    def baixar_fonte(diretorio: Path, versao: str, envelopes: Sequence[Envelope]) -> Manifesto:
        manifesto = Manifesto(fonte, versao, [baixar(url, diretorio / nome)])
        manifesto.parametros["envelopes"] = [list(e) for e in envelopes]
        manifesto.salvar(diretorio)
        return manifesto

    return baixar_fonte


def baixar_prodes(diretorio: Path, versao: str, envelopes: Sequence[Envelope]) -> Manifesto:
    """WFS por bioma e envelope. Biomas que não cobrem o envelope devolvem coleção vazia."""
    manifesto = Manifesto("PRODES", versao)
    manifesto.parametros["envelopes"] = [list(e) for e in envelopes]
    for bioma in BIOMAS_PRODES:
        for i, (x0, y0, x1, y1) in enumerate(envelopes):
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"prodes-{bioma}-nb:yearly_deforestation",
                "outputFormat": "application/json",
                "srsName": "EPSG:4674",
                "bbox": f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4674",
            }
            url = f"{WFS_PRODES.format(bioma=bioma)}?{urllib.parse.urlencode(params)}"
            manifesto.arquivos.append(baixar(url, diretorio / f"{bioma}_{i}.json", timeout=900))
    manifesto.salvar(diretorio)
    return manifesto


def _arcgis_json(params: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 (URL https fixa)
        f"{URL_TI}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310
        dados: dict[str, Any] = json.load(resp)
    if "error" in dados:  # o ArcGIS responde erro com HTTP 200
        raise RuntimeError(f"ArcGIS (PAMGIA) retornou erro: {dados['error']}")
    return dados


def baixar_ti(diretorio: Path, versao: str, envelopes: Sequence[Envelope]) -> Manifesto:
    """ArcGIS REST por envelope, paginado. Saída em GeoJSON com geometria em SIRGAS 2000.

    Um envelope sem TI é resultado legítimo; para não confundir com download quebrado, confere que a camada
    inteira tem registros e guarda a data da última sincronização com a Funai (data de referência).
    """
    total = _arcgis_json({"where": "1=1", "returnCountOnly": "true", "f": "json"}).get("count", 0)
    if not total:
        raise RuntimeError("Camada de terras indígenas do PAMGIA vazia: download não confiável")
    estat = _arcgis_json(
        {
            "where": "1=1",
            "outStatistics": json.dumps(
                [{"statisticType": "max", "onStatisticField": "dt_sync", "outStatisticFieldName": "s"}]
            ),
            "f": "json",
        }
    )
    manifesto = Manifesto("FUNAI", versao)
    manifesto.parametros["envelopes"] = [list(e) for e in envelopes]
    manifesto.parametros["registros_na_camada"] = total
    manifesto.parametros["dt_sync_ms"] = estat["features"][0]["attributes"]["s"]
    for i, (x0, y0, x1, y1) in enumerate(envelopes):
        for pagina in range(100):  # limite de segurança
            params = {
                "where": "1=1",
                "geometry": f"{x0},{y0},{x1},{y1}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4674",
                "outSR": "4674",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "orderByFields": "objectid",
                "resultOffset": str(pagina * PAGINA_ARCGIS),
                "resultRecordCount": str(PAGINA_ARCGIS),
                "f": "geojson",
            }
            destino = diretorio / f"ti_{i}_{pagina:03d}.json"
            manifesto.arquivos.append(baixar(f"{URL_TI}?{urllib.parse.urlencode(params)}", destino))
            resposta = json.loads(destino.read_text())
            if "error" in resposta or "features" not in resposta:
                raise RuntimeError(f"Resposta inválida do PAMGIA: {str(resposta)[:200]}")
            if len(resposta["features"]) < PAGINA_ARCGIS:
                break
    manifesto.salvar(diretorio)
    return manifesto


# Transformações ---------------------------------------------------------------------------


def transformar_cnuc(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    arquivo = _zip(diretorio)
    camadas = [n for n in zipfile.ZipFile(arquivo).namelist() if n.endswith(".shp") and "pontos" not in n]
    gdf = _no_escopo(ler_shapefile(arquivo, Path(camadas[0]).stem), _envelopes(manifesto))
    gdf = gdf[gdf["situacao"].map(normalizar) == "ATIVO"]
    as_of = date.fromisoformat(manifesto.source_as_of) if manifesto.source_as_of else AS_OF_CNUC

    def atributos(p: dict[str, Any]) -> dict[str, Any]:
        cat = normalizar(p.get("categoria"))
        return {
            "categoria": CATEGORIAS_UC.get(cat or "", cat),
            "categoria_original": p.get("categoria"),
            "grupo": normalizar(p.get("grupo")),
            "esfera": normalizar(p.get("esfera")),
            "cd_cnuc": p.get("cd_cnuc"),
        }

    def area(p: dict[str, Any]) -> float | None:
        try:
            return float(p["ha_total"]) if p.get("ha_total") else None
        except ValueError:
            return None

    return _carga(
        "CNUC",
        manifesto,
        as_of,
        Camada.UC,
        gdf,
        chave=lambda p: str(p.get("cd_cnuc") or p.get("uc_id")),
        nome=lambda p: p.get("nome_uc"),
        atributos=atributos,
        area_ref=area,
    )


def transformar_cnfp(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    gdf = _no_escopo(ler_shapefile(_zip(diretorio)), _envelopes(manifesto))
    as_of = date.fromisoformat(manifesto.source_as_of) if manifesto.source_as_of else AS_OF_CNFP

    def atributos(p: dict[str, Any]) -> dict[str, Any]:
        tipo = normalizar(p.get("tipo"))  # "TIPO B" → "TIPO_B"
        return {
            "tipo": tipo.removeprefix("TIPO_") if tipo else None,
            "classe": p.get("classe"),
            "categoria": p.get("categoria"),
            "governo": p.get("governo"),
            "estagio": p.get("estagio"),
        }

    return _carga(
        "SFB",
        manifesto,
        as_of,
        Camada.FPND,
        gdf,
        # CNFP não tem identificador estável: código quando houver; senão hash de nome + geocódigo + área
        chave=lambda p: str(p.get("codigo") or _hash(p.get("nome"), p.get("geocodigo"), p.get("area_ha"))),
        nome=lambda p: p.get("nome"),
        atributos=atributos,
        area_ref=lambda p: float(p["area_ha"]) if p.get("area_ha") else None,
    )


def transformar_quilombolas(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    gdf = _no_escopo(ler_shapefile(_zip(diretorio)), _envelopes(manifesto))
    # O INCRA gera o arquivo no momento do pedido: a data do download é a data dos dados
    return _carga(
        "INCRA",
        manifesto,
        manifesto.as_of,
        Camada.QUILOMBOLA,
        gdf,
        chave=lambda p: str(p.get("nr_process") or p.get("nm_comunid")),
        nome=lambda p: p.get("nm_comunid"),
        atributos=lambda p: {
            "fase": normalizar(p.get("fase")),  # RTID, PORTARIA, DECRETO, TITULADO, TITULO_PARCIAL...
            "processo": p.get("nr_process"),
            "esfera": normalizar(p.get("esfera")),
        },
        area_ref=lambda p: float(p["nr_area_ha"]) if p.get("nr_area_ha") else None,
    )


def _ler_geojsons(arquivos: Iterable[Path]) -> gpd.GeoDataFrame:
    # O WFS do TerraBrasilis às vezes declara WGS 84 mesmo pedindo SIRGAS 2000: reprojeta cada parte
    partes = [p.to_crs(4674) for p in (gpd.read_file(a) for a in arquivos) if not p.empty]
    if not partes:
        return gpd.GeoDataFrame(geometry=[], crs=4674)
    gdf = pd.concat(partes, ignore_index=True)
    return gpd.GeoDataFrame(gdf, geometry=gdf.geometry.name, crs=4674)


def transformar_prodes(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    gdf = _ler_geojsons(sorted(p for p in diretorio.glob("*.json") if p.name != "manifest.json"))
    # Data de referência: publicação mais recente informada; sem ela, a imagem mais recente
    pubs = [str(v) for v in gdf.get("pub_date", pd.Series(dtype=str)).dropna()]
    if pubs:
        as_of = datetime.strptime(max(pubs), "%Y%m%d").replace(tzinfo=UTC).date()
    elif not gdf.empty:
        as_of = date.fromisoformat(str(gdf["image_date"].dropna().max())[:10])
    else:
        as_of = manifesto.as_of
    return _carga(
        "PRODES",
        manifesto,
        as_of,
        Camada.PRODES,
        gdf,
        chave=lambda p: str(p.get("uuid") or p.get("fid")),
        nome=lambda p: p.get("class_name"),
        atributos=lambda p: {
            "data_deteccao": str(p["image_date"])[:10] if p.get("image_date") else None,
            "ano_prodes": p.get("year"),
            "classe": p.get("main_class"),
            "fonte": p.get("source"),
        },
        # Sem área de referência: area_km diverge >1% da área geodésica em 66% dos polígonos VÁLIDOS
        # (outra projeção), então não serve para a guarda de reparo
    )


def transformar_ti(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    gdf = _ler_geojsons(sorted(diretorio.glob("ti_*.json")))
    # Data de referência: última sincronização da camada inteira com a Funai (vale mesmo sem TI no entorno)
    as_of = datetime.fromtimestamp(int(manifesto.parametros["dt_sync_ms"]) / 1000, UTC).date()
    return _carga(
        "FUNAI",
        manifesto,
        as_of,
        Camada.TI,
        gdf,
        chave=lambda p: str(p.get("terrai_cod") or p.get("gid")),
        nome=lambda p: p.get("terrai_nom"),
        atributos=lambda p: {
            "fase": normalizar(p.get("fase_ti")),  # HOMOLOGADA, REGULARIZADA, ENCAMINHADA_RI...
            "modalidade": normalizar(p.get("modalidade")),  # RESERVA_INDIGENA, TRADICIONALMENTE_OCUPADA...
            "terrai_cod": p.get("terrai_cod"),
            "etnia": p.get("etnia_nome"),
            "data_atualizacao_funai": p.get("data_atual"),
        },
    )
