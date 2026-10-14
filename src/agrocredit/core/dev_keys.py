"""SOMENTE DESENVOLVIMENTO: par de chaves Ed25519 e emissão de tokens locais.

    python -m agrocredit.core.dev_keys gerar --dir .secrets
    python -m agrocredit.core.dev_keys token --dir .secrets --tenant coop-demo

Em produção os tokens vêm do provedor de identidade; a API só conhece as chaves públicas (JWKS).
"""

import argparse
import json
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

ISSUER = "https://auth.agrocredit.local"
AUDIENCE = "agrocredit-api"


def gerar(diretorio: Path) -> str:
    diretorio.mkdir(parents=True, exist_ok=True)
    chave = Ed25519PrivateKey.generate()
    kid = uuid.uuid4().hex[:16]
    privada = diretorio / "jwt_private.pem"
    privada.write_bytes(
        chave.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    privada.chmod(0o600)
    publica = json.loads(OKPAlgorithm.to_jwk(chave.public_key()))
    publica.update({"kid": kid, "alg": "EdDSA", "use": "sig"})
    (diretorio / "jwks.json").write_text(json.dumps({"keys": [publica]}))
    (diretorio / "kid").write_text(kid)
    return kid


def emitir(diretorio: Path, tenant: str, ttl: int = 3600, sub: str = "dev") -> str:
    chave = serialization.load_pem_private_key((diretorio / "jwt_private.pem").read_bytes(), password=None)
    agora = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "tenant_id": tenant,
        "iat": agora,
        "nbf": agora,
        "exp": agora + ttl,
    }
    return jwt.encode(claims, chave, algorithm="EdDSA", headers={"kid": (diretorio / "kid").read_text()})  # type: ignore[arg-type]


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agrocredit.core.dev_keys")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gerar")
    g.add_argument("--dir", type=Path, default=Path(".secrets"))
    t = sub.add_parser("token")
    t.add_argument("--dir", type=Path, default=Path(".secrets"))
    t.add_argument("--tenant", required=True)
    t.add_argument("--ttl", type=int, default=3600)
    args = p.parse_args()
    if args.cmd == "gerar":
        print(f"kid={gerar(args.dir)} em {args.dir}")
    else:
        print(emitir(args.dir, args.tenant, args.ttl))


if __name__ == "__main__":
    main()
