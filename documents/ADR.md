# ADR.md

**AgroCredit Intelligence API — Registro de Decisões de Arquitetura**

- **Versão:** 1.1.0
- **Data:** Outubro de 2026
- **Formato:** MADR

> **Notas da revisão 1.1.0**
> - Removidos artefatos de geração automática (`[cite: …]`, links `google.com/search` no índice).
> - ADR-002: referência normativa genérica (MCR 2-9); removida a alegação de que `&&` é necessário (ST_Intersects já usa o índice); adicionado o `ST_Union` dos embargos.
> - ADR-003: SHA-256 redefinido como proveniência.
> - ADR-005: HMAC reconhecido como pseudonimização (dado continua pessoal).
> - ADR-006: **substituído** pelo ADR-009 (tolerância como parâmetro do tenant, combinação "o que for maior").
> - Novos: ADR-007 (snapshots versionados), ADR-008 (ruleset versionado e avaliação completa), ADR-009 (tolerância).

## Índice

| ID | Título | Status |
| :-- | :-- | :-- |
| ADR-001 | ETL com DuckDB e Polars em vez de Spark | Aceito |
| ADR-002 | SIRGAS 2000 e cálculo de área em `geography` | Aceito (revisado) |
| ADR-003 | Ingestão em lote com manifesto de proveniência | Aceito (revisado) |
| ADR-004 | asyncpg sem ORM na camada de serving | Aceito |
| ADR-005 | Autenticação, isolamento por tenant e pseudonimização | Aceito (revisado) |
| ADR-006 | Tolerância técnica fixa de sobreposição | **Substituído por ADR-009** |
| ADR-007 | Carga por snapshot versionado | Aceito (novo) |
| ADR-008 | Ruleset versionado e avaliação completa das regras | Aceito (novo) |
| ADR-009 | Tolerância de sobreposição configurável por tenant | Aceito (novo) |

---

## ADR-001: ETL com DuckDB e Polars em vez de Spark

- **Status:** Aceito
- **Contexto:** a ingestão envolve tabelas da RFB com milhões de linhas e shapefiles volumosos, mas o MVP cobre 3 municípios. Um cluster Spark traria custo e complexidade sem benefício nessa escala.
- **Decisão:** DuckDB para agregação in-process (com spill em disco) e Polars para transformação tabular.
- **Consequências:**
  - (+) Roda em um container, sem coordenação distribuída.
  - (+) Leitura colunar de Parquet/CSV maiores que a RAM.
  - (−) Escala vertical apenas. Para a malha nacional, particionar por UF ou migrar o motor.
  - (−) Operações geométricas ficam em Shapely/GeoPandas (Python); acima de escala estadual, vetorizar ou mover para PostGIS/DuckDB spatial.

---

## ADR-002: SIRGAS 2000 e cálculo de área em `geography`

- **Status:** Aceito (revisado)
- **Contexto:** o cálculo de sobreposição entre imóveis e camadas de impedimento (MCR 2-9) é o núcleo do produto. As bases usam projeções distintas, e área calculada em coordenadas planas fora da zona correta distorce o resultado.
- **Decisão:**
  - Todas as camadas armazenadas em EPSG:4674.
  - Áreas em hectares calculadas sempre em `geography`.
  - Antes de medir, unir as geometrias de impedimento que intersectam o imóvel, para não somar a mesma área duas vezes quando embargos se sobrepõem:

```sql
ST_Area(ST_Intersection(c.geometria, ST_Union(e.geometria))::geography) / 10000.0
```

  - O percentual de sobreposição usa a área **geodésica** do imóvel, nunca a área declarada.
- **Consequências:**
  - (+) Precisão métrica sobre o elipsoide.
  - (−) Mais CPU que o cálculo planar. Mitigado pelo índice GiST (que `ST_Intersects` já usa sozinho), `statement_timeout` e testes de carga com geometrias reais.

---

## ADR-003: Ingestão em lote com manifesto de proveniência

- **Status:** Aceito (revisado)
- **Contexto:** portais públicos têm instabilidade e mudanças sem aviso. Consultar em tempo real tornaria a latência imprevisível.
- **Decisão:**
  - Download periódico para a camada Bronze, imutável.
  - Cada arquivo registrado em manifesto com URL, data, tamanho, SHA-256 e data de referência da fonte.
  - O SHA-256 garante **reprodutibilidade e detecção de mudança**. Não é comparado com hash oficial, porque os órgãos não publicam um.
  - Integridade validada por abertura do arquivo, limite de tamanho descompactado e checagem de volume contra a carga anterior.
  - Scraping com quebra de captcha está fora da arquitetura.
- **Consequências:**
  - (+) Latência determinística; a API continua operando se o portal cair.
  - (−) Defasagem de dados. Exige `data_as_of` por fonte na resposta e alertas de freshness (DATA_PIPELINE.md, seção 4).

---

## ADR-004: asyncpg sem ORM na camada de serving

- **Status:** Aceito
- **Contexto:** as queries espaciais (`ST_Intersects`, `ST_Intersection`, `ST_Union`, agregações JSON) são mal expressas por ORMs.
- **Decisão:** asyncpg com SQL explícito e parâmetros posicionais (`$1`, `$2`); validação de entrada e saída com Pydantic v2.
- **Consequências:**
  - (+) Controle total do plano de execução e prevenção de SQL injection por parametrização.
  - (−) SQL manual exige revisão de código e testes de integração com PostGIS real.

---

## ADR-005: Autenticação, isolamento por tenant e pseudonimização

- **Status:** Aceito (revisado)
- **Contexto:** a API processa CPFs cruzados com cadastros sancionatórios (MTE, embargos). Há risco de vazamento em logs e de acesso cruzado entre instituições concorrentes (BOLA/IDOR).
- **Decisão:**
  - JWT assinado com algoritmo assimétrico (RS256 ou EdDSA), validando `iss`, `aud`, `exp`, `nbf`.
  - Toda análise vinculada ao `client_tenant_id` do token; consultas de análises passadas filtram por tenant.
  - CPF nunca em texto claro em logs ou auditoria. A auditoria guarda o documento mascarado e um HMAC-SHA256 com *pepper* em cofre de segredos.
  - **O HMAC é pseudonimização, não anonimização.** O espaço de CPFs é pequeno (cerca de 10⁹), então quem tiver o pepper reverte o hash por força bruta. O dado continua pessoal para fins de LGPD, e o pepper é tratado como segredo crítico.
- **Consequências:**
  - (+) Redução do risco de exposição em logs e de acesso cruzado.
  - (−) Rotação do pepper invalida a busca histórica; exige estratégia de re-hash ou chave versionada (`hmac_key_version` na tabela de auditoria).

---

## ADR-006: Tolerância técnica fixa de sobreposição

- **Status:** **Substituído por ADR-009**
- **Motivo:** a regra "0,01% ou 0,1 ha, o que for menor" não tem base normativa e, com "o menor", fica praticamente em zero para imóveis grandes. A decisão de tolerância pertence à instituição financeira.

---

## ADR-007: Carga por snapshot versionado

- **Status:** Aceito (novo)
- **Contexto:** a carga por upsert (`ON CONFLICT DO UPDATE`) nunca remove registros. Embargos desembargados e CARs excluídos da fonte continuariam ativos, gerando bloqueios indevidos. Também não havia como reproduzir um parecer passado.
- **Decisão:**
  - Cada fonte é carregada como uma versão completa em tabela própria.
  - Publicação por troca atômica de view (`*_current`).
  - Checagens de sanidade (volume, validade geométrica) bloqueiam a publicação.
  - Versões anteriores retidas pelo prazo de auditoria; cada parecer registra as versões usadas.
- **Consequências:**
  - (+) Corrige o problema de registros fantasmas; permite reproduzir pareceres.
  - (−) Mais armazenamento. No escopo do MVP, irrelevante; em escala nacional, mover versões antigas para Parquet frio.

---

## ADR-008: Ruleset versionado e avaliação completa das regras

- **Status:** Aceito (novo)
- **Contexto:** os documentos v1.0.0 citavam a Res. CMN 5.081/2023, revogada pela 5.193/2024 e depois alterada pelas 5.268/2025 e 5.303/2026. Além disso, a árvore de decisão parava no primeiro bloqueio, escondendo outros impedimentos.
- **Decisão:**
  - Regras como dados versionados (`ruleset_version`, vigência de/até), revisadas por jurídico a cada mudança do MCR 2-9.
  - Todas as regras são avaliadas; cada uma devolve `IMPEDIDO`, `ALERTA`, `OK` ou `NAO_VERIFICADO`.
  - Status final derivado (ver PRD, seção 5.1), incluindo `INCONCLUSIVO` e `FORA_DE_COBERTURA`.
- **Consequências:**
  - (+) Mudança normativa vira atualização de regra, não refatoração de contrato.
  - (+) O cliente recebe todos os motivos de uma vez.
  - (−) Exige processo de governança de regras com revisão jurídica.

---

## ADR-009: Tolerância de sobreposição configurável por tenant

- **Status:** Aceito (novo; substitui ADR-006)
- **Contexto:** imprecisão de GPS e de vetorização gera falsas sobreposições de borda. A norma não define tolerância; aplicá-la é decisão de risco de quem concede o crédito.
- **Decisão:**
  - A API devolve sempre os números brutos: área de interseção (ha) e percentual sobre a área geodésica.
  - Tolerância configurada por tenant com dois limites combinados por **"o que for maior"**: `tolerancia_ha` (padrão 0,1 ha) e `tolerancia_pct` (padrão 0,01%).
  - Sobreposição abaixo da tolerância gera `ALERTA`, nunca `OK` silencioso.
  - A tolerância aplicada é registrada em cada parecer e na auditoria.
  - (rev. 1.2.0) A tolerância vale para todas as camadas espaciais (embargos, TI, UC, quilombolas, florestas públicas, PRODES). Nas camadas territoriais cada polígono é classificado isoladamente e prevalece o pior resultado, o que evita somar duas vezes polígonos sobrepostos da mesma camada.
- **Consequências:**
  - (+) Responsabilidade da decisão fica com o controlador; transparência para o titular.
  - (−) Exige monitoramento de alertas de borda para evitar que a tolerância esconda degradação real nas margens de áreas embargadas.
