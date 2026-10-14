"""Regras de embargo: federal (IBAMA, ICMBio) e estadual por geometria, por documento e sem geometria."""

from dataclasses import replace

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


def _avaliar_geometrico(rule_id: str, sob: Sobreposicao, ctx: ContextoRegra) -> ResultadoRegra:
    result = classificar_sobreposicao(sob.area_ha, sob.pct, ctx.tolerancia)
    if result is RuleResult.OK:
        return ResultadoRegra(rule_id, result)
    motivo = (
        "Sobreposição com embargo vigente acima da tolerância configurada."
        if result is RuleResult.IMPEDIDO
        else "Sobreposição com embargo vigente abaixo da tolerância configurada."
    )
    detail = {
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
    return ResultadoRegra(rule_id, result, motivo, detail)


def avaliar_federal(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    imovel = imovel_avaliavel(R_EMB_GEO, fatos)
    if isinstance(imovel, ResultadoRegra):
        return imovel
    if not fatos.fresca(Fonte.IBAMA):
        return nao_verificado(R_EMB_GEO, "Base de embargos do IBAMA indisponível ou desatualizada.")
    return _avaliar_geometrico(R_EMB_GEO, fatos.embargos_federais, ctx)


def avaliar_estadual(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    imovel = imovel_avaliavel(R_EMB_EST, fatos)
    if isinstance(imovel, ResultadoRegra):
        return imovel
    if not fatos.fresca(embargo_estadual(imovel.uf)):
        return nao_verificado(
            R_EMB_EST, f"Base estadual de embargos de {imovel.uf} indisponível ou desatualizada."
        )
    res = _avaliar_geometrico(R_EMB_EST, fatos.embargos_estaduais, ctx)
    # Fonte sem situação do embargo (vigente × levantado): não dá para afirmar impedimento (ex.: MG)
    if res.result is RuleResult.IMPEDIDO and imovel.uf in set(ctx.params.get("ufs_sem_situacao", [])):
        return replace(
            res,
            result=RuleResult.ALERTA,
            reason=f"Sobreposição com embargo estadual de {imovel.uf}; a fonte não informa se o embargo "
            "segue vigente. Verificação manual recomendada.",
        )
    return res


def avaliar_documental(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    if not fatos.fresca(Fonte.IBAMA):
        return nao_verificado(R_EMB_DOC, "Base de embargos do IBAMA indisponível ou desatualizada.")
    if fatos.embargos_documentais:
        detalhe = {
            "embargoes": [
                {
                    "source": e.orgao,
                    "process_number": e.numero_processo,
                    "infraction_date": e.data_infracao.isoformat() if e.data_infracao else None,
                    "embargo_date": e.data_embargo.isoformat() if e.data_embargo else None,
                }
                for e in fatos.embargos_documentais
            ]
        }
        # Órgãos cuja fonte não informa se o embargo segue vigente (ex.: SEMAD-MG): no máximo ALERTA
        sem_situacao = set(ctx.params.get("orgaos_sem_situacao", []))
        if all(e.orgao in sem_situacao for e in fatos.embargos_documentais):
            return ResultadoRegra(
                R_EMB_DOC,
                RuleResult.ALERTA,
                "Tomador consta como autuado em embargo de fonte que não informa se segue vigente; "
                "verificação manual recomendada.",
                detalhe,
            )
        return ResultadoRegra(
            R_EMB_DOC, RuleResult.IMPEDIDO, "Tomador consta como autuado em embargo vigente.", detalhe
        )
    # Fontes que publicam o documento mascarado (ex.: CPF) não permitem afirmar ausência
    verificaveis = set(ctx.params.get("tipos_documento_verificaveis", []))
    if fatos.documento.tipo not in verificaveis:
        return nao_verificado(
            R_EMB_DOC,
            f"A fonte não publica {fatos.documento.tipo} sem máscara; busca por documento inviável.",
        )
    return ResultadoRegra(R_EMB_DOC, RuleResult.OK)


def avaliar_sem_geometria(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    """Embargo sem polígono cujo ponto de referência cai dentro do imóvel.

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
        "Embargo vigente sem polígono com ponto de referência dentro do imóvel; "
        "verificação manual recomendada.",
        {"embargos_sem_poligono": n},
    )
