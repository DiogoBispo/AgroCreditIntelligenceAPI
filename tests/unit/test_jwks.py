import json
import time
from datetime import date

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

from agrocredit.core.security import ChavesJWKS, TokenInvalido, VerificadorJWT, _baixar_jwks
from agrocredit.domain.freshness import situacao_fontes


def _jwk(chave: Ed25519PrivateKey, kid: str) -> dict[str, str]:
    publica = json.loads(OKPAlgorithm.to_jwk(chave.public_key()))
    publica.update({"kid": kid, "alg": "EdDSA"})
    return publica


def _token(chave: Ed25519PrivateKey, kid: str) -> str:
    agora = int(time.time())
    claims = {"iss": "i", "aud": "a", "sub": "s", "tenant_id": "t", "nbf": agora, "exp": agora + 60}
    return jwt.encode(claims, chave, algorithm="EdDSA", headers={"kid": kid})


class ProvedorFalso:
    def __init__(self, *chaves: dict[str, str]) -> None:
        self.jwks = {"keys": list(chaves)}
        self.chamadas = 0
        self.falhar = False

    def __call__(self, url: str) -> bytes:
        self.chamadas += 1
        if self.falhar:
            raise OSError("provedor fora do ar")
        return json.dumps(self.jwks).encode()


async def test_rotacao_de_chave_sem_reiniciar() -> None:
    antiga, nova = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    provedor = ProvedorFalso(_jwk(antiga, "k1"))
    v = VerificadorJWT(
        ChavesJWKS(url="https://idp/jwks", intervalo_minimo=0, baixar=provedor), "i", "a", "tenant_id"
    )
    assert (await v.verificar(_token(antiga, "k1"))).tenant_id == "t"
    provedor.jwks = {"keys": [_jwk(antiga, "k1"), _jwk(nova, "k2")]}  # provedor publica a chave nova
    assert (await v.verificar(_token(nova, "k2"))).tenant_id == "t"
    assert provedor.chamadas == 2


async def test_kid_inventado_nao_vira_dos_no_provedor() -> None:
    chave = Ed25519PrivateKey.generate()
    provedor = ProvedorFalso(_jwk(chave, "k1"))
    v = VerificadorJWT(
        ChavesJWKS(url="https://idp/jwks", intervalo_minimo=60, baixar=provedor), "i", "a", "tenant_id"
    )
    for i in range(20):
        with pytest.raises(TokenInvalido):
            await v.verificar(_token(chave, f"falso-{i}"))
    assert provedor.chamadas == 1  # só a carga inicial


async def test_falha_do_provedor_mantem_chaves() -> None:
    chave = Ed25519PrivateKey.generate()
    provedor = ProvedorFalso(_jwk(chave, "k1"))
    chaves = ChavesJWKS(url="https://idp/jwks", ttl=0, intervalo_minimo=0, baixar=provedor)
    v = VerificadorJWT(chaves, "i", "a", "tenant_id")
    await v.verificar(_token(chave, "k1"))
    provedor.falhar = True
    assert (await v.verificar(_token(chave, "k1"))).tenant_id == "t"  # cache vencido, mas segue válido


async def test_ignora_chaves_de_algoritmo_nao_permitido() -> None:
    chave = Ed25519PrivateKey.generate()
    jwk_hs = {"kty": "oct", "k": "c2VncmVkbw", "kid": "hs", "alg": "HS256"}
    chaves = ChavesJWKS(jwks_json=json.dumps({"keys": [_jwk(chave, "k1"), jwk_hs]}))
    assert await chaves.chave("hs") is None
    assert await chaves.chave("k1") is not None


def test_configuracao_invalida() -> None:
    with pytest.raises(ValueError):
        ChavesJWKS()
    with pytest.raises(ValueError):
        ChavesJWKS(jwks_json='{"keys": []}', url="https://x")
    with pytest.raises(ValueError, match="HTTPS"):
        _baixar_jwks("http://idp/jwks")


def test_situacao_das_fontes() -> None:
    hoje = date(2026, 10, 14)
    s = {
        x.fonte: x.nivel
        for x in situacao_fontes(
            {
                "IBAMA": date(2026, 10, 13),
                "SICAR": date(2026, 9, 20),
                "CNUC": date(2025, 8, 1),
                "X": date(2020, 1, 1),
            },
            hoje,
        )
    }
    assert s == {"IBAMA": "OK", "SICAR": "P2", "CNUC": "P2"}  # fonte sem limite (X) é ignorada
