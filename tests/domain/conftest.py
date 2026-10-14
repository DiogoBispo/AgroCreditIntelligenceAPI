from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from agrocredit.domain.enums import CarStatus, Fonte, embargo_estadual
from agrocredit.domain.facts import Fatos, Imovel, Tolerancia
from agrocredit.domain.validators import parse_document

CAR = "GO-5218805-72819485728194857281948572819485"
TODAS_AS_FONTES = [*Fonte, embargo_estadual("GO")]


@pytest.fixture
def tol() -> Tolerancia:
    return Tolerancia(ha=Decimal("0.1"), pct=Decimal("0.01"))


@pytest.fixture
def imovel() -> Imovel:
    return Imovel(
        codigo_car=CAR,
        codigo_ibge=5218805,
        uf="GO",
        municipio="RIO VERDE",
        status=CarStatus.ATIVO,
        area_declarada_ha=Decimal("450.25"),
        area_geodesica_ha=Decimal("449.87"),
    )


@pytest.fixture
def fatos(imovel: Imovel) -> Callable[..., Fatos]:
    """Fatos "limpos" (tudo OK, todas as fontes frescas); sobrescreva o que o teste precisar."""

    def make(**overrides: Any) -> Fatos:
        base: dict[str, Any] = {
            "documento": parse_document("11222333000181"),
            "codigo_car": CAR,
            "em_cobertura": True,
            "fontes_frescas": dict.fromkeys(TODAS_AS_FONTES, True),
            "imovel": imovel,
        }
        base.update(overrides)
        return Fatos(**base)

    return make
