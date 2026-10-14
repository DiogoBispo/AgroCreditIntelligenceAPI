# AI_EVALS.md

**AgroCredit Intelligence API — Extração de Certidões Cartoriais com LLM e Framework de Avaliação (Fase 3)**

- **Versão:** 1.1.0-DRAFT
- **Data:** Outubro de 2026
- **Status:** Rascunho. **Não priorizar antes de o MVP atingir as métricas do PRD, seção 5.5.**
- **Base:** PRD v1.1.0, OWASP Top 10 for LLM Applications, LGPD

> **Notas da revisão 1.1.0**
> - Fonte preferencial passa a ser a certidão digital via ONR/SAEC (assinatura verificável, texto nativo); OCR de PDF digitalizado vira caminho secundário.
> - Removido o descarte do "miolo" da matrícula (anulava o gate de recall de gravames).
> - PII stripping por regex substituído por estratégia viável (modelo sem retenção ou local).
> - Grounding obrigatório: cada campo extraído traz trecho e página de origem, verificados deterministicamente.
> - Gates estatisticamente coerentes com o tamanho do dataset; dataset ampliado e estratificado por cartório.
> - Teste de gravames compara identidade (tipo + número do ato), não só contagem.
> - Relação matrícula × CAR tratada como N:M; limiar de área unificado.
> - Temperatura 0 deixa de ser listada como mitigação de prompt injection.
> - Removidos textos de encerramento de conversa.

---

## 1. Contexto

A matrícula do imóvel é o documento com fé pública sobre domínio, cadeia de titularidade e ônus (hipoteca, penhora, alienação fiduciária, usufruto). Na Fase 3 ela também resolve a limitação do MVP: **verificar o vínculo entre o tomador e o imóvel** (PRD, seção 5.4).

Desafios:

1. **Heterogeneidade:** formatos diferentes por cartório, atos antigos datilografados ou manuscritos.
2. **Risco de alucinação:** um dígito errado na matrícula ou um ônus omitido compromete a garantia.
3. **Dados de terceiros:** a certidão de inteiro teor expõe antigos proprietários, cônjuges e herdeiros.

**Regra de ouro:** o LLM não decide crédito. Ele extrai fatos, cada um com evidência no texto, e esses fatos passam por validações determinísticas.

---

## 2. Arquitetura

```
[Fonte preferencial] Certidão digital via ONR/SAEC
   - Verificar assinatura digital e autenticidade
   - Texto nativo (sem OCR)
        │
[Fonte secundária] PDF digitalizado enviado pelo cliente
   - OCR com caixas delimitadoras, 300 DPI
   - Confiança média < 85% → revisão humana (não rejeição silenciosa)
   - Documento sem assinatura verificável → marcado como NAO_AUTENTICADO
        │
        ▼
CAMADA 1: Segmentação em atos
   - Abertura da matrícula + cada R-n / AV-n como unidade, com página e posição
   - Documento inteiro é processado (sem descartar atos intermediários)
   - Documentos longos: processar em lotes de atos, nunca truncar
        │
        ▼
CAMADA 2: Extração estruturada (LLM)
   - Saída com schema estrito
   - Cada campo acompanha `evidencia`: id do ato, página e trecho literal
        │
        ▼
CAMADA 3: Validação determinística
   - Grounding: o trecho citado existe no texto de origem? Valor extraído aparece no trecho?
   - CPF/CNPJ: dígitos verificadores
   - Ônus: cada ônus ativo tem ato de constituição; cada ônus cancelado tem ato de cancelamento citado
   - Frações ideais dos proprietários somam 100% (±0,01)
   - Proprietário atual inclui o tomador? (vínculo tomador × imóvel)
   - Área: ver seção 7
        │
        ▼
Qualquer falha de validação → REQUER_VALIDACAO_DOCUMENTAL_HUMANA
```

---

## 3. Schema de Extração

```python
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class Evidencia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ato_id: str = Field(description="Ex.: ABERTURA, R-3, AV-5")
    pagina: int = Field(ge=1)
    trecho: str = Field(min_length=1, max_length=600, description="Trecho literal do documento")


class TipoOnus(str, Enum):
    HIPOTECA = "HIPOTECA"
    HIPOTECA_CEDULAR = "HIPOTECA_CEDULAR"
    PENHORA = "PENHORA"
    ALIENACAO_FIDUCIARIA = "ALIENACAO_FIDUCIARIA"
    USUFRUTO = "USUFRUTO"
    ARRESTO = "ARRESTO"
    INDISPONIBILIDADE = "INDISPONIBILIDADE"
    OUTRO = "OUTRO"


class Onus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tipo: TipoOnus
    ato_constituicao: str = Field(description="Ex.: R-3")
    credor_beneficiario: str
    ativo: bool
    ato_cancelamento: str | None = Field(default=None, description="Obrigatório se ativo=False")
    evidencia: list[Evidencia] = Field(min_length=1)


class Proprietario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nome: str
    documento: str | None = Field(default=None, description="Somente dígitos; None se não constar")
    fracao_ideal_pct: float = Field(ge=0, le=100)
    evidencia: list[Evidencia] = Field(min_length=1)


class ExtracaoMatricula(BaseModel):
    model_config = ConfigDict(extra="forbid")
    numero_matricula: str
    cartorio_cns: str | None = Field(default=None, description="Código Nacional de Serventia")
    comarca: str
    uf: str = Field(min_length=2, max_length=2)
    denominacao_imovel: str | None
    area_ha: float | None = Field(default=None, gt=0)
    ccir: str | None = None
    car_averbado: str | None = Field(default=None, description="Código CAR se averbado na matrícula")
    proprietarios_atuais: list[Proprietario] = Field(min_length=1)
    onus: list[Onus] = Field(default_factory=list, description="Ativos e cancelados")
    restricao_ambiental_averbada: bool
    evidencia_campos: dict[str, Evidencia] = Field(
        description="Evidência para numero_matricula, area_ha, ccir, car_averbado"
    )
```

Diferenças em relação à v1.0.0: a extração inclui ônus **cancelados** (com o ato de cancelamento), para que o "ativo" possa ser verificado; campos opcionais aceitam `None` em vez de forçar o modelo a inventar valor.

---

## 4. Segurança (OWASP LLM)

| Risco | Vetor | Mitigação |
| :-- | :-- | :-- |
| LLM01 Prompt injection | Texto inserido em certidão adulterada ("desconsidere os ônus anteriores") | 1) Preferir certidão digital com assinatura verificável (elimina adulteração). 2) Grounding: toda afirmação precisa de trecho literal; a instrução injetada não muda o que está escrito nos atos. 3) Regras de consistência (ônus cancelado exige ato de cancelamento citado). 4) Documento delimitado no prompt. *Temperatura 0 não é mitigação de injection; é usada apenas para reprodutibilidade.* |
| LLM02 Saída insegura | Strings maliciosas na extração | Validação Pydantic; persistência com queries parametrizadas |
| LLM06 Dados sensíveis | Dados de terceiros enviados a provedor externo | Provedor com contrato de não retenção e sem uso para treino, ou modelo executado em infraestrutura própria. Registro do provedor e região no RIPD. *Regex para "ocultar CPFs de atos encerrados" foi descartado: decidir quais atos estão encerrados é parte do que o modelo extrai.* |
| LLM10 Consumo | Matrículas com centenas de páginas | Segmentação em atos e processamento em lotes; limite de custo por documento com fila para revisão humana se excedido. Nunca truncar o conteúdo. |

---

## 5. Avaliação

### 5.1 Dataset

- **Tamanho:** mínimo de **300 matrículas** para o gate inicial, com meta de 1.000.
- **Estratificação:** por cartório (não só por município), por época dos atos (digitado × datilografado × manuscrito) e por complexidade (número de atos e de ônus).
- **Fora dos 3 municípios:** incluir ao menos 20% de cartórios de outras comarcas para medir generalização.
- **Gabarito:** anotação dupla independente (advogado + analista de crédito); divergências resolvidas por terceiro. Registrar a concordância entre anotadores (kappa de Cohen) como teto realista de acurácia.
- **Separação:** conjunto de desenvolvimento e conjunto de teste congelado. O teste congelado não é usado para ajustar prompts.

### 5.2 Métricas e gates

O tamanho do dataset limita o que pode ser afirmado. Com zero erros em *n* casos, o limite superior de confiança de 95% para a taxa de erro é cerca de 3/*n* ("regra de três"). Os gates abaixo usam o **limite superior do intervalo de confiança**, não a estimativa pontual.

| Métrica | Unidade | Gate (limite superior do IC 95%) | Bloqueia release? |
| :-- | :-- | :-- | :-- |
| Acurácia exata de `numero_matricula` | Documento | Erro ≤ 1% | Sim |
| Acurácia de documento do proprietário | Proprietário | Erro ≤ 1% | Sim |
| Recall de ônus ativos (identidade: tipo + ato de constituição) | Ônus | Falso negativo ≤ 1% | Sim |
| Precisão de "ativo" (ônus marcado ativo que está cancelado, e vice-versa) | Ônus | Erro ≤ 2% | Sim |
| Grounding: campos com trecho que não existe no texto | Campo | ≤ 0,5% | Sim |
| Área (diferença ≤ 0,01 ha) | Documento | Erro ≤ 2% | Sim |
| Taxa de encaminhamento à revisão humana | Documento | Medido; meta < 25% | Não |
| Latência por página | Página | p95 < 4 s | Não |

Com 300 documentos e cerca de 600 ônus, um gate de 1% para falso negativo de ônus exige praticamente zero erros. Isso é intencional: o custo de um ônus omitido é alto. Para afirmar taxas menores, ampliar o dataset.

### 5.3 Script de referência

```python
"""
tests/ai_evals/test_extraction.py
Avaliação agregada: calcula métricas no conjunto e aplica gates sobre o IC.
"""
import json
from pathlib import Path

import pytest
from scipy.stats import beta

from src.infrastructure.ai.matricula_extractor import extrair
from src.domain.services.grounding import verificar_evidencias

DATASET = Path("tests/ai_evals/data/test_frozen.jsonl")


def upper_ci(erros: int, n: int, conf: float = 0.95) -> float:
    """Limite superior Clopper-Pearson."""
    if n == 0:
        return 1.0
    return 1.0 if erros == n else beta.ppf(conf, erros + 1, n - erros)


def carregar():
    with DATASET.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f]


@pytest.fixture(scope="module")
def resultados():
    casos = carregar()
    return [(c, extrair(c["texto"], c["paginas"])) for c in casos]


def test_recall_onus_ativos(resultados):
    fn, total = 0, 0
    for caso, ext in resultados:
        esperado = {(o["tipo"], o["ato_constituicao"]) for o in caso["gabarito"]["onus"] if o["ativo"]}
        obtido = {(o.tipo.value, o.ato_constituicao) for o in ext.onus if o.ativo}
        total += len(esperado)
        fn += len(esperado - obtido)
    assert upper_ci(fn, total) <= 0.01, f"FN de ônus: {fn}/{total}"


def test_matricula_exata(resultados):
    erros = sum(1 for c, e in resultados if e.numero_matricula != c["gabarito"]["numero_matricula"])
    assert upper_ci(erros, len(resultados)) <= 0.01


def test_grounding(resultados):
    falhas, total = 0, 0
    for caso, ext in resultados:
        f, t = verificar_evidencias(ext, caso["texto"])
        falhas += f
        total += t
    assert upper_ci(falhas, total) <= 0.005
```

---

## 6. Verificação de vínculo tomador × imóvel

Com a matrícula extraída e validada:

- Tomador consta entre `proprietarios_atuais` → `owner_link_verified: true`.
- Tomador não consta → alerta de posse indireta; exigir contrato de arrendamento ou comodato (fora do escopo de extração por LLM nesta fase).
- `car_averbado` presente e diferente do CAR informado → alerta de inconsistência.

---

## 7. Relação matrícula × CAR

Um CAR pode abranger várias matrículas, e uma matrícula pode estar em mais de um CAR. A comparação de área é feita assim:

- O cliente informa **todas** as matrículas que compõem o imóvel do CAR.
- Compara-se a soma das áreas das matrículas com a área geodésica do CAR.
- Divergência > **10%** → `REQUER_VALIDACAO_DOCUMENTAL_HUMANA` (limiar único em todo o documento).

---

## 8. Critérios de Homologação

1. Todos os gates bloqueantes da seção 5.2 aprovados no conjunto de teste congelado.
2. Nenhum ônus ativo omitido nos casos marcados como críticos pelo jurídico.
3. Auditoria grava: hash SHA-256 do documento, origem (ONR ou upload), status de autenticidade, modelo e versão, versão do prompt, e as evidências de cada campo.
4. RIPD atualizado com provedor do modelo, região de processamento e política de retenção.
