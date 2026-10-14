"""RFB: situação cadastral de TODOS os estabelecimentos (CNPJ). Ver FASE0_FONTES.md §6.

Canal: compartilhamento público do Nextcloud da RFB via WebDAV (`public.php/webdav/<AAAA-MM>/`).
Só 4 colunas por estabelecimento (CNPJ, situação, CNAE principal, município): nome, endereço, telefone e
e-mail não são carregados (minimização). Todas as situações entram: INAPTA/BAIXADA geram ALERTA em R-RFB.
"""

import base64
import re
import urllib.request
import zipfile
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import duckdb

from agrocredit.pipeline.bronze import Manifesto, baixar
from agrocredit.pipeline.gold import Carga, Quarentena

FONTE = "RFB"
WEBDAV = "https://arquivos.receitafederal.gov.br/public.php/webdav"
# Identificador PÚBLICO do compartilhamento de dados abertos do CNPJ (parte da URL divulgada pela RFB);
# não é credencial: o acesso é anônimo.
TOKEN_COMPARTILHAMENTO = "YggdBLfdninEJX9"  # noqa: S105  # gitleaks:allow
COLUNAS = ("cnpj", "cnpj_raiz", "situacao_cadastral", "cnae_fiscal_principal", "codigo_ibge")
_PASTA = re.compile(rb"webdav/(\d{4}-\d{2})/</d:href>")
_ARQUIVO = re.compile(
    rb"<d:href>[^<]*/(Estabelecimentos\d\.zip)</d:href>.*?<d:getlastmodified>([^<]+)<", re.S
)


def _auth() -> str:
    return "Basic " + base64.b64encode(f"{TOKEN_COMPARTILHAMENTO}:".encode()).decode()


def _propfind(caminho: str) -> bytes:
    req = urllib.request.Request(  # noqa: S310 (URL https fixa)
        f"{WEBDAV}/{caminho}", method="PROPFIND", headers={"Depth": "1", "Authorization": _auth()}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310
        corpo: bytes = resp.read()
    return corpo


def edicao_mais_recente() -> tuple[str, dict[str, datetime]]:
    """Pasta AAAA-MM mais recente e a data de publicação de cada arquivo de estabelecimentos."""
    pastas = sorted(set(_PASTA.findall(_propfind(""))))
    if not pastas:
        raise RuntimeError("WebDAV da RFB sem pastas de edição")
    pasta = pastas[-1].decode()
    respostas = re.findall(rb"<d:response>.*?</d:response>", _propfind(f"{pasta}/"), re.S)
    arquivos: dict[str, datetime] = {}
    for r in respostas:
        m = _ARQUIVO.search(r)
        if m:
            arquivos[m[1].decode()] = parsedate_to_datetime(m[2].decode())
    if len(arquivos) != 10:
        raise RuntimeError(
            f"Edição {pasta}: esperados 10 arquivos de estabelecimentos, encontrados {len(arquivos)}"
        )
    return pasta, arquivos


def baixar_rfb(diretorio: Path, versao: str, tom_para_ibge: dict[int, int]) -> Manifesto:
    pasta, arquivos = edicao_mais_recente()
    manifesto = Manifesto(FONTE, versao)
    for nome in sorted(arquivos):
        manifesto.arquivos.append(
            baixar(f"{WEBDAV}/{pasta}/{nome}", diretorio / nome, auth_header=_auth(), timeout=3600)
        )
    manifesto.source_as_of = max(arquivos.values()).astimezone(UTC).date().isoformat()
    manifesto.parametros = {"edicao": pasta, "tom_para_ibge": {str(k): v for k, v in tom_para_ibge.items()}}
    manifesto.salvar(diretorio)
    return manifesto


def _para_utf8(zip_path: Path, destino: Path) -> None:
    """Descompacta o único membro do zip convertendo ISO-8859-1 → UTF-8 em blocos.

    O leitor de CSV do DuckDB recusa latin-1.
    """
    with zipfile.ZipFile(zip_path) as z:
        membros = z.namelist()
        if len(membros) != 1:
            raise ValueError(f"{zip_path.name}: esperado 1 arquivo, encontrados {len(membros)}")
        with z.open(membros[0]) as origem, destino.open("w", encoding="utf-8", newline="") as saida:
            while bloco := origem.read(16 * 1024 * 1024):
                saida.write(bloco.decode("latin-1"))


# Consultas estáticas (nenhum valor externo entra no texto SQL; caminhos vão como parâmetro ou pela API)
_SQL_LER_PARTE = """
INSERT INTO estab
SELECT column00 || column01 || column02, column00, column05, column11, column20
FROM read_csv(?, delim=';', quote='"', header=false, all_varchar=true)
"""
_SQL_SITUACOES = """
CREATE OR REPLACE TABLE situacoes AS
SELECT * FROM (VALUES
    ('01', 'NULA'), ('02', 'ATIVA'), ('03', 'SUSPENSA'), ('04', 'INAPTA'), ('08', 'BAIXADA')
) s(codigo, nome)
"""
_SQL_FINAL = """
CREATE OR REPLACE TABLE final AS
SELECT DISTINCT ON (e.cnpj) e.cnpj, e.cnpj_raiz, s.nome AS situacao_cadastral,
       regexp_replace(e.cnae, '[^0-9]', '', 'g') AS cnae_fiscal_principal,
       t.ibge AS codigo_ibge
FROM estab e
JOIN situacoes s ON s.codigo = e.situacao
LEFT JOIN tom_ibge t ON t.tom = e.municipio
WHERE regexp_matches(e.cnpj, '^[0-9A-Z]{12}[0-9]{2}$')
  AND length(regexp_replace(e.cnae, '[^0-9]', '', 'g')) = 7
ORDER BY e.cnpj
"""
_SQL_QUARENTENA = """
SELECT e.cnpj, e.situacao FROM estab e
WHERE NOT regexp_matches(e.cnpj, '^[0-9A-Z]{12}[0-9]{2}$')
   OR e.situacao NOT IN (SELECT codigo FROM situacoes)
   OR length(regexp_replace(e.cnae, '[^0-9]', '', 'g')) <> 7
LIMIT 10000
"""


def transformar_rfb(diretorio: Path) -> Carga:
    """Une as 10 partes, recorta 4 colunas e grava um CSV para COPY. Layout ESTABELE (sem cabeçalho):
    0-2 CNPJ (básico, ordem, DV), 5 situação, 11 CNAE principal, 20 município (TOM)."""
    manifesto = Manifesto.carregar(diretorio)
    tom_para_ibge = {int(k): v for k, v in manifesto.parametros.get("tom_para_ibge", {}).items()}
    trabalho = diretorio / "_trabalho"
    trabalho.mkdir(exist_ok=True)
    saida = trabalho / "estabelecimentos.csv"
    con = duckdb.connect(str(trabalho / "rfb.duckdb"))
    try:
        con.execute("CREATE OR REPLACE TABLE tom_ibge (tom VARCHAR, ibge INTEGER)")
        con.executemany(
            "INSERT INTO tom_ibge VALUES (?, ?)", [(f"{k:04d}", v) for k, v in tom_para_ibge.items()]
        )
        con.execute(
            "CREATE OR REPLACE TABLE estab (cnpj VARCHAR, cnpj_raiz VARCHAR, situacao VARCHAR,"
            " cnae VARCHAR, municipio VARCHAR)"
        )
        con.execute(_SQL_SITUACOES)
        for zip_path in sorted(diretorio.glob("Estabelecimentos*.zip")):
            csv = trabalho / (zip_path.stem + ".csv")
            _para_utf8(zip_path, csv)
            con.execute(_SQL_LER_PARTE, [str(csv)])
            csv.unlink()
        quarentena: Quarentena = [
            (cnpj, "cnpj_situacao_ou_cnae_invalidos", {"situacao": situacao})
            for cnpj, situacao in con.execute(_SQL_QUARENTENA).fetchall()
        ]
        con.execute(_SQL_FINAL)
        total = con.execute("SELECT count(*) FROM final").fetchone()
        con.table("final").write_csv(str(saida), header=True)
    finally:
        con.close()
    (trabalho / "rfb.duckdb").unlink(missing_ok=True)
    return Carga(
        base="empresas",
        fonte=FONTE,
        versao=manifesto.versao,
        source_as_of=date.fromisoformat(str(manifesto.source_as_of)),
        colunas=COLUNAS,
        registros=[],
        quarentena=quarentena,
        manifesto=manifesto,
        arquivo_csv=saida,
        total_csv=int(total[0]) if total else 0,
    )
