"""Embargos estaduais de MG (IDE-Sisema). Ver FASE0_FONTES.md §9.

A fonte não informa se o embargo segue vigente: tudo entra como VIGENTE e a regra R-EMB-EST limita o
resultado a ALERTA em MG (ruleset: ufs_sem_situacao). CPF vem mascarado; CNPJ vem completo.
"""

import re
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely

from agrocredit.domain.validators import mask_document, parse_document
from agrocredit.pipeline.bronze import USER_AGENT, Manifesto, baixar
from agrocredit.pipeline.geometria import sanitizar
from agrocredit.pipeline.gold import Carga, Quarentena

FONTE = "EMBARGOS_MG"
WFS = "https://geoserver.meioambiente.mg.gov.br/IDE/ows"
CAMADA = "IDE:ide_2304_mg_areas_embargadas_sisema_pol"
PAGINA = 5_000
CAMPOS = {"id", "num_ai", "doc_fiscal", "data_fisca", "outras_pen"}
COLUNAS = (
    "orgao",
    "id_embargo",
    "numero_processo",
    "tipo_documento",
    "documento_autuado",
    "documento_mascarado",
    "situacao",
    "data_infracao",
    "data_embargo",
    "codigo_ibge",
    "tipo_geom_origem",
    "geometria",
    "ponto",
)


def _url(**extra: str) -> str:
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": CAMADA, **extra}
    return f"{WFS}?{urllib.parse.urlencode(params)}"


def baixar_mg(diretorio: Path, versao: str) -> Manifesto:
    req = urllib.request.Request(_url(resultType="hits"), headers={"User-Agent": USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310
        m = re.search(rb'numberMatched="(\d+)"', resp.read())
    if not m:
        raise RuntimeError("WFS da IDE-Sisema não informou numberMatched")
    manifesto = Manifesto(FONTE, versao)
    for inicio in range(0, int(m.group(1)), PAGINA):
        url = _url(outputFormat="application/json", sortBy="id", count=str(PAGINA), startIndex=str(inicio))
        manifesto.arquivos.append(baixar(url, diretorio / f"pagina_{inicio:07d}.json", timeout=900))
    manifesto.salvar(diretorio)
    return manifesto


def _data(valor: Any) -> date | None:
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def transformar_mg(diretorio: Path, hoje: date | None = None) -> Carga:
    hoje = hoje or date.today()
    manifesto = Manifesto.carregar(diretorio)
    partes = [gpd.read_file(p) for p in sorted(diretorio.glob("pagina_*.json"))]
    gdf = pd.concat([p for p in partes if not p.empty], ignore_index=True)
    faltando = CAMPOS - set(gdf.columns)
    if faltando:
        raise ValueError(f"Schema drift na IDE-Sisema: colunas ausentes {sorted(faltando)}")
    registros: dict[str, tuple[Any, ...]] = {}
    quarentena: Quarentena = []
    datas: list[date] = []
    for props, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry.array, strict=True):
        ident = str(props["id"])
        res = sanitizar(geom)
        if res.geom is None:
            quarentena.append((ident, str(res.motivo), {"num_ai": props.get("num_ai")}))
            continue
        try:
            doc = parse_document(str(props.get("doc_fiscal") or ""))  # só o CNPJ vem sem máscara
        except ValueError:
            doc = None
        data_fisc = _data(props.get("data_fisca"))
        if data_fisc and data_fisc <= hoje:
            datas.append(data_fisc)
        registros[ident] = (
            "SEMAD-MG",
            ident,
            props.get("num_ai"),
            doc.tipo.value if doc else None,
            doc.valor if doc else None,
            mask_document(doc) if doc else props.get("doc_fiscal"),
            "VIGENTE",  # a fonte não traz situação: R-EMB-EST trata MG como no máximo ALERTA
            data_fisc,
            data_fisc,
            None,
            "POLIGONO",
            bytes(shapely.to_wkb(shapely.set_srid(res.geom, 4674), include_srid=True)),
            None,
        )
    if not datas:
        raise ValueError("IDE-Sisema sem data_fisca válida")
    # Data de referência: auto de infração mais recente (limite inferior da atualidade da base)
    as_of = max(datas)
    manifesto.source_as_of = as_of.isoformat()
    return Carga(
        base="embargos",
        fonte=FONTE,
        versao=manifesto.versao,
        source_as_of=as_of,
        colunas=COLUNAS,
        registros=list(registros.values()),
        quarentena=quarentena,
        manifesto=manifesto,
    )
