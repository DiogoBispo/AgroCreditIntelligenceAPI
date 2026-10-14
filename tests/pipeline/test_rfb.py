"""RFB com arquivo sintético no formato real: ESTABELE em latin-1, ';', sem cabeçalho, dentro de zip."""

import csv
import zipfile
from datetime import date
from pathlib import Path

from agrocredit.pipeline.bronze import ArquivoBaixado, Manifesto
from agrocredit.pipeline.fontes.rfb import transformar_rfb


def _linha(basico: str, ordem: str, dv: str, situacao: str, cnae: str, municipio: str) -> str:
    campos = [""] * 30
    campos[0], campos[1], campos[2] = basico, ordem, dv
    campos[4] = "FAZENDA SÃO JOSÉ"  # nome com acento em latin-1: não pode ir para a saída
    campos[5], campos[11], campos[20] = situacao, cnae, municipio
    campos[27] = "contato@exemplo.com"
    return ";".join(f'"{c}"' for c in campos)


def test_transforma_recorta_e_minimiza(tmp_path: Path) -> None:
    linhas = [
        _linha("11222333", "0001", "81", "02", "0111301", "9571"),  # ativa, Rio Verde
        _linha("11222333", "0002", "62", "04", "0151201", "1234"),  # inapta, município fora do escopo
        _linha("12ABC345", "01DE", "35", "08", "0115600", "4961"),  # alfanumérico, baixada
        _linha("11222333", "0001", "81", "02", "0111301", "9571"),  # duplicada
        _linha("XX", "1", "1", "99", "0111301", "9571"),  # inválida
    ]
    with zipfile.ZipFile(tmp_path / "Estabelecimentos0.zip", "w") as z:
        z.writestr("K3241.K03200Y0.D61010.ESTABELE", ("\n".join(linhas) + "\n").encode("latin-1"))
    m = Manifesto(
        "RFB",
        "20261014000000",
        [ArquivoBaixado("https://x", "Estabelecimentos0.zip", "0" * 64, 1, "2026-10-14T00:00:00+00:00")],
    )
    m.source_as_of = "2026-10-13"
    m.parametros = {"edicao": "2026-10", "tom_para_ibge": {"9571": 5218805, "4961": 3148103}}
    m.salvar(tmp_path)

    carga = transformar_rfb(tmp_path)

    assert carga.base == "empresas" and carga.total == 3
    assert carga.source_as_of == date(2026, 10, 13)
    assert [q[1] for q in carga.quarentena] == ["cnpj_situacao_ou_cnae_invalidos"]
    assert carga.arquivo_csv is not None
    with carga.arquivo_csv.open(encoding="utf-8") as f:
        linhas_saida = list(csv.DictReader(f))
    por_cnpj = {r["cnpj"]: r for r in linhas_saida}
    assert set(por_cnpj) == {"11222333000181", "11222333000262", "12ABC34501DE35"}
    assert por_cnpj["11222333000181"] == {
        "cnpj": "11222333000181",
        "cnpj_raiz": "11222333",
        "situacao_cadastral": "ATIVA",
        "cnae_fiscal_principal": "0111301",
        "codigo_ibge": "5218805",
    }
    assert por_cnpj["11222333000262"]["situacao_cadastral"] == "INAPTA"
    assert por_cnpj["11222333000262"]["codigo_ibge"] == ""  # TOM fora do de-para
    texto = carga.arquivo_csv.read_text(encoding="utf-8")
    assert "FAZENDA" not in texto and "@" not in texto  # nome e e-mail não saem (minimização)
