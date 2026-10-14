from agrocredit.domain.enums import Fonte, RuleResult
from agrocredit.domain.facts import Fatos
from agrocredit.domain.rules.base import ContextoRegra, ResultadoRegra, nao_verificado

RULE_ID = "R-MTE"


def avaliar(fatos: Fatos, ctx: ContextoRegra) -> ResultadoRegra:
    if not fatos.fresca(Fonte.MTE):
        return nao_verificado(RULE_ID, "Cadastro de empregadores do MTE indisponível ou desatualizado.")
    if not fatos.mte:
        return ResultadoRegra(RULE_ID, RuleResult.OK)
    # A resposta não expõe a lista: só o resultado e a data de inclusão (RIPD, minimização)
    registro = min(fatos.mte, key=lambda r: r.data_inclusao)
    motivo = (
        "Raiz do CNPJ consta no cadastro de empregadores do MTE."
        if registro.por_raiz_cnpj
        else "Documento consta no cadastro de empregadores do MTE."
    )
    return ResultadoRegra(
        RULE_ID,
        RuleResult.IMPEDIDO,
        motivo,
        {"data_inclusao": registro.data_inclusao.isoformat(), "por_raiz_cnpj": registro.por_raiz_cnpj},
    )
