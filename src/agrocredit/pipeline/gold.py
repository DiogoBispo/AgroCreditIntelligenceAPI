"""Camada Gold: carga em tabela versionada, checagens de sanidade e publicação atômica.

Cada (base, fonte) tem sua tabela por versão (core_geo.<base>_<fonte>_v<versao>). A view
core_geo.<base>_current é a união da última versão publicada de cada fonte da base.
Executa como role_etl (SET ROLE) para que tabelas e views pertençam ao papel, não ao usuário de login.
"""

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from agrocredit.pipeline.bronze import Manifesto

BASES = frozenset({"imoveis_car", "embargos", "mte", "empresas", "camadas_territoriais"})
_FONTE = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")
_VERSAO = re.compile(r"^\d{14}$")
VARIACAO_MAXIMA = 0.20  # ±20% de registros em relação à versão publicada anterior


# (chave do registro na fonte, motivo, payload para auditoria)
Quarentena = list[tuple[str | None, str, dict[str, Any] | None]]


class PublicacaoBloqueada(Exception):
    """Checagem de sanidade falhou; a versão foi carregada mas não publicada (RUNBOOK INC-05)."""


@dataclass
class Carga:
    base: str
    fonte: str
    versao: str
    source_as_of: date
    colunas: Sequence[str]
    registros: Sequence[tuple[Any, ...]]  # colunas de geometria como EWKB (SRID 4674)
    quarentena: Quarentena
    manifesto: Manifesto | None = None

    def __post_init__(self) -> None:
        validar_identificadores(self.base, self.fonte, self.versao)

    def resumo(self) -> str:
        motivos = Counter(motivo.split(":")[0] for _, motivo, _ in self.quarentena)
        return f"{self.fonte} {self.versao}: {len(self.registros)} registros; quarentena {dict(motivos)}"

    @property
    def tabela(self) -> str:
        return tabela_versao(self.base, self.fonte, self.versao)


def validar_identificadores(base: str, fonte: str, versao: str) -> None:
    """Os três entram em nomes de tabela (não parametrizáveis em SQL): só valores de allowlist/regex."""
    if base not in BASES:
        raise ValueError(f"Base desconhecida: {base}")
    if not _FONTE.match(fonte):
        raise ValueError(f"Fonte inválida: {fonte}")
    if not _VERSAO.match(versao):
        raise ValueError(f"Versão inválida: {versao}")


def tabela_versao(base: str, fonte: str, versao: str) -> str:
    return f"core_geo.{base}_{fonte.lower()}_v{versao}"


async def _conectar(dsn: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn)
    await conn.execute("SET ROLE role_etl")
    # EWKB direto no COPY binário (o send/recv da geometria do PostGIS é EWKB)
    await conn.set_type_codec("geometry", schema="public", encoder=bytes, decoder=bytes, format="binary")
    return conn


async def carregar(dsn: str, carga: Carga) -> int:
    """Cria a tabela da versão, copia os registros e registra manifesto e quarentena. Não publica."""
    conn = await _conectar(dsn)
    try:
        async with conn.transaction():
            await conn.execute(
                f"CREATE TABLE {carga.tabela} (LIKE core_geo.{carga.base}_template INCLUDING ALL)"
            )
            await conn.copy_records_to_table(
                carga.tabela.split(".")[1],
                schema_name="core_geo",
                columns=list(carga.colunas),
                records=carga.registros,
            )
            await conn.execute(f"ANALYZE {carga.tabela}")
            if carga.manifesto:
                await conn.executemany(
                    "INSERT INTO raw_ingest.sources_manifest"
                    " (fonte, versao, url, sha256, tamanho_bytes, source_as_of, baixado_em)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7::text::timestamptz)",
                    [
                        (
                            carga.fonte,
                            carga.versao,
                            a.url,
                            a.sha256,
                            a.tamanho_bytes,
                            carga.source_as_of,
                            a.baixado_em,
                        )
                        for a in carga.manifesto.arquivos
                    ],
                )
            if carga.quarentena:
                await conn.copy_records_to_table(
                    "quarantine_records",
                    schema_name="raw_ingest",
                    columns=["fonte", "versao", "chave", "motivo", "payload"],
                    records=[
                        (
                            carga.fonte,
                            carga.versao,
                            chave,
                            motivo,
                            json.dumps(payload, default=str) if payload is not None else None,
                        )
                        for chave, motivo, payload in carga.quarentena
                    ],
                )
            await conn.execute(
                "INSERT INTO core_geo.dataset_versions"
                " (fonte, versao, base, source_as_of, registros, quarentena) VALUES ($1, $2, $3, $4, $5, $6)",
                carga.fonte,
                carga.versao,
                carga.base,
                carga.source_as_of,
                len(carga.registros),
                len(carga.quarentena),
            )
        return len(carga.registros)
    finally:
        await conn.close()


async def _checar_sanidade(conn: asyncpg.Connection, base: str, fonte: str, versao: str) -> list[str]:
    tabela = tabela_versao(base, fonte, versao)
    problemas: list[str] = []
    # Nomes de tabela não são parametrizáveis; os identificadores foram validados (validar_identificadores)
    n = await conn.fetchval(f"SELECT count(*) FROM {tabela}")  # nosec B608
    if n == 0:
        problemas.append("versão vazia")
    anterior = await conn.fetchrow(
        "SELECT versao, registros FROM core_geo.dataset_versions"
        " WHERE base = $1 AND fonte = $2 AND publicado_em IS NOT NULL AND versao <> $3"
        " ORDER BY publicado_em DESC LIMIT 1",
        base,
        fonte,
        versao,
    )
    if anterior and anterior["registros"]:
        variacao = (n - anterior["registros"]) / anterior["registros"]
        if abs(variacao) > VARIACAO_MAXIMA:
            problemas.append(
                f"volume variou {variacao:+.1%} em relação à versão {anterior['versao']}"
                f" ({anterior['registros']} → {n})"
            )
    geo_cols = await conn.fetch(
        "SELECT attname FROM pg_attribute WHERE attrelid = $1::regclass AND atttypid = 'geometry'::regtype"
        " AND attnum > 0 AND NOT attisdropped",
        tabela,
    )
    for col in (r["attname"] for r in geo_cols):
        coluna = '"' + col.replace('"', '""') + '"'  # nome vindo do catálogo, citado mesmo assim
        sql = f"SELECT count(*) FROM {tabela} WHERE NOT ST_IsValid({coluna})"  # nosec B608
        invalidas = await conn.fetchval(sql)
        if invalidas:
            problemas.append(f"{invalidas} geometria(s) inválida(s) em {col}")
    return problemas


async def _recriar_view(conn: asyncpg.Connection, base: str) -> None:
    versoes = await conn.fetch(
        "SELECT DISTINCT ON (fonte) fonte, versao FROM core_geo.dataset_versions"
        " WHERE base = $1 AND publicado_em IS NOT NULL ORDER BY fonte, publicado_em DESC",
        base,
    )
    # Identificadores vindos de dataset_versions, revalidados por tabela_versao/validar_identificadores
    for r in versoes:
        validar_identificadores(base, r["fonte"], r["versao"])
    partes = [f"SELECT * FROM {tabela_versao(base, r['fonte'], r['versao'])}" for r in versoes]  # nosec B608
    corpo = " UNION ALL ".join(partes) or f"SELECT * FROM core_geo.{base}_template"  # nosec B608
    await conn.execute(f"CREATE OR REPLACE VIEW core_geo.{base}_current AS {corpo}")


async def publicar(
    dsn: str,
    base: str,
    fonte: str,
    versao: str,
    *,
    aprovado_por: str | None = None,
    motivo: str | None = None,
) -> None:
    """Publica uma versão carregada. Checagem que falha bloqueia, salvo aprovação explícita com motivo."""
    validar_identificadores(base, fonte, versao)
    if (aprovado_por is None) != (motivo is None):
        raise ValueError("Aprovação manual exige aprovado_por e motivo")
    conn = await _conectar(dsn)
    try:
        problemas = await _checar_sanidade(conn, base, fonte, versao)
        if problemas and aprovado_por is None:
            await conn.execute(
                "UPDATE core_geo.dataset_versions SET bloqueio = $3 WHERE fonte = $1 AND versao = $2",
                fonte,
                versao,
                "; ".join(problemas),
            )
            raise PublicacaoBloqueada("; ".join(problemas))
        async with conn.transaction():
            await conn.execute(
                "UPDATE core_geo.dataset_versions SET publicado_em = now(), aprovado_por = $3,"
                " motivo_aprovacao = $4, bloqueio = NULL WHERE fonte = $1 AND versao = $2",
                fonte,
                versao,
                aprovado_por,
                motivo,
            )
            await _recriar_view(conn, base)
    finally:
        await conn.close()
