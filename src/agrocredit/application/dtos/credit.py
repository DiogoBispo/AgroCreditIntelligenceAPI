"""Contratos da API (PRD §6). Entrada estrita; saída com propriedades explícitas (OWASP API3)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agrocredit.domain.enums import CreditStatus, RuleResult
from agrocredit.domain.validators import Documento, parse_car, parse_document


class CreditCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document: str = Field(max_length=18, description="CPF (11) ou CNPJ (14, inclusive alfanumérico)")
    car_code: str = Field(max_length=60, description="Código do CAR, com ou sem pontos")

    @field_validator("document")
    @classmethod
    def _doc(cls, v: str) -> str:
        return parse_document(v).valor

    @field_validator("car_code")
    @classmethod
    def _car(cls, v: str) -> str:
        return parse_car(v)

    def documento(self) -> Documento:
        return parse_document(self.document)


class InputOut(BaseModel):
    document_masked: str
    car_code: str


class PropertyOut(BaseModel):
    car_status: str
    municipio: str
    uf: str
    declared_area_ha: Decimal | None
    geodesic_area_ha: Decimal | None
    geometry_problem: str | None = None


class CadastralOut(BaseModel):
    rfb_status: str
    cnae_principal: str | None


class CheckOut(BaseModel):
    rule_id: str
    result: RuleResult
    basis: str | None = None  # dispositivo normativo que fundamenta a regra (ex.: "MCR 2-9 item 7")
    reason: str | None = None
    detail: dict[str, Any] | None = None


class DecisionOut(BaseModel):
    credit_status: CreditStatus
    summary: str


class CreditCheckResponse(BaseModel):
    analysis_id: UUID
    timestamp: datetime
    ruleset_version: str
    input: InputOut
    property: PropertyOut | None
    cadastral_status: CadastralOut
    checks: list[CheckOut]
    owner_link_verified: bool = False
    data_as_of: dict[str, date]
    data_staleness_warning: bool
    decision: DecisionOut


class AnalysisOut(BaseModel):
    """Consulta de análise passada (dados da auditoria; sem o documento em claro)."""

    analysis_id: UUID
    executed_at: datetime
    document_masked: str
    car_code: str
    ruleset_version: str
    dataset_versions: dict[str, str]
    tolerance_applied: dict[str, str]
    checks: list[dict[str, Any]]
    credit_status: str
