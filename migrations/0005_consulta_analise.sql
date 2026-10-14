-- 0005 — Consulta de análise passada sem dar SELECT na auditoria ao papel da API.
-- A função só devolve a linha quando o tenant bate (proteção contra BOLA/IDOR no próprio banco).

CREATE FUNCTION audit.consultar_analise(p_analysis_id UUID, p_tenant VARCHAR)
RETURNS TABLE (
    analysis_id UUID,
    executed_at TIMESTAMPTZ,
    document_masked VARCHAR,
    car_code VARCHAR,
    ruleset_version VARCHAR,
    dataset_versions JSONB,
    tolerance_applied JSONB,
    rule_results JSONB,
    credit_status VARCHAR
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, audit AS $$
    SELECT analysis_id, executed_at, document_masked, car_code, ruleset_version, dataset_versions,
           tolerance_applied, rule_results, credit_status
    FROM audit.credit_analysis_log
    WHERE analysis_id = p_analysis_id AND client_tenant_id = p_tenant
$$;

ALTER FUNCTION audit.consultar_analise(UUID, VARCHAR) OWNER TO role_maint;
REVOKE EXECUTE ON FUNCTION audit.consultar_analise(UUID, VARCHAR) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.consultar_analise(UUID, VARCHAR) TO role_api;
