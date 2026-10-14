"""Registro das regras disponíveis. O ruleset (YAML) escolhe quais valem e com quais parâmetros."""

from agrocredit.domain.rules import r_car, r_embargos, r_mte, r_rfb, r_territoriais
from agrocredit.domain.rules.base import Regra

REGISTRO: dict[str, Regra] = {
    r_car.RULE_ID: r_car.avaliar,
    r_mte.RULE_ID: r_mte.avaliar,
    r_embargos.R_EMB_GEO: r_embargos.avaliar_federal,
    r_embargos.R_EMB_EST: r_embargos.avaliar_estadual,
    r_embargos.R_EMB_DOC: r_embargos.avaliar_documental,
    r_embargos.R_EMB_SEMGEO: r_embargos.avaliar_sem_geometria,
    r_territoriais.R_TI: r_territoriais.avaliar_ti,
    r_territoriais.R_UC: r_territoriais.avaliar_uc,
    r_territoriais.R_QUI: r_territoriais.avaliar_quilombola,
    r_territoriais.R_FPND: r_territoriais.avaliar_fpnd,
    r_territoriais.R_PRODES: r_territoriais.avaliar_prodes,
    r_rfb.RULE_ID: r_rfb.avaliar,
}
