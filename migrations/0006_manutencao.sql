-- 0006 — Manutenção: arquivamento de versões antigas das bases (ADR-007) e leitura da auditoria pelo DPO.

-- Versão arquivada: a tabela sai do banco e fica em Parquet (com hash) para reproduzir pareceres antigos.
ALTER TABLE core_geo.dataset_versions
    ADD COLUMN arquivado_em TIMESTAMPTZ,
    ADD COLUMN arquivo_parquet TEXT,
    ADD COLUMN arquivo_sha256 CHAR(64);
