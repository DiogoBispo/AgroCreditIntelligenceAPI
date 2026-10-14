from agrocredit.domain.enums import Fonte, RuleResult, TipoDocumento
from agrocredit.domain.facts import Fatos
from agrocredit.domain.rules.base import ContextoRegra, ResultadoRegra, nao_verificado

RULE_ID = "R-RFB"


def avaliar(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    if fatos.documento.tipo is TipoDocumento.CPF:
        return nao_verificado(RULE_ID, "Não há fonte aberta de situação cadastral para CPF.")
    if not fatos.fresca(Fonte.RFB):
        return nao_verificado(RULE_ID, "Base de CNPJ da RFB indisponível ou desatualizada.")
    if fatos.empresa is None:
        return nao_verificado(RULE_ID, "CNPJ não encontrado na base carregada.")
    situacao = fatos.empresa.situacao_cadastral
    if situacao != "ATIVA":
        return ResultadoRegra(
            RULE_ID, RuleResult.ALERTA, f"Situação cadastral {situacao} na RFB.", {"rfb_status": situacao}
        )
    return ResultadoRegra(RULE_ID, RuleResult.OK, None, {"rfb_status": situacao})
