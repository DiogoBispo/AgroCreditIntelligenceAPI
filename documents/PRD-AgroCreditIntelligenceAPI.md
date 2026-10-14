# PRD — AgroCredit Intelligence API (MVP)

**Documento de Requisitos de Produto (Product Requirements Document)**

- **Projeto:** Motor de Consulta Cadastral e Conformidade Socioambiental para Crédito Agrícola
- **Versão:** 1.2.0
- **Status:** Em revisão (substitui v1.1.0)
- **Data:** Outubro de 2026

> **Atualização pós-Fase 0 (13/10/2026)** — ver `FASE0_FONTES.md`
> - Códigos TOM corrigidos com a tabela oficial da RFB (9571, 4961, 7495).
> - R-EMB-SEMGEO: só embargo sem polígono com ponto dentro do imóvel (antes: qualquer um no município).
> - R-EMB-DOC verifica CPF e CNPJ (IBAMA publica sem máscara).
> - R-EMB-EST: em UF cuja fonte não informa a situação do embargo (MG), o resultado máximo é `ALERTA`.
> - Embargos do ICMBio entram em R-EMB-GEO.
> - Bloqueio aberto: não há fonte atual de terras indígenas acessível (R-TI é obrigatória).
>
> **Notas da revisão 1.2.0**
> - Código IBGE de Patrocínio corrigido: **3148103** (3148107 falha no dígito verificador). Códigos TOM a confirmar na Fase 0.
> - CNPJ alfanumérico (RFB, desde jul/2026) suportado já no MVP.
> - Embargo estadual separado do federal (R-EMB-EST), com resultado e defasagem próprios.
> - Coluna "obrigatória" nas regras; R-RFB, R-EMB-DOC e R-EMB-EST não obrigatórias (evita `INCONCLUSIVO` sistemático para PF e estados sem base).
> - Tolerância passa a valer para todas as camadas espaciais, não só embargos.
> - Ruleset executável: `rules/mcr-2-9.yaml` (rascunho, revisão jurídica pendente).
>
> **Notas da revisão 1.1.0**
> - Base normativa atualizada: a Res. CMN 5.081/2023 foi revogada pela Res. CMN 5.193/2024 (vigência 02/01/2025) e a Seção 9 do Cap. 2 do MCR foi alterada pelas Res. CMN 5.268/2025 e 5.303/2026.
> - Motor de regras reescrito: avaliação de todas as regras (sem curto-circuito), CAR suspenso passa a bloquear, novos estados `INCONCLUSIVO` e `FORA_DE_COBERTURA`.
> - Tolerância de sobreposição deixa de ser regra fixa e passa a ser parâmetro por tenant.
> - Limitação explícita: o vínculo entre tomador e imóvel não é verificado no MVP.
> - Contrato da API ganha `ruleset_version` e `data_as_of` por fonte; `requested_amount_brl` removido.
> - Schema físico removido deste documento: a fonte única é a SPEC-001.
> - Métricas de sucesso do MVP adicionadas.

---

## 1. Visão Geral e Problema de Negócio

### 1.1 Contexto

A concessão de crédito rural exige verificar impedimentos sociais, ambientais e climáticos definidos na **Seção 9 do Capítulo 2 do Manual de Crédito Rural (MCR 2-9)**. A redação vigente decorre da **Res. CMN 5.193/2024**, alterada pelas **Res. CMN 5.268/2025** e **5.303/2026**.

A norma está em movimento: há projeto de decreto legislativo no Senado (PDL 262/2026) para sustar as Resoluções 5.193 e 5.268. Por isso o produto **não amarra regras a um número de resolução**. As regras são versionadas (`ruleset_version`) com data de vigência, e cada parecer registra qual versão aplicou.

Instituições financeiras, cooperativas e revendas de insumos ainda fazem essa verificação de forma manual e fragmentada.

### 1.2 O Problema

- **Fragmentação:** dados fiscais (RFB), ambientais (SICAR/MMA), territoriais (FUNAI, ICMBio, INCRA, SFB) e sancionatórios (IBAMA, órgãos estaduais, MTE) estão em plataformas e formatos distintos.
- **Dados inconsistentes:** erros de topologia, projeções divergentes, chaves cadastrais sem padronização (zeros truncados, códigos TOM × IBGE).
- **Custo de risco:** crédito concedido a imóvel com impedimento gera desclassificação da operação, perda de funding e risco reputacional.

### 1.3 A Solução

API REST que consolida e cruza bases públicas e devolve um **relatório de verificação de impedimentos**, com latência abaixo de 1 segundo, para esteiras de análise de crédito rural.

**O que a API não faz:** não decide o crédito. O parecer é insumo para a decisão da instituição financeira, que continua sendo a controladora e a responsável pela concessão.

---

## 2. Escopo do MVP

Escopo restrito a 3 municípios, para validação técnica:

| Município | UF | Cód. IBGE | Cód. RFB (TOM) | Perfil | Hipótese testada |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Rio Verde | GO | 5218805 | 9571 | Grãos e agroindústria | Grandes polígonos, alta densidade de CNPJs agro |
| Patrocínio | MG | 3148103 | 4961 | Café | Mosaico de propriedades médias, transição PF/PJ |
| Castro | PR | 4104907 | 7495 | Leite e cooperativismo | Múltiplas glebas contíguas |

### 2.1 Bases cobertas no MVP

| Impedimento (MCR 2-9) | Fonte | Tipo de checagem | MVP |
| :-- | :-- | :-- | :-- |
| CAR inexistente, cancelado ou suspenso | SICAR | Status do cadastro | Sim |
| Embargo ambiental federal | IBAMA | Geometria **e** documento do autuado | Sim |
| Embargo ambiental estadual | SEMAD-GO, SEMAD-MG, IAT-PR | Geometria e/ou documento | Sim, se a base for publicada; senão o parecer declara a lacuna |
| Cadastro de empregadores (trabalho análogo à escravidão) | MTE | Documento (CPF, CNPJ e raiz do CNPJ) | Sim |
| Terras indígenas | FUNAI | Interseção espacial | Sim |
| Unidades de Conservação | ICMBio / CNUC | Interseção espacial | Sim |
| Territórios quilombolas titulados | INCRA | Interseção espacial | Sim |
| Florestas Públicas Tipo B não destinadas | SFB / CNFP | Interseção espacial | Sim |
| Supressão de vegetação nativa após 31/07/2019 | PRODES/INPE (via MMA) | Interseção espacial | Sim (alerta; ver 5.2) |

### 2.2 Fora do escopo do MVP

- Malha fundiária nacional completa.
- Barramentos estaduais isolados fora do SICAR federal (ex.: SIMCAR/MT).
- Scraping em tempo real ou quebra de captcha.
- **Verificação do vínculo entre o tomador e o imóvel** (titularidade, posse, arrendamento). A base pública do SICAR não traz o CPF do titular. Ver seção 5.4.
- Leitura de certidões cartoriais com LLM (Fase 3, ver AI_EVALS.md).

---

## 3. Arquitetura de Dados e Stack

```
[Fontes públicas: RFB, SICAR, IBAMA, órgãos estaduais, MTE, FUNAI, ICMBio, INCRA, SFB, PRODES]
                      │
                      ▼
          [Bronze: arquivos brutos + manifesto de proveniência]
                      │
                      ▼  (DuckDB + Polars + Shapely)
          [Silver: limpeza, sanitização, quarentena]
                      │
                      ▼
          [Gold: PostgreSQL 16 + PostGIS 3.4, snapshots versionados]
                      │
                      ▼
          [FastAPI + asyncpg]
                      │
                      ▼
          [Esteira de crédito do cliente]
```

- **Linguagem:** Python 3.12+
- **ETL:** DuckDB e Polars
- **Geoespacial:** Shapely 2.x e GeoPandas
- **Armazenamento:** PostgreSQL 16 + PostGIS 3.4 (Docker)
- **API:** FastAPI, Pydantic v2, asyncpg

Detalhes de pipeline: DATA_PIPELINE.md. Schema físico e contratos: SPEC-001.md.

---

## 4. Requisitos de Sanitização e Governança de Dados

### 4.1 Dados cadastrais (RFB)

- Documento armazenado como par **(`tipo_documento`, `documento`)**: CPF com 11 dígitos, CNPJ com 14. Nunca usar LPAD de CPF para 14 dígitos (colide com CNPJs iniciados por `000`).
- Validação de dígitos verificadores (módulo 11); registros inválidos vão para quarentena.
- **CNPJ alfanumérico:** desde julho de 2026 a RFB emite CNPJ com 12 caracteres alfanuméricos + 2 dígitos verificadores numéricos (cada caractere vale `ord(c) − 48` no cálculo). Entrada, banco e pipeline aceitam os dois formatos.
- De-para TOM (4 dígitos) → IBGE (7 dígitos) obrigatório.
- Decodificação ISO-8859-1 → UTF-8, TRIM e caixa alta canônica.
- **Limitação conhecida:** a base aberta da RFB cobre só CNPJ. Para CPF não há fonte de situação cadastral no MVP; o campo volta como `NAO_DISPONIVEL`.
- A carga de CNPJ **não** é filtrada por município da sede: empresas sediadas fora dos 3 municípios podem operar imóveis neles.

### 4.2 Dados geoespaciais

- Reprojeção canônica para **SIRGAS 2000 (EPSG:4674)**.
- Remoção de coordenada Z (`force_2d`).
- Correção topológica com `make_valid`, **com guarda de área**: se a área mudar mais que 1% após o reparo, o registro vai para quarentena.
- Homogeneização para MultiPolygon.
- **Sem simplificação de geometria** nas camadas usadas para compliance. Simplificação só é permitida em cópias destinadas a exibição.
- Áreas sempre calculadas em `geography` (hectares sobre o elipsoide).
- Camadas de impedimento (embargos, TI, UC etc.) **não** são filtradas por município: são filtradas pelo envelope dos CARs do escopo mais uma margem, para capturar polígonos que cruzam a divisa.

### 4.3 Versionamento de dados

- Cada carga gera um `dataset_version` por fonte, com data de referência da fonte (`source_as_of`) e data de carga.
- A carga substitui o snapshot inteiro (registros que saem da fonte saem da base operacional). Embargos desembargados e CARs removidos **não podem** permanecer ativos.
- Snapshots anteriores são mantidos para reproduzir pareceres passados em auditoria.

---

## 5. Regras de Negócio e Matriz de Decisão

### 5.1 Princípios

1. **Todas as regras são avaliadas**, sem parar no primeiro bloqueio. O parecer lista todos os motivos encontrados.
2. Cada regra devolve um de quatro resultados: `IMPEDIDO`, `ALERTA`, `OK` ou `NAO_VERIFICADO` (dado ausente, fonte desatualizada ou fora de cobertura).
3. O status final é derivado dos resultados:

| Condição | `credit_status` |
| :-- | :-- |
| CAR fora dos municípios cobertos | `FORA_DE_COBERTURA` |
| Ao menos uma regra `IMPEDIDO` | `IMPEDIMENTO_IDENTIFICADO` |
| Nenhuma `IMPEDIDO` e ao menos uma regra obrigatória `NAO_VERIFICADO` | `INCONCLUSIVO` |
| Nenhuma `IMPEDIDO`, todas verificadas, com `ALERTA` | `SEM_IMPEDIMENTO_COM_ALERTAS` |
| Todas `OK` | `SEM_IMPEDIMENTO_NAS_BASES_CONSULTADAS` |

4. A linguagem do parecer é sempre **"nenhum impedimento encontrado nas bases X, Y, Z, com dados de [data]"**. Nunca "imóvel regular perante o BACEN".

### 5.2 Regras

| ID | Regra | Resultado | Obrigatória* |
| :-- | :-- | :-- | :-- |
| R-CAR | CAR não encontrado, `CANCELADO` ou `SUSPENSO` | `IMPEDIDO` | Sim |
| R-CAR (pendente) | CAR `PENDENTE` | `OK` com registro do status (o MCR não veda CAR pendente) | — |
| R-MTE | Documento, ou raiz do CNPJ, no cadastro de empregadores do MTE | `IMPEDIDO` | Sim |
| R-EMB-GEO | Interseção do CAR com embargo **federal (IBAMA e ICMBio)** vigente acima da tolerância; abaixo → `ALERTA` | `IMPEDIDO` | Sim |
| R-EMB-EST | Interseção com embargo **estadual** vigente, mesma lógica; base ausente → `NAO_VERIFICADO`; fonte sem situação do embargo (MG) → no máximo `ALERTA` | `IMPEDIDO` | Não |
| R-EMB-DOC | CPF ou CNPJ do tomador (ou outra filial da mesma empresa) como autuado em embargo vigente; se todos os achados vêm de fonte sem situação (MG), no máximo `ALERTA` | `IMPEDIDO` | Não (pendente jurídico: embargo de atividade) |
| R-EMB-SEMGEO | Embargo vigente sem polígono cujo ponto de referência cai dentro do imóvel | `ALERTA` | Não |
| R-TI | Interseção com terra indígena nas fases previstas no MCR; outras fases → `ALERTA` | `IMPEDIDO` | Sim |
| R-UC | Interseção com UC nas categorias previstas no MCR; outras categorias → `ALERTA` | `IMPEDIDO` | Sim |
| R-QUI | Interseção com território quilombola titulado (fases TITULADO e TITULO_PARCIAL); demais fases → `ALERTA` | `IMPEDIDO` | Sim |
| R-FPND | Interseção com Floresta Pública Tipo B (exceções do MCR exigem documento) | `IMPEDIDO` | Sim |
| R-PRODES | Supressão de vegetação nativa após 31/07/2019 | `ALERTA` (nunca `IMPEDIDO`) | Não |
| R-RFB | CNPJ com situação diferente de `ATIVA`; CPF → `NAO_VERIFICADO` (sem fonte) | `ALERTA` | Não |

\* Obrigatória: `NAO_VERIFICADO` nesta regra torna o parecer `INCONCLUSIVO`. Valores atuais são rascunho técnico em `rules/mcr-2-9.yaml`, **pendentes de revisão jurídica**.

Em todas as regras espaciais, cada polígono intersectado é classificado com a tolerância do tenant (seção 5.3) e prevalece o pior resultado; nos embargos, mede-se a área da união das interseções.

O detalhamento das exceções de cada regra (ex.: categorias de UC, módulos fiscais, datas de vigência) é mantido no repositório de regras versionado, revisado por jurídico a cada alteração do MCR.

### 5.3 Tolerância de sobreposição

- **Não existe tolerância na norma.** Qualquer margem é uma decisão de risco da instituição financeira.
- A API devolve sempre os números brutos (área de interseção em ha e percentual sobre a área geodésica do imóvel).
- Vale para **todas as camadas espaciais** (embargos, TI, UC, quilombolas, florestas públicas, PRODES): imprecisão de borda afeta todas.
- A tolerância é um parâmetro por tenant, com dois limites combinados por **"o que for maior"**: `tolerancia_ha` (padrão 0,1 ha) e `tolerancia_pct` (padrão 0,01%). Sobreposição abaixo do limite gera `ALERTA`, nunca `OK` silencioso.
- O padrão é configurado no contrato com o cliente e registrado em cada parecer.

### 5.4 Limitação: vínculo tomador × imóvel

A API não verifica se o tomador é titular, possuidor ou arrendatário do imóvel informado. Sem essa verificação, um tomador com impedimento pode informar o CAR de um imóvel limpo. Por isso:

- Todo parecer traz `owner_link_verified: false` e um aviso explícito.
- A verificação de vínculo é responsabilidade da instituição financeira até a Fase 3 (matrícula/recibo do CAR).

### 5.5 Métricas de sucesso do MVP

| Métrica | Meta | Como medir |
| :-- | :-- | :-- |
| Concordância com análise humana | ≥ 95% no status final | Conjunto de 100 casos reais rotulados por analistas, estratificado pelos 3 municípios |
| Falso negativo de impedimento | 0 casos no conjunto rotulado | Idem |
| Falso positivo de impedimento | ≤ 3% | Idem |
| Latência | p95 < 800 ms | Teste de carga com geometrias reais |
| Pareceres `INCONCLUSIVO` | Medido e reportado (sem meta) | Indica lacunas de dados a priorizar |

---

## 6. Especificação da API

### 6.1 Endpoint

- **Rota:** `POST /v1/credit/check`
- **Content-Type:** `application/json`
- **Autenticação:** `Authorization: Bearer <JWT>`

### 6.2 Requisição

```json
{
  "document": "12345678000195",
  "car_code": "GO-5218805-72819485728194857281948572819485"
}
```

`requested_amount_brl` foi removido: não é usado em nenhuma regra e violaria o princípio de minimização (LGPD, art. 6º, III). O código CAR é aceito com ou sem pontos separadores e normalizado.

### 6.3 Resposta (sem impedimento, com alerta)

```json
{
  "analysis_id": "a9c2f6d2-3c22-4ef8-a15e-85c13b192801",
  "timestamp": "2026-10-13T23:00:00Z",
  "ruleset_version": "MCR-2-9@2026-05-12",
  "input": {
    "document_masked": "12.345.678/****-**",
    "car_code": "GO-5218805-72819485728194857281948572819485"
  },
  "property": {
    "car_status": "ATIVO",
    "municipio": "Rio Verde",
    "uf": "GO",
    "declared_area_ha": 450.25,
    "geodesic_area_ha": 449.87
  },
  "cadastral_status": {
    "rfb_status": "ATIVA",
    "cnae_principal": "0111301"
  },
  "checks": [
    { "rule_id": "R-CAR", "result": "OK" },
    { "rule_id": "R-MTE", "result": "OK" },
    { "rule_id": "R-EMB-GEO", "result": "ALERTA",
      "detail": { "overlap_ha": 0.04, "overlap_pct": 0.009, "tolerance_applied": { "ha": 0.1, "pct": 0.01 } } },
    { "rule_id": "R-TI", "result": "OK" },
    { "rule_id": "R-UC", "result": "OK" },
    { "rule_id": "R-QUI", "result": "OK" },
    { "rule_id": "R-FPND", "result": "OK" },
    { "rule_id": "R-PRODES", "result": "OK" }
  ],
  "owner_link_verified": false,
  "data_as_of": {
    "sicar": "2026-10-01", "ibama_embargos": "2026-10-11", "mte": "2026-09-30",
    "funai": "2026-09-15", "icmbio": "2026-09-15", "incra_quilombolas": "2026-09-15",
    "sfb_cnfp": "2026-08-31", "prodes": "2025-12-31", "rfb_cnpj": "2026-09-20"
  },
  "data_staleness_warning": false,
  "decision": {
    "credit_status": "SEM_IMPEDIMENTO_COM_ALERTAS",
    "summary": "Nenhum impedimento encontrado nas bases consultadas, com dados nas datas indicadas. Sobreposição de 0,04 ha com embargo, abaixo da tolerância configurada. Vínculo do tomador com o imóvel não verificado."
  }
}
```

### 6.4 Resposta (impedimento)

```json
{
  "analysis_id": "b3e1f8a1-4d11-4fa2-9b2a-71b12c184712",
  "timestamp": "2026-10-13T23:05:00Z",
  "ruleset_version": "MCR-2-9@2026-05-12",
  "checks": [
    { "rule_id": "R-CAR", "result": "OK" },
    { "rule_id": "R-MTE", "result": "OK" },
    { "rule_id": "R-EMB-GEO", "result": "IMPEDIDO",
      "detail": {
        "overlap_ha": 14.50, "overlap_pct": 12.08,
        "embargoes": [
          { "source": "IBAMA", "process_number": "02001.001234/2023-45", "infraction_date": "2024-03-15", "overlap_ha": 14.50 }
        ]
      } }
  ],
  "decision": {
    "credit_status": "IMPEDIMENTO_IDENTIFICADO",
    "summary": "Sobreposição de 14,50 ha (12,08%) com embargo vigente do IBAMA, processo 02001.001234/2023-45 (MCR 2-9)."
  }
}
```

(Campos comuns omitidos por brevidade.)

---

## 7. Modelagem Física de Dados

Removida deste documento para evitar divergência. **Fonte única: SPEC-001.md, seção 6.**

---

## 8. Cronograma (sprints de 1 semana)

| Sprint | Entrega |
| :-- | :-- |
| 0 | Revisão jurídica do ruleset MCR 2-9 vigente; montagem do conjunto rotulado de 100 casos com analistas |
| 1 | Docker, schemas PostGIS, de-para de municípios, modelo de snapshots versionados |
| 2 | Ingestão SICAR, IBAMA, MTE, RFB com quarentena e manifesto de proveniência |
| 3 | Ingestão das camadas territoriais (FUNAI, ICMBio, INCRA, SFB, PRODES) e embargos estaduais disponíveis |
| 4 | Motor de regras e queries espaciais; testes contra o conjunto rotulado |
| 5 | API FastAPI, contratos, testes de segurança e de carga |

---

## 9. Riscos e Mitigações

| Risco | Severidade | Probabilidade | Mitigação |
| :-- | :-- | :-- | :-- |
| Mudança normativa (MCR 2-9 alterado ou sustado) | Alta | Alta | Ruleset versionado com vigência; revisão jurídica a cada resolução |
| Fraude por CAR de terceiro | Alta | Média | Aviso explícito `owner_link_verified: false`; verificação pela IF; Fase 3 |
| Embargo sem geometria ou com documento mascarado | Alta | Alta | Regra R-EMB-SEMGEO gera alerta para verificação manual |
| Embargo desembargado mantido na base | Alta | Média | Carga por snapshot completo, nunca upsert incremental |
| Bases estaduais indisponíveis | Média | Alta | Parecer declara a lacuna e sai `INCONCLUSIVO` quando a regra é obrigatória |
| Download do SICAR exigir captcha | Média | Média | Validar antes do Sprint 2; buscar canal oficial de dados abertos |
| Geometria corrompida | Alta | Média | `make_valid` com guarda de área e quarentena |
| Timeout espacial | Alta | Média | Índices GiST, `statement_timeout`, testes de carga com geometrias reais |
