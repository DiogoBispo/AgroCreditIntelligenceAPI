"""Aplicação FastAPI: recursos, middlewares de segurança e tratamento de erros sem vazamento.

uvicorn --factory agrocredit.presentation.api.app:create_app
"""

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from agrocredit.application.use_cases.check_credit_compliance import TenantNaoAutorizado
from agrocredit.core import logging as app_logging
from agrocredit.core.config import Settings, get_settings
from agrocredit.core.security import ChavesJWKS, VerificadorJWT
from agrocredit.domain.ruleset import carregar
from agrocredit.presentation.api.dependencies import LimitadorTaxa, Recursos
from agrocredit.presentation.api.metrics import Metricas, router as metrics_router
from agrocredit.presentation.api.v1.endpoints import credit, health

log = logging.getLogger("agrocredit.api")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")
_HEADERS_SEGURANCA = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


async def criar_recursos(settings: Settings) -> Recursos:
    pool = await asyncpg.create_pool(
        settings.database_url.get_secret_value(),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        server_settings={
            "statement_timeout": str(settings.statement_timeout_ms),
            "application_name": "agrocredit-api",
        },
    )
    return Recursos(
        pool=pool,
        verificador=VerificadorJWT(
            ChavesJWKS(jwks_json=_jwks_local(settings), url=settings.jwt_jwks_url),
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.jwt_tenant_claim,
        ),
        rulesets=carregar(),
        hmac_pepper=settings.hmac_pepper.get_secret_value().encode(),
        hmac_key_version=settings.hmac_key_version,
        limitador=LimitadorTaxa(),
    )


def _jwks_local(settings: Settings) -> str | None:
    if settings.jwt_jwks_arquivo:
        return Path(settings.jwt_jwks_arquivo).read_text(encoding="utf-8")
    return settings.jwt_jwks


def _erros_sem_eco(exc: RequestValidationError) -> list[dict[str, object]]:
    """Erros de validação sem o campo 'input' (que ecoaria o CPF/CNPJ recebido)."""
    return [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]


def _registrar_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_e_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        recebido = request.headers.get("X-Request-ID", "")
        request.state.request_id = recebido if _REQUEST_ID.match(recebido) else str(uuid.uuid4())
        inicio = time.perf_counter()
        response = await call_next(request)
        # Modelo da rota, não o caminho: um UUID por série explodiria a cardinalidade
        rota = request.scope.get("route")
        endpoint = getattr(rota, "path", "nao_roteado")
        metricas: Metricas = request.app.state.metricas
        metricas.requisicoes.labels(request.method, endpoint, str(response.status_code)).inc()
        metricas.duracao.labels(endpoint).observe(time.perf_counter() - inicio)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.update(_HEADERS_SEGURANCA)
        return response


def _registrar_tratadores(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validacao(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"detail": _erros_sem_eco(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    @app.exception_handler(TenantNaoAutorizado)
    async def tenant(request: Request, exc: TenantNaoAutorizado) -> JSONResponse:
        log.warning("tenant sem configuração ativa", extra={"request_id": request.state.request_id})
        return JSONResponse({"detail": "Tenant não autorizado."}, status_code=status.HTTP_403_FORBIDDEN)

    @app.exception_handler(asyncpg.QueryCanceledError)
    async def timeout(request: Request, exc: asyncpg.QueryCanceledError) -> JSONResponse:
        # statement_timeout: o tempo estourou no próprio serviço (RUNBOOK §1: 503, não 504)
        log.error("consulta cancelada por timeout", extra={"request_id": request.state.request_id})
        return JSONResponse(
            {"detail": "Serviço temporariamente indisponível.", "request_id": request.state.request_id},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def inesperado(request: Request, exc: Exception) -> JSONResponse:
        # Trace só no log interno (mascarado); o cliente recebe mensagem genérica e o request_id
        request_id = getattr(request.state, "request_id", None)
        log.exception("erro não tratado", extra={"request_id": request_id})
        return JSONResponse(
            {"detail": "Erro interno.", "request_id": request_id},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def create_app(recursos: Recursos | None = None, metrics_token: str | None = None) -> FastAPI:
    """`recursos` permite injetar pool e chaves (testes). Sem ele, tudo vem do ambiente."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        criados = recursos is None
        if criados:
            app_logging.configurar()
            settings = get_settings()
            app.state.recursos = await criar_recursos(settings)
            if settings.metrics_token is not None and settings.metrics_token.get_secret_value():
                app.state.metrics_token = settings.metrics_token.get_secret_value()
        else:
            app.state.recursos = recursos
        yield
        if criados:
            await app.state.recursos.pool.close()

    app = FastAPI(
        title="AgroCredit Intelligence API",
        version="0.1.0",
        description="Relatório de impedimentos do MCR 2-9 para crédito rural. Não decide o crédito.",
        lifespan=lifespan,
    )
    if recursos is not None:
        app.state.recursos = recursos
    app.state.metricas = Metricas()
    app.state.metrics_token = metrics_token
    app.include_router(credit.router)
    app.include_router(health.router)
    app.include_router(metrics_router)

    _registrar_middleware(app)
    _registrar_tratadores(app)
    return app
