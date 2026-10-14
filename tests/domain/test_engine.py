from collections.abc import Callable
from datetime import date
from decimal import Decimal as D

import pytest

from agrocredit.domain import engine
from agrocredit.domain.decision import derive_status
from agrocredit.domain.enums import CreditStatus, Fonte, RuleResult as R, TipoDocumento, embargo_estadual
from agrocredit.domain.facts import Empresa, Fatos, RegistroMte, Sobreposicao, Tolerancia
from agrocredit.domain.freshness import fontes_frescas
from agrocredit.domain.rules import REGISTRO
from agrocredit.domain.ruleset import VersaoRuleset, carregar, versao_vigente
from agrocredit.domain.validators import parse_document

FatosF = Callable[..., Fatos]
HOJE = date(2026, 10, 13)


@pytest.fixture(scope="module")
def versao() -> VersaoRuleset:
    return versao_vigente(carregar(), HOJE)


# derive_status ----------------------------------------------------------------------


def test_todas_as_regras_sao_consideradas() -> None:
    status = derive_status({"R-MTE": R.IMPEDIDO, "R-EMB-GEO": R.IMPEDIDO, "R-CAR": R.OK}, {"R-MTE"}, True)
    assert status is CreditStatus.IMPEDIMENTO_IDENTIFICADO


def test_obrigatoria_nao_verificada_gera_inconclusivo() -> None:
    status = derive_status({"R-CAR": R.OK, "R-EMB-GEO": R.NAO_VERIFICADO}, {"R-CAR", "R-EMB-GEO"}, True)
    assert status is CreditStatus.INCONCLUSIVO


def test_obrigatoria_ausente_dos_resultados_gera_inconclusivo() -> None:
    assert derive_status({"R-CAR": R.OK}, {"R-CAR", "R-MTE"}, True) is CreditStatus.INCONCLUSIVO


def test_opcional_nao_verificada_nao_muda_status() -> None:
    status = derive_status({"R-CAR": R.OK, "R-RFB": R.NAO_VERIFICADO}, {"R-CAR"}, True)
    assert status is CreditStatus.SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS


def test_impedimento_prevalece_sobre_inconclusivo() -> None:
    status = derive_status({"R-MTE": R.IMPEDIDO, "R-CAR": R.NAO_VERIFICADO}, {"R-CAR", "R-MTE"}, True)
    assert status is CreditStatus.IMPEDIMENTO_IDENTIFICADO


def test_fora_de_cobertura_prevalece() -> None:
    assert derive_status({"R-MTE": R.IMPEDIDO}, {"R-MTE"}, False) is CreditStatus.FORA_DE_COBERTURA


# Ruleset ------------------------------------------------------------------------------


def test_ruleset_cobre_todas_as_regras_registradas(versao: VersaoRuleset) -> None:
    assert {r.rule_id for r in versao.regras} == set(REGISTRO)


def test_ruleset_marcado_como_pendente_de_revisao(versao: VersaoRuleset) -> None:
    # Remover este teste só quando o jurídico aprovar o ruleset
    assert versao.revisao_juridica == "PENDENTE"


def test_regras_que_nao_podem_ser_obrigatorias(versao: VersaoRuleset) -> None:
    # R-RFB (sem fonte para CPF) e R-EMB-DOC (CPF mascarado) tornariam todo pedido de PF INCONCLUSIVO
    assert "R-RFB" not in versao.obrigatorias
    assert "R-EMB-DOC" not in versao.obrigatorias


def test_sem_versao_vigente() -> None:
    with pytest.raises(LookupError):
        versao_vigente(carregar(), date(2020, 1, 1))


# Freshness ----------------------------------------------------------------------------


def test_freshness_por_fonte() -> None:
    frescas = fontes_frescas(
        {
            Fonte.IBAMA: date(2026, 10, 3),
            Fonte.MTE: date(2026, 9, 1),
            embargo_estadual("GO"): date(2026, 9, 1),
        },
        HOJE,
    )
    assert frescas[Fonte.IBAMA] is True  # 10 dias: no limite
    assert frescas[Fonte.MTE] is False  # 42 dias > 30
    assert frescas["EMBARGOS_GO"] is False  # 42 dias > 14


# Motor completo -------------------------------------------------------------------------


def test_parecer_limpo(fatos: FatosF, versao: VersaoRuleset, tol: Tolerancia) -> None:
    p = engine.avaliar(fatos(empresa=Empresa("ATIVA", "0111301")), versao, tol)
    assert p.credit_status is CreditStatus.SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS
    assert len(p.resultados) == len(REGISTRO)
    assert "Nenhum impedimento encontrado nas bases consultadas" in p.summary
    assert engine.AVISO_VINCULO in p.summary
    assert "BACEN" not in p.summary


def test_parecer_lista_todos_os_impedimentos(fatos: FatosF, versao: VersaoRuleset, tol: Tolerancia) -> None:
    f = fatos(
        mte=(RegistroMte(TipoDocumento.CNPJ, date(2025, 1, 1), False),),
        embargos_federais=Sobreposicao(D("14.5"), D("3.2")),
    )
    p = engine.avaliar(f, versao, tol)
    impedidos = {r.rule_id for r in p.resultados if r.result is R.IMPEDIDO}
    assert impedidos == {"R-MTE", "R-EMB-GEO"}
    assert p.credit_status is CreditStatus.IMPEDIMENTO_IDENTIFICADO
    assert "[R-MTE]" in p.summary and "[R-EMB-GEO]" in p.summary


def test_produtor_pf_sem_base_estadual_nao_e_inconclusivo(
    fatos: FatosF, versao: VersaoRuleset, tol: Tolerancia
) -> None:
    frescas = dict.fromkeys(Fonte, True)  # nenhuma base estadual publicada
    f = fatos(documento=parse_document("52998224725"), fontes_frescas=frescas)
    p = engine.avaliar(f, versao, tol)
    assert p.credit_status is CreditStatus.SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS
    # Fase 0: IBAMA publica CPF sem máscara, então R-EMB-DOC é verificável para PF
    assert "Não verificado: R-EMB-EST, R-RFB." in p.summary


def test_ibama_defasado_torna_inconclusivo(fatos: FatosF, versao: VersaoRuleset, tol: Tolerancia) -> None:
    frescas = {**dict.fromkeys(Fonte, True), Fonte.IBAMA: False}
    p = engine.avaliar(fatos(fontes_frescas=frescas), versao, tol)
    assert p.credit_status is CreditStatus.INCONCLUSIVO
    assert "R-EMB-GEO (obrigatória)" in p.summary


def test_fora_de_cobertura_ainda_verifica_mte(fatos: FatosF, versao: VersaoRuleset, tol: Tolerancia) -> None:
    f = fatos(
        em_cobertura=False, imovel=None, mte=(RegistroMte(TipoDocumento.CNPJ, date(2025, 1, 1), False),)
    )
    p = engine.avaliar(f, versao, tol)
    assert p.credit_status is CreditStatus.FORA_DE_COBERTURA
    assert {r.rule_id: r.result for r in p.resultados}["R-MTE"] is R.IMPEDIDO
