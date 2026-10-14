"""Logs estruturados em JSON com mascaramento de CPF/CNPJ em qualquer mensagem (RIPD R-01)."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# CPF/CNPJ com ou sem máscara, inclusive CNPJ alfanumérico. Sequências de 11 ou 14 caracteres
# isoladas: falso positivo (ex.: um número qualquer) só custa um mascaramento a mais.
_DOC = re.compile(
    r"(?<![0-9A-Za-z])("
    r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}"
    r"|[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2}"
    r")(?![0-9A-Za-z])"
)
_CAMPOS_PADRAO = set(vars(logging.makeLogRecord({})))


def mascarar(texto: str) -> str:
    return _DOC.sub("[DOC]", texto)


class FormatoJSON(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        dados: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for chave, valor in vars(record).items():
            if chave not in _CAMPOS_PADRAO and not chave.startswith("_"):
                dados[chave] = valor
        if record.exc_info:
            dados["exc"] = self.formatException(record.exc_info)
        return mascarar(json.dumps(dados, ensure_ascii=False, default=str))


class FiltroMascara(logging.Filter):
    """Mascara também no texto já formatado por outros handlers (ex.: captura de logs em testes)."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Mascara a mensagem e cada argumento sem mudar a estrutura: formatadores como o de acesso do
        # uvicorn desempacotam record.args (ip, método, caminho, versão, status)
        record.msg = mascarar(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(mascarar(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: mascarar(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def configurar(nivel: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(FormatoJSON())
    raiz = logging.getLogger()
    raiz.handlers = [handler]
    raiz.setLevel(nivel)
    for nome in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "agrocredit"):
        logging.getLogger(nome).addFilter(FiltroMascara())
