"""Carrega o ruleset versionado (rules/*.yaml) e escolhe a versão vigente numa data."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agrocredit.domain.rules import REGISTRO

# No repositório, rules/ fica na raiz; na imagem Docker o caminho vem de AGROCREDIT_RULESET
RULESET_PADRAO = Path(
    os.environ.get("AGROCREDIT_RULESET", Path(__file__).resolve().parents[3] / "rules" / "mcr-2-9.yaml")
)


@dataclass(frozen=True, slots=True)
class ConfigRegra:
    rule_id: str
    obrigatoria: bool
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class VersaoRuleset:
    id: str
    vigencia_inicio: date
    vigencia_fim: date | None
    revisao_juridica: str
    regras: tuple[ConfigRegra, ...]

    @property
    def obrigatorias(self) -> frozenset[str]:
        return frozenset(r.rule_id for r in self.regras if r.obrigatoria)

    def vigente_em(self, dia: date) -> bool:
        return self.vigencia_inicio <= dia and (self.vigencia_fim is None or dia < self.vigencia_fim)


def _parse_versao(raw: Mapping[str, Any]) -> VersaoRuleset:
    regras = []
    for rule_id, cfg in raw["regras"].items():
        if rule_id not in REGISTRO:
            raise ValueError(f"Regra desconhecida no ruleset {raw['id']}: {rule_id}")
        cfg = dict(cfg or {})
        obrigatoria = cfg.pop("obrigatoria")
        if not isinstance(obrigatoria, bool):
            raise ValueError(f"{rule_id}: 'obrigatoria' deve ser booleano")
        regras.append(ConfigRegra(rule_id, obrigatoria, cfg))
    return VersaoRuleset(
        id=str(raw["id"]),
        vigencia_inicio=raw["vigencia_inicio"],
        vigencia_fim=raw.get("vigencia_fim"),
        revisao_juridica=str(raw["revisao_juridica"]),
        regras=tuple(regras),
    )


def carregar(path: Path = RULESET_PADRAO) -> tuple[VersaoRuleset, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    versoes = tuple(_parse_versao(v) for v in data["versoes"])
    ids = [v.id for v in versoes]
    if len(ids) != len(set(ids)):
        raise ValueError("IDs de versão duplicados no ruleset")
    return versoes


def versao_vigente(versoes: tuple[VersaoRuleset, ...], dia: date) -> VersaoRuleset:
    vigentes = [v for v in versoes if v.vigente_em(dia)]
    if len(vigentes) != 1:
        raise LookupError(f"Esperada exatamente 1 versão vigente em {dia}, encontradas {len(vigentes)}")
    return vigentes[0]
