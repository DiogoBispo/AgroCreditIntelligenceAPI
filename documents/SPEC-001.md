# SPEC-001: AgroCredit Intelligence API — Motor de Regras e Segurança

- **Versão:** 1.2.0
- **Data:** Outubro de 2026
- **Status:** Em implementação (substitui v1.1.0)
- **Base:** PRD v1.1.0, MCR 2-9 vigente (Res. CMN 5.193/2024, 5.268/2025, 5.303/2026), OWASP API Security Top 10 (2023), OWASP ASVS, LGPD

> **Notas da revisão 1.2.0**
> - **O código passa a ser a fonte única** do que já foi implementado: schema em `migrations/`, validadores e regras em `src/agrocredit/domain/`, ruleset em `rules/mcr-2-9.yaml`. Esta SPEC descreve decisões e o que ainda falta implementar.
> - Schema: área geodésica como coluna gerada (a carga não precisa calculá-la); partição DEFAULT na auditoria e trigger de somente inserção; embargo com geometria de origem PONTO/NENHUMA (cai em R-EMB-SEMGEO); chave dos embargos `(orgao, id_embargo)`; validação de formato de documento no banco; tabela de sócios removida (não usada por nenhuma regra e com CPF mascarado na fonte).
> - CNPJ alfanumérico implementado no MVP.
> - Query 6.1: lista de embargos filtrada por `geom_inter`, não por `numero_processo` (que pode ser nulo).
> - Pacote Python em `src/agrocredit/` (em vez de importar `src` como pacote).
>
> **Notas da revisão 1.1.0**
> - **Fonte única do schema físico** (o PRD deixou de ter schema próprio).
> - Documento como (`tipo_documento`, `documento`) em todas as tabelas.
> - Tabelas versionadas + views `*_current`; embargos com situação, órgão e geometria opcional; camadas territoriais; tabela de regras e de versões de dados.
> - Query espacial corrigida: `ST_Union` antes da área, percentual sobre área geodésica, sem `&&` redundante.
> - Motor de regras com avaliação completa e quatro resultados por regra.
> - DTOs: removido `requested_amount_brl`; CAR aceito com pontos; respostas com `ruleset_version`, `data_as_of`, `owner_link_verified`; `float` monetário eliminado.
> - Auditoria particionada, com versões de dados, tolerância aplicada e `hmac_key_version`; IP fora da auditoria de 5 anos.
> - Testes de regressão de domínio adicionados aos de segurança.

---

## 1. Riscos Técnicos Endereçados

| Risco | Correção nesta spec |
| :-- | :-- |
| DoS por geometria complexa | Índice GiST, `statement_timeout = 3000 ms`, rate limit por tenant |
| CPF em logs | Logger com mascaramento; teste TEST-SEC-003 |
| Privilégios amplos no banco | Schemas `raw_ingest`, `core_geo`, `audit`; papéis separados para API, ETL e manutenção |
| Embargos levantados continuando ativos | Snapshots versionados (ADR-007) |
| Soma dupla de áreas de embargos sobrepostos | `ST_Union` antes de `ST_Area` (ADR-002) |
| Regras desatualizadas | `ruleset_version` (ADR-008) |
| Colisão CPF × CNPJ | Chave composta (`tipo_documento`, `documento`) |

---

## 2. LGPD

Ver RIPD.md. Pontos com impacto direto no código:

- O documento do tomador só existe em memória durante a requisição. Na auditoria: versão mascarada + HMAC com `hmac_key_version`.
- Sem `requested_amount_brl` na entrada.
- IP registrado apenas no log de segurança (retenção de 6 meses), não na auditoria.
- Para o MTE, a resposta traz só o resultado da regra e, havendo impedimento, a data de inclusão. Não expõe a lista.

---

## 3. Arquitetura de Software

Clean Architecture (portas e adaptadores):

```text
agrocredit_api/
├── src/agrocredit/
│   ├── core/              config, exceptions, logging (mascaramento), security (JWT, HMAC)
│   ├── domain/
│   │   ├── entities.py    Imovel, ResultadoRegra, Parecer
│   │   ├── enums.py       ResultadoRegraEnum, CreditStatus, TipoDocumento
│   │   ├── rules/         uma regra por arquivo (r_car.py, r_mte.py, r_emb_geo.py, ...)
│   │   ├── ruleset.py     carrega regras por ruleset_version e vigência
│   │   ├── decision.py    deriva credit_status a partir dos resultados
│   │   └── validators.py  módulo 11, normalização de CAR
│   ├── application/
│   │   ├── dtos/
│   │   └── use_cases/check_credit_compliance.py
│   ├── infrastructure/db/
│   │   ├── connection.py
│   │   └── repositories/  car, embargos, mte, territorial, rfb, dataset_versions, audit
│   └── presentation/api/v1/endpoints/credit.py
├── rules/                 definição versionada das regras (YAML) com vigência
├── tests/{unit,integration,security,domain}/
├── migrations/
└── Dockerfile
```

Princípios: validação na borda, entidades imutáveis, SQL sempre parametrizado, regras de domínio sem dependência de banco (recebem fatos já consultados).

---

## 4. Ameaças e Controles

| OWASP | Vetor | Controle |
| :-- | :-- | :-- |
| API1 BOLA | Consultar `analysis_id` de outro tenant | Toda leitura filtra por `client_tenant_id` do JWT; teste TEST-SEC-005 |
| API2 Autenticação | Token forjado | JWT assimétrico (RS256/EdDSA), `iss`, `aud`, `exp`, `nbf`; JWKS com rotação |
| API3 Propriedades | Mass assignment / exposição | `extra="forbid"` na entrada; DTOs de saída explícitos |
| API4 Consumo | Rajadas e geometrias pesadas | Rate limit por tenant (60 req/min padrão); `statement_timeout`; índice GiST |
| API8 Configuração | Stack trace exposto | Handler global com mensagem genérica e `request_id` |
| Ingestão | Zip bomb, shapefile malicioso | Limite de tamanho descompactado; leitura em processo isolado; quarentena |

---

## 5. Contratos da API

### 5.1 Validadores

Implementação: `src/agrocredit/domain/validators.py` (testes em `tests/unit/test_validators.py`).

- CPF: 11 dígitos, módulo 11.
- CNPJ: numérico ou **alfanumérico** (RFB, desde jul/2026): 12 caracteres `[0-9A-Z]` + 2 DVs numéricos; no cálculo cada caractere vale `ord(c) − 48`. Exemplo oficial `12.ABC.345/01DE-35` coberto por teste.
- Entrada aceita máscara (`.`, `/`, `-`, espaços) e minúsculas; normaliza para maiúsculas sem separadores.
- CAR: aceita pontos separadores; valida o formato e o dígito verificador do código IBGE embutido.
- Erros levantam `ValueError` (o Pydantic converte em 422).

### 5.2 Requisição

```python
# src/agrocredit/application/dtos/request.py
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agrocredit.domain.enums import TipoDocumento
from agrocredit.domain.validators import parse_car, parse_document


class CreditCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document: str = Field(..., max_length=18, description="CPF (11) ou CNPJ (14), com ou sem máscara")
    car_code: str = Field(..., max_length=60, description="Código CAR, com ou sem pontos")

    @field_validator("document")
    @classmethod
    def _doc(cls, v: str) -> str:
        return parse_document(v).valor  # CPF ou CNPJ (inclusive alfanumérico)

    @field_validator("car_code")
    @classmethod
    def _car(cls, v: str) -> str:
        return parse_car(v)

    @property
    def document_type(self) -> TipoDocumento:
        return TipoDocumento.CPF if len(self.document) == 11 else TipoDocumento.CNPJ
```

Os validadores levantam `ValueError` (convertido pelo Pydantic em 422), em vez de exceções de domínio que escapariam do tratamento padrão.

### 5.3 Resposta

```python
# src/application/dtos/response.py
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class RuleResult(str, Enum):
    IMPEDIDO = "IMPEDIDO"
    ALERTA = "ALERTA"
    OK = "OK"
    NAO_VERIFICADO = "NAO_VERIFICADO"


class CreditStatus(str, Enum):
    IMPEDIMENTO_IDENTIFICADO = "IMPEDIMENTO_IDENTIFICADO"
    INCONCLUSIVO = "INCONCLUSIVO"
    SEM_IMPEDIMENTO_COM_ALERTAS = "SEM_IMPEDIMENTO_COM_ALERTAS"
    SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS = "SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS"
    FORA_DE_COBERTURA = "FORA_DE_COBERTURA"


class EmbargoHit(BaseModel):
    source: str
    process_number: str
    infraction_date: date | None
    overlap_ha: Decimal


class CheckOut(BaseModel):
    rule_id: str
    result: RuleResult
    reason: str | None = None
    detail: dict | None = None


class PropertyOut(BaseModel):
    car_status: str
    municipio: str
    uf: str
    declared_area_ha: Decimal | None
    geodesic_area_ha: Decimal


class CadastralOut(BaseModel):
    rfb_status: str          # "NAO_DISPONIVEL" para CPF
    cnae_principal: str | None


class DecisionOut(BaseModel):
    credit_status: CreditStatus
    summary: str


class CreditCheckResponse(BaseModel):
    analysis_id: UUID
    timestamp: datetime
    ruleset_version: str
    document_masked: str
    car_code: str
    property: PropertyOut | None
    cadastral_status: CadastralOut
    checks: list[CheckOut]
    owner_link_verified: bool = False
    data_as_of: dict[str, date]
    data_staleness_warning: bool
    decision: DecisionOut
```

### 5.4 Motor de regras

Implementação: `src/agrocredit/domain/` (testes em `tests/domain/`).

| Arquivo | Papel |
| :-- | :-- |
| `facts.py` | Fatos já consultados nas bases (as regras não acessam banco) |
| `rules/` | Regras agrupadas por fonte: `r_car`, `r_mte`, `r_embargos`, `r_territoriais`, `r_rfb` |
| `rules/base.py` | `classificar_sobreposicao`: tolerância "o que for maior" (ADR-009) |
| `ruleset.py` | Carrega `rules/mcr-2-9.yaml` e escolhe a versão vigente na data |
| `freshness.py` | Limiares de defasagem crítica (DATA_PIPELINE §4) |
| `decision.py` | `derive_status` (PRD §5.1); regra obrigatória ausente conta como `NAO_VERIFICADO` |
| `engine.py` | Avalia todas as regras e monta o resumo do parecer |

---

## 6. Schema Físico---

## 6. Schema Físico (fonte única)

**Fonte única: `migrations/0001_schema_base.sql`** (testes em `tests/integration/test_schema.py`).

Pontos principais:

- Schemas `raw_ingest`, `core_geo`, `audit`; `public` sem acesso para `PUBLIC`.
- Tabelas versionadas a partir de `*_template` (`LIKE ... INCLUDING ALL`, que **não** copia FKs: a integridade com `municipios` é checada antes de publicar) e views `*_current` pertencentes a `role_etl`.
- `imoveis_car.area_geodesica_ha`: coluna gerada `ST_Area(geometria::geography) / 10000`.
- `embargos`: PK `(orgao, id_embargo)`; `tipo_geom_origem` POLIGONO/PONTO/NENHUMA; só POLIGONO tem `geometria` (CHECK); ponto guardado em `ponto`.
- `core_geo.documento_formato_ok(tipo, doc)`: CPF `^[0-9]{11}$`, CNPJ `^[0-9A-Z]{12}[0-9]{2}$`.
- Auditoria particionada por mês, com partição DEFAULT (evita falha de INSERT), trigger que bloqueia UPDATE/DELETE para todos e `audit.criar_particoes(inicio, meses)` (SECURITY DEFINER, executada pelo papel de manutenção).
- Papéis: `role_api` (SELECT nas views e configuração; INSERT na auditoria), `role_etl` (raw_ingest, criação de versões, dono das views; sem auditoria), `role_maint` (dono da auditoria).

### 6.1 Query de embargos (geométrica)

```sql
-- $1 = codigo_car
WITH imovel AS (
    SELECT codigo_car, area_geodesica_ha, geometria
    FROM core_geo.imoveis_car_current
    WHERE codigo_car = $1
),
hits AS (
    SELECT e.orgao, e.numero_processo, e.data_infracao,
           ST_Intersection(i.geometria, e.geometria) AS geom_inter
    FROM imovel i
    JOIN core_geo.embargos_current e
      ON e.situacao = 'VIGENTE'
     AND e.geometria IS NOT NULL
     AND ST_Intersects(i.geometria, e.geometria)   -- usa o índice GiST
)
SELECT
    i.codigo_car,
    i.area_geodesica_ha,
    -- União antes da área: embargos sobrepostos não são contados duas vezes
    COALESCE(ST_Area(ST_Union(h.geom_inter)::geography) / 10000.0, 0) AS area_embargada_ha,
    COALESCE(
        ST_Area(ST_Union(h.geom_inter)::geography) / 10000.0 / NULLIF(i.area_geodesica_ha, 0) * 100,
        0
    ) AS overlap_pct,
    COALESCE(json_agg(json_build_object(
        'source', h.orgao,
        'process_number', h.numero_processo,
        'infraction_date', h.data_infracao,
        'overlap_ha', round((ST_Area(h.geom_inter::geography) / 10000.0)::numeric, 4)
    )) FILTER (WHERE h.geom_inter IS NOT NULL), '[]') AS embargos
FROM imovel i
LEFT JOIN hits h ON TRUE
GROUP BY i.codigo_car, i.area_geodesica_ha;
```

A interseção é calculada uma vez por par (CTE `hits`). O percentual usa a área geodésica do imóvel.

### 6.2 Query de embargos (documental) e sem geometria

```sql
-- Por documento ($1 = tipo, $2 = documento)
SELECT orgao, numero_processo, data_infracao
FROM core_geo.embargos_current
WHERE situacao = 'VIGENTE' AND tipo_documento = $1 AND documento_autuado = $2;

-- Embargos sem geometria no município do CAR ($1 = codigo_ibge) → regra R-EMB-SEMGEO
SELECT count(*) FROM core_geo.embargos_current
WHERE situacao = 'VIGENTE' AND geometria IS NULL AND codigo_ibge = $1;
```

### 6.3 Query do MTE

```sql
-- $1 = tipo, $2 = documento, $3 = cnpj_raiz (nulo para CPF)
SELECT tipo_documento, data_inclusao
FROM core_geo.mte_current
WHERE (tipo_documento = $1 AND documento = $2)
   OR ($3 IS NOT NULL AND cnpj_raiz = $3);
```

### 6.4 Camadas territoriais

```sql
-- $1 = codigo_car
SELECT t.camada, t.nome, t.atributos,
       ST_Area(ST_Intersection(i.geometria, t.geometria)::geography) / 10000.0 AS overlap_ha
FROM core_geo.imoveis_car_current i
JOIN core_geo.camadas_territoriais_current t ON ST_Intersects(i.geometria, t.geometria)
WHERE i.codigo_car = $1;
```

As regras de domínio (`r_ti.py`, `r_uc.py`, `r_qui.py`, `r_fpnd.py`, `r_prodes.py`) interpretam `atributos` conforme a `ruleset_version` vigente (ex.: fases da TI, categorias de UC, data de corte do PRODES).

---

## 7. DevSecOps

### 7.1 Container

- Base `python:3.12-slim-bookworm`, build multi-stage, usuário não-root (UID 10001).
- Segredos injetados pelo orquestrador; sem `.env` na imagem.

### 7.2 Gates de CI

| Gate | Ferramenta |
| :-- | :-- |
| Lint e complexidade | `ruff` |
| SAST | `bandit -r src -ll` |
| Dependências | `pip-audit` |
| Segredos | `gitleaks` |
| Tipagem | `mypy --strict src` |
| Testes de domínio e segurança | `pytest` (integração contra PostGIS real em container) |
| Regressão de regras | Conjunto rotulado do PRD 5.5; falha se algum falso negativo de impedimento |

---

## 8. Testes de Regressão

### 8.1 Segurança

```python
# tests/security/test_security.py
import pytest
from httpx import AsyncClient

CAR_OK = "GO-5218805-72819485728194857281948572819485"
CNPJ_OK = "11222333000181"


@pytest.mark.asyncio
async def test_sec_001_sql_injection_no_car(client: AsyncClient, token: str):
    r = await client.post("/v1/credit/check",
                          json={"document": CNPJ_OK, "car_code": "GO-5218805' OR '1'='1"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sec_002_sem_token(client: AsyncClient):
    r = await client.post("/v1/credit/check", json={"document": CNPJ_OK, "car_code": CAR_OK})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_sec_003_cpf_fora_dos_logs(client: AsyncClient, token: str, caplog):
    cpf = "52998224725"
    await client.post("/v1/credit/check", json={"document": cpf, "car_code": CAR_OK},
                      headers={"Authorization": f"Bearer {token}"})
    assert cpf not in caplog.text


@pytest.mark.asyncio
async def test_sec_004_campos_extras(client: AsyncClient, token: str):
    r = await client.post("/v1/credit/check",
                          json={"document": CNPJ_OK, "car_code": CAR_OK, "is_admin": True},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sec_005_bola_entre_tenants(client: AsyncClient, token_a: str, token_b: str):
    r = await client.post("/v1/credit/check", json={"document": CNPJ_OK, "car_code": CAR_OK},
                          headers={"Authorization": f"Bearer {token_a}"})
    analysis_id = r.json()["analysis_id"]
    r2 = await client.get(f"/v1/credit/analyses/{analysis_id}",
                          headers={"Authorization": f"Bearer {token_b}"})
    assert r2.status_code == 404
```

### 8.2 Domínio

Implementado em `tests/unit/test_validators.py`, `tests/domain/test_rules.py` e `tests/domain/test_engine.py`. Cobre, entre outros: tolerância "o que for maior" e `ALERTA` abaixo dela; CAR suspenso/cancelado; MTE por raiz de CNPJ; base estadual ausente; CPF mascarado na fonte; UCs sobrepostas (pior resultado prevalece); data de corte do PRODES; produtor PF não sai `INCONCLUSIVO` por falta de fonte de CPF; fonte obrigatória defasada gera `INCONCLUSIVO`.

### 8.3 Integração espacial (PostGIS real)

- Dois embargos sobrepostos no mesmo trecho do imóvel: área embargada igual à área da união, não à soma.
- Embargo com `situacao = 'DESEMBARGADO'`: não aparece.
- Embargo sem geometria no município: aciona R-EMB-SEMGEO.
- CAR em município fora da cobertura: `FORA_DE_COBERTURA`.
- Mesmo CNPJ raiz de empresa listada no MTE (filial diferente): `IMPEDIDO`.

---

## 9. Estado da implementação (14/10/2026)

| Componente | Onde | Observações |
| :-- | :-- | :-- |
| Repositório de fatos | `infrastructure/db/repositories/fatos.py` | Uma transação `REPEATABLE READ` por requisição (snapshot consistente mesmo durante publicação do ETL); união das interseções por esfera (federal: IBAMA + ICMBio; estadual: demais); embargo documental por documento exato ou raiz de CNPJ; ponto de embargo sem polígono dentro do imóvel |
| Cobertura | idem | CAR encontrado = coberto; não encontrado → município do código do CAR decide entre `R-CAR IMPEDIDO` e `FORA_DE_COBERTURA` |
| Caso de uso | `application/use_cases/check_credit_compliance.py` | Auditoria na mesma transação: se falhar, nenhum parecer é devolvido. `analysis_id` gerado na aplicação (`INSERT ... RETURNING` exigiria SELECT na auditoria) |
| Consulta de análise | `GET /v1/credit/analyses/{id}` + função `audit.consultar_analise` (migration 0005) | `SECURITY DEFINER` filtra pelo tenant; outro tenant recebe 404 |
| Autenticação | `core/security.py` | JWKS com `kid` (JSON, arquivo ou URL do provedor com cache e recarga limitada); só EdDSA/RS256, chaves de outros algoritmos no JWKS são ignoradas; exige `iss`, `aud`, `exp`, `nbf`, `sub`, `tenant_id`; 401 genérico |
| Métricas | `presentation/api/metrics.py` | `GET /metrics` (Prometheus), token opcional; registro por app; rótulo de rota pelo modelo, não pelo caminho |
| Pseudonimização | `core/config.py` (`ChavesHMAC`), `core/security.py` | Pepper versionado com versões anteriores, para rotação sem perder a busca do titular |
| Manutenção | `manutencao/` | Partições e expurgo da auditoria, arquivamento de versões das bases, defasagem e busca do titular (RUNBOOK §4) |
| Imagens | `Dockerfile` | `api` (271 MB) e `tools` (pipeline + manutenção + migrations, com poppler); ambas não-root |
| Rate limit | `presentation/api/dependencies.py` | Janela de 60 s por tenant, em memória; limite de `tenant_config` em cache por 60 s |
| Erros | `presentation/api/app.py` | 422 sem o campo `input` (não ecoa o documento); timeout de query → 503 + `Retry-After`; 500 genérico com `request_id` |
| Logs | `core/logging.py` | JSON; CPF/CNPJ mascarados em qualquer mensagem e argumento |
| Testes de segurança | `tests/integration/test_api.py` | SEC-001 a SEC-005 + `alg: none`, troca para HS256, token expirado, audiência errada, `kid` desconhecido, cabeçalhos, injeção em `X-Request-ID`, rate limit |

Carga local (uma instância, 10 conexões concorrentes, imóveis reais aleatórios): p50 ≈ 25 ms, p95 ≈ 40 ms, p99 < 100 ms, ~350 req/s.

## 9b. Plano do Sprint 1 (status)

1. ✅ Repositório, tooling e CI com os gates da seção 7.2.
2. ✅ PostGIS em Docker com o schema da seção 6, papéis e particionamento da auditoria.
2b. ✅ Domínio: validadores, regras, ruleset, decisão e motor (antecipado do Sprint 4).
3. Mecanismo de snapshot (criação de versão, checagens de sanidade, troca de view).
4. Validação dos campos reais do shapefile do SICAR e do acesso ao download (pendências do DATA_PIPELINE, seção 2.3).
