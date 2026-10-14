"""Dependências das rotas: autenticação (JWT) e limite de consumo por tenant (OWASP API2, API4)."""

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import asyncpg
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agrocredit.core.security import Identidade, TokenInvalido, VerificadorJWT
from agrocredit.domain.ruleset import VersaoRuleset

_bearer = HTTPBearer(auto_error=False)


@dataclass
class LimitadorTaxa:
    """Janela deslizante de 60 s por tenant, em memória (uma instância da API no MVP; RUNBOOK INC-04).

    O limite de cada tenant vem de tenant_config e fica em cache por `ttl` segundos.
    """

    limite_padrao: int = 60
    ttl: float = 60.0
    _janelas: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _limites: dict[str, tuple[int, float]] = field(default_factory=dict)

    async def limite(self, pool: asyncpg.Pool, tenant: str) -> int:
        agora = time.monotonic()
        cache = self._limites.get(tenant)
        if cache and agora - cache[1] < self.ttl:
            return cache[0]
        rpm = await pool.fetchval(
            "SELECT rate_limit_rpm FROM core_geo.tenant_config WHERE client_tenant_id = $1", tenant
        )
        valor = int(rpm) if rpm else self.limite_padrao
        self._limites[tenant] = (valor, agora)
        return valor

    def registrar(self, tenant: str, limite: int) -> int | None:
        """Registra a chamada; devolve os segundos de espera se o limite estourou."""
        agora = time.monotonic()
        janela = self._janelas[tenant]
        while janela and agora - janela[0] >= 60:
            janela.popleft()
        if len(janela) >= limite:
            return int(60 - (agora - janela[0])) + 1
        janela.append(agora)
        return None


@dataclass(frozen=True)
class Recursos:
    pool: asyncpg.Pool
    verificador: VerificadorJWT
    rulesets: tuple[VersaoRuleset, ...]
    hmac_pepper: bytes
    hmac_key_version: int
    limitador: LimitadorTaxa


def recursos(request: Request) -> Recursos:
    r: Recursos = request.app.state.recursos
    return r


async def identidade(
    credenciais: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    rec: Recursos = Depends(recursos),  # noqa: B008
) -> Identidade:
    if credenciais is None or credenciais.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente.", {"WWW-Authenticate": "Bearer"})
    try:
        return await rec.verificador.verificar(credenciais.credentials)
    except TokenInvalido:
        # Motivo detalhado só no log interno; o cliente recebe mensagem genérica
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Token inválido.", {"WWW-Authenticate": "Bearer"}
        ) from None


async def limitar_taxa(
    ident: Identidade = Depends(identidade),  # noqa: B008
    rec: Recursos = Depends(recursos),  # noqa: B008
) -> Identidade:
    limite = await rec.limitador.limite(rec.pool, ident.tenant_id)
    espera = rec.limitador.registrar(ident.tenant_id, limite)
    if espera is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Limite de requisições excedido.", {"Retry-After": str(espera)}
        )
    return ident
