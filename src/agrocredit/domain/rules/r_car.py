from agrocredit.domain.enums import CarStatus, Fonte, RuleResult
from agrocredit.domain.facts import Fatos
from agrocredit.domain.rules.base import ContextoRegra, ResultadoRegra, nao_verificado

RULE_ID = "R-CAR"
_IMPEDITIVOS = {CarStatus.CANCELADO, CarStatus.SUSPENSO}


def avaliar(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    if not fatos.em_cobertura:
        return nao_verificado(RULE_ID, "Município do CAR fora da cobertura da base.")
    if not fatos.fresca(Fonte.SICAR):
        return nao_verificado(RULE_ID, "Base do SICAR indisponível ou desatualizada.")
    imovel = fatos.imovel
    if imovel is None:
        return ResultadoRegra(RULE_ID, RuleResult.IMPEDIDO, "CAR não encontrado na base do SICAR.")
    if imovel.status in _IMPEDITIVOS:
        return ResultadoRegra(
            RULE_ID, RuleResult.IMPEDIDO, f"CAR com status {imovel.status}.", {"car_status": imovel.status}
        )
    # CAR pendente de análise não é vedado pelo MCR 2-9: OK com o status registrado
    return ResultadoRegra(RULE_ID, RuleResult.OK, None, {"car_status": imovel.status})
