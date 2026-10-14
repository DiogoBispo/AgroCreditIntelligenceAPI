"""CLI de manutenção (RUNBOOK §4).

    python -m agrocredit.manutencao fontes                    # defasagem das bases; saída 0 OK, 1 P2, 2 P1
    python -m agrocredit.manutencao particoes --meses 3       # partições mensais da auditoria à frente
    python -m agrocredit.manutencao expurgar-auditoria --destino /frio/auditoria
    python -m agrocredit.manutencao limpar-versoes --manter 2 --destino /frio/versoes
    python -m agrocredit.manutencao titular --documento 529.982.247-25   # pedido do titular (LGPD art. 18)

Variáveis: ETL_DATABASE_URL (fontes, limpar-versoes), MAINT_DATABASE_URL (auditoria, titular) e, para
`titular`, as chaves HMAC (AGROCREDIT_HMAC_PEPPER, AGROCREDIT_HMAC_KEY_VERSION e
AGROCREDIT_HMAC_PEPPERS_ANTERIORES).
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import asyncpg

from agrocredit.core.config import ChavesHMAC
from agrocredit.domain.validators import parse_document
from agrocredit.manutencao import tarefas, validacao


async def _com_papel[T](env: str, papel: str, tarefa: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
    dsn = os.environ.get(env)
    if not dsn:
        sys.exit(f"Defina {env}")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"SET ROLE {papel}")  # papel fixo no código, nunca entrada do usuário
        return await tarefa(conn)
    finally:
        await conn.close()


def _imprimir(dados: Any) -> None:
    print(json.dumps(dados, ensure_ascii=False, default=str, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agrocredit.manutencao")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fontes", help="defasagem das bases publicadas (para alertas)")
    p_part = sub.add_parser("particoes", help="cria partições mensais da auditoria")
    p_part.add_argument("--meses", type=int, default=3)
    p_exp = sub.add_parser("expurgar-auditoria", help="exporta e remove partições além da retenção")
    p_exp.add_argument("--destino", type=Path, required=True)
    p_exp.add_argument("--anos", type=int, default=tarefas.RETENCAO_AUDITORIA_ANOS)
    p_ver = sub.add_parser("limpar-versoes", help="arquiva versões antigas das bases em Parquet")
    p_ver.add_argument("--destino", type=Path, required=True)
    p_ver.add_argument("--manter", type=int, default=2)
    p_ver.add_argument("--fonte", action="append", help="restringe a estas fontes (repetível)")
    p_val = sub.add_parser("validar-espacial", help="compara sobreposições da API com cálculo independente")
    p_val.add_argument("--limite", type=int, help="máximo de imóveis (padrão: todos os cobertos)")
    p_tit = sub.add_parser("titular", help="análises sobre um documento (pedido do titular, LGPD art. 18)")
    p_tit.add_argument("--documento", required=True)
    args = p.parse_args()

    if args.cmd == "fontes":
        situacoes = asyncio.run(_com_papel("ETL_DATABASE_URL", "role_etl", tarefas.verificar_fontes))
        _imprimir([asdict(s) for s in situacoes])
        niveis = {s.nivel for s in situacoes}
        sys.exit(2 if "P1" in niveis else 1 if "P2" in niveis else 0)
    elif args.cmd == "particoes":
        asyncio.run(
            _com_papel("MAINT_DATABASE_URL", "role_maint", lambda c: tarefas.criar_particoes(c, args.meses))
        )
        print(f"Partições garantidas para os próximos {args.meses} meses")
    elif args.cmd == "expurgar-auditoria":
        feitas = asyncio.run(
            _com_papel(
                "MAINT_DATABASE_URL",
                "role_maint",
                lambda c: tarefas.expurgar_auditoria(c, args.destino, anos=args.anos),
            )
        )
        _imprimir([asdict(f) for f in feitas])
    elif args.cmd == "validar-espacial":
        rel = asyncio.run(
            _com_papel("ETL_DATABASE_URL", "role_etl", lambda c: validacao.validar(c, args.limite))
        )
        _imprimir(
            {
                "imoveis": rel.imoveis,
                "comparacoes": rel.comparacoes,
                "classificacao_divergente": rel.classificacao_divergente,
                "area_divergente_acima_de_0_01_ha": rel.area_divergente,
                "maior_diferenca_ha": round(rel.maior_diferenca_ha, 6),
                "resultados_independentes": dict(rel.resultados),
            }
        )
        sys.exit(1 if rel.classificacao_divergente else 0)
    elif args.cmd == "limpar-versoes":
        arquivadas = asyncio.run(
            _com_papel(
                "ETL_DATABASE_URL",
                "role_etl",
                lambda c: tarefas.arquivar_versoes(
                    c, args.destino, args.manter, set(args.fonte) if args.fonte else None
                ),
            )
        )
        _imprimir([asdict(a) for a in arquivadas])
    else:
        documento = parse_document(args.documento)
        chaves = ChavesHMAC().todas()  # type: ignore[call-arg]  # vem do ambiente
        _imprimir(
            asyncio.run(
                _com_papel(
                    "MAINT_DATABASE_URL",
                    "role_maint",
                    lambda c: tarefas.analises_do_titular(c, documento, chaves),
                )
            )
        )


if __name__ == "__main__":
    main()
