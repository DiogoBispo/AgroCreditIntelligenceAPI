# FASE0_FONTES.md

**AgroCredit Intelligence API — Fase 0: exploração das fontes reais**

- **Data da coleta:** 13/10/2026
- **Escopo:** Rio Verde (GO), Patrocínio (MG), Castro (PR)
- **Objetivo:** confirmar acesso, formato, campos, qualidade e atualização de cada fonte antes de escrever o pipeline.
- **Arquivos baixados:** `data/fase0/` (fora do git), com SHA-256 no anexo.

---

## 1. Resumo

| Fonte | Canal confirmado | Situação | Impacto |
| :-- | :-- | :-- | :-- |
| SICAR | GeoServer WFS oficial, sem captcha | ✅ Pronta | Município real ≠ código do CAR em 0,1–0,4% dos casos |
| Embargos IBAMA | CSV diário (blob oficial) com WKT na linha | ✅ Pronta | CPF **sem máscara**; 49% sem polígono |
| Embargos ICMBio | ArcGIS REST do IBAMA (PAMGIA) | ⚠️ Atualização desconhecida | Fonte federal não prevista; 12 embargos perto de Castro |
| MTE | Somente PDF | ✅ Utilizável | Parser de PDF; checagem por contagem |
| RFB (CNPJ) | Nextcloud da RFB (WebDAV público) | ✅ Pronta | ISO-8859-1; volume decide o recorte |
| Municípios IBGE × TOM | RFB `Municipios.zip` | ✅ | **Os três códigos TOM do PRD estavam errados** |
| Terras indígenas | FUNAI WFS bloqueado (403); cópia no PAMGIA de 2020 | ❌ Sem canal atual | R-TI é obrigatória: bloqueia o MVP se nada mudar |
| UC federais e estaduais, florestas públicas | PAMGIA | ⚠️ Atualização desconhecida | Só há polígonos perto de Castro |
| Quilombolas | INCRA (zip) | ✅ | 3 áreas perto de Castro |
| PRODES | TerraBrasilis WFS (Cerrado e Mata Atlântica) | ✅ | Data em `image_date`; publicação de jul/2026 |
| Embargos estaduais | MG: WFS IDE-Sisema · GO: nenhum · PR: não confirmado | ⚠️ Parcial | MG sem situação (vigente/levantado) e com CPF mascarado |

---

## 2. SICAR

- **Canal:** `https://geoserver.car.gov.br/geoserver/sicar/wfs`, camadas `sicar:sicar_imoveis_<uf>`. Sem captcha, sem autenticação, EPSG:4674 nativo. Download dos 3 municípios em ~1,5 s cada; resposta completa (`numberMatched = numberReturned`).
- **Campos reais:** `cod_imovel`, `status_imovel` (AT, PE, SU, CA), `condicao` (texto da análise), `area`, `cod_municipio_ibge`, `municipio`, `uf`, `m_fiscal`, `tipo_imovel` (IRU, AST, PCT), `dat_criacao`, `data_atualizacao`, `geo_area_imovel`.
- **Volume:** Rio Verde 6.302, Patrocínio 5.962, Castro 4.963 imóveis.
- **Qualidade:** 100% dentro do regex do CAR; nenhum código duplicado; nenhuma geometria nula ou 3D; 17 geometrias inválidas (8 + 3 + 6); área declarada coincide com a calculada (5 casos com diferença acima de 5%); até 4.188 vértices.
- **Atualização:** registros alterados até 13/10/2026 (dados do dia).

**Achado crítico: o código IBGE dentro do `cod_imovel` nem sempre é o município do imóvel.**

- 0,1% a 0,4% dos imóveis têm código de outro município (ex.: `GO-5202601-…` em Firminópolis, `5207808`).
- No sentido inverso, 23 CARs com código de um município do escopo ficam hoje em município vizinho (8 + 8 + 7).
- **Consequências:**
  - O município vem de `cod_municipio_ibge`, nunca do código.
  - A carga traz `cod_municipio_ibge IN (escopo) OR cod_imovel LIKE 'UF-IBGE-%'` para cada município do escopo. Sem isso, a API responderia "CAR não encontrado" (`IMPEDIDO`) para um CAR existente.
  - A API decide a cobertura pela base carregada: CAR encontrado está coberto. O código só serve de pista quando o CAR não é encontrado.

---

## 3. Embargos IBAMA

- **Canal:** `https://stibamadadosabertosprd.blob.core.windows.net/dados-abertos/dados/TERMOS_DE_EMBARGO/TERMO_EMBARGO/termo_de_embargo.csv` (210 MB, atualização diária, última em 13/10/2026 03:54). O shapefile antigo do PAMGIA (`adm_embargo_ibama_a.shp.zip`) não existe mais (404).
- **Geometria:** coluna `GEOM_AREA_EMBARGADA` em WKT (POLYGON/MULTIPOLYGON) na própria linha; ponto de referência em `NUM_LONGITUDE_TAD` / `NUM_LATITUDE_TAD`.
- **Volume nacional:** 116.782 termos; 59.497 com polígono (51%).
- **Documento do autuado:** 88.660 CPFs e 17.767 CNPJs **sem máscara**; 10.353 sem documento.
- **Situação:** a combinação vigente é `DES_STATUS_FORMULARIO = 'Lavrado'`, `SIT_CANCELADO = 'N'` e `SIT_DESEMBARGO` nulo (89.923). Também existem os status Cancelado, Substituído por outro e Excluído, e desembargos administrativos ou judiciais.
- **Problemas de qualidade:**
  - 3.098 linhas com `SEQ_TAD` e status nulos.
  - Datas impossíveis, como `2925-09-25` e `2090-03-16` (5 casos).
- **Escopo:**

| Município | Termos | Aparentemente vigentes | Com polígono | Vigentes sem polígono |
| :-- | --: | --: | --: | --: |
| Rio Verde | 66 | 32 | 8 | 27 |
| Patrocínio | 15 | 9 | 7 | 6 |
| Castro | 3 | 1 | 2 | 0 |

**Achado: a maioria dos embargos sem polígono embarga uma atividade, não uma área.** Exemplos: criação amadora de passeriformes, transporte de carvão. A regra R-EMB-SEMGEO da v1.2 ("embargo sem polígono no município → ALERTA") marcaria praticamente todo imóvel de Rio Verde por causa de criadores de passarinho.

- **Proposta:** R-EMB-SEMGEO passa a considerar só embargos sem polígono cujo **ponto de referência cai dentro do imóvel**. O vínculo pessoa ↔ embargo fica com R-EMB-DOC, que agora é viável também para CPF.
- **Pergunta ao jurídico:** um embargo de **atividade** (sem área) contra o tomador impede o crédito rural pelo MCR 2-9, ou só embargos de área ou imóvel?

---

## 4. Embargos ICMBio

- **Canal:** `pamgia.ibama.gov.br/server/rest/services/01_Publicacoes_Bases/adm_embargo_icmbio_a/MapServer` (ArcGIS REST, máximo de 2.000 registros por consulta, exige `User-Agent` de navegador).
- **Volume:** 8.025 no país; 12 no entorno de Castro, 0 nos demais.
- **Campos:** `cpf_cnpj`, `autuado`, `numero_ai`, `nome_uc`, `data_auto`, `julgamento`, `nr_process`, entre outros. A camada não informa a data de atualização.
- **Proposta:** carregar como órgão `ICMBIO` na mesma tabela de embargos e incluir em R-EMB-GEO. Pendente: descobrir a data de atualização.

---

## 5. MTE (Cadastro de Empregadores)

- **Canal:** somente PDF (`gov.br/trabalho-e-emprego/.../cadastro_de_empregadores.pdf`), 40 páginas, atualização de 06/10/2026.
- **Conteúdo:** 508 empregadores; 167 CNPJs e 341 CPFs, todos sem máscara.
- **Extração:** `pdftotext -layout` + regex de CPF/CNPJ.
- **Checagem de sanidade:** número de documentos extraídos = maior ID da lista (508 = 508). Se divergir, a carga não é publicada.

---

## 6. RFB (CNPJ)

- **Canal:** o endereço antigo (`/dados/cnpj/dados_abertos_cnpj/`) responde 404. Os arquivos estão num Nextcloud com compartilhamento público acessível por WebDAV: `https://arquivos.receitafederal.gov.br/public.php/webdav/<AAAA-MM>/` (token do compartilhamento como usuário, sem senha). A edição 2026-10 foi publicada em 13/10/2026.
- **Arquivos:** `Estabelecimentos0..9.zip` (~5,4 GB compactados no total), `Empresas0..9`, `Socios0..9`, tabelas de domínio.
- **Codificação:** ISO-8859-1; o leitor do DuckDB recusa e é preciso converter com `iconv` antes de ler.
- **Volume (parte 1 de 10):** 4,75 milhões de estabelecimentos, 24,5% ativos; 70.839 ativos com CNAE 01 (principal ou secundária). Nenhum CNPJ alfanumérico nesta parte.

**Decisão pendente: recorte da carga.**

| Opção | Linhas (estimativa) | Prós | Contras |
| :-- | :-- | :-- | :-- |
| A. CNAE 01, todas as situações | ~1–1,5 mi | Leve | PJ com CNAE de outro grupo fica `NAO_VERIFICADO` |
| B. Todos os estabelecimentos, 4 colunas (cnpj, situação, CNAE, município) | ~60–75 mi | R-RFB cobre qualquer CNPJ | Carga e índice de alguns GB |

Recomendação: **B**. A situação `INAPTA` ou `BAIXADA` de qualquer tomador é informação útil; com 4 colunas, a tabela fica administrável. A carga precisa incluir todas as situações, não só as ativas, senão a regra nunca gera o `ALERTA` de situação irregular.

---

## 7. Municípios IBGE × TOM

Tabela oficial `Municipios.zip` da RFB:

| Município | IBGE | TOM no PRD v1.2 | **TOM real** |
| :-- | :-- | :-- | :-- |
| Rio Verde | 5218805 | 9373 | **9571** |
| Patrocínio | 3148103 | 4967 | **4961** |
| Castro | 4104907 | 7493 | **7495** |

- A tabela da RFB não traz UF; o de-para pelo nome é ambíguo em escala nacional.
- Para o MVP, os 3 nomes são únicos na tabela. Em escala nacional, é preciso um de-para oficial TOM ↔ IBGE.

---

## 8. Camadas territoriais

| Camada | Canal | Atualização | No entorno |
| :-- | :-- | :-- | :-- |
| Terras indígenas | FUNAI WFS: GetFeature retorna 403 (bloqueio do servidor) | — | — |
| Terras indígenas (cópia) | PAMGIA `lim_terra_indigena_a` | **31/07/2020** | 0 nos 3 municípios |
| UC federais (ICMBio) | PAMGIA `lim_unidade_conservacao_a` | desconhecida | Castro: 2 |
| UC estaduais (SFB) | PAMGIA `lim_unidade_conservacao_est_a` | desconhecida | Castro: 3 |
| Florestas públicas (SFB) | PAMGIA `lim_floresta_publica_a` | desconhecida | Castro: 5 |
| Quilombolas | INCRA `certificacao.incra.gov.br/csv_shp/zip/Áreas de Quilombolas.zip` | — | Castro: 3 |
| PRODES Cerrado | TerraBrasilis WFS `prodes-cerrado-nb:yearly_deforestation` | publicação jul/2026 | Rio Verde 8.861; Patrocínio 5.358 |
| PRODES Mata Atlântica | TerraBrasilis WFS `prodes-mata-atlantica-nb:yearly_deforestation` | — | Castro 11.612 |

**Detalhes técnicos:**
- **FUNAI:** o servidor envia o certificado TLS sem o intermediário da Sectigo. Resolvido sem desligar a verificação: o intermediário é baixado pela URL AIA do próprio certificado e somado ao bundle do sistema. Mesmo assim, o GetFeature é recusado (403), embora o GetCapabilities funcione. Parece um bloqueio intencional e não foi contornado.
- **Fases de TI na fonte:** Regularizada (475), Declarada (72), Delimitada (46), Encaminhada RI (19), Homologada (8), Em Estudo (7). A carga normaliza para `REGULARIZADA`, `ENCAMINHADA_RI` etc.
- **PRODES:** a data de detecção é `image_date`, e não `data_deteccao`; a carga mapeia o campo. O ano PRODES vai de agosto a julho, por isso a data de corte usa a imagem, não o ano.

**Bloqueio:** R-TI é obrigatória no ruleset e o limite de defasagem é de 60 dias. Com a única fonte acessível datada de 2020, todo parecer sairia `INCONCLUSIVO`. Opções:
1. Obter um canal atual da FUNAI (contato institucional ou pedido via LAI).
2. Aceitar a cópia de 2020 com limite de defasagem próprio para TI, já que demarcações mudam devagar, e registrar a data no parecer. É uma decisão de risco do produto.
3. Marcar R-TI como não obrigatória até resolver.

---

## 9. Embargos estaduais

| UF | Canal | Situação |
| :-- | :-- | :-- |
| MG | WFS IDE-Sisema `IDE:ide_2304_mg_areas_embargadas_sisema_pol` | 51 polígonos no entorno de Patrocínio. **Sem campo de situação** (vigente ou levantado); documento mascarado (`XXX.473.786-XX`); só iniciais do nome |
| GO | Não encontrado em dados abertos (SEMAD emite apenas certidão individual) | Sem base |
| PR | IAT menciona camada no GeoServer, sem endereço confirmado | Não confirmado |

**Consequências:**
- Para MG, R-EMB-EST não consegue afirmar que um embargo está vigente. Proposta: em UF sem informação de situação, o resultado máximo é `ALERTA`, nunca `IMPEDIDO`.
- R-EMB-EST continua não obrigatória.
- Para GO e PR, a regra retorna `NAO_VERIFICADO`, como previsto.

---

## 9b. Atualização (14/10/2026): carga das camadas restantes

| Fonte | Canal adotado | Data de referência | Observação |
| :-- | :-- | :-- | :-- |
| UCs (todas as esferas) | **CNUC/MMA** no portal de dados abertos (`shp_cnuc_2025_08.zip`) | 01/08/2025 | As versões 2026_03 e 2026_07 só existem num link do SharePoint que exige login. Com limite de 240 dias (publicação semestral), **R-UC sai `NAO_VERIFICADO`** até alguém baixar a versão 2026 manualmente (`run cnuc --arquivo ... --as-of 2026-07-01`). O DBF tem texto UTF-8 cortado no meio de caractere; a leitura trata isso |
| Florestas públicas | **CNFP 2025/SFB** (item do portal ArcGIS do SFB) | 23/07/2026 | No escopo só há florestas TIPO A (destinadas) |
| Quilombolas | INCRA (arquivo gerado no momento do pedido) | data do download | Situação em `fase` (RTID, PORTARIA, DECRETO, TITULADO, TITULO PARCIAL...), não em `st_titulad` |
| PRODES | TerraBrasilis WFS (Cerrado e Mata Atlântica) | `pub_date` (17/07/2026) | Algumas respostas vêm declaradas em WGS 84 mesmo pedindo SIRGAS 2000. `area_km` **não serve** de referência para a guarda de reparo: diverge >1% da área geodésica em 66% dos polígonos válidos |
| Embargos MG | WFS IDE-Sisema (estado inteiro) | 05/05/2026 (auto mais recente) | Camada parada desde maio: R-EMB-EST de MG sai `NAO_VERIFICADO`. CNPJ vem sem máscara (alimenta R-EMB-DOC, limitado a ALERTA) |
| **Embargos ICMBio** | PAMGIA | **fev/2022** (auto mais recente) | **Não carregado**: camada abandonada; geraria impedimentos a partir de embargos possivelmente levantados |

Varredura de todos os 17.227 imóveis do escopo (documento fictício, sem regras documentais): 282 com impedimento (16 por embargo IBAMA, os demais por CAR cancelado/suspenso); 982 com alerta de PRODES pós-2019; 65 sobrepostos a quilombola em fase RTID; 5 embargos só de borda; 8 com ponto de embargo sem polígono dentro do imóvel.

## 10. Decisões necessárias

| # | Decisão | Quem | Recomendação |
| :-- | :-- | :-- | :-- |
| 1 | Fonte e defasagem aceitável para terras indígenas | Produto + jurídico | Opção 2 provisoriamente, com pedido formal à FUNAI |
| 2 | Embargo de atividade (sem área) contra o tomador impede o crédito? | Jurídico | — |
| 3 | R-EMB-DOC obrigatória, agora que o CPF é verificável | Jurídico | Sim, se a resposta da decisão 2 for afirmativa |
| 4 | Recorte da carga da RFB | Engenharia | Opção B |
| 5 | Embargos ICMBio em R-EMB-GEO | Jurídico | Sim (órgão federal), **mas a única fonte acessível está parada desde 2022**: pedir canal atual ao ICMBio |
| 6 | Limites de defasagem por cadência de publicação (CNUC 240 dias, CNFP 395 dias) | Produto | Aceitar; documentado em DATA_PIPELINE §4 |
| 7 | Obter manualmente o CNUC 2026_07 (link do MMA exige login) | Operação | Sim; carregar com `--arquivo` |

---

## Anexo: arquivos baixados (SHA-256)

| Arquivo | SHA-256 |
| :-- | :-- |
| sicar/sicar_5218805.json | `03bd5f3d706c313859e9714e40a311e0f28152723d0c86e4329a392bdf9223e2` |
| sicar/sicar_3148103.json | `4be8cc9ce0aacd074694b9ad2130d9005d429777f8b8f99d9ce56d6b73eb0609` |
| sicar/sicar_4104907.json | `8b6e6f3e9522d809cdc5f610033d7d3fa9cd322b224613718602f45c91758779` |
| ibama/termo_de_embargo.csv | `dfbf6126d7a88b03f68412cf5522e7f59027e5760b040c7072b5580170dcba59` |
| mte/cadastro_de_empregadores.pdf | `21fb9efe5698d07d00843c4bd0795905ee57f10e2d74ab56a5e549fe36db059e` |
| rfb/Municipios.zip | `e5521c91e7fa62879f441af8223e3ed07564c855105edc39939190c108411d4a` |
| incra/quilombolas.zip | `b0c0dba62fb38e2d49bb2580608167525153f60b391e020e20a5e71d608702be` |
| mg/embargos_patrocinio.json | `ab5306498ccf0d880abe4791405670d4c41151b97e76f03ec0516238177361f4` |
