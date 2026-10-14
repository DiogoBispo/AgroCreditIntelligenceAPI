import pytest

from agrocredit.domain.enums import TipoDocumento
from agrocredit.domain.validators import (
    car_codigo_ibge,
    ibge_dv_ok,
    mask_document,
    parse_car,
    parse_document,
    validate_cnpj,
    validate_cpf,
)


@pytest.mark.parametrize("cpf", ["52998224725", "12345678909"])
def test_cpf_valido(cpf: str) -> None:
    assert validate_cpf(cpf)


@pytest.mark.parametrize("cpf", ["52998224724", "11111111111", "1234567890", "5299822472a"])
def test_cpf_invalido(cpf: str) -> None:
    assert not validate_cpf(cpf)


@pytest.mark.parametrize(
    "cnpj",
    [
        "11222333000181",
        "12345678000195",
        "12ABC34501DE35",  # exemplo oficial da RFB (CNPJ alfanumérico)
    ],
)
def test_cnpj_valido(cnpj: str) -> None:
    assert validate_cnpj(cnpj)


@pytest.mark.parametrize(
    "cnpj",
    [
        "98765432000101",  # exemplo da v1.0 do PRD: DV errado
        "12ABC34501DE36",
        "00000000000000",
        "12ABC34501DEA5",  # DV não pode ser letra
        "12abc34501de35",  # minúsculas só depois de normalizar
    ],
)
def test_cnpj_invalido(cnpj: str) -> None:
    assert not validate_cnpj(cnpj)


def test_parse_document_aceita_mascara_e_minusculas() -> None:
    doc = parse_document("12.abc.345/01de-35")
    assert doc.tipo is TipoDocumento.CNPJ
    assert doc.valor == "12ABC34501DE35"
    assert doc.cnpj_raiz == "12ABC345"


def test_parse_document_cpf() -> None:
    doc = parse_document("529.982.247-25")
    assert doc.tipo is TipoDocumento.CPF
    assert doc.cnpj_raiz is None


@pytest.mark.parametrize("raw", ["", "123", "529982247251", "52998224725' OR '1'='1", "529.982.247-24"])
def test_parse_document_rejeita(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_document(raw)


def test_cpf_nao_vira_cnpj_com_zeros() -> None:
    # A v1.0 fazia LPAD do CPF para 14 dígitos; aqui CPF e CNPJ nunca se confundem
    assert parse_document("52998224725").tipo is TipoDocumento.CPF


def test_mascaras() -> None:
    assert mask_document(parse_document("52998224725")) == "***.982.247-**"
    assert mask_document(parse_document("11222333000181")) == "11.222.333/****-**"


@pytest.mark.parametrize(
    "codigo,ok", [("5218805", True), ("3148103", True), ("4104907", True), ("3148107", False)]
)
def test_ibge_dv(codigo: str, ok: bool) -> None:
    # 3148107 era o código de Patrocínio nos documentos v1.0/v1.1; o correto é 3148103
    assert ibge_dv_ok(codigo) is ok


def test_parse_car_com_pontos() -> None:
    code = parse_car("go-5218805-7281.9485.7281.9485.7281.9485.7281.9485")
    assert code == "GO-5218805-72819485728194857281948572819485"
    assert car_codigo_ibge(code) == 5218805


@pytest.mark.parametrize(
    "raw",
    [
        "GO-5218805' OR '1'='1",
        "GO-5218805-7281948572819485728194857281948",  # 31 hex
        "MG-3148107-72819485728194857281948572819485",  # IBGE com DV inválido
    ],
)
def test_parse_car_rejeita(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_car(raw)
