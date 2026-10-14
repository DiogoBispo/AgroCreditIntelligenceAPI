"""Health checks sem detalhes de infraestrutura (RUNBOOK §2.1)."""

import logging

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from agrocredit.presentation.api.dependencies import Recursos, recursos

router = APIRouter(prefix="/health", tags=["health"])
log = logging.getLogger("agrocredit.health")

# Sem estas bases publicadas a API não tem o que responder. As demais faltando viram NAO_VERIFICADO.
FONTES_ESSENCIAIS = ("SICAR", "IBAMA", "MTE")


@router.get("/liveness")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readiness")
async def readiness(rec: Recursos = Depends(recursos)) -> JSONResponse:  # noqa: B008
    try:
        async with rec.pool.acquire() as conn:
            await conn.fetchval("SELECT postgis_lib_version()")
            publicadas = await conn.fetchval(
                "SELECT count(DISTINCT fonte) FROM core_geo.dataset_versions"
                " WHERE publicado_em IS NOT NULL AND fonte = ANY($1::text[])",
                list(FONTES_ESSENCIAIS),
            )
    except (OSError, asyncpg.PostgresError):
        log.exception("readiness: banco indisponível")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    if publicadas != len(FONTES_ESSENCIAIS):
        log.warning("readiness: fontes essenciais não publicadas")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})
