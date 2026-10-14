"""Defasagem crítica por fonte (DATA_PIPELINE.md §4, fonte única dos limiares).

Passado o limiar, as regras daquela fonte devolvem NAO_VERIFICADO. Idade = hoje − source_as_of.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from agrocredit.domain.enums import Fonte

LIMITE_CRITICO_DIAS: dict[str, int] = {
    Fonte.MTE: 30,
    Fonte.IBAMA: 10,
    Fonte.SICAR: 30,
    Fonte.RFB: 45,
    Fonte.FUNAI: 180,  # cópia sincronizada da Funai (PAMGIA/IBAMA); fase de TI muda raramente
    Fonte.CNUC: 450,  # versão aberta mais recente do MMA tem atraso; as de 2026 exigem login
    Fonte.INCRA: 60,  # exportação gerada no momento do download
    Fonte.SFB: 395,  # CNFP anual + margem
    Fonte.PRODES: 395,  # publicação anual: 13 meses
}
LIMITE_EMBARGO_ESTADUAL_DIAS = 14


def limite_dias(fonte: str) -> int | None:
    if fonte.startswith("EMBARGOS_"):
        return LIMITE_EMBARGO_ESTADUAL_DIAS
    return LIMITE_CRITICO_DIAS.get(fonte)


def fontes_frescas(data_as_of: Mapping[str, date], hoje: date) -> dict[str, bool]:
    """Fonte ausente de `data_as_of` não aparece (o motor a trata como não verificável).

    Fonte publicada sem limite definido é ignorada: nenhuma regra depende dela.
    """
    frescas = {}
    for fonte, as_of in data_as_of.items():
        limite = limite_dias(fonte)
        if limite is not None:
            frescas[fonte] = (hoje - as_of).days <= limite
    return frescas


# Alerta antecipado (P2), antes do limite crítico (P1). Fontes sem valor aqui só têm o nível crítico.
LIMITE_ALERTA_DIAS: dict[str, int] = {
    Fonte.IBAMA: 7,
    Fonte.SICAR: 15,
    Fonte.RFB: 35,
    Fonte.FUNAI: 90,
    Fonte.INCRA: 35,
    Fonte.CNUC: 240,
    Fonte.SFB: 365,
    Fonte.PRODES: 365,
}
LIMITE_ALERTA_EMBARGO_ESTADUAL_DIAS = 7


@dataclass(frozen=True, slots=True)
class SituacaoFonte:
    fonte: str
    as_of: date
    idade_dias: int
    nivel: str  # OK, P2 (alerta) ou P1 (crítico: regras da fonte saem NAO_VERIFICADO)


def situacao_fontes(data_as_of: Mapping[str, date], hoje: date) -> list[SituacaoFonte]:
    """Situação de cada fonte com limite definido, para alertas operacionais (RUNBOOK INC-03)."""
    situacoes = []
    for fonte, as_of in sorted(data_as_of.items()):
        critico = limite_dias(fonte)
        if critico is None:
            continue
        alerta = (
            LIMITE_ALERTA_EMBARGO_ESTADUAL_DIAS
            if fonte.startswith("EMBARGOS_")
            else LIMITE_ALERTA_DIAS.get(fonte)
        )
        idade = (hoje - as_of).days
        nivel = "P1" if idade > critico else "P2" if alerta is not None and idade > alerta else "OK"
        situacoes.append(SituacaoFonte(fonte, as_of, idade, nivel))
    return situacoes
