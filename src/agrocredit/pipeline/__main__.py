"""CLI do pipeline.

    python -m agrocredit.pipeline run sicar               # baixa, transforma, carrega e publica
    python -m agrocredit.pipeline run ibama --versao V    # reprocessa um Bronze já baixado
    python -m agrocredit.pipeline run cnuc --arquivo cnuc_2026_07.zip --as-of 2026-07-01
        # arquivo obtido fora do pipeline (link que exige login, resposta via LAI)
    python -m agrocredit.pipeline publicar ibama V --aprovado-por NOME --motivo "..."   # INC-05

Lê ETL_DATABASE_URL. Bronze em $AGROCREDIT_DATA/bronze/<fonte>/<versao>/ (padrão ./data).
As camadas territoriais usam os envelopes dos municípios cobertos: carregue o SICAR antes.
"""

import argparse
import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import asyncpg

from agrocredit.pipeline import gold
from agrocredit.pipeline.bronze import Manifesto, nova_versao, registrar_manual
from agrocredit.pipeline.fontes import embargos_mg, ibama, mte, sicar, territoriais

MARGEM_GRAUS = 0.05  # ~5 km além do envelope dos imóveis cobertos


@dataclass(frozen=True)
class Escopo:
    municipios: list[tuple[int, str]]
    envelopes: list[territoriais.Envelope] = field(default_factory=list)


@dataclass(frozen=True)
class Fonte:
    nome_fonte: str
    base: str
    baixar: Callable[[Path, str, Escopo], Manifesto]
    transformar: Callable[[Path], gold.Carga]
    precisa_envelopes: bool = False
    aceita_arquivo: bool = False
    # Data de referência vem do conteúdo (não do download): baixar o mesmo arquivo de novo não traz
    # informação nova, então a carga é pulada. Onde a data é a do download (SICAR, INCRA), recarregar
    # reconfirma a atualidade e não pode ser pulado.
    data_do_conteudo: bool = True


def _territorial(url: str, arquivo: str, nome: str) -> Callable[[Path, str, Escopo], Manifesto]:
    baixar = territoriais.baixar_arquivo_unico(url, arquivo, nome)
    return lambda d, v, esc: baixar(d, v, esc.envelopes)


FONTES: dict[str, Fonte] = {
    "sicar": Fonte(
        "SICAR",
        "imoveis_car",
        lambda d, v, e: sicar.baixar_sicar(e.municipios, d, v),
        sicar.transformar_sicar,
        data_do_conteudo=False,
    ),
    "ibama": Fonte("IBAMA", "embargos", lambda d, v, _: ibama.baixar_ibama(d, v), ibama.transformar_ibama),
    "embargos_mg": Fonte(
        "EMBARGOS_MG", "embargos", lambda d, v, _: embargos_mg.baixar_mg(d, v), embargos_mg.transformar_mg
    ),
    "mte": Fonte("MTE", "mte", lambda d, v, _: mte.baixar_mte(d, v), mte.transformar_mte),
    "cnuc": Fonte(
        "CNUC",
        "camadas_territoriais",
        _territorial(territoriais.URL_CNUC, "cnuc.zip", "CNUC"),
        territoriais.transformar_cnuc,
        precisa_envelopes=True,
        aceita_arquivo=True,
    ),
    "cnfp": Fonte(
        "SFB",
        "camadas_territoriais",
        _territorial(territoriais.URL_CNFP, "cnfp.zip", "SFB"),
        territoriais.transformar_cnfp,
        precisa_envelopes=True,
        aceita_arquivo=True,
    ),
    "quilombolas": Fonte(
        "INCRA",
        "camadas_territoriais",
        _territorial(territoriais.URL_QUILOMBOLAS, "quilombolas.zip", "INCRA"),
        territoriais.transformar_quilombolas,
        precisa_envelopes=True,
        aceita_arquivo=True,
        data_do_conteudo=False,
    ),
    "prodes": Fonte(
        "PRODES",
        "camadas_territoriais",
        lambda d, v, e: territoriais.baixar_prodes(d, v, e.envelopes),
        territoriais.transformar_prodes,
        precisa_envelopes=True,
    ),
}


async def escopo(dsn: str, com_envelopes: bool) -> Escopo:
    conn = await asyncpg.connect(dsn)
    try:
        municipios = [
            (r["codigo_ibge"], r["uf"])
            for r in await conn.fetch(
                "SELECT codigo_ibge, uf FROM core_geo.municipios WHERE em_cobertura ORDER BY codigo_ibge"
            )
        ]
        envelopes: list[territoriais.Envelope] = []
        if com_envelopes:
            rows = await conn.fetch(
                "SELECT ST_XMin(e) - $1 AS x0, ST_YMin(e) - $1 AS y0,"
                " ST_XMax(e) + $1 AS x1, ST_YMax(e) + $1 AS y1"
                " FROM (SELECT ST_Extent(i.geometria) AS e FROM core_geo.imoveis_car_current i"
                "       JOIN core_geo.municipios m ON m.codigo_ibge = i.codigo_ibge AND m.em_cobertura"
                "       GROUP BY i.codigo_ibge) s",
                MARGEM_GRAUS,
            )
            envelopes = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in rows]
            if not envelopes:
                sys.exit("Sem imóveis do SICAR publicados: rode `run sicar` antes das camadas territoriais.")
    finally:
        await conn.close()
    return Escopo(municipios, envelopes)


async def _ja_carregada(dsn: str, carga: gold.Carga) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM core_geo.dataset_versions WHERE fonte = $1 AND versao = $2",
                carga.fonte,
                carga.versao,
            )
        )
    finally:
        await conn.close()


async def _sem_mudanca(dsn: str, fonte: str, manifesto: Manifesto) -> bool:
    """Os arquivos baixados são idênticos (SHA-256) aos da versão publicada em uso?"""
    conn = await asyncpg.connect(dsn)
    try:
        anteriores = await conn.fetch(
            "SELECT m.sha256 FROM raw_ingest.sources_manifest m"
            " JOIN (SELECT versao FROM core_geo.dataset_versions"
            "       WHERE fonte = $1 AND publicado_em IS NOT NULL"
            "       ORDER BY publicado_em DESC LIMIT 1) v ON v.versao = m.versao"
            " WHERE m.fonte = $1",
            fonte,
        )
    finally:
        await conn.close()
    return bool(anteriores) and sorted(r["sha256"].strip() for r in anteriores) == sorted(
        a.sha256 for a in manifesto.arquivos
    )


async def run(
    nome: str, versao: str | None, dsn: str, dados: Path, arquivo: Path | None, as_of: date | None
) -> None:
    fonte = FONTES[nome]
    if versao is None:
        versao = nova_versao()
        diretorio = dados / "bronze" / nome / versao
        diretorio.mkdir(parents=True)
        esc = await escopo(dsn, fonte.precisa_envelopes)
        if arquivo:
            manifesto = Manifesto(
                fonte.nome_fonte, versao, [registrar_manual(arquivo, diretorio / arquivo.name, arquivo.name)]
            )
            manifesto.parametros["envelopes"] = [list(e) for e in esc.envelopes]
            manifesto.source_as_of = as_of.isoformat() if as_of else None
            manifesto.salvar(diretorio)
            print(f"[{nome}] arquivo manual registrado na versão {versao} (dados de {as_of})")
        else:
            print(f"[{nome}] baixando versão {versao} em {diretorio}")
            manifesto = fonte.baixar(diretorio, versao, esc)
            if fonte.data_do_conteudo and await _sem_mudanca(dsn, fonte.nome_fonte, manifesto):
                shutil.rmtree(diretorio)
                print(f"[{nome}] arquivos idênticos aos da versão publicada: nada a carregar")
                return
    else:
        diretorio = dados / "bronze" / nome / versao
    carga = fonte.transformar(diretorio)
    if await _ja_carregada(dsn, carga):
        # Reprocessamento: mesmo Bronze, nova versão no Gold (a transformação pode ter mudado)
        carga = replace(carga, versao=nova_versao())
    print(f"[{nome}] {carga.resumo()}")
    await gold.carregar(dsn, carga)
    try:
        await gold.publicar(dsn, carga.base, carga.fonte, carga.versao)
    except gold.PublicacaoBloqueada as exc:
        sys.exit(f"[{nome}] versão {carga.versao} carregada mas NÃO publicada: {exc}. Ver RUNBOOK INC-05.")
    print(f"[{nome}] versão {carga.versao} publicada (dados de {carga.source_as_of})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agrocredit.pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="baixa (ou reaproveita --versao), transforma, carrega e publica")
    p_run.add_argument("fonte", choices=sorted(FONTES))
    p_run.add_argument("--versao", help="reprocessa um Bronze existente em vez de baixar")
    p_run.add_argument("--arquivo", type=Path, help="usa um arquivo obtido fora do pipeline (só fontes zip)")
    p_run.add_argument(
        "--as-of", type=date.fromisoformat, help="data de referência do --arquivo (AAAA-MM-DD)"
    )
    p_pub = sub.add_parser("publicar", help="publica uma versão carregada e bloqueada, com aprovação manual")
    p_pub.add_argument("fonte", choices=sorted(FONTES))
    p_pub.add_argument("versao")
    p_pub.add_argument("--aprovado-por", required=True)
    p_pub.add_argument("--motivo", required=True)
    args = parser.parse_args()

    dsn = os.environ.get("ETL_DATABASE_URL")
    if not dsn:
        sys.exit("Defina ETL_DATABASE_URL")
    dados = Path(os.environ.get("AGROCREDIT_DATA", "data"))

    if args.cmd == "run":
        if args.arquivo and not FONTES[args.fonte].aceita_arquivo:
            sys.exit(f"A fonte {args.fonte} não aceita --arquivo")
        if args.arquivo and not args.as_of:
            sys.exit("--arquivo exige --as-of (data de referência dos dados)")
        asyncio.run(run(args.fonte, args.versao, dsn, dados, args.arquivo, args.as_of))
    else:
        fonte = FONTES[args.fonte]
        asyncio.run(
            gold.publicar(
                dsn,
                fonte.base,
                fonte.nome_fonte,
                args.versao,
                aprovado_por=args.aprovado_por,
                motivo=args.motivo,
            )
        )
        print(f"[{args.fonte}] versão {args.versao} publicada com aprovação de {args.aprovado_por}")


if __name__ == "__main__":
    main()
