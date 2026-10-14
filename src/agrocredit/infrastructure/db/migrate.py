"""Aplica as migrations SQL de `migrations/` em ordem, uma transação por arquivo.

Uso: python -m agrocredit.infrastructure.db.migrate  (lê MIGRATIONS_DATABASE_URL)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

# No repositório, migrations/ fica na raiz; na imagem de ferramentas vem de AGROCREDIT_MIGRATIONS
_PADRAO = Path(__file__).resolve().parents[4] / "migrations"
MIGRATIONS_DIR = Path(os.environ.get("AGROCREDIT_MIGRATIONS", _PADRAO))

_CONTROL_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    arquivo     TEXT PRIMARY KEY,
    sha256      CHAR(64) NOT NULL,
    aplicado_em TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def apply_migrations(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Aplica as migrations pendentes e devolve os nomes aplicados.

    Falha se uma migration já aplicada tiver sido alterada (o hash não bate).
    """
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_CONTROL_TABLE)
        rows = await conn.fetch("SELECT arquivo, sha256 FROM public.schema_migrations")
        applied = {r["arquivo"]: r["sha256"] for r in rows}
        done: list[str] = []
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise RuntimeError(f"Migration já aplicada foi alterada: {path.name}")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations (arquivo, sha256) VALUES ($1, $2)",
                    path.name,
                    digest,
                )
            done.append(path.name)
        return done
    finally:
        await conn.close()


def main() -> None:
    dsn = os.environ.get("MIGRATIONS_DATABASE_URL")
    if not dsn:
        sys.exit("Defina MIGRATIONS_DATABASE_URL")
    applied = asyncio.run(apply_migrations(dsn))
    print("Nenhuma migration pendente." if not applied else "Aplicadas: " + ", ".join(applied))


if __name__ == "__main__":
    main()
