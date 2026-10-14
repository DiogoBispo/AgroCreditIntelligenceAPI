"""Métricas Prometheus (RUNBOOK §2.2). Um registro por app (vários apps no mesmo processo nos testes)."""

import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from agrocredit.application.dtos.credit import CreditCheckResponse
from agrocredit.domain.freshness import situacao_fontes

_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.8, 1.0, 2.0, 3.0, 5.0)
_NIVEL = {"OK": 0, "P2": 1, "P1": 2}


@dataclass
class Metricas:
    registro: CollectorRegistry = field(default_factory=CollectorRegistry)

    def __post_init__(self) -> None:
        r = self.registro
        self.requisicoes = Counter(
            "agrocredit_http_requests", "Requisições HTTP", ["method", "endpoint", "status"], registry=r
        )
        self.duracao = Histogram(
            "agrocredit_http_request_duration_seconds",
            "Latência HTTP",
            ["endpoint"],
            buckets=_BUCKETS,
            registry=r,
        )
        self.pareceres = Counter(
            "agrocredit_credit_status", "Pareceres emitidos", ["tenant", "credit_status"], registry=r
        )
        self.regras = Counter(
            "agrocredit_rule_result", "Resultado por regra", ["rule_id", "result"], registry=r
        )
        self.idade_fonte = Gauge(
            "agrocredit_dataset_age_days", "Idade da base publicada", ["fonte"], registry=r
        )
        self.nivel_fonte = Gauge(
            "agrocredit_dataset_staleness_level",
            "0 = OK, 1 = alerta (P2), 2 = crítico (P1)",
            ["fonte"],
            registry=r,
        )
        self.pool_tamanho = Gauge("asyncpg_pool_size", "Conexões abertas no pool", registry=r)
        self.pool_livres = Gauge("asyncpg_pool_idle", "Conexões livres no pool", registry=r)

    def registrar_parecer(self, tenant: str, resposta: CreditCheckResponse) -> None:
        self.pareceres.labels(tenant, resposta.decision.credit_status.value).inc()
        for c in resposta.checks:
            self.regras.labels(c.rule_id, c.result.value).inc()


router = APIRouter(tags=["ops"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    token = request.app.state.metrics_token
    if token is not None:
        recebido = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(recebido.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Não autorizado.")
    m: Metricas = request.app.state.metricas
    rec = request.app.state.recursos
    rows = await rec.pool.fetch(
        "SELECT DISTINCT ON (fonte) fonte, source_as_of FROM core_geo.dataset_versions"
        " WHERE publicado_em IS NOT NULL ORDER BY fonte, publicado_em DESC"
    )
    for s in situacao_fontes({r["fonte"]: r["source_as_of"] for r in rows}, datetime.now(UTC).date()):
        m.idade_fonte.labels(s.fonte).set(s.idade_dias)
        m.nivel_fonte.labels(s.fonte).set(_NIVEL[s.nivel])
    m.pool_tamanho.set(rec.pool.get_size())
    m.pool_livres.set(rec.pool.get_idle_size())
    return Response(generate_latest(m.registro), media_type=CONTENT_TYPE_LATEST)
