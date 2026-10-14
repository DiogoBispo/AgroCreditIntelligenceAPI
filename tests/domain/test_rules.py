from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from agrocredit.domain.enums import Camada, CarStatus, Fonte, RuleResult as R, TipoDocumento
from agrocredit.domain.facts import (
    EmbargoDocumental,
    EmbargoHit,
    Empresa,
    Fatos,
    Imovel,
    RegistroMte,
    Sobreposicao,
    SobreposicaoTerritorial,
    Tolerancia,
)
from agrocredit.domain.rules import r_car, r_embargos, r_mte, r_rfb, r_territoriais
from agrocredit.domain.rules.base import ContextoRegra, classificar_sobreposicao
from agrocredit.domain.validators import parse_document

FatosF = Callable[..., Fatos]


def ctx(tol: Tolerancia, **params: object) -> ContextoRegra:
    return ContextoRegra(tol, params)


# Tolerância --------------------------------------------------------------------------


def test_sem_sobreposicao_e_ok(tol: Tolerancia) -> None:
    assert classificar_sobreposicao(D(0), D(0), tol) is R.OK


def test_abaixo_da_tolerancia_e_alerta_nunca_ok(tol: Tolerancia) -> None:
    assert classificar_sobreposicao(D("0.05"), D("0.001"), tol) is R.ALERTA


def test_tolerancia_usa_o_maior_limite() -> None:
    # 1.000 ha com tol_pct = 0,5%: limite = max(0,1 ha; 5 ha) = 5 ha -> 3 ha é ALERTA
    tol = Tolerancia(ha=D("0.1"), pct=D("0.5"))
    assert classificar_sobreposicao(D("3"), D("0.3"), tol) is R.ALERTA
    assert classificar_sobreposicao(D("6"), D("0.6"), tol) is R.IMPEDIDO


def test_acima_dos_dois_limites_e_impedido(tol: Tolerancia) -> None:
    assert classificar_sobreposicao(D("14.5"), D("12.08"), tol) is R.IMPEDIDO


# R-CAR ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CarStatus.CANCELADO, CarStatus.SUSPENSO])
def test_car_cancelado_ou_suspenso_e_impedido(
    fatos: FatosF, imovel: Imovel, tol: Tolerancia, status: CarStatus
) -> None:
    f = fatos(imovel=replace(imovel, status=status))
    assert r_car.avaliar(f, ctx(tol)).result is R.IMPEDIDO


def test_car_pendente_e_ok(fatos: FatosF, imovel: Imovel, tol: Tolerancia) -> None:
    f = fatos(imovel=replace(imovel, status=CarStatus.PENDENTE))
    res = r_car.avaliar(f, ctx(tol))
    assert res.result is R.OK
    assert res.detail == {"car_status": CarStatus.PENDENTE}


def test_car_nao_encontrado_e_impedido(fatos: FatosF, tol: Tolerancia) -> None:
    assert r_car.avaliar(fatos(imovel=None), ctx(tol)).result is R.IMPEDIDO


def test_sicar_defasado_nao_afirma_nada(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(fontes_frescas={Fonte.SICAR: False})
    assert r_car.avaliar(f, ctx(tol)).result is R.NAO_VERIFICADO


def test_fonte_ausente_conta_como_nao_verificada(fatos: FatosF, tol: Tolerancia) -> None:
    assert r_car.avaliar(fatos(fontes_frescas={}), ctx(tol)).result is R.NAO_VERIFICADO


# R-MTE ------------------------------------------------------------------------------


def test_mte_por_raiz_do_cnpj(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(mte=(RegistroMte(TipoDocumento.CNPJ, date(2025, 4, 1), por_raiz_cnpj=True),))
    res = r_mte.avaliar(f, ctx(tol))
    assert res.result is R.IMPEDIDO
    assert res.detail == {"data_inclusao": "2025-04-01", "por_raiz_cnpj": True}


def test_mte_nao_depende_da_cobertura(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(em_cobertura=False, imovel=None)
    assert r_mte.avaliar(f, ctx(tol)).result is R.OK


# Embargos ------------------------------------------------------------------------------


def test_embargo_federal_impedido_com_detalhe(fatos: FatosF, tol: Tolerancia) -> None:
    hit = EmbargoHit("IBAMA", "02001.001234/2023-45", date(2024, 3, 15), D("14.50"))
    f = fatos(embargos_federais=Sobreposicao(D("14.50"), D("12.08"), (hit,)))
    res = r_embargos.avaliar_federal(f, ctx(tol))
    assert res.result is R.IMPEDIDO
    assert res.detail is not None
    assert res.detail["embargoes"][0]["process_number"] == "02001.001234/2023-45"
    assert res.detail["tolerance_applied"] == {"ha": "0.1", "pct": "0.01"}


def test_base_estadual_ausente_e_nao_verificado(fatos: FatosF, tol: Tolerancia) -> None:
    frescas = {Fonte.IBAMA: True, Fonte.SICAR: True}  # sem EMBARGOS_GO
    res = r_embargos.avaliar_estadual(fatos(fontes_frescas=frescas), ctx(tol))
    assert res.result is R.NAO_VERIFICADO
    assert res.reason is not None and "GO" in res.reason


def test_quilombola_so_titulado_impede(fatos: FatosF, tol: Tolerancia) -> None:
    params = {"fases_impeditivas": ["TITULADO", "TITULO_PARCIAL"]}
    rtid = fatos(territoriais=(terr(Camada.QUILOMBOLA, "20", "4.4", fase="RTID"),))
    titulado = fatos(territoriais=(terr(Camada.QUILOMBOLA, "20", "4.4", fase="TITULADO"),))
    assert r_territoriais.avaliar_quilombola(rtid, ContextoRegra(tol, params)).result is R.ALERTA
    assert r_territoriais.avaliar_quilombola(titulado, ContextoRegra(tol, params)).result is R.IMPEDIDO


def test_embargo_documental_e_informativo(fatos: FatosF, tol: Tolerancia) -> None:
    # MCR 2-9 item 10 trata do embargo no imóvel; o histórico do tomador não é impedimento por si só
    f = fatos(embargos_documentais=(EmbargoDocumental("IBAMA", "123", None),))
    res = r_embargos.avaliar_documental(f, ctx(tol, tipos_documento_verificaveis=["CNPJ"]))
    assert res.result is R.ALERTA


def test_embargo_estadual_nunca_impede(fatos: FatosF, tol: Tolerancia) -> None:
    # Item 10: só embargo registrado na lista do IBAMA impede
    f = fatos(embargos_estaduais=Sobreposicao(D("30"), D("6")))
    res = r_embargos.avaliar_estadual(f, ctx(tol))
    assert res.result is R.ALERTA
    assert res.reason is not None and "item 10" in res.reason


def test_embargo_em_car_coletivo_e_alerta(fatos: FatosF, imovel: Imovel, tol: Tolerancia) -> None:
    # Item 13: em CAR de assentamento/povos tradicionais o impedimento não alcança o imóvel inteiro
    f = fatos(imovel=replace(imovel, tipo_imovel="AST"), embargos_federais=Sobreposicao(D("30"), D("6")))
    res = r_embargos.avaliar_federal(f, ctx(tol))
    assert res.result is R.ALERTA
    assert res.reason is not None and "item 13" in res.reason


@pytest.mark.parametrize(
    "pct,ha,data_embargo,hoje,esperado",
    [
        ("4", "25", date(2024, 1, 1), date(2026, 10, 14), True),  # ≤5% e notificação antiga
        ("4", "25", date(2025, 3, 1), date(2026, 10, 14), False),  # notificação recente: limite de 20 ha
        ("6", "10", date(2024, 1, 1), date(2026, 10, 14), False),  # acima de 5%
        ("4", "10", date(2024, 1, 1), date(2027, 7, 1), False),  # depois de 30/06/2027
    ],
)
def test_possivel_excecao_do_item_12(
    fatos: FatosF, tol: Tolerancia, pct: str, ha: str, data_embargo: date, hoje: date, esperado: bool
) -> None:
    hit = EmbargoHit("IBAMA", "P", None, D(ha), data_embargo)
    f = fatos(embargos_federais=Sobreposicao(D(ha), D(pct), (hit,)))
    res = r_embargos.avaliar_federal(f, ContextoRegra(tol, {}, hoje))
    assert res.result is R.IMPEDIDO
    assert res.detail is not None and res.detail["possivel_excecao_item_12"] is esperado


def test_embargo_documental_cpf_verificavel(fatos: FatosF, tol: Tolerancia) -> None:
    # Fase 0: o IBAMA publica CPF sem máscara
    f = fatos(documento=parse_document("52998224725"))
    res = r_embargos.avaliar_documental(f, ctx(tol, tipos_documento_verificaveis=["CPF", "CNPJ"]))
    assert res.result is R.OK


def test_embargo_documental_cpf_mascarado_na_fonte(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(documento=parse_document("52998224725"))
    res = r_embargos.avaliar_documental(f, ctx(tol, tipos_documento_verificaveis=["CNPJ"]))
    assert res.result is R.NAO_VERIFICADO


def test_embargo_documental_cnpj_sem_registro(fatos: FatosF, tol: Tolerancia) -> None:
    res = r_embargos.avaliar_documental(fatos(), ctx(tol, tipos_documento_verificaveis=["CNPJ"]))
    assert res.result is R.OK


def test_embargo_sem_poligono_com_ponto_no_imovel_e_alerta(fatos: FatosF, tol: Tolerancia) -> None:
    res = r_embargos.avaliar_sem_geometria(fatos(embargos_sem_poligono_no_imovel=2), ctx(tol))
    assert res.result is R.ALERTA


def test_car_com_geometria_irrecuperavel_existe_mas_nao_tem_intersecao(
    fatos: FatosF, imovel: Imovel, tol: Tolerancia
) -> None:
    f = fatos(imovel=replace(imovel, area_geodesica_ha=None, problema_geometria="reparo_alterou_area"))
    assert r_car.avaliar(f, ctx(tol)).result is R.OK
    res = r_embargos.avaliar_federal(f, ctx(tol))
    assert res.result is R.NAO_VERIFICADO
    assert res.reason is not None and "reparo_alterou_area" in res.reason
    assert r_territoriais.avaliar_ti(f, ctx(tol)).result is R.NAO_VERIFICADO


def test_regra_espacial_sem_imovel_nao_verifica(fatos: FatosF, tol: Tolerancia) -> None:
    assert r_embargos.avaliar_federal(fatos(imovel=None), ctx(tol)).result is R.NAO_VERIFICADO


# Camadas territoriais ------------------------------------------------------------------


def terr(camada: Camada, ha: str, pct: str, **atributos: object) -> SobreposicaoTerritorial:
    return SobreposicaoTerritorial(camada, f"{camada}-1", "Área teste", atributos, D(ha), D(pct))


def test_ti_em_fase_impeditiva(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.TI, "20", "4.4", fase="HOMOLOGADA"),))
    res = r_territoriais.avaliar_ti(f, ctx(tol, fases_impeditivas=["HOMOLOGADA"]))
    assert res.result is R.IMPEDIDO


def test_ti_em_fase_nao_impeditiva_e_alerta(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.TI, "20", "4.4", fase="EM_ESTUDO"),))
    assert r_territoriais.avaliar_ti(f, ctx(tol, fases_impeditivas=["HOMOLOGADA"])).result is R.ALERTA


def test_tolerancia_vale_para_camadas_territoriais(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.QUILOMBOLA, "0.03", "0.006", fase="TITULADO"),))
    assert r_territoriais.avaliar_quilombola(f, ctx(tol, fases_impeditivas=["TITULADO"])).result is R.ALERTA


def test_uc_sobrepostas_pior_resultado_prevalece(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(
        territoriais=(
            terr(Camada.UC, "100", "22", categoria="APA"),
            terr(Camada.UC, "30", "6.6", categoria="RPPN"),
        )
    )
    res = r_territoriais.avaliar_uc(f, ctx(tol, categorias_dominio_publico=["PARQUE"]))
    assert res.result is R.IMPEDIDO
    assert res.detail is not None and len(res.detail["features"]) == 2


def test_ti_reserva_indigena_impede_pela_modalidade(fatos: FatosF, tol: Tolerancia) -> None:
    params = {"fases_impeditivas": ["HOMOLOGADA"], "modalidades_impeditivas": ["RESERVA_INDIGENA"]}
    f = fatos(territoriais=(terr(Camada.TI, "20", "4.4", fase="EM_ESTUDO", modalidade="RESERVA_INDIGENA"),))
    assert r_territoriais.avaliar_ti(f, ContextoRegra(tol, params)).result is R.IMPEDIDO


@pytest.mark.parametrize(
    "categoria,esperado", [("APA", R.IMPEDIDO), ("RPPN", R.IMPEDIDO), ("PARQUE", R.ALERTA)]
)
def test_uc_dominio_publico_depende_de_regularizacao(
    fatos: FatosF, tol: Tolerancia, categoria: str, esperado: R
) -> None:
    # Itens 5 e 6: UC que admite terra privada impede; de domínio exclusivamente público vira ALERTA
    f = fatos(territoriais=(terr(Camada.UC, "30", "6.6", categoria=categoria),))
    res = r_territoriais.avaliar_uc(f, ctx(tol, categorias_dominio_publico=["PARQUE", "ESEC"]))
    assert res.result is esperado


@pytest.mark.parametrize(
    "mf,tipo,inicio",
    [
        ("20", "IRU", "2027-01-04"),
        ("10", "IRU", "2027-07-01"),
        ("3", "IRU", "2028-01-03"),
        (None, "IRU", "2027-01-04"),
        ("50", "AST", "2028-01-03"),
    ],
)
def test_prodes_inicio_da_exigencia_por_porte(
    fatos: FatosF, imovel: Imovel, tol: Tolerancia, mf: str | None, tipo: str, inicio: str
) -> None:
    im = replace(imovel, modulos_fiscais=D(mf) if mf else None, tipo_imovel=tipo)
    f = fatos(imovel=im, territoriais=(terr(Camada.PRODES, "12", "2.6", data_deteccao="2024-08-10"),))
    res = r_territoriais.avaliar_prodes(f, ContextoRegra(tol, {}, date(2026, 10, 14)))
    assert res.result is R.ALERTA
    assert res.detail is not None and res.detail["exigencia_a_partir_de"] == inicio


def test_camada_de_outra_regra_e_ignorada(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.UC, "100", "22", categoria="PARNA"),))
    assert r_territoriais.avaliar_ti(f, ctx(tol, fases_impeditivas=["HOMOLOGADA"])).result is R.OK


def test_floresta_publica_tipo_a_nao_impede(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.FPND, "50", "11", tipo="A"),))
    assert r_territoriais.avaliar_fpnd(f, ctx(tol, tipos_impeditivos=["B"])).result is R.OK


@pytest.mark.parametrize(
    "data_deteccao,esperado",
    [("2019-07-31", R.OK), ("2019-08-01", R.ALERTA), (None, R.ALERTA)],
)
def test_prodes_data_de_corte(fatos: FatosF, tol: Tolerancia, data_deteccao: str | None, esperado: R) -> None:
    attrs = {} if data_deteccao is None else {"data_deteccao": data_deteccao}
    f = fatos(territoriais=(terr(Camada.PRODES, "12", "2.6", **attrs),))
    assert r_territoriais.avaliar_prodes(f, ctx(tol, data_corte="2019-07-31")).result is esperado


def test_prodes_nunca_impede(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(territoriais=(terr(Camada.PRODES, "300", "66", data_deteccao="2024-08-10"),))
    assert r_territoriais.avaliar_prodes(f, ctx(tol)).result is R.ALERTA


def test_camada_defasada(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(fontes_frescas={Fonte.FUNAI: False})
    assert r_territoriais.avaliar_ti(f, ctx(tol)).result is R.NAO_VERIFICADO


# R-RFB --------------------------------------------------------------------------------


def test_rfb_cpf_nao_verificado(fatos: FatosF, tol: Tolerancia) -> None:
    assert r_rfb.avaliar(fatos(documento=parse_document("52998224725")), ctx(tol)).result is R.NAO_VERIFICADO


def test_rfb_inapta_e_alerta(fatos: FatosF, tol: Tolerancia) -> None:
    f = fatos(empresa=Empresa("INAPTA", "0111301"))
    assert r_rfb.avaliar(f, ctx(tol)).result is R.ALERTA


def test_rfb_cnpj_fora_da_base(fatos: FatosF, tol: Tolerancia) -> None:
    assert r_rfb.avaliar(fatos(empresa=None), ctx(tol)).result is R.NAO_VERIFICADO
