-- 0003 — Ajustes para o pipeline (Fase 3)

-- PostGIS (tipos e funções ST_*) vive no schema public, que teve o acesso de PUBLIC revogado.
GRANT USAGE ON SCHEMA public TO role_api, role_etl;

-- Município e UF reais do imóvel (cod_municipio_ibge do SICAR, não o código embutido no CAR).
ALTER TABLE core_geo.imoveis_car_template
    ADD COLUMN municipio VARCHAR(100) NOT NULL,
    ADD COLUMN uf CHAR(2) NOT NULL;
CREATE OR REPLACE VIEW core_geo.imoveis_car_current AS SELECT * FROM core_geo.imoveis_car_template;

-- Minimização (RIPD): o nome do empregador não é usado por nenhuma regra nem devolvido pela API.
ALTER TABLE core_geo.mte_template ALTER COLUMN nome_infrator DROP NOT NULL;

-- Uma tabela versionada por (base, fonte): embargos_ibama_v<versao>, embargos_icmbio_v<versao>...
-- A view <base>_current é a união da última versão publicada de cada fonte da base.
ALTER TABLE core_geo.dataset_versions
    ADD COLUMN base VARCHAR(40),
    ADD COLUMN registros INT,
    ADD COLUMN quarentena INT,
    ADD COLUMN bloqueio TEXT;  -- motivo da checagem de sanidade que impediu a publicação (INC-05)
