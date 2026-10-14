"""Motor de regras: avalia todas as regras do ruleset vigente e deriva o parecer."""

from dataclasses import dataclass, replace
from datetime import date

from agrocredit.domain.decision import derive_status
from agrocredit.domain.enums import CreditStatus, RuleResult
from agrocredit.domain.facts import Fatos, Tolerancia
from agrocredit.domain.rules import REGISTRO
from agrocredit.domain.rules.base import ContextoRegra, ResultadoRegra
from agrocredit.domain.ruleset import VersaoRuleset

AVISO_VINCULO = "Vínculo do tomador com o imóvel não verificado."


@dataclass(frozen=True, slots=True)
class Parecer:
    ruleset_version: str
    resultados: tuple[ResultadoRegra, ...]
    credit_status: CreditStatus
    summary: str
    tolerancia: Tolerancia


def avaliar(fatos: Fatos, versao: VersaoRuleset, tolerancia: Tolerancia, hoje: date | None = None) -> Parecer:
    hoje = hoje or date.today()
    resultados = tuple(
        replace(
            REGISTRO[cfg.rule_id](fatos, ContextoRegra(tolerancia, cfg.params, hoje)),
            fundamento=cfg.fundamento,
        )
        for cfg in versao.regras
    )
    status = derive_status({r.rule_id: r.result for r in resultados}, versao.obrigatorias, fatos.em_cobertura)
    return Parecer(versao.id, resultados, status, _resumo(status, resultados, versao), tolerancia)


def _ids(resultados: tuple[ResultadoRegra, ...], result: RuleResult) -> list[ResultadoRegra]:
    return [r for r in resultados if r.result is result]


def _resumo(status: CreditStatus, resultados: tuple[ResultadoRegra, ...], versao: VersaoRuleset) -> str:
    """Linguagem do PRD §5.1: nunca afirmar "regular perante o BACEN", só o que as bases mostram."""
    impedidos = _ids(resultados, RuleResult.IMPEDIDO)
    alertas = _ids(resultados, RuleResult.ALERTA)
    nao_verif = _ids(resultados, RuleResult.NAO_VERIFICADO)

    partes: list[str] = []
    if status is CreditStatus.FORA_DE_COBERTURA:
        partes.append("Município do CAR fora da cobertura da base; verificação espacial não realizada.")
    if impedidos:
        partes.append("Impedimento identificado: " + " ".join(f"[{r.rule_id}] {r.reason}" for r in impedidos))
    elif status is not CreditStatus.FORA_DE_COBERTURA:
        partes.append("Nenhum impedimento encontrado nas bases consultadas, com dados nas datas indicadas.")
    if alertas:
        partes.append("Alertas: " + " ".join(f"[{r.rule_id}] {r.reason}" for r in alertas))
    if nao_verif:
        obrig = versao.obrigatorias
        partes.append(
            "Não verificado: "
            + ", ".join(f"{r.rule_id}{' (obrigatória)' if r.rule_id in obrig else ''}" for r in nao_verif)
            + "."
        )
    partes.append(AVISO_VINCULO)
    return " ".join(partes)
