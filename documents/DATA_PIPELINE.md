# DATA_PIPELINE.md

**AgroCredit Intelligence API — Engenharia de Dados e Pipeline ETL**

- **Versão:** 1.2.0
- **Status:** Em revisão (substitui v1.1.0)
- **Data:** Outubro de 2026
- **Stack:** Python 3.12+, DuckDB, Polars, Shapely 2.x, GeoPandas, PostgreSQL 16 / PostGIS 3.4
- **Escopo MVP:** Rio Verde (GO), Patrocínio (MG), Castro (PR)

> **Terras indígenas e RFB (14/10/2026)**:
> - **TI**: `pipeline/fontes/territoriais.py` (`run ti`), cópia da Funai sincronizada pelo IBAMA (PAMGIA), ArcGIS REST por envelope. Data de referência = `dt_sync` da camada inteira. O download falha se o ArcGIS responder erro ou se a camada inteira vier vazia; um envelope sem TI é publicado vazio (`permite_vazia`), porque é resultado legítimo.
> - **RFB**: `pipeline/fontes/rfb.py` (`run rfb`), todos os estabelecimentos via WebDAV público da RFB, só 4 colunas (CNPJ, situação, CNAE, município), todas as situações. Transformação em DuckDB a partir de streaming latin-1 → UTF-8; carga por `COPY` de CSV com PK e índices criados depois da cópia.
> - Primeira carga completa (edição 2026-10): 73.809.379 estabelecimentos (28,4 mi ativos, 34,8 mi baixados, 10,2 mi inaptos), sem quarentena, em 31 min; tabela de 9,2 GB; consulta por CNPJ em 0,06 ms pela PK. Já há 1 CNPJ alfanumérico na base. Memória do DuckDB limitada a 4 GB, com transbordo para disco (sem limite, o pico foi ~10 GB).
> - Validação cruzada após cada carga: `python -m agrocredit.manutencao validar-espacial`.
>
> **Camadas territoriais e embargos de MG (14/10/2026)** — `pipeline/fontes/territoriais.py` e `embargos_mg.py`. Canais e achados em `FASE0_FONTES.md` §9b. Destaques:
> - Limites de defasagem ajustados à cadência real de publicação (CNUC 240 dias, CNFP 395 dias); antes, 60 dias faria toda base semestral ou anual nascer "desatualizada".
> - Referência da guarda de reparo é validada por fonte antes do uso: confiável no SICAR e no IBAMA; **descartada no PRODES** (`area_km` em outra projeção).
> - Ingestão manual com proveniência: `run <fonte> --arquivo X --as-of AAAA-MM-DD` (link com login, resposta via LAI).
> - Reprocessar um Bronze já carregado gera nova versão no Gold.
> - Carga sem novidade é descartada: se os arquivos baixados têm o mesmo SHA-256 da versão em uso e a data de referência vem do conteúdo (MTE, IBAMA, CNUC, CNFP, PRODES, MG), não há versão nova. SICAR e INCRA sempre recarregam (a data deles é a do download).
>
> **Implementação (Fase 3, 14/10/2026)** — código em `src/agrocredit/pipeline/`, que prevalece sobre os trechos de referência abaixo:
> - **Uma tabela por (base, fonte, versão)** (`embargos_ibama_v<versao>`); a view `<base>_current` é a união da última versão publicada de cada fonte, para que republicar o IBAMA não apague embargos do ICMBio ou dos estados.
> - **Reparo com `make_valid(method="structure")`**: o método padrão ("linework") transformava sobreposição entre partes em buraco. Com ele, 2 imóveis reais de Rio Verde ganhavam +26% e +68% de área; com "structure", a diferença para a área declarada é de 0,08%.
> - **Guarda de área contra a área declarada pela fonte** (SICAR `area`, IBAMA `QTD_AREA_EMBARGADA`), medida em geodésico (GRS80). Sem referência, só vale para polígono simples. Nos polígonos válidos do IBAMA, a área declarada diverge mais de 1% em só 0,4% dos casos, então é uma referência confiável.
> - **Imóvel com geometria irrecuperável fica na base sem geometria** (`problema_geometria`): R-CAR avalia o status e as regras espaciais saem `NAO_VERIFICADO`. Antes, a quarentena faria a API dizer "CAR não encontrado" (`IMPEDIDO`).
> - **IBAMA nacional** (R-EMB-DOC precisa de embargos do tomador em qualquer UF). Polígono descartado não descarta o embargo: ele fica com ponto e documento.
> - **MTE sem nome do empregador** (minimização).
> - **Silver em memória**: Bronze é imutável e a transformação é determinística; `run <fonte> --versao V` reprocessa.
> - Primeira carga real: SICAR 17.250 imóveis; IBAMA 108.610 embargos (3.327 registros em quarentena, a maioria sem `SEQ_TAD`); MTE 498 empregadores.
>
> **Atualização pós-Fase 0 (13/10/2026)** — canais, campos e volumes confirmados em `FASE0_FONTES.md`, que prevalece sobre as suposições abaixo onde houver conflito. Principais mudanças:
> - **SICAR:** WFS oficial `geoserver.car.gov.br/geoserver/sicar/wfs` (sem captcha). Status em `status_imovel` (AT, PE, SU, CA); município em `cod_municipio_ibge`. **Não** derivar o município do `cod_imovel`: 0,1–0,4% divergem. Filtro de carga: `cod_municipio_ibge IN (escopo) OR cod_imovel LIKE 'UF-IBGE-%'`.
> - **IBAMA:** CSV diário no blob oficial, com WKT em `GEOM_AREA_EMBARGADA`, ponto em `NUM_LONGITUDE_TAD`/`NUM_LATITUDE_TAD` e CPF/CNPJ sem máscara. Vigente = `DES_STATUS_FORMULARIO='Lavrado'` e `SIT_CANCELADO='N'` e `SIT_DESEMBARGO` nulo. Linhas sem `SEQ_TAD` → quarentena; datas fora de 1980..hoje → nulas, com motivo registrado.
> - **ICMBio:** embargos via PAMGIA (`adm_embargo_icmbio_a`), órgão `ICMBIO`.
> - **MTE:** somente PDF; extração por `pdftotext -layout`; publicação bloqueada se nº de documentos ≠ maior ID.
> - **RFB:** WebDAV público do Nextcloud (`arquivos.receitafederal.gov.br/public.php/webdav/<AAAA-MM>/`); ISO-8859-1 convertido com `iconv` antes do DuckDB; carregar todas as situações, não só ativas.
> - **PRODES:** `image_date` → `atributos.data_deteccao`. **TI:** fases normalizadas (`Encaminhada RI` → `ENCAMINHADA_RI`).
> - **MG:** WFS IDE-Sisema `IDE:ide_2304_mg_areas_embargadas_sisema_pol` (sem situação; CPF mascarado). GO: sem base aberta. PR: não confirmada.
>
> **Notas da revisão 1.2.0**
> - Código IBGE de Patrocínio corrigido para **3148103**. Com 3148107, o filtro do script descartava todos os imóveis de Patrocínio sem passar pela quarentena.
> - Filtro dos embargos: com polígono, pelo envelope; sem polígono (ponto ou nenhuma), pelo município. Antes, o filtro por envelope descartava justamente os embargos que a regra R-EMB-SEMGEO precisa.
> - Embargos de origem PONTO: ponto guardado em coluna própria, sem buffer inventado; tratados por R-EMB-SEMGEO.
> - Tabela de sócios removida (nenhuma regra a usa; o CPF de sócio PF vem mascarado na fonte).
> - Área geodésica do CAR calculada pelo banco (coluna gerada); removido o `UPDATE` pós-carga, que conflitava com `NOT NULL`.
> - CNPJ alfanumérico aceito na carga da RFB.
>
> **Notas da revisão 1.1.0**
> - Carga por **snapshot versionado** substitui o upsert (o upsert mantinha embargos desembargados e CARs removidos).
> - Removida a simplificação de geometria nas camadas de compliance.
> - `make_valid` passa a ter guarda de área; falhas vão para quarentena com motivo (antes eram descartadas em silêncio).
> - Script de referência reescrito: reprojeção real, achatamento de coleções, quarentena, campos do SICAR a validar.
> - SHA-256 redefinido como **proveniência**, não como validação contra "manifesto oficial" (os órgãos não publicam hashes).
> - Documento armazenado como (`tipo_documento`, `documento`); fim do LPAD de CPF para 14 dígitos.
> - Novas fontes: camadas territoriais (FUNAI, ICMBio, INCRA, SFB), PRODES e embargos estaduais.
> - Limiar de alerta de defasagem unificado com o RUNBOOK.

---

## 1. Arquitetura Medallion

```
BRONZE  Arquivos brutos, imutáveis, um diretório por (fonte, data de download)
        + manifesto de proveniência (URL, data, tamanho, SHA-256, data de referência da fonte)
   │
   ▼
SILVER  Parquet sanitizado por fonte e versão
        - Documentos: tipo + dígitos, módulo 11
        - Geometrias: EPSG:4674, 2D, válidas, MultiPolygon, guarda de área
        - Quarentena com motivo para tudo que for rejeitado
   │
   ▼
GOLD    PostGIS: uma versão por fonte, publicada por troca atômica
        - Tabelas versionadas + views "current" consumidas pela API
        - Versões anteriores retidas para auditoria
```

---

## 2. Contratos de Ingestão

### 2.1 Municípios (IBGE × TOM)

| Campo | Tipo | Regra |
| :-- | :-- | :-- |
| `codigo_ibge` | INT PK | 7 dígitos |
| `codigo_tom` | INT UNIQUE | 4 dígitos (RFB) |
| `nome_municipio` | VARCHAR(100) | Caixa alta, sem acento (NFKD) |
| `uf` | CHAR(2) | Caixa alta |

### 2.2 CNPJ (RFB)

- **Origem:** arquivos abertos da RFB (Empresas, Estabelecimentos, Sócios).
- **Filtro:** CNAE do grupo 01 (atividade principal **ou** secundária). **Sem filtro por município da sede**: empresas sediadas em outros municípios podem operar imóveis no escopo.
- **Sócios:** não carregados (nenhuma regra os usa e o CPF de sócio PF vem mascarado na fonte; carregar violaria a minimização).
- **Decisão pendente (Fase 0):** o filtro por CNAE 01 deixa de fora tomadores PJ com CNAE de outro grupo (ex.: holdings, tradings), que voltariam `NAO_VERIFICADO` em R-RFB. Medir o volume da carga completa de estabelecimentos ativos e decidir.

| Campo | Regra |
| :-- | :-- |
| `cnpj` | 14 caracteres (numérico ou alfanumérico), DV módulo 11; inválido → quarentena |
| `cnpj_raiz` | 8 primeiros dígitos (para cruzamento matriz/filial) |
| `razao_social` | TRIM, sem caracteres de controle, UTF-8 |
| `situacao_cadastral` | Enum: ATIVA, SUSPENSA, INAPTA, BAIXADA, NULA |
| `cnae_fiscal_principal` | 7 dígitos, sem máscara |
| `codigo_ibge` | Via TOM; TOM desconhecido → quarentena |

**Limitação:** não há fonte aberta de situação cadastral de CPF. A API devolve `NAO_DISPONIVEL` para pessoa física.

### 2.3 SICAR (Imóveis rurais)

- **Origem:** shapefiles/GeoPackage do SICAR federal.
- **Pendência antes do Sprint 2:** confirmar se o download por município exige captcha. Se exigir, buscar canal oficial (dados abertos do SFB/MMA) antes de qualquer automação. Quebra de captcha está fora do escopo.
- **Pendência de nomes de campos:** validar no arquivo real. A expectativa é que o status venha em `ind_status` (AT, PE, SU, CA) e que `des_condic` seja a condição de análise, **não** o status. O código do município deve ser derivado do próprio `cod_imovel` (segmento de 7 dígitos), não de uma coluna presumida.

| Campo | Regra |
| :-- | :-- |
| `codigo_car` | Normalizar (remover pontos, caixa alta); validar `^[A-Z]{2}-\d{7}-[A-F0-9]{32}$` |
| `codigo_ibge` | Extraído do `codigo_car`; deve bater com a UF |
| `area_declarada_ha` | Valor declarado; nulo permanece nulo (não vira 0) |
| `area_geodesica_ha` | Calculada no PostGIS em `geography` |
| `status_cadastro` | Enum: ATIVO, PENDENTE, SUSPENSO, CANCELADO; valor desconhecido ou nulo → quarentena (não vira PENDENTE) |
| `geometria` | Protocolo da seção 3 |

### 2.4 Embargos IBAMA

| Campo | Regra |
| :-- | :-- |
| `id_embargo` | Identificador da fonte (não serial interno); chave `(orgao, id_embargo)` |
| `numero_processo` | Texto |
| `tipo_documento` / `documento_autuado` | Preenchido apenas se a fonte trouxer o documento sem máscara |
| `documento_mascarado` | Valor como publicado (PF costuma vir mascarado) |
| `situacao` | Vigente / desembargado; só vigentes entram na view `current` |
| `data_infracao`, `data_embargo` | ISO-8601 |
| `geometria` | **Pode ser nula**; embargos sem geometria são mantidos (regra R-EMB-SEMGEO) |
| `tipo_geom_origem` | POLIGONO, PONTO ou NENHUMA. Só POLIGONO preenche `geometria`; PONTO preenche `ponto` |

Filtro: embargos **com polígono** pelo envelope dos CARs do escopo + margem de 5 km (nunca por código de município, para pegar polígonos que cruzam a divisa); embargos **sem polígono** (ponto ou nenhuma) pelo `codigo_ibge` dos municípios do escopo, para alimentar R-EMB-SEMGEO.

### 2.5 Embargos estaduais

Mesmo contrato do 2.4, com `orgao` (SEMAD-GO, SEMAD-MG, IAT-PR). Para cada estado, registrar no manifesto se a base existe e é publicada. Se não existir, o motor de regras marca a checagem como `NAO_VERIFICADO`.

### 2.6 MTE (Cadastro de Empregadores)

| Campo | Regra |
| :-- | :-- |
| `tipo_documento` / `documento` | CPF 11 dígitos ou CNPJ 14 dígitos |
| `cnpj_raiz` | Para CNPJ |
| `nome_infrator` | Texto |
| `data_inclusao` | Data da inclusão |
| `ativo` | Presença na última publicação; registros que saem da lista ficam inativos |

### 2.7 Camadas territoriais

| Camada | Fonte | Atributos mínimos |
| :-- | :-- | :-- |
| Terras indígenas | FUNAI | nome, fase do processo |
| Unidades de Conservação | ICMBio / CNUC | nome, categoria, esfera, plano de manejo |
| Territórios quilombolas | INCRA | nome, situação da titulação |
| Florestas Públicas | SFB / CNFP | tipo (A/B), destinação |
| Supressão de vegetação | PRODES/INPE via MMA | ano, área, data de detecção |

Mesmo protocolo geométrico da seção 3 e mesmo filtro por envelope.

---

## 3. Protocolo Geométrico

```
[1] Ler com CRS de origem (detectar SIRGAS 2000, SAD69, WGS84; CRS ausente → quarentena)
[2] Reprojetar para EPSG:4674
[3] force_2d
[4] Se inválida: make_valid
      → extrair apenas partes poligonais (achatando coleções aninhadas)
      → se vazio: quarentena ("geometria colapsada")
      → se |área_depois − área_antes| / área_antes > 1%: quarentena ("reparo alterou área")
[5] Homogeneizar para MultiPolygon
[6] (Removido) Sem simplificação nas camadas de compliance
```

Simplificação (se necessária para mapas) é feita em tabela separada, nunca usada pelo motor de regras.

---

## 4. Freshness e Alertas

| Base | Ingestão | Defasagem máxima (alerta P2) | Defasagem crítica (P1 + `data_staleness_warning`) |
| :-- | :-- | :-- | :-- |
| MTE | A cada publicação (checagem diária) | 7 dias após nova publicação | 30 dias |
| Embargos IBAMA | Semanal | 7 dias | 10 dias |
| Embargos estaduais | Semanal ou conforme publicação | 7 dias | 14 dias |
| SICAR | Quinzenal | 15 dias | 30 dias |
| RFB CNPJ | Mensal | 35 dias | 45 dias |
| FUNAI (cópia sincronizada no PAMGIA) | Semanal | 90 dias | 180 dias |
| INCRA | Semanal | 35 dias | 60 dias |
| CNUC (UCs, MMA) | Mensal (publicação semestral, versão aberta com atraso) | 240 dias | 450 dias |
| CNFP (SFB) | Anual | — | 395 dias |
| PRODES | Conforme publicação anual | — | 13 meses |
| IBGE | Anual | — | — |

Estes limiares são a fonte única; o RUNBOOK referencia esta tabela.

A API expõe `data_as_of` por fonte em toda resposta. Se uma fonte obrigatória passar do limiar crítico, a regra correspondente devolve `NAO_VERIFICADO` e o parecer sai `INCONCLUSIVO`.

---

## 5. Erros, Schema Drift e Quarentena

1. **Proveniência (SHA-256):** cada arquivo baixado tem o hash registrado em `raw_ingest.sources_manifest` com URL, data, tamanho e data de referência da fonte. O hash serve para reprodutibilidade e para detectar **mudança** do arquivo entre downloads. Não há manifesto oficial dos órgãos para comparar.
2. **Validação de integridade:** abrir e validar o arquivo (zip íntegro, tamanho descompactado máximo, número de registros dentro de uma faixa esperada em relação à carga anterior). Variação de volume acima de ±20% bloqueia a publicação até revisão.
3. **Schema drift:** checagem de colunas obrigatórias antes do processamento; coluna ausente ou renomeada aborta o job.
4. **Quarentena (`raw_ingest.quarantine_records`):** fonte, versão, payload bruto, motivo e timestamp. Todo registro rejeitado vai para cá; nada é descartado em silêncio. O relatório de carga mostra contagem por motivo.

---

## 6. Script de Referência (SICAR, Bronze → Silver)

```python
"""
src/data_pipeline/ingest_sicar.py
Bronze → Silver para o SICAR. Nada é descartado sem registro em quarentena.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import polars as pl
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

EPSG_CANONICAL = 4674
AREA_CHANGE_LIMIT = 0.01  # 1%
CAR_REGEX = re.compile(r"^[A-Z]{2}-\d{7}-[A-F0-9]{32}$")
MUNICIPIOS_MVP = {"5218805": "GO", "3148103": "MG", "4104907": "PR"}
STATUS_MAP = {"AT": "ATIVO", "PE": "PENDENTE", "SU": "SUSPENSO", "CA": "CANCELADO"}

# Validar no arquivo real antes do Sprint 2
FIELD_CAR = "cod_imovel"
FIELD_STATUS = "ind_status"
FIELD_AREA = "num_area"


@dataclass(frozen=True)
class GeomResult:
    geom: MultiPolygon | None
    reason: str | None


def _polygon_parts(geom: BaseGeometry) -> list[Polygon]:
    """Achata qualquer coleção, devolvendo apenas polígonos."""
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        parts: list[Polygon] = []
        for g in geom.geoms:
            parts.extend(_polygon_parts(g))
        return parts
    return []


def sanitize_geometry(geom: BaseGeometry | None) -> GeomResult:
    if geom is None or geom.is_empty:
        return GeomResult(None, "geometria_ausente")

    geom = shapely.force_2d(geom)

    if not geom.is_valid:
        area_before = geom.area
        repaired = shapely.make_valid(geom)
        parts = _polygon_parts(repaired)
        if not parts:
            return GeomResult(None, "geometria_colapsada")
        geom = MultiPolygon(parts)
        if area_before > 0 and abs(geom.area - area_before) / area_before > AREA_CHANGE_LIMIT:
            return GeomResult(None, "reparo_alterou_area")
    else:
        parts = _polygon_parts(geom)
        if not parts:
            return GeomResult(None, "tipo_nao_poligonal")
        geom = MultiPolygon(parts)

    return GeomResult(geom, None)


def process_sicar(input_path: Path, silver_path: Path, quarantine_path: Path, dataset_version: str) -> None:
    gdf = gpd.read_file(input_path)
    if gdf.crs is None:
        raise ValueError("Arquivo sem CRS: abortando (não presumir projeção)")

    missing = {FIELD_CAR, FIELD_STATUS, FIELD_AREA} - set(gdf.columns)
    if missing:
        raise ValueError(f"Schema drift no SICAR: colunas ausentes {missing}")

    gdf = gdf.to_crs(epsg=EPSG_CANONICAL)

    ok_rows: list[dict] = []
    bad_rows: list[dict] = []

    for rec in gdf.itertuples(index=False):
        raw_code = str(getattr(rec, FIELD_CAR) or "")
        code = raw_code.replace(".", "").strip().upper()
        reason: str | None = None

        if not CAR_REGEX.match(code):
            reason = "codigo_car_invalido"
        else:
            ibge = code.split("-")[1]
            uf = code.split("-")[0]
            if ibge not in MUNICIPIOS_MVP:
                continue  # fora do escopo, não é erro
            if MUNICIPIOS_MVP[ibge] != uf:
                reason = "uf_inconsistente_com_ibge"

        status = STATUS_MAP.get(str(getattr(rec, FIELD_STATUS) or "").strip().upper())
        if reason is None and status is None:
            reason = "status_desconhecido"

        geom_res = sanitize_geometry(rec.geometry)
        if reason is None and geom_res.reason:
            reason = geom_res.reason

        if reason:
            bad_rows.append({
                "fonte": "SICAR", "dataset_version": dataset_version,
                "chave": raw_code, "motivo": reason,
                "payload_wkt": rec.geometry.wkt if rec.geometry is not None else None,
            })
            continue

        area = getattr(rec, FIELD_AREA)
        ok_rows.append({
            "codigo_car": code,
            "codigo_ibge": int(code.split("-")[1]),
            "status_cadastro": status,
            "area_declarada_ha": float(area) if area is not None else None,
            "geometria_wkb": shapely.to_wkb(geom_res.geom),
            "dataset_version": dataset_version,
        })

    pl.DataFrame(ok_rows).write_parquet(silver_path, compression="zstd")
    pl.DataFrame(bad_rows).write_parquet(quarantine_path, compression="zstd")

    print(f"[ETL] SICAR {dataset_version}: ok={len(ok_rows)} quarentena={len(bad_rows)}")
    if bad_rows:
        print(pl.DataFrame(bad_rows).group_by("motivo").len())
```

Observação: o volume dos 3 municípios permite o loop em Python. Para escala estadual, vetorizar com as funções de array do Shapely 2.x.

---

## 7. Carga no PostGIS: Snapshot Versionado

O upsert foi removido. Ele não apagava registros que saíram da fonte, o que mantinha embargos já desembargados bloqueando crédito.

```sql
-- 1. Nova versão em tabela própria (sem impacto na API)
CREATE TABLE core_geo.imoveis_car_v20261013 (LIKE core_geo.imoveis_car_template INCLUDING ALL);

-- 2. Carga em massa (COPY a partir do Parquet Silver, via orquestrador)

-- 3. area_geodesica_ha é coluna gerada (ST_Area em geography): nada a calcular aqui
ANALYZE core_geo.imoveis_car_v20261013;

-- 4. Checagens de sanidade (bloqueiam a publicação se falharem)
--    - contagem dentro de ±20% da versão anterior
--    - nenhuma geometria inválida: SELECT count(*) WHERE NOT ST_IsValid(geometria) = 0

-- 5. Publicação atômica
BEGIN;
CREATE OR REPLACE VIEW core_geo.imoveis_car_current AS
    SELECT * FROM core_geo.imoveis_car_v20261013;
INSERT INTO core_geo.dataset_versions (fonte, versao, source_as_of, publicado_em)
VALUES ('SICAR', '20261013', DATE '2026-10-01', now());
COMMIT;

-- 6. Retenção: versões antigas mantidas pelo prazo de auditoria
--    (ou exportadas para Parquet frio e removidas do banco)
```

A API lê apenas as views `*_current`. O `analysis_id` de cada parecer registra as versões de dados usadas, permitindo reproduzir o resultado depois.
