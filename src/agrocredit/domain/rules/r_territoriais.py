"""Regras de interseção com camadas territoriais (TI, UC, quilombolas, florestas públicas, PRODES).

Cada polígono intersectado é classificado isoladamente com a tolerância do tenant e o pior resultado
prevalece. Assim não há soma dupla quando polígonos da mesma camada se sobrepõem (ex.: APA que
contém um parque). Os critérios de cada camada vêm dos `params` do ruleset (revisão jurídica).
"""

from collections.abc import Callable
from datetime import date

from agrocredit.domain.enums import FONTE_DA_CAMADA, Camada, RuleResult
from agrocredit.domain.facts import Fatos, SobreposicaoTerritorial
from agrocredit.domain.rules.base import (
    ContextoRegra,
    ResultadoRegra,
    classificar_sobreposicao,
    nao_verificado,
    pior,
    tolerancia_detail,
)
from agrocredit.domain.rules.r_embargos import imovel_avaliavel

R_TI = "R-TI"
R_UC = "R-UC"
R_QUI = "R-QUI"
R_FPND = "R-FPND"
R_PRODES = "R-PRODES"

Classificador = Callable[[SobreposicaoTerritorial, ContextoRegra], RuleResult]


def _por_tolerancia(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    return classificar_sobreposicao(s.overlap_ha, s.pct, ctx.tolerancia)


def _impeditivo_se(atributo: str, param: str) -> Classificador:
    """IMPEDIDO (sujeito à tolerância) se atributos[atributo] estiver em params[param]; senão ALERTA."""

    def classificar(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
        base = _por_tolerancia(s, ctx)
        if base is RuleResult.OK or str(s.atributos.get(atributo)) in set(ctx.params.get(param, [])):
            return base
        return RuleResult.ALERTA

    return classificar


def _fpnd(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    if str(s.atributos.get("tipo")) not in set(ctx.params.get("tipos_impeditivos", ["B"])):
        return RuleResult.OK
    return _por_tolerancia(s, ctx)


def _prodes(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Supressão após a data de corte gera ALERTA (exige documentação de regularidade), nunca IMPEDIDO.

    Polígono sem data é tratado como posterior ao corte (lado conservador).
    """
    corte = date.fromisoformat(str(ctx.params.get("data_corte", "2019-07-31")))
    deteccao = s.atributos.get("data_deteccao")
    if deteccao is not None and date.fromisoformat(str(deteccao)) <= corte:
        return RuleResult.OK
    base = _por_tolerancia(s, ctx)
    return RuleResult.OK if base is RuleResult.OK else RuleResult.ALERTA


def _avaliar_camada(
    rule_id: str, camada: Camada, classificar: Classificador
) -> Callable[..., ResultadoRegra]:
    fonte = FONTE_DA_CAMADA[camada]

    def avaliar(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
        imovel = imovel_avaliavel(rule_id, fatos)
        if isinstance(imovel, ResultadoRegra):
            return imovel
        if not fatos.fresca(fonte):
            return nao_verificado(rule_id, f"Base {fonte} indisponível ou desatualizada.")
        avaliados = [(s, classificar(s, ctx)) for s in fatos.territoriais if s.camada is camada]
        relevantes = [(s, r) for s, r in avaliados if r is not RuleResult.OK]
        result = pior([r for _, r in relevantes])
        if result is RuleResult.OK:
            return ResultadoRegra(rule_id, RuleResult.OK)
        detail = {
            "tolerance_applied": tolerancia_detail(ctx.tolerancia),
            "features": [
                {
                    "id": s.id,
                    "name": s.nome,
                    "attributes": dict(s.atributos),
                    "overlap_ha": str(s.overlap_ha),
                    "overlap_pct": str(s.pct),
                    "result": r,
                }
                for s, r in relevantes
            ],
        }
        return ResultadoRegra(
            rule_id, result, f"Interseção com {camada} ({len(relevantes)} polígono(s)).", detail
        )

    return avaliar


avaliar_ti = _avaliar_camada(R_TI, Camada.TI, _impeditivo_se("fase", "fases_impeditivas"))
avaliar_uc = _avaliar_camada(R_UC, Camada.UC, _impeditivo_se("categoria", "categorias_impeditivas"))
avaliar_quilombola = _avaliar_camada(R_QUI, Camada.QUILOMBOLA, _impeditivo_se("fase", "fases_impeditivas"))
avaliar_fpnd = _avaliar_camada(R_FPND, Camada.FPND, _fpnd)
avaliar_prodes = _avaliar_camada(R_PRODES, Camada.PRODES, _prodes)
