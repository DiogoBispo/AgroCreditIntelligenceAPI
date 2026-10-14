"""Validação e normalização de documentos e do código CAR, sem dependências externas."""

import re
from dataclasses import dataclass

from agrocredit.domain.enums import TipoDocumento

CAR_REGEX = re.compile(r"^[A-Z]{2}-\d{7}-[A-F0-9]{32}$")
_CNPJ_REGEX = re.compile(r"^[0-9A-Z]{12}\d{2}$")
_SEPARADORES = re.compile(r"[.\-/\s]")
_W_CNPJ_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_W_CNPJ_2 = (6, *_W_CNPJ_1)


@dataclass(frozen=True, slots=True)
class Documento:
    tipo: TipoDocumento
    valor: str

    @property
    def cnpj_raiz(self) -> str | None:
        return self.valor[:8] if self.tipo is TipoDocumento.CNPJ else None

    def mascarado(self) -> str:
        return mask_document(self)


def _dv_mod11(valores: list[int], pesos: tuple[int, ...]) -> int:
    resto = sum(v * p for v, p in zip(valores, pesos, strict=True)) % 11
    return 0 if resto < 2 else 11 - resto


def validate_cpf(cpf: str) -> bool:
    if len(cpf) != 11 or not cpf.isdigit() or len(set(cpf)) == 1:
        return False
    d = [int(c) for c in cpf]
    return all(d[i] == _dv_mod11(d[:i], tuple(range(i + 1, 1, -1))) for i in (9, 10))


def validate_cnpj(cnpj: str) -> bool:
    """CNPJ numérico ou alfanumérico (RFB): cada caractere vale ord(c) - 48; DVs sempre numéricos."""
    if not _CNPJ_REGEX.match(cnpj) or len(set(cnpj)) == 1:
        return False
    valores = [ord(c) - 48 for c in cnpj]
    return valores[12] == _dv_mod11(valores[:12], _W_CNPJ_1) and valores[13] == _dv_mod11(
        valores[:13], _W_CNPJ_2
    )


def parse_document(raw: str) -> Documento:
    """Aceita CPF/CNPJ com ou sem máscara. Levanta ValueError (vira 422 no Pydantic)."""
    valor = _SEPARADORES.sub("", raw).upper()
    if len(valor) == 11 and valor.isdigit():
        if validate_cpf(valor):
            return Documento(TipoDocumento.CPF, valor)
        raise ValueError("CPF com dígitos verificadores inválidos.")
    if len(valor) == 14:
        if validate_cnpj(valor):
            return Documento(TipoDocumento.CNPJ, valor)
        raise ValueError("CNPJ com dígitos verificadores inválidos.")
    raise ValueError("Documento deve ser um CPF (11 dígitos) ou CNPJ (14 caracteres).")


def mask_document(doc: Documento) -> str:
    v = doc.valor
    if doc.tipo is TipoDocumento.CPF:
        return f"***.{v[3:6]}.{v[6:9]}-**"
    return f"{v[0:2]}.{v[2:5]}.{v[5:8]}/****-**"


def ibge_dv_ok(codigo: str) -> bool:
    """Dígito verificador do código de município do IBGE (7 dígitos)."""
    if len(codigo) != 7 or not codigo.isdigit():
        return False
    soma = 0
    for i, c in enumerate(codigo[:6]):
        p = int(c) * (1 if i % 2 == 0 else 2)
        soma += p // 10 + p % 10
    return (10 - soma % 10) % 10 == int(codigo[6])


def normalize_car(value: str) -> str:
    """Aceita o código com pontos separadores (como no recibo do SICAR)."""
    return value.replace(".", "").replace(" ", "").upper()


def parse_car(raw: str) -> str:
    code = normalize_car(raw)
    if not CAR_REGEX.match(code):
        raise ValueError("Código CAR inválido. Formato: UF-IBGE7-32 hexadecimais.")
    if not ibge_dv_ok(code[3:10]):
        raise ValueError("Código CAR com código de município IBGE inválido.")
    return code


def car_codigo_ibge(code: str) -> int:
    return int(code[3:10])


def car_uf(code: str) -> str:
    return code[:2]
