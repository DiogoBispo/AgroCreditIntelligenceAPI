"""Tarefas de manutenção (RUNBOOK §4). Cada uma recebe a conexão do papel adequado."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import polars as pl

from agrocredit.core.security import hashes_documento
from agrocredit.domain.freshness import SituacaoFonte, situacao_fontes
from agrocredit.domain.validators import Documento
from agrocredit.pipeline.gold import BASES, tabela_versao, validar_identificadores

log = logging.getLogger("agrocredit.manutencao")
_PARTICAO = re.compile(r"^credit_analysis_log_(\d{4})_(\d{2})$")
RETENCAO_AUDITORIA_ANOS = 5


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _valor_parquet(v: Any) -> Any:
    """Tipos que o Parquet não guarda nativamente viram texto sem perda (UUID, Decimal, JSON)."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, (UUID, Decimal)):
        return str(v)
    return v


def _para_parquet(linhas: list[asyncpg.Record], destino: Path) -> str:
    """Grava em Parquet (zstd) e devolve o SHA-256, conferindo a contagem relida do arquivo."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    registros: list[dict[str, Any]] = [{k: _valor_parquet(v) for k, v in dict(r).items()} for r in linhas]
    df = pl.DataFrame(registros, infer_schema_length=None) if registros else pl.DataFrame()
    df.write_parquet(destino, compression="zstd")
    relido = pl.read_parquet(destino).height
    if relido != len(linhas):
        raise RuntimeError(f"Exportação inconsistente: {len(linhas)} linhas, {relido} no arquivo")
    sha = _sha256(destino)
    destino.with_suffix(destino.suffix + ".sha256").write_text(f"{sha}  {destino.name}\n")
    return sha


# Defasagem das fontes -----------------------------------------------------------------------


async def verificar_fontes(conn: asyncpg.Connection, hoje: date | None = None) -> list[SituacaoFonte]:
    rows = await conn.fetch(
        "SELECT DISTINCT ON (fonte) fonte, source_as_of FROM core_geo.dataset_versions"
        " WHERE publicado_em IS NOT NULL AND fonte NOT LIKE 'TESTE%' ORDER BY fonte, publicado_em DESC"
    )
    return situacao_fontes({r["fonte"]: r["source_as_of"] for r in rows}, hoje or datetime.now(UTC).date())


# Auditoria ------------------------------------------------------------------------------------


async def criar_particoes(conn: asyncpg.Connection, meses: int) -> None:
    await conn.execute("SELECT audit.criar_particoes(current_date, $1)", meses)


@dataclass(frozen=True)
class ParticaoExpurgada:
    particao: str
    linhas: int
    arquivo: Path
    sha256: str


async def expurgar_auditoria(
    conn: asyncpg.Connection, destino: Path, hoje: date | None = None, anos: int = RETENCAO_AUDITORIA_ANOS
) -> list[ParticaoExpurgada]:
    """Exporta para Parquet e remove partições mensais inteiramente mais antigas que `anos` (RUNBOOK §4.3).

    A remoção só acontece depois de o arquivo ser relido e conferido. O arquivo deve ir para armazenamento
    frio cifrado (a cifra é do armazenamento, não deste processo).
    """
    hoje = hoje or datetime.now(UTC).date()
    corte = date(hoje.year - anos, hoje.month, 1)
    nomes = await conn.fetch(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'audit.credit_analysis_log'::regclass ORDER BY 1"
    )
    feitas: list[ParticaoExpurgada] = []
    for row in nomes:
        m = _PARTICAO.match(row["relname"])
        if not m or date(int(m[1]), int(m[2]), 1) >= corte:
            continue  # partição DEFAULT ou ainda dentro da retenção
        nome = row["relname"]  # validado pela regex acima
        linhas = await conn.fetch(f"SELECT * FROM audit.{nome}")  # nosec B608
        arquivo = destino / f"{nome}.parquet"
        sha = _para_parquet(linhas, arquivo)
        async with conn.transaction():
            await conn.execute(f"ALTER TABLE audit.credit_analysis_log DETACH PARTITION audit.{nome}")
            await conn.execute(f"DROP TABLE audit.{nome}")
        log.info("partição expurgada", extra={"particao": nome, "linhas": len(linhas), "sha256": sha})
        feitas.append(ParticaoExpurgada(nome, len(linhas), arquivo, sha))
    antigas_no_default = await conn.fetchval(
        "SELECT count(*) FROM audit.credit_analysis_log_default WHERE executed_at < $1", corte
    )
    if antigas_no_default:
        log.warning("partição DEFAULT tem linhas fora da retenção", extra={"linhas": antigas_no_default})
    return feitas


# Versões das bases ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersaoArquivada:
    base: str
    fonte: str
    versao: str
    linhas: int
    arquivo: Path


async def arquivar_versoes(
    conn: asyncpg.Connection, destino: Path, manter: int = 2, fontes: set[str] | None = None
) -> list[VersaoArquivada]:
    """Arquiva em Parquet e remove do banco as versões além das `manter` mais recentes de cada fonte.

    Nunca remove a versão publicada em uso pela view. O Parquet guarda a geometria em EWKB, o que permite
    reproduzir pareceres antigos (ADR-007).
    """
    if manter < 1:
        raise ValueError("manter deve ser >= 1")
    await conn.set_type_codec("geometry", schema="public", encoder=bytes, decoder=bytes, format="binary")
    em_uso = {
        (r["fonte"], r["versao"])
        for r in await conn.fetch(
            "SELECT DISTINCT ON (fonte) fonte, versao FROM core_geo.dataset_versions"
            " WHERE publicado_em IS NOT NULL ORDER BY fonte, publicado_em DESC"
        )
    }
    candidatas = await conn.fetch(
        "SELECT base, fonte, versao FROM ("
        "  SELECT base, fonte, versao, row_number() OVER (PARTITION BY fonte ORDER BY carregado_em DESC) AS n"
        "  FROM core_geo.dataset_versions WHERE arquivado_em IS NULL AND base IS NOT NULL"
        ") v WHERE n > $1 ORDER BY fonte, versao",
        manter,
    )
    feitas: list[VersaoArquivada] = []
    for c in candidatas:
        base, fonte, versao = c["base"], c["fonte"], c["versao"]
        if (fonte, versao) in em_uso or base not in BASES or (fontes is not None and fonte not in fontes):
            continue
        validar_identificadores(base, fonte, versao)
        tabela = tabela_versao(base, fonte, versao)
        existe = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", tabela)
        linhas = await conn.fetch(f"SELECT * FROM {tabela}") if existe else []  # nosec B608
        arquivo = destino / base / fonte.lower() / f"v{versao}.parquet"
        sha = _para_parquet(linhas, arquivo)
        async with conn.transaction():
            if existe:
                await conn.execute(f"DROP TABLE {tabela}")
            await conn.execute(
                "UPDATE core_geo.dataset_versions SET arquivado_em = now(), arquivo_parquet = $3,"
                " arquivo_sha256 = $4 WHERE fonte = $1 AND versao = $2",
                fonte,
                versao,
                str(arquivo),
                sha,
            )
        feitas.append(VersaoArquivada(base, fonte, versao, len(linhas), arquivo))
    return feitas


# Direitos do titular (LGPD art. 18) --------------------------------------------------------------


async def analises_do_titular(
    conn: asyncpg.Connection, documento: Documento, chaves: dict[int, bytes]
) -> list[dict[str, Any]]:
    """Análises feitas sobre o documento, em todos os tenants e em todas as versões da chave HMAC."""
    hashes = hashes_documento(documento, chaves)
    rows = await conn.fetch(
        "SELECT analysis_id, executed_at, client_tenant_id, document_masked, car_code, ruleset_version,"
        " credit_status, hmac_key_version FROM audit.credit_analysis_log"
        " WHERE (hmac_key_version, document_hash) IN (SELECT * FROM unnest($1::smallint[], $2::text[]))"
        " ORDER BY executed_at",
        list(hashes.keys()),
        list(hashes.values()),
    )
    return [dict(r) for r in rows]
