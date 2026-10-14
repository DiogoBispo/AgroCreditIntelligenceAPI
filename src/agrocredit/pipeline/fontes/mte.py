"""Cadastro de Empregadores do MTE ("lista suja"): publicado só em PDF. Ver FASE0_FONTES.md §5.

Extração com `pdftotext -layout` (poppler-utils). O nome do empregador não é guardado (minimização).
"""

import re
import shutil

# subprocess executa um binário fixo (pdftotext), sem shell e com argumentos controlados
import subprocess  # nosec B404
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agrocredit.domain.enums import TipoDocumento
from agrocredit.domain.validators import parse_document
from agrocredit.pipeline.bronze import Manifesto, baixar
from agrocredit.pipeline.gold import Carga, Quarentena

FONTE = "MTE"
URL = (
    "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/inspecao-do-trabalho/"
    "areas-de-atuacao/cadastro_de_empregadores.pdf"
)
ARQUIVO = "cadastro_de_empregadores.pdf"
COLUNAS = ("tipo_documento", "documento", "cnpj_raiz", "nome_infrator", "data_inclusao")

_ID = re.compile(r"^\s{0,6}(\d{1,4})\s+(?:19|20)\d{2}\s+[A-Z]{2}\b", re.M)
# CNPJ numérico ou alfanumérico, ou CPF, sempre com máscara no PDF
_DOC = re.compile(
    r"\b[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}\b|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"
)
_DATA = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_ATUALIZACAO = re.compile(r"Atualiza\S+ peri\S+ de (\d{1,2}) de (\w+) de (\d{4})", re.I)
_MESES = {
    m: i + 1
    for i, m in enumerate(
        [
            "janeiro",
            "fevereiro",
            "março",
            "abril",
            "maio",
            "junho",
            "julho",
            "agosto",
            "setembro",
            "outubro",
            "novembro",
            "dezembro",
        ]
    )
}


class ExtracaoInconsistente(ValueError):
    """O nº de documentos extraídos não bate com o maior ID da lista."""


def baixar_mte(diretorio: Path, versao: str) -> Manifesto:
    manifesto = Manifesto(FONTE, versao, [baixar(URL, diretorio / ARQUIVO)])
    manifesto.salvar(diretorio)
    return manifesto


def pdf_para_texto(pdf: Path) -> str:
    binario = shutil.which("pdftotext")
    if binario is None:
        raise RuntimeError("pdftotext não encontrado (instale poppler-utils)")
    res = subprocess.run(  # noqa: S603  # nosec B603
        [binario, "-layout", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, check=True, timeout=120
    )
    return res.stdout.decode("utf-8")


def data_atualizacao(texto: str) -> date:
    m = _ATUALIZACAO.search(texto)
    if not m:
        raise ValueError("Data de atualização não encontrada no PDF do MTE")
    return date(int(m.group(3)), _MESES[m.group(2).lower()], int(m.group(1)))


def extrair(texto: str) -> tuple[list[tuple[Any, ...]], Quarentena]:
    """Uma linha por documento; data de inclusão = última data da linha do documento."""
    quarentena: Quarentena = []
    encontrados: list[tuple[str, str, date]] = []
    for linha in texto.splitlines():
        for doc_raw in _DOC.findall(linha):
            datas = _DATA.findall(linha)
            try:
                doc = parse_document(doc_raw)
            except ValueError:
                quarentena.append((doc_raw, "documento_invalido", {"linha": linha.strip()}))
                continue
            if not datas:
                quarentena.append((doc.valor, "data_inclusao_ausente", {"linha": linha.strip()}))
                continue
            d, m, a = datas[-1]
            encontrados.append((doc.tipo.value, doc.valor, datetime(int(a), int(m), int(d)).date()))

    ids = [int(i) for i in _ID.findall(texto)]
    maior_id = max(ids, default=0)
    total = len(encontrados) + len(quarentena)
    if total != maior_id:
        raise ExtracaoInconsistente(f"{total} documento(s) extraído(s), mas o maior ID da lista é {maior_id}")

    # O mesmo empregador pode aparecer mais de uma vez (estabelecimentos distintos):
    # fica a inclusão mais antiga
    por_doc: dict[tuple[str, str], date] = {}
    for tipo, valor, inclusao in encontrados:
        chave = (tipo, valor)
        por_doc[chave] = min(inclusao, por_doc.get(chave, inclusao))
    registros = [
        (tipo, valor, valor[:8] if tipo == TipoDocumento.CNPJ else None, None, inclusao)
        for (tipo, valor), inclusao in por_doc.items()
    ]
    return registros, quarentena


def transformar_mte(diretorio: Path) -> Carga:
    manifesto = Manifesto.carregar(diretorio)
    texto = pdf_para_texto(diretorio / ARQUIVO)
    registros, quarentena = extrair(texto)
    as_of = data_atualizacao(texto)
    manifesto.source_as_of = as_of.isoformat()
    return Carga(
        base="mte",
        fonte=FONTE,
        versao=manifesto.versao,
        source_as_of=as_of,
        colunas=COLUNAS,
        registros=registros,
        quarentena=quarentena,
        manifesto=manifesto,
    )
