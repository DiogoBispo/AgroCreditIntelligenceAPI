"""Pseudonimização do documento e validação de JWT."""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWK

from agrocredit.domain.validators import Documento

log = logging.getLogger("agrocredit.security")
ALGORITMOS_PERMITIDOS = ("EdDSA", "RS256")  # nunca HS* (ADR-005)


def hash_documento(doc: Documento, pepper: bytes) -> str:
    """HMAC-SHA256 do tipo + documento. Pseudonimização: continua sendo dado pessoal (ADR-005)."""
    return hmac.new(pepper, f"{doc.tipo}:{doc.valor}".encode(), hashlib.sha256).hexdigest()


def hashes_documento(doc: Documento, chaves: dict[int, bytes]) -> dict[int, str]:
    """Hash do documento em cada versão de chave (busca do titular atravessa rotações)."""
    return {versao: hash_documento(doc, pepper) for versao, pepper in chaves.items()}


class TokenInvalido(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Identidade:
    tenant_id: str
    subject: str


def _parse_jwks(texto: str | bytes) -> dict[str, PyJWK]:
    chaves: dict[str, PyJWK] = {}
    for k in json.loads(texto).get("keys", []):
        if k.get("kid") is None:
            raise ValueError("Toda chave do JWKS precisa de 'kid'")
        if k.get("alg") not in (None, *ALGORITMOS_PERMITIDOS):
            continue  # chave de outro algoritmo publicada pelo provedor: ignorada
        chaves[k["kid"]] = PyJWK.from_dict(k)
    return chaves


def _baixar_jwks(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("JWKS remoto exige HTTPS")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310  # nosec B310
        corpo: bytes = resp.read(_JWKS_TAMANHO_MAXIMO + 1)
    if len(corpo) > _JWKS_TAMANHO_MAXIMO:
        raise ValueError("JWKS remoto grande demais")
    return corpo


_JWKS_TAMANHO_MAXIMO = 256 * 1024


class ChavesJWKS:
    """Chaves públicas: JSON fixo ou URL do provedor, com cache.

    Com URL, recarrega quando o cache vence (`ttl`) ou quando chega um `kid` desconhecido, mas no máximo
    uma vez por `intervalo_minimo` segundos: tokens com `kid` inventado não viram DoS no provedor.
    Se a recarga falha, mantém as chaves que já tinha.
    """

    def __init__(
        self,
        jwks_json: str | None = None,
        url: str | None = None,
        ttl: float = 600.0,
        intervalo_minimo: float = 30.0,
        baixar: Callable[[str], bytes] = _baixar_jwks,
    ) -> None:
        if (jwks_json is None) == (url is None):
            raise ValueError("Informe exatamente um entre jwks_json e url")
        self._url = url
        self._ttl = ttl
        self._intervalo = intervalo_minimo
        self._baixar = baixar
        self._chaves: dict[str, PyJWK] = _parse_jwks(jwks_json) if jwks_json else {}
        self._carregado_em = time.monotonic() if jwks_json else float("-inf")
        self._tentativa_em = float("-inf")
        self._lock = asyncio.Lock()
        if jwks_json is not None and not self._chaves:
            raise ValueError("JWKS sem chaves")

    async def chave(self, kid: str) -> PyJWK | None:
        if self._url is not None:
            agora = time.monotonic()
            vencido = agora - self._carregado_em > self._ttl
            if (vencido or kid not in self._chaves) and agora - self._tentativa_em >= self._intervalo:
                await self._recarregar()
        return self._chaves.get(kid)

    async def _recarregar(self) -> None:
        async with self._lock:
            if time.monotonic() - self._tentativa_em < self._intervalo:
                return  # outra corrotina acabou de recarregar
            self._tentativa_em = time.monotonic()
            url = self._url
            if url is None:
                return
            try:
                novas = _parse_jwks(await asyncio.to_thread(self._baixar, url))
            except (OSError, ValueError) as exc:
                log.warning("falha ao recarregar JWKS; mantendo chaves anteriores: %s", exc)
                return
            if novas:
                self._chaves = novas
                self._carregado_em = time.monotonic()


class VerificadorJWT:
    def __init__(self, chaves: ChavesJWKS, issuer: str, audience: str, tenant_claim: str) -> None:
        self._chaves = chaves
        self._issuer = issuer
        self._audience = audience
        self._tenant_claim = tenant_claim

    async def verificar(self, token: str) -> Identidade:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALGORITMOS_PERMITIDOS:
                raise TokenInvalido("Algoritmo não permitido")
            chave = await self._chaves.chave(str(header.get("kid", "")))
            if chave is None:
                raise TokenInvalido("Chave desconhecida")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=chave,
                algorithms=list(ALGORITMOS_PERMITIDOS),
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "nbf", "iss", "aud", "sub", self._tenant_claim]},
            )
        except jwt.PyJWTError as exc:
            raise TokenInvalido(str(exc)) from exc
        tenant = claims[self._tenant_claim]
        if not isinstance(tenant, str) or not tenant:
            raise TokenInvalido("Tenant inválido")
        return Identidade(tenant_id=tenant, subject=str(claims["sub"]))
