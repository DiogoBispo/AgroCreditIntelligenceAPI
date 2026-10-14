"""API HTTP ponta a ponta + regressões de segurança (SPEC-001 §8, OWASP API Top 10).

Roda no banco isolado de testes (conftest), recriado a cada sessão: a auditoria gravada aqui não sobra.
"""

import base64
import io
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agrocredit.core import dev_keys, logging as app_logging
from agrocredit.core.security import ChavesJWKS, VerificadorJWT
from agrocredit.domain.ruleset import carregar
from agrocredit.presentation.api.app import create_app
from agrocredit.presentation.api.dependencies import LimitadorTaxa, Recursos
from tests.integration.conftest import API_DSN
from tests.integration.test_caso_de_uso import CAR_A, TENANT, cenario  # noqa: F401 (fixture)

pytestmark = pytest.mark.integration

CPF = "52998224725"
CNPJ = "11222333000181"
URL = "/v1/credit/check"


@pytest.fixture(scope="module")
def chaves(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("chaves")
    dev_keys.gerar(d)
    return d


@pytest.fixture
async def cliente(cenario: None, chaves: Path, migrated: str) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    owner = await asyncpg.connect(migrated)
    await owner.execute(
        "INSERT INTO core_geo.tenant_config (client_tenant_id, rate_limit_rpm) VALUES ('teste-limite', 2)"
        " ON CONFLICT DO NOTHING"
    )
    await owner.close()
    pool = await asyncpg.create_pool(API_DSN, min_size=1, max_size=4)
    rec = Recursos(
        pool=pool,
        verificador=VerificadorJWT(
            ChavesJWKS(jwks_json=(chaves / "jwks.json").read_text()),
            dev_keys.ISSUER,
            dev_keys.AUDIENCE,
            "tenant_id",
        ),
        rulesets=carregar(),
        hmac_pepper=b"p" * 32,
        hmac_key_version=1,
        limitador=LimitadorTaxa(),
    )
    app = create_app(rec)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        yield c
    await pool.close()


def _auth(chaves: Path, tenant: str = TENANT) -> dict[str, str]:
    return {"Authorization": f"Bearer {dev_keys.emitir(chaves, tenant)}"}


def _token(claims: dict[str, object], chave: object, alg: str, kid: str) -> str:
    return jwt.encode(claims, chave, algorithm=alg, headers={"kid": kid})  # type: ignore[arg-type]


def _claims(**extra: object) -> dict[str, object]:
    agora = int(time.time())
    base: dict[str, object] = {
        "iss": dev_keys.ISSUER,
        "aud": dev_keys.AUDIENCE,
        "sub": "x",
        "tenant_id": TENANT,
        "nbf": agora,
        "exp": agora + 300,
    }
    base.update(extra)
    return base


# Fluxo principal --------------------------------------------------------------------------


async def test_check_devolve_relatorio(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(URL, json={"document": CNPJ, "car_code": CAR_A}, headers=_auth(chaves))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["credit_status"] == "IMPEDIMENTO_IDENTIFICADO"
    assert body["owner_link_verified"] is False
    assert body["input"]["document_masked"] == "11.222.333/****-**"
    assert CNPJ not in r.text  # documento nunca volta em claro
    assert {"sicar", "ibama", "mte"} <= set(body["data_as_of"])


async def test_metricas(cliente: httpx.AsyncClient, chaves: Path) -> None:
    await cliente.post(URL, json={"document": CNPJ, "car_code": CAR_A}, headers=_auth(chaves))
    await cliente.get(f"/v1/credit/analyses/{uuid.uuid4()}", headers=_auth(chaves))
    texto = (await cliente.get("/metrics")).text
    assert (
        f'agrocredit_credit_status_total{{credit_status="IMPEDIMENTO_IDENTIFICADO",tenant="{TENANT}"}}'
        in texto
    )
    assert 'agrocredit_rule_result_total{result="IMPEDIDO",rule_id="R-EMB-GEO"}' in texto
    assert 'agrocredit_dataset_age_days{fonte="SICAR"}' in texto
    # Rótulo é o modelo da rota, não o UUID (cardinalidade controlada)
    assert 'endpoint="/v1/credit/analyses/{analysis_id}"' in texto
    assert CNPJ not in texto


async def test_metricas_com_token(chaves: Path, migrated: str) -> None:
    pool = await asyncpg.create_pool(API_DSN, min_size=1, max_size=1)
    rec = Recursos(
        pool=pool,
        verificador=VerificadorJWT(
            ChavesJWKS(jwks_json=(chaves / "jwks.json").read_text()),
            dev_keys.ISSUER,
            dev_keys.AUDIENCE,
            "tenant_id",
        ),
        rulesets=carregar(),
        hmac_pepper=b"p" * 32,
        hmac_key_version=1,
        limitador=LimitadorTaxa(),
    )
    app = create_app(rec, metrics_token="segredo-do-prometheus")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        assert (await c.get("/metrics")).status_code == 401
        ok = await c.get("/metrics", headers={"Authorization": "Bearer segredo-do-prometheus"})
        assert ok.status_code == 200
    await pool.close()


async def test_health(cliente: httpx.AsyncClient) -> None:
    assert (await cliente.get("/health/liveness")).json() == {"status": "alive"}
    assert (await cliente.get("/health/readiness")).status_code == 200


# SEC-001..005 (SPEC §8.1) ----------------------------------------------------------------


async def test_sec_001_sql_injection_no_car(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(
        URL, json={"document": CNPJ, "car_code": "GO-5218805' OR '1'='1"}, headers=_auth(chaves)
    )
    assert r.status_code == 422


async def test_sec_002_sem_token(cliente: httpx.AsyncClient) -> None:
    r = await cliente.post(URL, json={"document": CNPJ, "car_code": CAR_A})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


async def test_sec_003_documento_nao_vai_para_o_log(
    cliente: httpx.AsyncClient, chaves: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await cliente.post(URL, json={"document": CPF, "car_code": CAR_A}, headers=_auth(chaves))
    await cliente.post(URL, json={"document": "529.982.247-24", "car_code": CAR_A}, headers=_auth(chaves))
    await cliente.post(
        URL, json={"document": CPF, "car_code": CAR_A}, headers=_auth(chaves, "tenant-desconhecido")
    )
    assert CPF not in caplog.text and "529.982.247" not in caplog.text


def test_sec_003_formatador_mascara_o_que_escapar() -> None:
    saida = io.StringIO()
    handler = logging.StreamHandler(saida)
    handler.setFormatter(app_logging.FormatoJSON())
    logger = logging.getLogger("agrocredit.teste.mascara")
    logger.addHandler(handler)
    logger.propagate = False
    logger.error("falha com documento %s", "529.982.247-25", extra={"doc": "11.222.333/0001-81"})
    texto = saida.getvalue()
    assert "529.982.247" not in texto and "11.222.333" not in texto
    assert json.loads(texto)["msg"] == "falha com documento [DOC]"


def test_filtro_mascara_preserva_args_do_log_de_acesso() -> None:
    # O formatador de acesso do uvicorn desempacota record.args: o filtro não pode achatá-los
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/x?doc=52998224725", "1.1", 200),
        None,
    )
    assert app_logging.FiltroMascara().filter(rec)
    assert rec.args == ("1.2.3.4", "GET", "/x?doc=[DOC]", "1.1", 200)
    assert rec.getMessage() == '1.2.3.4 - "GET /x?doc=[DOC] HTTP/1.1" 200'


async def test_sec_004_campos_extras(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(
        URL, json={"document": CNPJ, "car_code": CAR_A, "is_admin": True}, headers=_auth(chaves)
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "extra_forbidden"


async def test_sec_005_bola_entre_tenants(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(URL, json={"document": CPF, "car_code": CAR_A}, headers=_auth(chaves))
    analysis_id = r.json()["analysis_id"]
    propria = await cliente.get(f"/v1/credit/analyses/{analysis_id}", headers=_auth(chaves))
    assert propria.status_code == 200 and propria.json()["document_masked"] == "***.982.247-**"
    alheia = await cliente.get(f"/v1/credit/analyses/{analysis_id}", headers=_auth(chaves, "teste-coop-b"))
    assert alheia.status_code == 404  # não revela que existe


# Autenticação --------------------------------------------------------------------------


async def test_tokens_invalidos(cliente: httpx.AsyncClient, chaves: Path) -> None:
    kid = (chaves / "kid").read_text()
    outra_chave = Ed25519PrivateKey.generate()
    jwks = json.loads((chaves / "jwks.json").read_text())
    publica_x = jwks["keys"][0]["x"].encode()
    sem_alg = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": kid}).encode()).rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(json.dumps(_claims()).encode()).rstrip(b"=").decode()
        + "."
    )
    privada = (chaves / "jwt_private.pem").read_bytes()
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    chave_ok = load_pem_private_key(privada, password=None)
    casos = {
        "assinado com outra chave": _token(_claims(), outra_chave, "EdDSA", kid),
        "alg none": sem_alg,
        "HS256 com a chave pública como segredo": jwt.encode(
            _claims(), publica_x, algorithm="HS256", headers={"kid": kid}
        ),
        "expirado": _token(_claims(exp=int(time.time()) - 10), chave_ok, "EdDSA", kid),
        "audiência errada": _token(_claims(aud="outra-api"), chave_ok, "EdDSA", kid),
        "sem tenant": _token(
            {k: v for k, v in _claims().items() if k != "tenant_id"}, chave_ok, "EdDSA", kid
        ),
        "kid desconhecido": _token(_claims(), chave_ok, "EdDSA", "kid-falso"),
    }
    for nome, token in casos.items():
        r = await cliente.post(
            URL, json={"document": CNPJ, "car_code": CAR_A}, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401, nome
        assert r.json() == {"detail": "Token inválido."}, nome  # sem detalhe do motivo


async def test_tenant_sem_configuracao(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(URL, json={"document": CNPJ, "car_code": CAR_A}, headers=_auth(chaves, "tenant-x"))
    assert r.status_code == 403


# Vazamento, cabeçalhos e consumo -------------------------------------------------------------


async def test_422_nao_ecoa_o_documento(cliente: httpx.AsyncClient, chaves: Path) -> None:
    r = await cliente.post(URL, json={"document": "529.982.247-24", "car_code": CAR_A}, headers=_auth(chaves))
    assert r.status_code == 422
    assert "529.982.247" not in r.text
    assert "input" not in r.json()["detail"][0]


async def test_cabecalhos_de_seguranca_e_request_id(cliente: httpx.AsyncClient) -> None:
    r = await cliente.get("/health/liveness", headers={"X-Request-ID": "abc12345-req"})
    assert r.headers["X-Request-ID"] == "abc12345-req"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    assert "max-age" in r.headers["Strict-Transport-Security"]
    malicioso = await cliente.get("/health/liveness", headers={"X-Request-ID": "x\r\nSet-Cookie: a=b"})
    assert malicioso.headers["X-Request-ID"] != "x\r\nSet-Cookie: a=b"


async def test_rate_limit_por_tenant(cliente: httpx.AsyncClient, chaves: Path) -> None:
    h = _auth(chaves, "teste-limite")  # rate_limit_rpm = 2
    corpo = {"document": CNPJ, "car_code": "GO-5218805-' OR 1=1"}  # 422 barato, mas conta no limite
    codigos = [(await cliente.post(URL, json=corpo, headers=h)).status_code for _ in range(3)]
    assert codigos[:2] == [422, 422]
    assert codigos[2] == 429
