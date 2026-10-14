from collections.abc import Mapping, Set

from agrocredit.domain.enums import CreditStatus, RuleResult


def derive_status(results: Mapping[str, RuleResult], mandatory: Set[str], in_coverage: bool) -> CreditStatus:
    """Status final a partir dos resultados de TODAS as regras (PRD §5.1)."""
    if not in_coverage:
        return CreditStatus.FORA_DE_COBERTURA
    values = set(results.values())
    if RuleResult.IMPEDIDO in values:
        return CreditStatus.IMPEDIMENTO_IDENTIFICADO
    if any(results.get(r, RuleResult.NAO_VERIFICADO) is RuleResult.NAO_VERIFICADO for r in mandatory):
        return CreditStatus.INCONCLUSIVO
    if RuleResult.ALERTA in values:
        return CreditStatus.SEM_IMPEDIMENTO_COM_ALERTAS
    return CreditStatus.SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS
