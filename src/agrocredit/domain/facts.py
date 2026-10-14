"""Fatos já consultados nas bases, entregues ao motor de regras.

As regras não acessam banco: recebem estes objetos prontos (montados pelos repositórios).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from agrocredit.domain.enums import Camada, CarStatus, TipoDocumento
from agrocredit.domain.validators import Documento


@dataclass(frozen=True, slots=True)
class Imovel:
    codigo_car: str
    codigo_ibge: int
    uf: str
    municipio: str
    status: CarStatus
    area_declarada_ha: Decimal | None
    area_geodesica_ha: Decimal | None
    problema_geometria: str | None = None  # geometria irrecuperável na fonte: sem interseções


@dataclass(frozen=True, slots=True)
class EmbargoHit:
    orgao: str
    numero_processo: str | None
    data_infracao: date | None
    overlap_ha: Decimal
    data_embargo: date | None = None  # o CSV do IBAMA traz a data do embargo, não a da infração


@dataclass(frozen=True, slots=True)
class Sobreposicao:
    """Área da UNIÃO das interseções (embargos sobrepostos não são somados duas vezes)."""

    area_ha: Decimal
    pct: Decimal  # percentual sobre a área geodésica do imóvel
    hits: tuple[EmbargoHit, ...] = ()

    @classmethod
    def vazia(cls) -> "Sobreposicao":
        return cls(Decimal(0), Decimal(0))


@dataclass(frozen=True, slots=True)
class EmbargoDocumental:
    orgao: str
    numero_processo: str | None
    data_infracao: date | None
    data_embargo: date | None = None


@dataclass(frozen=True, slots=True)
class RegistroMte:
    tipo_documento: TipoDocumento
    data_inclusao: date
    por_raiz_cnpj: bool


@dataclass(frozen=True, slots=True)
class SobreposicaoTerritorial:
    camada: Camada
    id: str
    nome: str | None
    atributos: Mapping[str, Any]
    overlap_ha: Decimal
    pct: Decimal


@dataclass(frozen=True, slots=True)
class Empresa:
    situacao_cadastral: str
    cnae_principal: str


@dataclass(frozen=True, slots=True)
class Tolerancia:
    ha: Decimal
    pct: Decimal


@dataclass(frozen=True, slots=True)
class Fatos:
    documento: Documento
    codigo_car: str
    em_cobertura: bool
    fontes_frescas: Mapping[str, bool]
    imovel: Imovel | None = None
    embargos_federais: Sobreposicao = field(default_factory=Sobreposicao.vazia)  # IBAMA e ICMBio
    embargos_estaduais: Sobreposicao = field(default_factory=Sobreposicao.vazia)
    embargos_documentais: tuple[EmbargoDocumental, ...] = ()
    embargos_sem_poligono_no_imovel: int = 0  # ponto de referência dentro do imóvel
    mte: tuple[RegistroMte, ...] = ()
    territoriais: tuple[SobreposicaoTerritorial, ...] = ()
    empresa: Empresa | None = None

    def fresca(self, fonte: str) -> bool:
        """Fonte ausente conta como não verificável."""
        return self.fontes_frescas.get(fonte, False)
