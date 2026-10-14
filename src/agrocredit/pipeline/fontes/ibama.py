"""Embargos do IBAMA: CSV diário oficial com WKT na linha. Ver documents/FASE0_FONTES.md §3.

Carga nacional (não só o escopo): R-EMB-DOC precisa achar embargo do tomador em qualquer UF.
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import shapely

from agrocredit.domain.validators import Documento, mask_document, parse_document
from agrocredit.pipeline.bronze import Manifesto, baixar
from agrocredit.pipeline.geometria import sanitizar
from agrocredit.pipeline.gold import Carga, Quarentena

FONTE = "IBAMA"
URL = (
    "https://stibamadadosabertosprd.blob.core.windows.net/dados-abertos/dados/"
    "TERMOS_DE_EMBARGO/TERMO_EMBARGO/termo_de_embargo.csv"
)
ARQUIVO = "termo_de_embargo.csv"
CAMPOS = (
    "SEQ_TAD",
    "DES_STATUS_FORMULARIO",
    "SIT_CANCELADO",
    "SIT_DESEMBARGO",
    "NUM_PROCESSO",
    "CPF_CNPJ_EMBARGADO",
    "DAT_EMBARGO",
    "COD_MUNICIPIO",
    "NUM_LONGITUDE_TAD",
    "NUM_LATITUDE_TAD",
    "GEOM_AREA_EMBARGADA",
    "QTD_AREA_EMBARGADA",
    "ULTIMA_ATUALIZACAO_RELATORIO",
)
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
DATA_MINIMA = date(1980, 1, 1)
# Envelope do Brasil: pontos fora dele são erro de digitação na fonte
_LON = (-74.0, -28.0)
_LAT = (-34.0, 6.0)


def baixar_ibama(diretorio: Path, versao: str) -> Manifesto:
    manifesto = Manifesto(FONTE, versao, [baixar(URL, diretorio / ARQUIVO)])
    manifesto.salvar(diretorio)
    return manifesto


def _ler(csv: Path) -> tuple[list[tuple[Any, ...]], int]:
    """Classifica a situação no DuckDB. Devolve linhas de embargo (vigente/desembargado) e o nº ignorado."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM read_csv(?, delim=';', quote='\"', header=true, all_varchar=true,"
        " max_line_size=50000000)",
        [str(csv)],
    )
    faltando = set(CAMPOS) - {c[0] for c in con.execute("DESCRIBE t").fetchall()}
    if faltando:
        raise ValueError(f"Schema drift no IBAMA: colunas ausentes {sorted(faltando)}")
    # Lavrado + não cancelado: VIGENTE, ou DESEMBARGADO se houve desembargo. Cancelado, substituído e
    # excluído não são embargos. Status nulo (linhas sem SEQ_TAD) vai para a quarentena.
    linhas = con.execute(
        """
        SELECT SEQ_TAD, NUM_PROCESSO, CPF_CNPJ_EMBARGADO, DAT_EMBARGO, COD_MUNICIPIO,
               NUM_LONGITUDE_TAD, NUM_LATITUDE_TAD, GEOM_AREA_EMBARGADA, QTD_AREA_EMBARGADA,
               ULTIMA_ATUALIZACAO_RELATORIO,
               CASE
                 WHEN DES_STATUS_FORMULARIO IS NULL OR SEQ_TAD IS NULL THEN 'SEM_STATUS'
                 WHEN DES_STATUS_FORMULARIO = 'Lavrado' AND SIT_CANCELADO = 'N'
                   THEN CASE WHEN SIT_DESEMBARGO = 'S' THEN 'DESEMBARGADO' ELSE 'VIGENTE' END
               END AS situacao
        FROM t
        """
    ).fetchall()
    ignoradas = sum(1 for r in linhas if r[-1] is None)
    return [r for r in linhas if r[-1] is not None], ignoradas


def _data(valor: str | None, hoje: date) -> date | None:
    if not valor:
        return None
    try:
        d = datetime.fromisoformat(valor.strip()).date()
    except ValueError:
        return None
    return d if DATA_MINIMA <= d <= hoje else None


def _ponto(lon: str | None, lat: str | None) -> bytes | None:
    try:
        x, y = float(str(lon).replace(",", ".")), float(str(lat).replace(",", "."))
    except ValueError:
        return None
    if not (_LON[0] <= x <= _LON[1] and _LAT[0] <= y <= _LAT[1]):
        return None
    return bytes(shapely.to_wkb(shapely.set_srid(shapely.Point(x, y), 4674), include_srid=True))


def _documento(raw: str | None) -> Documento | None:
    if not raw:
        return None
    try:
        return parse_document(raw)
    except ValueError:
        return None


def _area_ha(valor: str | None) -> float | None:
    try:
        return float(str(valor).replace(".", "").replace(",", ".")) if valor else None
    except ValueError:
        return None


def _poligono(wkt: str | None, area_termo: str | None) -> tuple[bytes | None, str | None]:
    if not wkt:
        return None, None
    try:
        geom = shapely.from_wkt(wkt)
    except shapely.errors.ShapelyError:
        return None, "wkt_invalido"
    # A área informada no termo é a referência da guarda de área no reparo
    res = sanitizar(geom, _area_ha(area_termo))
    if res.geom is None:
        return None, res.motivo
    return bytes(shapely.to_wkb(shapely.set_srid(res.geom, 4674), include_srid=True)), None


def _registro(linha: tuple[Any, ...], hoje: date, quarentena: Quarentena) -> tuple[Any, ...]:
    seq, processo, doc_raw, dat_embargo, cod_mun, lon, lat, wkt, area_termo, _, situacao = linha
    doc = _documento(doc_raw)
    if doc_raw and doc is None:
        quarentena.append((seq, "documento_invalido_mantido_sem_documento", None))
    data_embargo = _data(dat_embargo, hoje)
    if dat_embargo and data_embargo is None:
        quarentena.append((seq, "data_embargo_invalida_mantido_sem_data", {"DAT_EMBARGO": dat_embargo}))
    poligono, motivo = _poligono(wkt, area_termo)
    if motivo:
        # O embargo continua valendo por documento e por ponto; só a geometria é descartada
        quarentena.append((seq, f"poligono_descartado:{motivo}", {"wkt": wkt[:20000] if wkt else None}))
    ponto = _ponto(lon, lat)
    tipo_geom = "POLIGONO" if poligono else "PONTO" if ponto else "NENHUMA"
    return (
        "IBAMA",
        seq,
        processo,
        doc.tipo.value if doc else None,
        doc.valor if doc else None,
        mask_document(doc) if doc else None,
        situacao,
        None,  # data da infração não vem neste arquivo
        data_embargo,
        int(cod_mun) if cod_mun and cod_mun.isdigit() else None,
        tipo_geom,
        poligono,
        ponto,
    )


def transformar_ibama(diretorio: Path, hoje: date | None = None) -> Carga:
    hoje = hoje or date.today()
    manifesto = Manifesto.carregar(diretorio)
    linhas, ignoradas = _ler(diretorio / ARQUIVO)
    quarentena: Quarentena = []
    registros: dict[str, tuple[Any, ...]] = {}
    as_of: date | None = None
    for linha in linhas:
        atualizacao = _data(linha[9], hoje)
        if atualizacao and (as_of is None or atualizacao > as_of):
            as_of = atualizacao
        if linha[-1] == "SEM_STATUS":
            quarentena.append((linha[0], "status_ou_seq_tad_ausente", {"NUM_PROCESSO": linha[1]}))
            continue
        if linha[0] in registros:
            quarentena.append((linha[0], "seq_tad_duplicado", None))
            continue
        registros[linha[0]] = _registro(linha, hoje, quarentena)
    if as_of is None:
        raise ValueError("IBAMA sem ULTIMA_ATUALIZACAO_RELATORIO válida")
    manifesto.source_as_of = as_of.isoformat()
    print(f"[IBAMA] ignorados (cancelados/substituídos/excluídos, não são embargos): {ignoradas}")
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
