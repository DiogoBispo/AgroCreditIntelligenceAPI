"""Configuração do tenant e trilha de auditoria (somente inserção para o papel da API)."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from agrocredit.domain.facts import Tolerancia
from agrocredit.infrastructure.db.tipos import Conexao


@dataclass(frozen=True, slots=True)
class ConfigTenant:
    tolerancia: Tolerancia
    rate_limit_rpm: int


async def config_tenant(conn: Conexao, tenant_id: str) -> ConfigTenant | None:
    row = await conn.fetchrow(
        "SELECT tolerancia_ha, tolerancia_pct, rate_limit_rpm FROM core_geo.tenant_config"
        " WHERE client_tenant_id = $1 AND ativo",
        tenant_id,
    )
    if row is None:
        return None
    return ConfigTenant(
        Tolerancia(Decimal(row["tolerancia_ha"]), Decimal(row["tolerancia_pct"])), row["rate_limit_rpm"]
    )


@dataclass(frozen=True, slots=True)
class RegistroAuditoria:
    analysis_id: UUID
    executed_at: datetime
    request_id: str
    client_tenant_id: str
    document_hash: str
    hmac_key_version: int
    document_masked: str
    car_code: str
    ruleset_version: str
    dataset_versions: dict[str, str]
    tolerance_applied: dict[str, str]
    rule_results: list[dict[str, Any]]
    credit_status: str


async def registrar(conn: Conexao, r: RegistroAuditoria) -> None:
    # Sem RETURNING: o papel da API não tem SELECT na auditoria (o id e o horário vêm da aplicação)
    await conn.execute(
        "INSERT INTO audit.credit_analysis_log (analysis_id, executed_at, request_id, client_tenant_id,"
        " document_hash, hmac_key_version, document_masked, car_code, ruleset_version, dataset_versions,"
        " tolerance_applied, rule_results, credit_status)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12::jsonb, $13)",
        r.analysis_id,
        r.executed_at,
        r.request_id,
        r.client_tenant_id,
        r.document_hash,
        r.hmac_key_version,
        r.document_masked,
        r.car_code,
        r.ruleset_version,
        json.dumps(r.dataset_versions),
        json.dumps(r.tolerance_applied),
        json.dumps(r.rule_results, default=str),
        r.credit_status,
    )


_JSONB = ("dataset_versions", "tolerance_applied", "rule_results")


async def consultar(conn: Conexao, analysis_id: UUID, tenant_id: str) -> dict[str, Any] | None:
    """Só devolve a análise se for do próprio tenant (BOLA). Função SECURITY DEFINER no banco."""
    row = await conn.fetchrow("SELECT * FROM audit.consultar_analise($1, $2)", analysis_id, tenant_id)
    if row is None:
        return None
    dados = dict(row)
    for campo in _JSONB:
        dados[campo] = json.loads(dados[campo])
    return dados
