# Resultados das execuções reais

Registro revisado das execuções com modelo real. Os arquivos brutos (JSON com rastro, gabarito e
veredito de cada caso, e o resumo gerado) ficam em `evals/results/raw/`, fora do Git. Nada aqui é
uma taxa de acerto do corpus de 26 casos: só o tier `smoke` foi executado.

## Configuração

| Item | Valor |
|---|---|
| Data das execuções | 2026-10-04 (UTC) |
| `CINEDATA_REFERENCE_DATE` | 2026-10-01 |
| `CINEDATA_MODEL` | `openrouter/free` (roteador gratuito do OpenRouter, não um modelo) |
| `CINEDATA_FALLBACK_MODELS` | vazio |
| Limites | `CINEDATA_REQUEST_LIMIT=5`, `CINEDATA_MAX_ROWS=50`, `CINEDATA_SQL_TIMEOUT_S=30` |
| Impressão digital | avaliação `ae4dfbc84d86a3f2`, agente `f9dd1e23a1049507` (as duas execuções abaixo e o código atual) |
| Ambiente | Python 3.14.3, SQLite 3.50.4, PydanticAI 2.52.0, cliente OpenAI 3.23.0 |
| Banco | `cinerocket.db`, SHA-256 `5afada60e383…` |

## Smoke: execução original

Tier `smoke` com `--live`: os 4 casos em sequência, sem retentativa automática. O smoke cobre um
ranking com empate no corte, uma agregação por grupo, uma pergunta livre com resolução de gênero
e papel e o título ambíguo.

| Caso | Categoria | Veredito | Respostas do modelo | Modelos que responderam (`models_used`) |
|---|---|---|---|---|
| `oficial_03_maior_margem` | official | pass | 3 | `nvidia/nemotron-3.5-lightning:free`, `nvidia/nemotron-3-super-120b-a12b:free`, `cohere/north-mini-code:free` |
| `oficial_06_nota_imdb_por_ano` | official | pass | 2 | `cohere/north-mini-code:free`, `qwen/qwen3.8-27b:free` |
| `livre_01_top5_atores_terror` | freeform | pass | 5 | `nvidia/nemotron-3.5-lightning:free`, `liquid/lfm-2.5-2.6b:free`, `qwen/qwen3.8-27b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `inclusionai/ling-3.0-flash-sante:free` |
| `politica_01_titulo_ambiguo` | policy | **fail** (`agent_protocol`) | 5 | `qwen/qwen3.8-27b:free`, `nvidia/nemotron-3.5-lightning:free`, `liquid/lfm-2.5-2.6b:free` |

Resultado da execução: **3 pass e 1 fail**, nenhum caso não avaliado. Nos três casos que passaram,
uma consulta lida pelo modelo reproduziu o gabarito e o texto trouxe todas as linhas exigidas (10,
13 e 5).

No título ambíguo, as duas buscas de "Elemental" encontraram os 2 homônimos (`exact_multiple`),
mas o agente encerrou a pergunta pela regra de 3 falhas seguidas da mesma ferramenta (`run_sql`),
sem resposta final; o rastro não registra nenhuma consulta executada no banco. Pela regra da
avaliação, `agent_protocol` é falha do conjunto modelo + agente e fica no denominador.

## Diagnóstico separado do título ambíguo

Depois do smoke, só o caso `politica_01_titulo_ambiguo` foi executado de novo, num arquivo
próprio, com o mesmo código, configuração e pergunta.

| Caso | Veredito | Respostas do modelo | Chamadas de ferramenta | Modelos que responderam |
|---|---|---|---|---|
| `politica_01_titulo_ambiguo` | pass | 3 | 1 | `cohere/north-mini-code:free`, `nvidia/nemotron-3-ultra-550b-a55b:free` |

O agente pediu esclarecimento depois de a busca encontrar os 2 homônimos, citando o desambiguador
de cada um. Esse diagnóstico é uma execução separada: **não altera** o resultado do smoke, que
continua 3 pass e 1 fail.

## Como ler estes resultados

- **Parte determinística:** o gabarito é recalculado por SQL de referência e a pontuação é código;
  o mesmo rastro sempre recebe o mesmo veredito. Essa parte é validada offline e no banco real
  pelos testes (`pytest`, `pytest -m realdb`), sem modelo nenhum.
- **Parte estocástica:** o comportamento do modelo. `openrouter/free` escolhe um modelo gratuito a
  cada requisição: 7 modelos diferentes responderam nos 4 casos do smoke, e a execução que falhou e
  a que passou no título ambíguo não tiveram nenhum modelo em comum. Uma execução real é uma
  amostra dessa composição, não uma medida de um modelo; repetir a mesma pergunta pode dar outro
  desfecho.
- **Consequência operacional:** com `openrouter/free`, a variação de roteamento faz parte da
  operação. Para comparar modelos ou reproduzir no nível do modelo, fixe um id explícito em
  `CINEDATA_MODEL`, ciente de que um endpoint gratuito específico pode estar com limite de uso.

## Outras execuções

Uma execução do smoke com o modelo fixo `qwen/qwen3.8-27b:free` não avaliou nenhum caso: o
provedor respondeu HTTP 429 (limite de uso) no primeiro caso, e os outros 3 ficaram pendentes.
Falha de provedor é "não avaliado" e não conta como falha semântica.
