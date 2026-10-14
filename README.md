# AgroCredit Intelligence API

[![ci](https://github.com/DiogoBispo/AgroCreditIntelligenceAPI/actions/workflows/ci.yml/badge.svg)](https://github.com/DiogoBispo/AgroCreditIntelligenceAPI/actions/workflows/ci.yml)

API que cruza bases públicas brasileiras (SICAR, IBAMA, MTE, Funai, MMA, SFB, INCRA, INPE, RFB) e devolve, para um par
**documento do tomador + código CAR do imóvel**, um relatório de impedimentos socioambientais do crédito rural
previstos na Seção 9 do Capítulo 2 do Manual de Crédito Rural (MCR 2-9).

**A API não decide o crédito.** Ela informa o que as bases públicas mostram, com a data de cada base; a decisão
é da instituição financeira.

> **Estado: MVP técnico.** Escopo de três municípios — Rio Verde (GO), Patrocínio (MG) e Castro (PR). As regras
> em [`rules/mcr-2-9.yaml`](rules/mcr-2-9.yaml) seguem o **texto oficial da Seção 9 do MCR (atualização nº 756,
> 15/05/2026)**, com o item citado em cada resultado, mas são uma leitura técnica **sem parecer jurídico** e
> sem validação contra casos reais rotulados. Use como apoio à análise, não como decisão de crédito.

## Como funciona

```text
 fontes públicas ──► pipeline (Bronze → Gold) ──► PostGIS (versões por fonte) ──► API ──► relatório + auditoria
                      sanitiza geometrias,           views *_current = última          JWT, motor de regras,
                      quarentena, proveniência       versão publicada de cada fonte   resultado por regra
```

- **Cada regra cita o dispositivo do MCR que a fundamenta** (`basis`) e, no motivo, a exceção documental
  cabível (plano de manejo, pertencimento a povo indígena ou quilombola, matrícula, documentos do item 18 etc.),
  que a instituição financeira avalia.
- **Todas as regras são avaliadas** (não para no primeiro impedimento). Cada uma devolve `IMPEDIDO`, `ALERTA`,
  `OK` ou `NAO_VERIFICADO`, e o parecer final é um de: `IMPEDIMENTO_IDENTIFICADO`, `INCONCLUSIVO`,
  `SEM_IMPEDIMENTO_COM_ALERTAS`, `SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS` ou `FORA_DE_COBERTURA`.
- **Base ausente ou desatualizada nunca vira "sem impedimento"**: a regra sai `NAO_VERIFICADO` e, se for
  obrigatória, o parecer sai `INCONCLUSIVO`.
- **Tolerância de sobreposição por tenant** (imprecisão de GPS na borda vira `ALERTA`, não impedimento).
- **Vínculo tomador × imóvel não é verificado** no MVP (`owner_link_verified: false` em toda resposta).
- **Privacidade**: o documento nunca fica em claro em logs nem na auditoria (HMAC com pepper versionado +
  máscara); auditoria somente-inserção, isolada por tenant.

### Bases

| Base | Fonte | Situação |
| :-- | :-- | :-- |
| Imóveis rurais | SICAR (WFS oficial) | ✅ |
| Embargos | IBAMA (CSV diário, lista exigida pelo MCR 2-9 item 10) | ✅ |
| Embargos estaduais (informativo) | MG (IDE-Sisema) | ✅ camada sem atualização desde mai/2026 |
| Cadastro de empregadores (trabalho análogo à escravidão) | MTE (PDF) | ✅ |
| Terras indígenas | Funai, cópia sincronizada pelo IBAMA (PAMGIA) | ✅ o WFS da própria Funai recusa downloads |
| Unidades de conservação | CNUC/MMA | ✅ versão aberta de ago/2025; a de 2026 exige login |
| Florestas públicas | CNFP/SFB | ✅ |
| Territórios quilombolas | INCRA | ✅ |
| Desmatamento | PRODES/INPE (Cerrado, Mata Atlântica) | ✅ |
| CNPJ (situação cadastral, todos os estabelecimentos) | RFB (WebDAV público) | ✅ carregador pronto; primeira carga completa em andamento |
| Embargos ICMBio | PAMGIA | ❌ fonte sem atualização desde 2022 |
| Embargos estaduais de GO e PR | — | ❌ sem base aberta |

Canais, campos, volumes e problemas de qualidade de cada fonte: [FASE0_FONTES.md](documents/FASE0_FONTES.md).

Resultado atual nos 17.228 imóveis do escopo (documento de teste, sem regras por documento): 15.575 sem
impedimento nas bases consultadas, 1.064 com alertas e 589 com impedimento — 266 por CAR cancelado ou suspenso,
16 por embargo do IBAMA e 307 por unidade de conservação (302 na APA da Escarpa Devoniana, em Castro, sujeitos
à exceção do plano de manejo).

## Começando

Requisitos: Docker com Compose e [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 e as dependências).
O pipeline do MTE usa `pdftotext` (`poppler-utils`), já incluído na imagem `tools`.

```bash
make setup      # dependências (inclui o pipeline geoespacial)
make db-up      # PostGIS 16 + 3.4 na porta 5433
make migrate    # aplica migrations/
make pipeline   # baixa as fontes automatizáveis e publica no banco (~2 min)
make run        # API em http://localhost:8000 (OpenAPI em /docs)
```

Consulta, em outro terminal (o tenant precisa estar em `core_geo.tenant_config`):

```bash
docker compose exec db psql -U agrocredit_owner -d agrocredit_db \
  -c "INSERT INTO core_geo.tenant_config (client_tenant_id) VALUES ('coop-demo') ON CONFLICT DO NOTHING"
TOKEN=$(make -s token TENANT=coop-demo)   # token de desenvolvimento (chaves em .secrets/)
curl -s -X POST localhost:8000/v1/credit/check \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"document": "11.222.333/0001-81", "car_code": "MG-3148103-E8213784E21049EEBE438FCB50CD6B2A"}'
```

Resposta (resumida):

```json
{
  "decision": { "credit_status": "IMPEDIMENTO_IDENTIFICADO", "summary": "Impedimento identificado: [R-EMB-GEO] ..." },
  "checks": [
    { "rule_id": "R-EMB-GEO", "result": "IMPEDIDO", "basis": "MCR 2-9 itens 10 a 13",
      "detail": { "overlap_ha": "70.4316", "overlap_pct": "26.5681", "embargoes": [ { "source": "IBAMA", "process_number": "..." } ] } },
    { "rule_id": "R-TI", "result": "OK", "basis": "MCR 2-9 item 7" }
  ],
  "owner_link_verified": false,
  "data_as_of": { "sicar": "2026-10-14", "ibama": "2026-10-13", "mte": "2026-10-06", "funai": "2026-08-11" }
}
```

### Endpoints

| Rota | Descrição |
| :-- | :-- |
| `POST /v1/credit/check` | Relatório de impedimentos para `{document, car_code}` (CPF ou CNPJ, inclusive alfanumérico) |
| `GET /v1/credit/analyses/{id}` | Análise passada do próprio tenant (de outro tenant: 404) |
| `GET /health/liveness`, `GET /health/readiness` | Health checks |
| `GET /metrics` | Métricas Prometheus (token opcional) |

### Em containers

```bash
make up                                          # banco + API (imagem `api`)
make tools CMD="agrocredit.pipeline run ibama"   # pipeline na imagem `tools`
make tools CMD="agrocredit.manutencao fontes"    # manutenção
```

Variáveis de ambiente: [`.env.example`](.env.example).

## Operação

- **Pipeline**: `python -m agrocredit.pipeline run <fonte>` baixa, sanitiza, carrega numa versão nova e publica
  após checagens de sanidade (variação de volume acima de ±20% bloqueia até aprovação manual). Arquivo obtido
  fora do pipeline entra com proveniência: `run cnuc --arquivo X.zip --as-of 2026-07-01`.
- **Manutenção**: `python -m agrocredit.manutencao` — defasagem das bases (código de saída para alerta),
  partições e expurgo da auditoria, arquivamento de versões antigas em Parquet, pedido do titular (LGPD art. 18)
  e **validação cruzada** das sobreposições (`validar-espacial`: recalcula com shapely + pyproj, sem o PostGIS,
  e compara com a API; última execução: 0 divergências em 41.089 comparações).
- **Agendamento**: [`ops/agrocredit.crontab`](ops/agrocredit.crontab), executado por
  [`ops/agendar.sh`](ops/agendar.sh) (sem sobreposição, syslog, alerta opcional).
- **Métricas e alertas sugeridos**, playbooks de incidente e rotação de chaves: [RUNBOOK.md](documents/RUNBOOK.md).

## Desenvolvimento

```bash
make check       # ruff, mypy --strict, bandit, pip-audit e testes (os mesmos gates do CI)
make test-unit   # só os testes sem banco
```

Os testes de integração rodam contra PostGIS real num banco próprio (`agrocredit_test`), recriado a cada
execução: não dependem dos dados carregados no banco de desenvolvimento nem os alteram.

```text
src/agrocredit/
  domain/          regras do MCR 2-9, sem dependência de banco (validadores, regras, ruleset, motor)
  application/     caso de uso de verificação e contratos (DTOs)
  infrastructure/  banco: migrations e repositórios (fatos, tenant, auditoria)
  presentation/    FastAPI: rotas, JWT, rate limit, métricas, tratamento de erros
  pipeline/        ingestão Bronze → Gold, uma fonte por arquivo em pipeline/fontes/
  manutencao/      tarefas operacionais
migrations/        schema SQL (fonte única do schema físico)
rules/             ruleset versionado do MCR 2-9
ops/               agendamento
documents/         PRD, SPEC, ADR, pipeline, RIPD (LGPD), runbook, avaliação de IA (fase 3), fontes
tests/             unit, domain, pipeline, integration (inclui regressões de segurança da API)
```

## Documentação

| Documento | Conteúdo |
| :-- | :-- |
| [PRD](documents/PRD-AgroCreditIntelligenceAPI.md) | Problema, escopo, regras e contrato da API |
| [SPEC-001](documents/SPEC-001.md) | Arquitetura, segurança e estado da implementação |
| [ADR](documents/ADR.md) | Decisões de arquitetura |
| [DATA_PIPELINE](documents/DATA_PIPELINE.md) | Ingestão, sanitização, versionamento e defasagem |
| [FASE0_FONTES](documents/FASE0_FONTES.md) | Levantamento das fontes reais e decisões pendentes |
| [RIPD](documents/RIPD.md) | Relatório de impacto à proteção de dados (LGPD) |
| [RUNBOOK](documents/RUNBOOK.md) | Operação, métricas, incidentes e manutenção |
| [AI_EVALS](documents/AI_EVALS.md) | Fase 3: leitura de matrículas com LLM (não iniciada) |

## O que falta

O projeto usa **apenas dados públicos**, sem parceiros nem credenciais. Dentro dessa restrição:

- **Parecer jurídico** sobre a leitura do MCR 2-9. Destaque: pela letra do item 5, imóvel em APA impede o
  crédito salvo atividade conforme o plano de manejo; o sistema marca `IMPEDIDO` com a exceção no motivo
  (302 imóveis em Castro). Tratar APA como `ALERTA` é uma linha em `rules/mcr-2-9.yaml`.
- **Casos reais rotulados** para medir as metas de concordância do PRD §5.5. A validação cruzada confirma o
  cálculo espacial, não a interpretação da norma.
- **CNUC 2026**: só existe em link do MMA que exige login; quem o obtiver carrega com
  `run cnuc --arquivo X.zip --as-of 2026-07-01`.
- **Embargos do ICMBio e estaduais de GO e PR**: sem fonte pública atual (não são exigidos pelo MCR 2-9, que
  usa a lista do IBAMA).
- **Vínculo tomador × imóvel** (fase 3, leitura de matrícula; ver AI_EVALS).
- **Rate limit compartilhado** (Redis), necessário só com mais de uma instância da API.
