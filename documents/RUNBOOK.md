# RUNBOOK.md

**AgroCredit Intelligence API — Operação e Resposta a Incidentes**

- **Versão:** 1.1.0
- **Data:** Outubro de 2026
- **Stack:** FastAPI, asyncpg, PostgreSQL 16, PostGIS 3.4, Docker, Prometheus, Grafana

> **Notas da revisão 1.1.0**
> - SLOs dimensionados para o MVP (ambiente de estudo), com metas de produção separadas.
> - Limiares de defasagem passam a referenciar o DATA_PIPELINE (fonte única).
> - Readiness verifica o PostGIS de fato.
> - INC-03 reescrito: o SHA-256 é proveniência, não há manifesto oficial para comparar.
> - Novo INC-05: publicação de snapshot bloqueada; novo INC-06: mudança normativa.
> - Rate limit sem Redis/Nginx no MVP (não fazem parte da stack); reindexação semanal removida (snapshots recriam índices).
> - Auditoria particionada por mês: expurgo por `DROP PARTITION` em vez de `DELETE`.
> - Português padronizado (PT-BR).

---

## 1. Objetivos de Nível de Serviço

| Indicador | MVP (estudo) | Produção (meta futura) | Ação em violação |
| :-- | :-- | :-- | :-- |
| Disponibilidade de `POST /v1/credit/check` | 99% mensal, horário comercial | 99,9% mensal | Investigar e registrar; sem failover no MVP |
| Latência | p95 < 800 ms; p99 < 2 s | Idem | INC-01 |
| Timeout de query | `statement_timeout = 3000 ms` | Idem | Retorno 503 com `retry_after` (não 504: o timeout é do próprio serviço) |
| Erros 5xx | < 1% | < 0,1% | INC-02 |
| Defasagem de dados | Limiares do DATA_PIPELINE.md, seção 4 | Idem | INC-03 |
| Pareceres `INCONCLUSIVO` | Monitorar tendência | Idem | Investigar a fonte que causou `NAO_VERIFICADO` |

---

## 2. Monitoramento

### 2.1 Health checks

- **Liveness (`GET /health/liveness`):** loop de eventos respondendo. `200 {"status":"alive"}`.
- **Readiness (`GET /health/readiness`):** banco acessível, PostGIS ativo e dados publicados:

```sql
SELECT postgis_lib_version();
SELECT count(*) FROM core_geo.dataset_versions
WHERE publicado_em IS NOT NULL;   -- deve haver ao menos uma versão por fonte obrigatória
```

  `200` se tudo ok; `503` se o banco não responder ou se SICAR, IBAMA ou MTE não tiverem versão publicada. As demais fontes faltando não derrubam a API: as regras correspondentes saem `NAO_VERIFICADO`.

### 2.2 Métricas

Endpoint `GET /metrics` (formato Prometheus). Com `AGROCREDIT_METRICS_TOKEN` definido, exige `Authorization: Bearer <token>`.

| Métrica | Uso |
| :-- | :-- |
| `agrocredit_http_requests_total{method,endpoint,status}` | Volume e erros. `endpoint` é o modelo da rota (ex.: `/v1/credit/analyses/{analysis_id}`), nunca o caminho com o UUID |
| `agrocredit_http_request_duration_seconds{endpoint}` (histograma) | Latência por rota; a de `/v1/credit/check` inclui as consultas espaciais e a gravação da auditoria |
| `agrocredit_credit_status_total{tenant,credit_status}` | Distribuição dos pareceres (pico de `INCONCLUSIVO` ou de impedimentos indica problema de dados) |
| `agrocredit_rule_result_total{rule_id,result}` | Regras que mais geram `NAO_VERIFICADO` |
| `agrocredit_dataset_age_days{fonte}` | Idade de cada base publicada (calculada na coleta) |
| `agrocredit_dataset_staleness_level{fonte}` | 0 OK, 1 alerta (P2), 2 crítico (P1: regras da fonte saem `NAO_VERIFICADO`) |
| `asyncpg_pool_size`, `asyncpg_pool_idle` | Saturação do pool (idle perto de 0 com tamanho no máximo: INC-02) |

Alertas sugeridos: `agrocredit_dataset_staleness_level == 2` (P1), `== 1` por mais de 1 dia (P2), taxa de `status=~"5.."` > 1%, p95 de `/v1/credit/check` > 800 ms.

---

## 3. Playbooks

```
CPU > 90% ou query > 3 s ............. INC-01
503 / timeout de pool ................ INC-02
Fonte defasada ou carga falhou ....... INC-03
Abuso de consumo (429) ............... INC-04
Publicação de snapshot bloqueada ..... INC-05
Mudança no MCR 2-9 ................... INC-06
```

### INC-01: Query espacial lenta

**Causa provável:** CAR com geometria muito complexa, estatísticas desatualizadas ou índice não usado.

1. Identificar queries longas:

```sql
SELECT pid, now() - query_start AS duracao, state, left(query, 200)
FROM pg_stat_activity
WHERE state <> 'idle' AND now() - query_start > interval '2 seconds'
ORDER BY duracao DESC;
```

2. Cancelar (`pg_cancel_backend`) e, se persistir, encerrar (`pg_terminate_backend`).
3. Conferir o plano com o código CAR afetado:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT c.codigo_car
FROM core_geo.imoveis_car_current c
JOIN core_geo.embargos_current e ON ST_Intersects(c.geometria, e.geometria)
WHERE c.codigo_car = '<CAR>';
```

   Se aparecer `Seq Scan` na tabela de embargos: rodar `ANALYZE` na versão publicada; se não resolver, verificar se o índice GiST foi criado na versão atual.
4. Registrar o CAR e o número de vértices (`ST_NPoints`). Geometrias com muitos vértices recorrentes são candidatas a pré-cálculo de interseções durante a carga.

### INC-02: Pool de conexões esgotado

1. Verificar conexões por estado:

```sql
SELECT client_addr, state, count(*)
FROM pg_stat_activity WHERE datname = 'agrocredit_db'
GROUP BY 1, 2;
```

2. Conferir a regra de dimensionamento:
   `max_connections ≥ instâncias × workers × pool_max + 10`
   (MVP: `POOL_MIN=5`, `POOL_MAX=20`).
3. Procurar vazamento (conexões adquiridas fora de `async with`).
4. Reiniciar a API: `docker compose restart api`.

### INC-03: Fonte defasada ou carga falhou

1. Ver idade e última versão:

```sql
SELECT fonte, max(versao), max(source_as_of), max(publicado_em)
FROM core_geo.dataset_versions GROUP BY fonte;
```

2. Ver o log do job e o relatório de quarentena da última tentativa.
3. Causas comuns e ação:
   - **Portal fora do ar:** registrar e tentar no próximo ciclo. A API continua com a versão anterior; passando do limiar crítico, as regras dessa fonte retornam `NAO_VERIFICADO` automaticamente.
   - **Schema drift:** ajustar o contrato da fonte no código (com revisão) e reprocessar.
   - **Arquivo mudou (hash diferente do último download):** é esperado quando a fonte publica nova versão. Não há manifesto oficial a comparar. O que valida o arquivo são as checagens de abertura, tamanho e volume (INC-05).
4. **Nunca** copiar arquivos manualmente para Silver/Gold fora do pipeline. Arquivo obtido fora do pipeline (link que exige login, resposta via LAI) entra pelo próprio pipeline, com proveniência registrada:

```bash
python -m agrocredit.pipeline run cnuc --arquivo cnuc_2026_07.zip --as-of 2026-07-01
```

### INC-04: Abuso de consumo

1. Identificar o tenant nos logs estruturados:

```bash
docker logs --tail 5000 api | jq -r 'select(.event=="rate_limit_exceeded") | .client_tenant_id' | sort | uniq -c
```

2. Rate limit no MVP é aplicado em memória na própria API (por tenant). Ajustar a cota do tenant na configuração e reiniciar.
3. Em caso de abuso deliberado, revogar o client credential do tenant.

(Redis e reverse proxy entram apenas quando houver mais de uma instância da API; até lá não fazem parte da stack.)

### INC-05: Publicação de snapshot bloqueada

**Sintoma:** job de carga terminou, mas a view `*_current` não foi trocada.

1. Ver qual checagem falhou (volume ±20%, geometrias inválidas, contagem de quarentena acima do normal).
2. Comparar com a versão anterior:

```sql
SELECT 'nova' v, count(*) FROM core_geo.embargos_v<nova>
UNION ALL
SELECT 'atual', count(*) FROM core_geo.embargos_current;
```

3. Se a variação for legítima (ex.: IBAMA publicou lote grande de desembargos), aprovar a publicação manualmente com registro do motivo e de quem aprovou:

```bash
ETL_DATABASE_URL=... python -m agrocredit.pipeline publicar ibama <versao> --aprovado-por "Nome" --motivo "Lote de desembargos de out/2026"
```

   O motivo do bloqueio fica em `core_geo.dataset_versions.bloqueio`.
4. Se não for legítima, manter a versão atual e abrir chamado com a engenharia de dados.

### INC-06: Mudança normativa no MCR 2-9

**Gatilho:** nova resolução do CMN alterando a Seção 9, ou decisão que suspenda resolução vigente.

1. Jurídico analisa e documenta o impacto por regra.
2. Engenharia cria nova `ruleset_version` com data de vigência.
3. Testes contra o conjunto rotulado antes da ativação.
4. Comunicar clientes com antecedência quando possível.
5. Atualizar PRD (seção 5.2) e RIPD.

---

## 4. Manutenção

Tarefas no CLI `python -m agrocredit.manutencao` (imagem `tools`; em desenvolvimento, `make tools CMD="..."`).
Cada comando assume o papel de banco com o mínimo necessário (`role_etl` ou `role_maint`).

| Comando | Faz | Agendamento sugerido |
| :-- | :-- | :-- |
| `fontes` | Defasagem de cada base; saída 0 OK, 1 P2, 2 P1 | De hora em hora |
| `particoes --meses 3` | Garante as partições mensais da auditoria | Mensal |
| `expurgar-auditoria --destino DIR` | Exporta para Parquet (com `.sha256`), relê e confere, só então remove partições com mais de 5 anos | Mensal |
| `limpar-versoes --manter 2 --destino DIR [--fonte X]` | Arquiva em Parquet (geometria em EWKB) e remove versões antigas das bases; nunca a publicada | Semanal |
| `validar-espacial [--limite N]` | Recalcula as sobreposições de todos os imóveis cobertos sem o PostGIS (shapely + pyproj) e compara com a API; saída 1 se alguma classificação divergir | Após cargas de SICAR, IBAMA ou camadas |
| `titular --documento X` | Pedido do titular (LGPD art. 18): análises do documento em todos os tenants e versões da chave HMAC | Sob demanda (DPO) |

O agendamento completo (bases e manutenção) está em `ops/agrocredit.crontab`, executado por `ops/agendar.sh`:
- trava por tarefa (`flock`): execução sobreposta é pulada (código 75), sem alerta;
- saída e falhas vão para o syslog (`journalctl -t agrocredit-<tarefa>`);
- `AGROCREDIT_ALERTA_CMD` recebe a mensagem de falha pela entrada padrão.

Em Kubernetes, cada linha do crontab vira um CronJob com a imagem `tools`.

Carga sem novidade: nas fontes cuja data de referência vem do conteúdo (MTE, IBAMA, CNUC, CNFP, PRODES, MG), arquivos idênticos aos da versão em uso são descartados sem gerar versão nova. SICAR e INCRA sempre recarregam, porque a data deles é a do download.

### 4.1 Índices e estatísticas

Não há reindexação semanal: cada snapshot é carregado em tabela nova, com índices recriados e `ANALYZE` executado antes da publicação. A tabela de auditoria (só inserção) recebe `VACUUM ANALYZE` automático do autovacuum.

### 4.2 Segredos

- **Pepper do HMAC:** versionado. Para rotacionar: mover o pepper atual para `AGROCREDIT_HMAC_PEPPERS_ANTERIORES` (JSON `{"1": "..."}`), definir o novo em `AGROCREDIT_HMAC_PEPPER` e subir `AGROCREDIT_HMAC_KEY_VERSION`. Novas análises usam a chave nova; `manutencao titular` calcula o hash com todas as versões. Nunca descartar uma versão antiga enquanto houver auditoria dentro da retenção que a use.
- **Chaves de assinatura JWT:** com `AGROCREDIT_JWT_JWKS_URL`, a API busca o JWKS do provedor de identidade, mantém em cache por 10 min e recarrega ao receber um `kid` desconhecido (no máximo uma vez a cada 30 s, para que tokens forjados não sobrecarreguem o provedor). Se o provedor cair, as chaves já carregadas continuam valendo. Rotação: o provedor publica a chave nova junto com a antiga; a API passa a aceitá-la sem reinício. Alternativas: `AGROCREDIT_JWT_JWKS` (JSON) ou `AGROCREDIT_JWT_JWKS_ARQUIVO` (segredo montado), que exigem reinício para trocar a chave.

### 4.3 Retenção da auditoria

`audit.credit_analysis_log` é particionada por mês. Partições com mais de 5 anos são exportadas para Parquet cifrado (armazenamento frio) e removidas:

```sql
-- Mensal, pelo job de manutenção
ALTER TABLE audit.credit_analysis_log DETACH PARTITION audit.credit_analysis_log_2021_10;
-- exportar a partição destacada para Parquet cifrado e verificar o arquivo
DROP TABLE audit.credit_analysis_log_2021_10;
```

O papel da API não tem permissão de `DELETE` na auditoria; a operação roda com papel de manutenção dedicado.

Logs de segurança com IP: retenção de 6 meses (ver RIPD, seção 3.2).

---

## 5. Escalonamento

| Nível | Papel | Canal | Resposta (MVP) |
| :-- | :-- | :-- | :-- |
| 1 | Engenharia de plantão | Canal da equipe | Horário comercial |
| 2 | Tech Lead | Canal da equipe | Horário comercial; incidente de segurança: imediato |
| 3 | DPO e Segurança | E-mail dedicado | Incidente com dado pessoal: mesmo dia útil (comunicação à ANPD conforme regulamento vigente) |
| — | Jurídico | E-mail dedicado | INC-06 |

Plantão 24/7 e PagerDuty entram apenas quando houver cliente em produção com SLA contratado.
