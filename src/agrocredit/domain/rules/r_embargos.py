"""Regras de embargo (MCR 2-9 itens 10 a 13).

Só o embargo de área registrado na lista do IBAMA é impedimento (item 10). Embargo estadual fora dessa lista,
embargo por documento do tomador e embargo de atividade sem polígono são informativos (ALERTA).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from agrocredit.domain.enums import Fonte, RuleResult, embargo_estadual
from agrocredit.domain.facts import Fatos, Imovel, Sobreposicao
from agrocredit.domain.rules.base import (
    ContextoRegra,
    ResultadoRegra,
    classificar_sobreposicao,
    nao_verificado,
    tolerancia_detail,
)

R_EMB_GEO = "R-EMB-GEO"
R_EMB_EST = "R-EMB-EST"
R_EMB_DOC = "R-EMB-DOC"
R_EMB_SEMGEO = "R-EMB-SEMGEO"

# Item 12: até 30/06/2027, exceção documental para embargo de até 5% da área (ou 20 ha, o que for menor,
# para notificações a partir de 02/01/2025)
_ITEM_12_ATE = date(2027, 6, 30)
_ITEM_12_PCT = Decimal(5)
_ITEM_12_HA = Decimal(20)
_ITEM_12_NOTIFICACAO_DESDE = date(2025, 1, 2)


def imovel_avaliavel(rule_id: str, fatos: Fatos) -> Imovel | ResultadoRegra:
    """Pré-condição das regras espaciais: imóvel coberto e localizado."""
    if not fatos.em_cobertura:
        return nao_verificado(rule_id, "Município do CAR fora da cobertura da base.")
    if fatos.imovel is None:
        return nao_verificado(rule_id, "Imóvel não localizado no SICAR; interseção não calculada.")
    if fatos.imovel.problema_geometria:
        return nao_verificado(
            rule_id,
            f"Geometria do CAR irrecuperável na fonte ({fatos.imovel.problema_geometria}); "
            "interseção não calculada.",
        )
    return fatos.imovel


def _detalhe(sob: Sobreposicao, ctx: ContextoRegra) -> dict[str, Any]:
    return {
        "overlap_ha": str(sob.area_ha),
        "overlap_pct": str(sob.pct),
        "tolerance_applied": tolerancia_detail(ctx.tolerancia),
        "embargoes": [
            {
                "source": h.orgao,
                "process_number": h.numero_processo,
                "infraction_date": h.data_infracao.isoformat() if h.data_infracao else None,
                "embargo_date": h.data_embargo.isoformat() if h.data_embargo else None,
                "overlap_ha": str(h.overlap_ha),
            }
            for h in sob.hits
        ],
    }


def _cabe_no_item_12(sob: Sobreposicao, hoje: date) -> bool:
    """Sobreposição dentro dos limites do item 12 (a exceção ainda exige documentação do mutuário)."""
    if hoje > _ITEM_12_ATE or sob.pct > _ITEM_12_PCT:
        return False
    recentes = any(h.data_embargo and h.data_embargo >= _ITEM_12_NOTIFICACAO_DESDE for h in sob.hits)
    return not recentes or sob.area_ha <= _ITEM_12_HA


def avaliar_federal(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    imovel = imovel_avaliavel(R_EMB_GEO, fatos)
    if isinstance(imovel, ResultadoRegra):
        return imovel
    if not fatos.fresca(Fonte.IBAMA):
        return nao_verificado(R_EMB_GEO, "Base de embargos do IBAMA indisponível ou desatualizada.")
    sob = fatos.embargos_federais
    result = classificar_sobreposicao(sob.area_ha, sob.pct, ctx.tolerancia)
    if result is RuleResult.OK:
        return ResultadoRegra(R_EMB_GEO, result)
    detalhe = _detalhe(sob, ctx)
    if result is RuleResult.ALERTA:
        return ResultadoRegra(
            R_EMB_GEO, result, "Sobreposição com embargo do IBAMA abaixo da tolerância configurada.", detalhe
        )
    detalhe["possivel_excecao_item_12"] = _cabe_no_item_12(sob, ctx.hoje)
    if imovel.uso_coletivo:
        # Item 13: em CAR coletivo o impedimento alcança só a área embargada e o responsável pelo embargo
        return ResultadoRegra(
            R_EMB_GEO,
            RuleResult.ALERTA,
            "Embargo do IBAMA em CAR de uso coletivo: o impedimento alcança só a área embargada e o "
            "responsável pelo embargo (MCR 2-9 item 13); verificar se o proponente é o autuado.",
            detalhe,
        )
    motivo = "Sobreposição com embargo do IBAMA acima da tolerância configurada."
    if detalhe["possivel_excecao_item_12"]:
        motivo += " Dentro dos limites do item 12 até 30/06/2027, mediante a documentação exigida."
    return ResultadoRegra(R_EMB_GEO, RuleResult.IMPEDIDO, motivo, detalhe)


def avaliar_estadual(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    """Informativo: o item 10 exige registro na lista de embargos do IBAMA."""
    imovel = imovel_avaliavel(R_EMB_EST, fatos)
    if isinstance(imovel, ResultadoRegra):
        return imovel
    if not fatos.fresca(embargo_estadual(imovel.uf)):
        return nao_verificado(
            R_EMB_EST, f"Base estadual de embargos de {imovel.uf} indisponível ou desatualizada."
        )
    sob = fatos.embargos_estaduais
    if classificar_sobreposicao(sob.area_ha, sob.pct, ctx.tolerancia) is RuleResult.OK:
        return ResultadoRegra(R_EMB_EST, RuleResult.OK)
    return ResultadoRegra(
        R_EMB_EST,
        RuleResult.ALERTA,
        f"Sobreposição com embargo estadual de {imovel.uf}. Só é impedimento se também constar na lista de "
        "embargos do IBAMA (MCR 2-9 item 10); verificação manual recomendada.",
        _detalhe(sob, ctx),
    )


def avaliar_documental(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    """Informativo: o item 10 trata do embargo no imóvel, não do histórico do tomador."""
    if not fatos.fresca(Fonte.IBAMA):
        return nao_verificado(R_EMB_DOC, "Base de embargos do IBAMA indisponível ou desatualizada.")
    if fatos.embargos_documentais:
        return ResultadoRegra(
            R_EMB_DOC,
            RuleResult.ALERTA,
            "Tomador consta como autuado em embargo vigente (em qualquer imóvel ou atividade). Não é "
            "impedimento do MCR 2-9 por si só; relevante para a análise de risco e para o item 12, alínea b.",
            {
                "embargoes": [
                    {
                        "source": e.orgao,
                        "process_number": e.numero_processo,
                        "infraction_date": e.data_infracao.isoformat() if e.data_infracao else None,
                        "embargo_date": e.data_embargo.isoformat() if e.data_embargo else None,
                    }
                    for e in fatos.embargos_documentais
                ]
            },
        )
    # Fontes que publicam o documento mascarado não permitem afirmar ausência
    verificaveis = set(ctx.params.get("tipos_documento_verificaveis", []))
    if fatos.documento.tipo not in verificaveis:
        return nao_verificado(
            R_EMB_DOC,
            f"A fonte não publica {fatos.documento.tipo} sem máscara; busca por documento inviável.",
        )
    return ResultadoRegra(R_EMB_DOC, RuleResult.OK)


def avaliar_sem_geometria(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    """Embargo sem polígono cujo ponto de referência cai dentro do imóvel (informativo).

    Não usa o município inteiro: a maioria dos embargos sem polígono embarga atividades (Fase 0).
    """
    imovel = imovel_avaliavel(R_EMB_SEMGEO, fatos)
    if isinstance(imovel, ResultadoRegra):
        return imovel
    n = fatos.embargos_sem_poligono_no_imovel
    if n == 0:
        return ResultadoRegra(R_EMB_SEMGEO, RuleResult.OK)
    return ResultadoRegra(
        R_EMB_SEMGEO,
        RuleResult.ALERTA,
        "Embargo vigente sem polígono com ponto de referência dentro do imóvel; verificar se é embargo de "
        "área por desmatamento (MCR 2-9 item 10) ou de atividade.",
        {"embargos_sem_poligono": n},
    )
