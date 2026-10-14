from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agrocredit.application.dtos.credit import AnalysisOut, CreditCheckRequest, CreditCheckResponse
from agrocredit.application.use_cases.check_credit_compliance import Contexto, verificar
from agrocredit.core.security import Identidade
from agrocredit.infrastructure.db.repositories import auditoria
from agrocredit.presentation.api.dependencies import Recursos, limitar_taxa, recursos

router = APIRouter(prefix="/v1/credit", tags=["credit"])


@router.post("/check", response_model=CreditCheckResponse)
async def check(
    body: CreditCheckRequest,
    request: Request,
    ident: Identidade = Depends(limitar_taxa),  # noqa: B008
    rec: Recursos = Depends(recursos),  # noqa: B008
) -> CreditCheckResponse:
    """Relatório de impedimentos do MCR 2-9 para o par (documento, CAR). Não decide o crédito."""
    ctx = Contexto(
        request_id=request.state.request_id,
        identidade=ident,
        hmac_pepper=rec.hmac_pepper,
        hmac_key_version=rec.hmac_key_version,
        rulesets=rec.rulesets,
    )
    async with rec.pool.acquire() as conn:
        resposta = await verificar(conn, body.documento(), body.car_code, ctx)
    request.app.state.metricas.registrar_parecer(ident.tenant_id, resposta)
    return resposta


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
async def analysis(
    analysis_id: UUID,
    ident: Identidade = Depends(limitar_taxa),  # noqa: B008
    rec: Recursos = Depends(recursos),  # noqa: B008
) -> AnalysisOut:
    """Análise passada do próprio tenant. De outro tenant: 404, nunca 403 (não revela existência)."""
    async with rec.pool.acquire() as conn:
        row = await auditoria.consultar(conn, analysis_id, ident.tenant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Análise não encontrada.")
    return AnalysisOut(
        analysis_id=row["analysis_id"],
        executed_at=row["executed_at"],
        document_masked=row["document_masked"],
        car_code=row["car_code"],
        ruleset_version=row["ruleset_version"],
        dataset_versions=row["dataset_versions"],
        tolerance_applied=row["tolerance_applied"],
        checks=row["rule_results"],
        credit_status=row["credit_status"],
    )
