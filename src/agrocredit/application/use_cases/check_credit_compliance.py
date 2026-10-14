"""Caso de uso principal: verificação de impedimentos do MCR 2-9 para um par (documento, CAR)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

from agrocredit.application.dtos.credit import (
    CadastralOut,
    CheckOut,
    CreditCheckResponse,
    DecisionOut,
    InputOut,
    PropertyOut,
)
from agrocredit.core.security import Identidade, hash_documento
from agrocredit.domain import engine
from agrocredit.domain.enums import TipoDocumento
from agrocredit.domain.facts import Fatos
from agrocredit.domain.freshness import fontes_frescas
from agrocredit.domain.rules.base import tolerancia_detail
from agrocredit.domain.ruleset import VersaoRuleset, versao_vigente
from agrocredit.domain.validators import Documento
from agrocredit.infrastructure.db.repositories import auditoria
from agrocredit.infrastructure.db.repositories.fatos import coletar_fatos, versoes_publicadas
from agrocredit.infrastructure.db.tipos import Conexao


class TenantNaoAutorizado(Exception):
    """Tenant do token sem configuração ativa."""


@dataclass(frozen=True, slots=True)
class Contexto:
    request_id: str
    identidade: Identidade
    hmac_pepper: bytes
    hmac_key_version: int
    rulesets: tuple[VersaoRuleset, ...]


def _cadastral(fatos: Fatos) -> CadastralOut:
    if fatos.documento.tipo is TipoDocumento.CPF:
        return CadastralOut(rfb_status="NAO_DISPONIVEL", cnae_principal=None)
    if fatos.empresa is None:
        return CadastralOut(rfb_status="NAO_ENCONTRADO", cnae_principal=None)
    return CadastralOut(
        rfb_status=fatos.empresa.situacao_cadastral, cnae_principal=fatos.empresa.cnae_principal
    )


def _property(fatos: Fatos) -> PropertyOut | None:
    i = fatos.imovel
    if i is None:
        return None
    return PropertyOut(
        car_status=i.status,
        municipio=i.municipio,
        uf=i.uf,
        declared_area_ha=i.area_declarada_ha,
        geodesic_area_ha=i.area_geodesica_ha,
        geometry_problem=i.problema_geometria,
    )


async def verificar(
    conn: Conexao, documento: Documento, codigo_car: str, ctx: Contexto, hoje: date | None = None
) -> CreditCheckResponse:
    """Uma transação REPEATABLE READ: snapshot consistente das bases e auditoria gravada junto.

    Se a auditoria falhar, a transação inteira falha e nenhum parecer é devolvido.
    """
    agora = datetime.now(UTC)
    hoje = hoje or agora.date()
    versao = versao_vigente(ctx.rulesets, hoje)
    async with conn.transaction(isolation="repeatable_read"):
        tenant = await auditoria.config_tenant(conn, ctx.identidade.tenant_id)
        if tenant is None:
            raise TenantNaoAutorizado(ctx.identidade.tenant_id)
        dados = await versoes_publicadas(conn)
        frescas = fontes_frescas(dados.data_as_of, hoje)
        fatos = await coletar_fatos(conn, documento, codigo_car, frescas)
        parecer = engine.avaliar(fatos, versao, tenant.tolerancia)

        analysis_id = uuid4()
        checks = [
            CheckOut(
                rule_id=r.rule_id,
                result=r.result,
                reason=r.reason,
                detail=dict(r.detail) if r.detail else None,
            )
            for r in parecer.resultados
        ]
        await auditoria.registrar(
            conn,
            auditoria.RegistroAuditoria(
                analysis_id=analysis_id,
                executed_at=agora,
                request_id=ctx.request_id,
                client_tenant_id=ctx.identidade.tenant_id,
                document_hash=hash_documento(documento, ctx.hmac_pepper),
                hmac_key_version=ctx.hmac_key_version,
                document_masked=documento.mascarado(),
                car_code=codigo_car,
                ruleset_version=parecer.ruleset_version,
                dataset_versions=dict(dados.versoes),
                tolerance_applied=tolerancia_detail(tenant.tolerancia),
                rule_results=[c.model_dump(mode="json") for c in checks],
                credit_status=parecer.credit_status.value,
            ),
        )

    return CreditCheckResponse(
        analysis_id=analysis_id,
        timestamp=agora,
        ruleset_version=parecer.ruleset_version,
        input=InputOut(document_masked=documento.mascarado(), car_code=codigo_car),
        property=_property(fatos),
        cadastral_status=_cadastral(fatos),
        checks=checks,
        owner_link_verified=False,
        data_as_of={fonte.lower(): d for fonte, d in dados.data_as_of.items()},
        data_staleness_warning=not all(frescas.values()),
        decision=DecisionOut(credit_status=parecer.credit_status, summary=parecer.summary),
    )
