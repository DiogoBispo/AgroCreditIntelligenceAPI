"""Regras de interseção com camadas territoriais (MCR 2-9 itens 5 a 9, 14, 15 e 17).

Cada polígono intersectado é classificado isoladamente com a tolerância do tenant e o pior resultado
prevalece. Assim não há soma dupla quando polígonos da mesma camada se sobrepõem (ex.: APA que contém um
parque). Os critérios vêm dos `params` do ruleset; os motivos citam o item do MCR e a exceção cabível.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from agrocredit.domain.enums import FONTE_DA_CAMADA, Camada, RuleResult
from agrocredit.domain.facts import Fatos, Imovel, SobreposicaoTerritorial
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


@dataclass(frozen=True)
class Motivos:
    impedido: str
    alerta: str


def _por_tolerancia(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    return classificar_sobreposicao(s.overlap_ha, s.pct, ctx.tolerancia)


def _no_param(s: SobreposicaoTerritorial, ctx: ContextoRegra, atributo: str, param: str) -> bool:
    return str(s.atributos.get(atributo)) in set(ctx.params.get(param, []))


def _ti(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Item 7: só TI homologada, regularizada ou Reserva Indígena impede; demais fases, ALERTA."""
    base = _por_tolerancia(s, ctx)
    impeditiva = _no_param(s, ctx, "fase", "fases_impeditivas") or _no_param(
        s, ctx, "modalidade", "modalidades_impeditivas"
    )
    return base if base is RuleResult.OK or impeditiva else RuleResult.ALERTA


def _uc(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Itens 5 e 6: toda UC do CNUC impede, exceto as de domínio exclusivamente público (dependem da
    regularização fundiária, que as bases públicas não informam): essas viram ALERTA."""
    base = _por_tolerancia(s, ctx)
    if base is RuleResult.OK or not _no_param(s, ctx, "categoria", "categorias_dominio_publico"):
        return base
    return RuleResult.ALERTA


def _quilombola(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Item 8: território titulado ou com título parcial impede; demais fases, ALERTA."""
    base = _por_tolerancia(s, ctx)
    return (
        base if base is RuleResult.OK or _no_param(s, ctx, "fase", "fases_impeditivas") else RuleResult.ALERTA
    )


def _fpnd(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Item 14: só Floresta Pública Tipo B (não destinada)."""
    if not _no_param(s, ctx, "tipo", "tipos_impeditivos"):
        return RuleResult.OK
    return _por_tolerancia(s, ctx)


def _prodes(s: SobreposicaoTerritorial, ctx: ContextoRegra) -> RuleResult:
    """Item 17: supressão após a data de corte gera ALERTA (exige documento do item 18), nunca IMPEDIDO.

    Polígono sem data é tratado como posterior ao corte (lado conservador).
    """
    corte = date.fromisoformat(str(ctx.params.get("data_corte", "2019-07-31")))
    deteccao = s.atributos.get("data_deteccao")
    if deteccao is not None and date.fromisoformat(str(deteccao)) <= corte:
        return RuleResult.OK
    base = _por_tolerancia(s, ctx)
    return RuleResult.OK if base is RuleResult.OK else RuleResult.ALERTA


_INICIO_EXIGENCIA_PADRAO = {
    "acima_15_mf": "2027-01-04",
    "de_4_a_15_mf": "2027-07-01",
    "ate_4_mf": "2028-01-03",
}


def exigencia_prodes(imovel: Imovel, params: Mapping[str, Any]) -> date:
    """Item 17, alíneas a-c e 17-A: início da exigência conforme módulos fiscais ou CAR coletivo.

    Sem módulos fiscais conhecidos, usa a data mais cedo (lado conservador).
    """
    brutos = params.get("inicio_exigencia") or _INICIO_EXIGENCIA_PADRAO
    prazos = {k: date.fromisoformat(str(v)) for k, v in brutos.items()}
    mf = imovel.modulos_fiscais
    if imovel.uso_coletivo:
        return prazos["ate_4_mf"]
    if mf is None or mf > Decimal(15):
        return prazos["acima_15_mf"]
    return prazos["de_4_a_15_mf"] if mf > Decimal(4) else prazos["ate_4_mf"]


def _motivo(
    rule_id: str, camada: Camada, result: RuleResult, n: int, imovel: Imovel, ctx: ContextoRegra
) -> str:
    base = f"Interseção com {camada} ({n} polígono(s))."
    if rule_id == R_PRODES:
        inicio = exigencia_prodes(imovel, ctx.params)
        situacao = "exigida" if ctx.hoje >= inicio else "exigida a partir de"
        return (
            f"Supressão de vegetação nativa após 31/07/2019 detectada pelo PRODES ({n} polígono(s)). "
            f"Verificação {situacao} {inicio.strftime('%d/%m/%Y')} para este imóvel (MCR 2-9 item 17); "
            "crédito com recursos controlados ou direcionados condicionado a documento do item 18."
        )
    m = _MOTIVOS[rule_id]
    return f"{base} {m.impedido if result is RuleResult.IMPEDIDO else m.alerta}"


_MOTIVOS = {
    R_TI: Motivos(
        "Terra indígena homologada, regularizada ou Reserva Indígena (MCR 2-9 item 7). Exceção: proponente "
        "pertencente ao povo ocupante (item 7, b).",
        "Terra indígena em fase não impeditiva ou interseção abaixo da tolerância (MCR 2-9 item 7, a).",
    ),
    R_UC: Motivos(
        "Unidade de Conservação registrada no CNUC (MCR 2-9 item 5). Exceção: atividade em conformidade "
        "com o Plano de Manejo da UC.",
        "UC de domínio exclusivamente público: impede só se a regularização fundiária do imóvel estiver "
        "concluída (MCR 2-9 item 6), ou interseção abaixo da tolerância; verificação manual recomendada.",
    ),
    R_QUI: Motivos(
        "Território quilombola titulado ou com título parcial (MCR 2-9 item 8). Exceção: proponente da "
        "comunidade (item 9).",
        "Território quilombola sem título (fase não impeditiva pelo MCR 2-9 item 8) ou interseção abaixo da "
        "tolerância.",
    ),
    R_FPND: Motivos(
        "Floresta Pública Tipo B (não destinada) no CNFP (MCR 2-9 item 14). Exceções do item 15: imóvel "
        "matriculado em registro de imóveis; ou até 15 módulos fiscais mantendo a vegetação nativa na "
        "floresta.",
        "Floresta Pública Tipo B com interseção abaixo da tolerância.",
    ),
}


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
        detail: dict[str, object] = {
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
        if rule_id == R_PRODES:
            detail["exigencia_a_partir_de"] = exigencia_prodes(imovel, ctx.params).isoformat()
        if rule_id == R_FPND and imovel.modulos_fiscais is not None:
            detail["modulos_fiscais"] = str(imovel.modulos_fiscais)
        return ResultadoRegra(
            rule_id, result, _motivo(rule_id, camada, result, len(relevantes), imovel, ctx), detail
        )

    return avaliar


avaliar_ti = _avaliar_camada(R_TI, Camada.TI, _ti)
avaliar_uc = _avaliar_camada(R_UC, Camada.UC, _uc)
avaliar_quilombola = _avaliar_camada(R_QUI, Camada.QUILOMBOLA, _quilombola)
avaliar_fpnd = _avaliar_camada(R_FPND, Camada.FPND, _fpnd)
avaliar_prodes = _avaliar_camada(R_PRODES, Camada.PRODES, _prodes)
