from enum import StrEnum


class TipoDocumento(StrEnum):
    CPF = "CPF"
    CNPJ = "CNPJ"


class RuleResult(StrEnum):
    IMPEDIDO = "IMPEDIDO"
    ALERTA = "ALERTA"
    OK = "OK"
    NAO_VERIFICADO = "NAO_VERIFICADO"


class CreditStatus(StrEnum):
    IMPEDIMENTO_IDENTIFICADO = "IMPEDIMENTO_IDENTIFICADO"
    INCONCLUSIVO = "INCONCLUSIVO"
    SEM_IMPEDIMENTO_COM_ALERTAS = "SEM_IMPEDIMENTO_COM_ALERTAS"
    SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS = "SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS"
    FORA_DE_COBERTURA = "FORA_DE_COBERTURA"


class CarStatus(StrEnum):
    ATIVO = "ATIVO"
    PENDENTE = "PENDENTE"
    SUSPENSO = "SUSPENSO"
    CANCELADO = "CANCELADO"


class Camada(StrEnum):
    TI = "TI"
    UC = "UC"
    QUILOMBOLA = "QUILOMBOLA"
    FPND = "FPND"
    PRODES = "PRODES"


class Fonte(StrEnum):
    """Fontes de dados. Embargos estaduais usam `embargo_estadual(uf)`."""

    SICAR = "SICAR"
    IBAMA = "IBAMA"
    MTE = "MTE"
    RFB = "RFB"
    FUNAI = "FUNAI"
    CNUC = "CNUC"  # Cadastro Nacional de UCs (MMA): todas as esferas
    INCRA = "INCRA"
    SFB = "SFB"
    PRODES = "PRODES"


def embargo_estadual(uf: str) -> str:
    return f"EMBARGOS_{uf.upper()}"


FONTE_DA_CAMADA: dict[Camada, Fonte] = {
    Camada.TI: Fonte.FUNAI,
    Camada.UC: Fonte.CNUC,
    Camada.QUILOMBOLA: Fonte.INCRA,
    Camada.FPND: Fonte.SFB,
    Camada.PRODES: Fonte.PRODES,
}
