-- 0001 — Schema base (SPEC-001 §6, revisão 1.2.0)
-- Papéis role_api, role_etl e role_maint são criados fora das migrations (initdb local / infraestrutura).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS raw_ingest;
CREATE SCHEMA IF NOT EXISTS core_geo;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TYPE core_geo.tipo_documento AS ENUM ('CPF', 'CNPJ');

-- CPF: 11 dígitos. CNPJ: 12 caracteres alfanuméricos + 2 dígitos verificadores (RFB, a partir de jul/2026).
CREATE FUNCTION core_geo.documento_formato_ok(tipo core_geo.tipo_documento, doc TEXT)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE tipo
        WHEN 'CPF'  THEN doc ~ '^[0-9]{11}$'
        WHEN 'CNPJ' THEN doc ~ '^[0-9A-Z]{12}[0-9]{2}$'
    END
$$;

-- Controle de versões e proveniência ------------------------------------------

CREATE TABLE core_geo.dataset_versions (
    fonte            VARCHAR(40) NOT NULL,
    versao           VARCHAR(20) NOT NULL,
    source_as_of     DATE        NOT NULL,
    carregado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
    publicado_em     TIMESTAMPTZ,
    aprovado_por     VARCHAR(100),
    motivo_aprovacao TEXT,          -- obrigatório quando a publicação ignora uma checagem de sanidade (INC-05)
    PRIMARY KEY (fonte, versao)
);

CREATE TABLE raw_ingest.sources_manifest (
    fonte          VARCHAR(40) NOT NULL,
    versao         VARCHAR(20) NOT NULL,
    url            TEXT        NOT NULL,
    sha256         CHAR(64)    NOT NULL,
    tamanho_bytes  BIGINT      NOT NULL,
    source_as_of   DATE,
    baixado_em     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fonte, versao, url)
);

CREATE TABLE raw_ingest.quarantine_records (
    id             BIGSERIAL PRIMARY KEY,
    fonte          VARCHAR(40) NOT NULL,
    versao         VARCHAR(20) NOT NULL,
    chave          TEXT,
    motivo         VARCHAR(60) NOT NULL,
    payload        JSONB,
    registrado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON raw_ingest.quarantine_records (fonte, versao, motivo);

-- Municípios (não versionado) -------------------------------------------------

CREATE TABLE core_geo.municipios (
    codigo_ibge    INT PRIMARY KEY CHECK (codigo_ibge BETWEEN 1000000 AND 9999999),
    codigo_tom     INT NOT NULL UNIQUE,
    nome_municipio VARCHAR(100) NOT NULL,
    uf             CHAR(2) NOT NULL,
    em_cobertura   BOOLEAN NOT NULL DEFAULT FALSE
);

-- Templates das tabelas versionadas ---------------------------------------------
-- Cada carga cria core_geo.<base>_v<versao> (LIKE <base>_template INCLUDING ALL) e
-- publica trocando a view core_geo.<base>_current.
-- LIKE não copia chaves estrangeiras: a integridade com municipios é checada antes da publicação.

CREATE TABLE core_geo.empresas_template (
    cnpj                  CHAR(14) PRIMARY KEY CHECK (core_geo.documento_formato_ok('CNPJ', cnpj)),
    cnpj_raiz             CHAR(8)  NOT NULL,
    razao_social          VARCHAR(255) NOT NULL,
    situacao_cadastral    VARCHAR(10) NOT NULL
        CHECK (situacao_cadastral IN ('ATIVA', 'SUSPENSA', 'INAPTA', 'BAIXADA', 'NULA')),
    cnae_fiscal_principal CHAR(7) NOT NULL,
    codigo_ibge           INT
);
CREATE INDEX ON core_geo.empresas_template (cnpj_raiz);

CREATE TABLE core_geo.imoveis_car_template (
    codigo_car        VARCHAR(50) PRIMARY KEY,
    codigo_ibge       INT NOT NULL,
    status_cadastro   VARCHAR(10) NOT NULL
        CHECK (status_cadastro IN ('ATIVO', 'PENDENTE', 'SUSPENSO', 'CANCELADO')),
    area_declarada_ha NUMERIC(14, 4),
    geometria         GEOMETRY(MultiPolygon, 4674) NOT NULL,
    -- Calculada pelo banco: a carga não precisa (nem consegue) informar
    area_geodesica_ha NUMERIC(14, 4) GENERATED ALWAYS AS (ST_Area(geometria::geography) / 10000.0) STORED
);
CREATE INDEX ON core_geo.imoveis_car_template USING GIST (geometria);

CREATE TABLE core_geo.embargos_template (
    orgao               VARCHAR(20) NOT NULL,     -- IBAMA, SEMAD-GO, SEMAD-MG, IAT-PR
    id_embargo          VARCHAR(60) NOT NULL,     -- identificador da fonte
    numero_processo     VARCHAR(100),
    tipo_documento      core_geo.tipo_documento,
    documento_autuado   VARCHAR(14),              -- só quando publicado sem máscara
    documento_mascarado VARCHAR(20),
    situacao            VARCHAR(20) NOT NULL CHECK (situacao IN ('VIGENTE', 'DESEMBARGADO')),
    data_infracao       DATE,
    data_embargo        DATE,
    codigo_ibge         INT,
    tipo_geom_origem    VARCHAR(10) NOT NULL CHECK (tipo_geom_origem IN ('POLIGONO', 'PONTO', 'NENHUMA')),
    geometria           GEOMETRY(MultiPolygon, 4674),
    ponto               GEOMETRY(Point, 4674),
    PRIMARY KEY (orgao, id_embargo),
    -- Só polígonos entram na interseção; ponto e ausência de geometria caem na regra R-EMB-SEMGEO
    CHECK ((tipo_geom_origem = 'POLIGONO') = (geometria IS NOT NULL)),
    CHECK (tipo_geom_origem <> 'PONTO' OR ponto IS NOT NULL),
    CHECK ((tipo_documento IS NULL) = (documento_autuado IS NULL)),
    CHECK (documento_autuado IS NULL OR core_geo.documento_formato_ok(tipo_documento, documento_autuado))
);
CREATE INDEX ON core_geo.embargos_template USING GIST (geometria);
CREATE INDEX ON core_geo.embargos_template (tipo_documento, documento_autuado);
CREATE INDEX ON core_geo.embargos_template (codigo_ibge) WHERE geometria IS NULL;

CREATE TABLE core_geo.mte_template (
    tipo_documento core_geo.tipo_documento NOT NULL,
    documento      VARCHAR(14) NOT NULL,
    cnpj_raiz      CHAR(8),
    nome_infrator  VARCHAR(255) NOT NULL,
    data_inclusao  DATE NOT NULL,
    PRIMARY KEY (tipo_documento, documento),
    CHECK (core_geo.documento_formato_ok(tipo_documento, documento)),
    CHECK ((tipo_documento = 'CNPJ') = (cnpj_raiz IS NOT NULL))
);
CREATE INDEX ON core_geo.mte_template (cnpj_raiz);

CREATE TABLE core_geo.camadas_territoriais_template (
    camada      VARCHAR(20) NOT NULL CHECK (camada IN ('TI', 'UC', 'QUILOMBOLA', 'FPND', 'PRODES')),
    id          VARCHAR(80) NOT NULL,
    nome        VARCHAR(255),
    atributos   JSONB NOT NULL DEFAULT '{}',   -- fase (TI), categoria/esfera (UC), ano/data (PRODES) etc.
    geometria   GEOMETRY(MultiPolygon, 4674) NOT NULL,
    PRIMARY KEY (camada, id)
);
CREATE INDEX ON core_geo.camadas_territoriais_template USING GIST (geometria);

-- Views publicadas. Começam apontando para os templates (vazios) e passam a pertencer
-- ao papel de ETL, que as troca a cada publicação. CREATE OR REPLACE preserva os GRANTs.
CREATE VIEW core_geo.empresas_current AS SELECT * FROM core_geo.empresas_template;
CREATE VIEW core_geo.imoveis_car_current AS SELECT * FROM core_geo.imoveis_car_template;
CREATE VIEW core_geo.embargos_current AS SELECT * FROM core_geo.embargos_template;
CREATE VIEW core_geo.mte_current AS SELECT * FROM core_geo.mte_template;
CREATE VIEW core_geo.camadas_territoriais_current AS SELECT * FROM core_geo.camadas_territoriais_template;

-- Configuração por tenant -------------------------------------------------------

CREATE TABLE core_geo.tenant_config (
    client_tenant_id VARCHAR(100) PRIMARY KEY,
    tolerancia_ha    NUMERIC(8, 4) NOT NULL DEFAULT 0.1  CHECK (tolerancia_ha >= 0),
    tolerancia_pct   NUMERIC(6, 4) NOT NULL DEFAULT 0.01 CHECK (tolerancia_pct >= 0),
    rate_limit_rpm   INT NOT NULL DEFAULT 60 CHECK (rate_limit_rpm > 0),
    ativo            BOOLEAN NOT NULL DEFAULT TRUE
);

-- Auditoria: particionada por mês, somente inserção --------------------------------

CREATE TABLE audit.credit_analysis_log (
    analysis_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    executed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id        VARCHAR(64) NOT NULL,
    client_tenant_id  VARCHAR(100) NOT NULL,
    document_hash     CHAR(64) NOT NULL,
    hmac_key_version  SMALLINT NOT NULL,
    document_masked   VARCHAR(20) NOT NULL,
    car_code          VARCHAR(50) NOT NULL,
    ruleset_version   VARCHAR(40) NOT NULL,
    dataset_versions  JSONB NOT NULL,
    tolerance_applied JSONB NOT NULL,
    rule_results      JSONB NOT NULL,
    credit_status     VARCHAR(40) NOT NULL,
    PRIMARY KEY (analysis_id, executed_at)
) PARTITION BY RANGE (executed_at);

-- Partição DEFAULT: sem ela, a falta da partição do mês faria o INSERT falhar e derrubaria a API
CREATE TABLE audit.credit_analysis_log_default PARTITION OF audit.credit_analysis_log DEFAULT;

CREATE INDEX ON audit.credit_analysis_log (document_hash);
CREATE INDEX ON audit.credit_analysis_log (client_tenant_id, executed_at);

CREATE FUNCTION audit.bloquear_alteracao() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.credit_analysis_log é somente inserção (% bloqueado)', TG_OP;
END
$$;

CREATE TRIGGER somente_insercao
    BEFORE UPDATE OR DELETE ON audit.credit_analysis_log
    FOR EACH ROW EXECUTE FUNCTION audit.bloquear_alteracao();

-- Cria as partições mensais a partir de `inicio`, `meses` à frente. Idempotente.
-- SECURITY DEFINER: as partições nascem pertencendo ao papel de manutenção, que precisa removê-las.
CREATE FUNCTION audit.criar_particoes(inicio DATE, meses INT) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
DECLARE
    mes DATE;
    nome TEXT;
BEGIN
    FOR i IN 0 .. meses - 1 LOOP
        mes := date_trunc('month', inicio)::date + make_interval(months => i);
        nome := format('credit_analysis_log_%s', to_char(mes, 'YYYY_MM'));
        IF to_regclass('audit.' || nome) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE audit.%I PARTITION OF audit.credit_analysis_log FOR VALUES FROM (%L) TO (%L)',
                nome, mes, mes + interval '1 month');
        END IF;
    END LOOP;
END
$$;

-- Permissões ----------------------------------------------------------------------

-- API: lê views publicadas e configuração; só insere na auditoria.
GRANT USAGE ON SCHEMA core_geo, audit TO role_api;
GRANT SELECT ON core_geo.empresas_current, core_geo.imoveis_car_current, core_geo.embargos_current,
    core_geo.mte_current, core_geo.camadas_territoriais_current,
    core_geo.municipios, core_geo.tenant_config, core_geo.dataset_versions TO role_api;
GRANT INSERT ON audit.credit_analysis_log TO role_api;

-- ETL: escreve em raw_ingest e cria versões em core_geo; sem acesso à auditoria.
GRANT USAGE ON SCHEMA raw_ingest TO role_etl;
GRANT USAGE, CREATE ON SCHEMA core_geo TO role_etl;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA raw_ingest TO role_etl;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA raw_ingest TO role_etl;
GRANT SELECT ON core_geo.empresas_template, core_geo.imoveis_car_template, core_geo.embargos_template,
    core_geo.mte_template, core_geo.camadas_territoriais_template TO role_etl;
GRANT SELECT, INSERT, UPDATE ON core_geo.municipios, core_geo.dataset_versions TO role_etl;
ALTER VIEW core_geo.empresas_current OWNER TO role_etl;
ALTER VIEW core_geo.imoveis_car_current OWNER TO role_etl;
ALTER VIEW core_geo.embargos_current OWNER TO role_etl;
ALTER VIEW core_geo.mte_current OWNER TO role_etl;
ALTER VIEW core_geo.camadas_territoriais_current OWNER TO role_etl;

-- Manutenção: dona da auditoria (DETACH/DROP de partições); UPDATE/DELETE seguem bloqueados pelo trigger.
GRANT USAGE, CREATE ON SCHEMA audit TO role_maint;
ALTER TABLE audit.credit_analysis_log OWNER TO role_maint;
ALTER TABLE audit.credit_analysis_log_default OWNER TO role_maint;
ALTER FUNCTION audit.criar_particoes(DATE, INT) OWNER TO role_maint;
REVOKE EXECUTE ON FUNCTION audit.criar_particoes(DATE, INT) FROM PUBLIC;

SELECT audit.criar_particoes(current_date, 3);
