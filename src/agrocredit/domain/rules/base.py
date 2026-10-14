from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agrocredit.domain.enums import RuleResult
from agrocredit.domain.facts import Fatos, Tolerancia

# Ordem de gravidade usada para combinar resultados parciais de uma mesma regra
GRAVIDADE = {
    RuleResult.OK: 0,
    RuleResult.NAO_VERIFICADO: 1,
    RuleResult.ALERTA: 2,
    RuleResult.IMPEDIDO: 3,
}


@dataclass(frozen=True, slots=True)
class ResultadoRegra:
    rule_id: str
    result: RuleResult
    reason: str | None = None
    detail: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ContextoRegra:
    tolerancia: Tolerancia
    params: Mapping[str, Any] = field(default_factory=dict)


Regra = Callable[[Fatos, ContextoRegra], ResultadoRegra]


def classificar_sobreposicao(overlap_ha: Decimal, pct: Decimal, tol: Tolerancia) -> RuleResult:
    """Tolerância do tenant, "o que for MAIOR" entre limite absoluto e relativo (ADR-009).

    overlap <= max(tol.ha, tol.pct% da área)  <=>  overlap_ha <= tol.ha OU pct <= tol.pct.
    Abaixo da tolerância é ALERTA, nunca OK silencioso.
    """
    if overlap_ha <= 0:
        return RuleResult.OK
    if overlap_ha <= tol.ha or pct <= tol.pct:
        return RuleResult.ALERTA
    return RuleResult.IMPEDIDO


def tolerancia_detail(tol: Tolerancia) -> dict[str, str]:
    return {"ha": str(tol.ha), "pct": str(tol.pct)}


def nao_verificado(rule_id: str, motivo: str) -> ResultadoRegra:
    return ResultadoRegra(rule_id, RuleResult.NAO_VERIFICADO, motivo)


def pior(resultados: list[RuleResult]) -> RuleResult:
    return max(resultados, key=GRAVIDADE.__getitem__, default=RuleResult.OK)
