-- 0002 — Ajustes da Fase 0 (documents/FASE0_FONTES.md)

-- Municípios do escopo. Códigos TOM conferidos na tabela Municipios.zip da RFB (2026-10);
-- os do PRD v1.2 (9373, 4967, 7493) estavam errados.
INSERT INTO core_geo.municipios (codigo_ibge, codigo_tom, nome_municipio, uf, em_cobertura) VALUES
    (5218805, 9571, 'RIO VERDE', 'GO', TRUE),
    (3148103, 4961, 'PATROCINIO', 'MG', TRUE),
    (4104907, 7495, 'CASTRO', 'PR', TRUE);

-- R-EMB-SEMGEO passa a usar o ponto de referência do embargo dentro do imóvel, não o município:
-- a maioria dos embargos sem polígono embarga atividades (ex.: criação de passeriformes).
DROP INDEX core_geo.embargos_template_codigo_ibge_idx;
CREATE INDEX ON core_geo.embargos_template USING GIST (ponto) WHERE geometria IS NULL;
