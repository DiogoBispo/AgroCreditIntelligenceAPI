# RIPD.md

**AgroCredit Intelligence API — Relatório de Impacto à Proteção de Dados Pessoais (RIPD)**

- **Base:** art. 38 da Lei 13.709/2018 (LGPD)
- **Projeto:** AgroCredit Intelligence API (MVP)
- **Versão:** 1.1.0
- **Data:** Outubro de 2026
- **Status:** **Rascunho para revisão do DPO e do jurídico** (substitui v1.0.0)

> **Notas da revisão 1.1.0**
> - Base normativa atualizada (MCR 2-9 vigente, Res. CMN 5.193/2024, 5.268/2025, 5.303/2026).
> - Papel de "operador" revisto: há elementos de controladoria conjunta, a confirmar com o jurídico.
> - Removido `requested_amount_brl` (dado não necessário).
> - HMAC reclassificado como pseudonimização.
> - Retenção de IP revista.
> - Novos riscos: vínculo tomador × imóvel, dados desatualizados, falso negativo.
> - Parecer conclusivo reescrito: registra riscos residuais em vez de declarar conformidade integral.
> - Português padronizado (PT-BR) e removidos artefatos `[cite: …]`.

---

## 1. Agentes de Tratamento

| Papel | Entidade | Atribuição |
| :-- | :-- | :-- |
| Controlador | Instituição financeira, cooperativa ou revenda credora | Decide a concessão do crédito com base no parecer e em outros elementos |
| Operador **ou controlador conjunto** (a definir) | AgroCredit Intelligence API | Trata dados em nome do controlador. **Ponto de atenção:** a API define as fontes, a retenção de dados públicos e regras que influenciam o resultado (ex.: interpretação do MCR). Isso pode caracterizar controladoria conjunta (art. 5º, VI e VII). Enquanto não houver parecer jurídico, os contratos com clientes devem tratar o tema expressamente. |
| Encarregado (DPO) | A designar nominalmente | Canal com titulares e ANPD |

**Observação:** o RIPD é obrigação do controlador (art. 38). Este documento é a contribuição do fornecedor da API e deve ser incorporado ao RIPD de cada cliente.

---

## 2. Finalidade e Bases Legais

### 2.1 Finalidade

Verificar impedimentos sociais, ambientais e climáticos para crédito rural definidos na Seção 9 do Capítulo 2 do Manual de Crédito Rural (MCR 2-9), na redação vigente (Res. CMN 5.193/2024, com alterações das Res. CMN 5.268/2025 e 5.303/2026).

### 2.2 Bases legais (art. 7º)

1. **Inciso II — cumprimento de obrigação legal ou regulatória pelo controlador:** a verificação dos impedimentos do MCR 2-9 é exigência normativa para a instituição concedente.
2. **Inciso X — proteção do crédito:** subsidiar a avaliação de risco da operação.

---

## 3. Mapeamento dos Dados

### 3.1 Fluxo

```
[Entrada: POST /v1/credit/check]
   ├─ Documento do tomador (CPF/CNPJ)
   └─ Código CAR do imóvel
        │
        ▼
[Cruzamento com bases públicas pré-carregadas]
   ├─ RFB (CNPJ; sócios não são carregados)
   ├─ SICAR (status, geometria)
   ├─ Embargos IBAMA e estaduais
   ├─ Cadastro de empregadores do MTE
   └─ Camadas territoriais (FUNAI, ICMBio, INCRA, SFB, PRODES)
        │
        ▼
[Saída]
   ├─ Resposta: resultado por regra, números de sobreposição, datas das bases
   └─ Auditoria: UUID, tenant, HMAC do documento, documento mascarado,
                 resultado, ruleset_version, versões de dados, tolerância aplicada
```

### 3.2 Inventário

| Dado | Origem | Classificação | Finalidade | Retenção |
| :-- | :-- | :-- | :-- | :-- |
| CPF/CNPJ do tomador | Cliente via API | Pessoal (PF) | Cruzamento com MTE, embargos e RFB | Não persistido em claro. HMAC e versão mascarada na auditoria por 5 anos |
| HMAC do documento | Gerado | **Pseudonimizado (continua pessoal)** | Atender titular e auditoria | 5 anos |
| Código CAR | Cliente via API | Pode identificar indiretamente o produtor | Localização do imóvel | Na auditoria, 5 anos |
| Dados do cadastro do MTE | Fonte pública | Sancionatório, alto impacto reputacional | Impedimento do MCR 2-9 | Base local substituída a cada publicação; auditoria guarda só o resultado da regra |
| Embargos (processo, data, área) | Fontes públicas | Sancionatório ambiental | Impedimento do MCR 2-9 | Snapshot versionado pelo prazo de auditoria |
| IP de origem | Requisição | Dado técnico | Segurança e investigação de abuso | **6 meses** em log de segurança separado; não gravado na tabela de auditoria de 5 anos |

**Removido:** `requested_amount_brl`. Não é usado por nenhuma regra; coletá-lo violaria o princípio da necessidade.

---

## 4. Princípios (art. 6º)

- **Finalidade e adequação:** uso exclusivo para verificação de impedimentos do crédito rural. Vedado uso para marketing, prospecção ou perfilamento.
- **Necessidade:** a entrada se limita a documento e código CAR. A resposta traz apenas o resultado por regra e os dados técnicos que o justificam.
- **Qualidade dos dados:** toda resposta traz a data de referência de cada base (`data_as_of`). Bases defasadas além do limite tornam a regra `NAO_VERIFICADO`, em vez de afirmar regularidade com dado velho. Carga por snapshot evita que embargos já levantados continuem bloqueando.
- **Transparência:** o parecer informa as bases consultadas, as datas e as limitações (ex.: vínculo tomador × imóvel não verificado).
- **Segurança:** seção 6.
- **Não discriminação:** as regras derivam de critérios normativos objetivos. Não há uso de atributos pessoais além do documento.

---

## 5. Riscos aos Titulares

| ID | Risco | Impacto | Prob. inerente | Sev. inerente | Salvaguardas | Risco residual |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| R-01 | CPF em logs ou tracing | Exposição de dado pessoal | Média | Alta | Mascaramento no logger; teste automatizado (SPEC, TEST-SEC-003) | Baixo |
| R-02 | Falso positivo de embargo por imprecisão geométrica | Crédito negado indevidamente | Alta | Alta | Números brutos sempre devolvidos; tolerância do tenant gera alerta, não bloqueio; sem simplificação de geometria; guarda de área no reparo | Médio |
| R-03 | Embargo já levantado ainda na base | Crédito negado indevidamente | Média | Alta | Carga por snapshot completo; `data_as_of` na resposta | Baixo |
| R-04 | Falso positivo no cadastro do MTE | Dano moral grave | Baixa | Crítica | Cruzamento só por documento válido (nunca por nome); registro só ativo na última publicação | Baixo |
| R-05 | Acesso entre instituições concorrentes (BOLA/IDOR) | Exposição de consultas de terceiros | Média | Crítica | Tenant derivado do JWT; filtros por tenant em toda consulta; testes de regressão | Baixo |
| R-06 | **Reversão do HMAC** | Reidentificação dos CPFs da auditoria | Baixa | Alta | Pepper em cofre de segredos com acesso restrito e auditado; versão de chave | Médio |
| R-07 | **Falso negativo por dado ausente** (embargo estadual não publicado, embargo sem geometria, CPF mascarado na fonte) | Titular e controlador recebem um "sem impedimento" que não reflete a realidade | Alta | Alta | Regras `NAO_VERIFICADO` e `ALERTA` explícitas; status `INCONCLUSIVO`; linguagem "nas bases consultadas" | Médio |
| R-08 | **Uso de CAR de terceiro** | Terceiro com impedimento obtém parecer limpo | Média | Alta | `owner_link_verified: false` em toda resposta; responsabilidade contratual da IF | Alto (aceito no MVP, tratado na Fase 3) |
| R-09 | Mudança normativa não refletida | Parecer com regra revogada | Alta | Alta | Ruleset versionado com revisão jurídica; `ruleset_version` na resposta | Médio |
| R-10 | DoS por geometrias complexas | Indisponibilidade da esteira | Média | Média | Índice GiST, `statement_timeout`, rate limit por tenant | Baixo |

---

## 6. Medidas de Segurança

### 6.1 Pseudonimização e criptografia

- HMAC-SHA256 do documento com pepper em cofre de segredos, com `hmac_key_version`. Tratado como dado pessoal pseudonimizado.
- TLS 1.2+ (preferencialmente 1.3) em trânsito; HSTS.
- Criptografia em repouso do volume do banco.

### 6.2 Menor privilégio

- Papel da API com `SELECT` nas views `*_current` e `INSERT` na auditoria. Sem `UPDATE`, `DELETE` ou DDL.
- Papel do ETL separado, sem acesso à auditoria.
- Acesso ao pepper restrito ao serviço da API e registrado.

---

## 7. Direitos dos Titulares (art. 18 e art. 20)

1. **Confirmação e acesso:** pedido via controlador ou canal do DPO. Busca pelo HMAC do documento, filtrada pelo tenant do controlador solicitante (ou por todos, se o pedido vier do titular ao DPO, com verificação de identidade).
2. **Correção:** dados de origem pública são corrigidos no órgão emissor. A próxima carga reflete a correção; o `data_as_of` mostra quando isso ocorreu.
3. **Revisão de decisão automatizada (art. 20):** a API não toma a decisão de crédito, mas o parecer pode ser determinante. Para viabilizar a revisão, todo impedimento vem com:
   - regra aplicada e `ruleset_version`;
   - fonte e data de referência da base;
   - número do processo, data e área de sobreposição (quando aplicável);
   - tolerância aplicada.

   O controlador deve oferecer revisão humana, com aceitação de contraprova (certidão de desembargo, decisão judicial, documento de regularidade ambiental).

---

## 8. Conclusão

O desenho incorpora salvaguardas relevantes de proteção de dados: minimização da entrada, pseudonimização na auditoria, mascaramento em logs, isolamento por tenant e transparência sobre bases e datas.

Permanecem **riscos residuais que exigem decisão**:

- **R-08 (CAR de terceiro):** alto no MVP; aceito com aviso explícito e responsabilidade contratual do cliente até a Fase 3.
- **R-07 (falso negativo por dado ausente):** médio; mitigado pela linguagem e pelos estados `INCONCLUSIVO`/`ALERTA`, mas depende da disponibilidade de bases estaduais.
- **Papel do fornecedor (operador × controlador conjunto):** pendente de parecer jurídico.

Este RIPD deve ser revisado a cada alteração do MCR 2-9, inclusão de nova fonte de dados ou início da Fase 3.

**Revisores (pendente):**

- DPO / Encarregado:
- Jurídico:
- Tech Lead:
- Segurança da Informação:
