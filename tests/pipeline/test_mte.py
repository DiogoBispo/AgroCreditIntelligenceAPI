from datetime import date

import pytest

from agrocredit.pipeline.fontes.mte import ExtracaoInconsistente, data_atualizacao, extrair

CABECALHO = "                                Atualização periódica de 6 de outubro de 2026.\n"
LINHAS = """
                        11.222.333 FULANO
  1     2024     SP                                  11.222.333/0001-81        ROD. X, KM 1           2   4399-1/03   15/05/2025   06/10/2025
                                    DOS SANTOS

  2     2024     MT      BELTRANO                    529.982.247-25            ZONA RURAL             2   0151-2/01   08/10/2024   09/04/2025
  3     2025     MT      BELTRANO                    529.982.247-25            OUTRA FAZENDA          5   0151-2/01   08/10/2024   09/10/2025
"""  # noqa: E501


def test_data_de_atualizacao() -> None:
    assert data_atualizacao(CABECALHO) == date(2026, 10, 6)


def test_extrai_cpf_cnpj_e_deduplica_pela_inclusao_mais_antiga() -> None:
    registros, quarentena = extrair(CABECALHO + LINHAS)
    assert quarentena == []
    por_doc = {r[1]: r for r in registros}
    assert por_doc["11222333000181"] == ("CNPJ", "11222333000181", "11222333", None, date(2025, 10, 6))
    assert por_doc["52998224725"] == ("CPF", "52998224725", None, None, date(2025, 4, 9))


def test_nome_nao_e_guardado() -> None:
    registros, _ = extrair(CABECALHO + LINHAS)
    assert all(r[3] is None for r in registros)


def test_extracao_incompleta_bloqueia() -> None:
    # ID 4 sem documento extraível: 3 documentos para maior ID 4
    texto = (
        CABECALHO + LINHAS + "  4     2025     PR      SICRANO     (documento ilegível)     1   0151-2/01\n"
    )
    with pytest.raises(ExtracaoInconsistente):
        extrair(texto)


def test_documento_com_dv_invalido_vai_para_quarentena() -> None:
    texto = CABECALHO + LINHAS.replace("529.982.247-25            ZONA", "529.982.247-24            ZONA")
    registros, quarentena = extrair(texto)
    assert [q[1] for q in quarentena] == ["documento_invalido"]
    assert len(registros) == 2
