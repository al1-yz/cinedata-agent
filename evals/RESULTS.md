# Resultados das execuções reais

Registro revisado das execuções com modelo real. Os arquivos brutos (JSON com rastro, gabarito e
veredito de cada caso, e o resumo gerado) ficam em `evals/results/raw/`, fora do Git. Nada aqui é
uma taxa de acerto do corpus de 26 casos: só o tier `smoke` foi executado.

> **Evidência histórica, não validação da revisão atual.** As execuções das seções abaixo, até
> [Outras execuções](#outras-execuções), rodaram com o agente de impressão digital
> `f9dd1e23a1049507`. Depois delas, `src/cinedata/db.py` mudou
> (carga de extensões do SQLite desligada explicitamente e diagnóstico de aspas duplas decidido
> pelo SQL, para o Linux) e o agente passou a ter outra impressão digital (`3a7f3b052714eb18`
> quando esta nota foi escrita). O código da avaliação não mudou. Por isso:
>
> - estes resultados mostram o comportamento real do projeto **naquela revisão**; a revisão atual
>   foi executada à parte, em [Revisão atual: smoke controlado](#revisão-atual-smoke-controlado),
>   no fim deste arquivo;
> - a revisão atual também é coberta pelos testes determinísticos (offline e `realdb`), que
>   rodam no CI;
> - os arquivos brutos ficam preservados como estão, e um `--resume` deles é recusado de
>   propósito; a execução da revisão atual tem arquivos brutos próprios.

## Configuração

| Item | Valor |
|---|---|
| Data das execuções | 2026-10-04 (UTC) |
| `CINEDATA_REFERENCE_DATE` | 2026-10-01 |
| `CINEDATA_MODEL` | `openrouter/free` (roteador gratuito do OpenRouter, não um modelo) |
| `CINEDATA_FALLBACK_MODELS` | vazio |
| Limites | `CINEDATA_REQUEST_LIMIT=5`, `CINEDATA_MAX_ROWS=50`, `CINEDATA_SQL_TIMEOUT_S=30` |
| Impressão digital destas execuções | avaliação `ae4dfbc84d86a3f2` (igual à do código atual); agente `f9dd1e23a1049507` (diferente da do código atual; veja a nota acima) |
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
  pelos testes (`pytest`, `pytest -m realdb`), sem modelo nenhum, e essa validação vale para a
  revisão atual.
- **Revisão:** os vereditos acima são de uma revisão anterior do agente (veja a nota no início).
  Eles não provam que a revisão atual passa no mesmo smoke.
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

## Revisão atual: smoke controlado

Execução real nova do tier `smoke` com o código atual, depois da mudança do `db.py`. Ela foi
gravada em arquivos brutos próprios, sem `--resume` e sem reaproveitar nenhum resultado
histórico.

| Item | Valor |
|---|---|
| Commit | `c1ea294` |
| Impressão digital | agente `3a7f3b052714eb18`; avaliação `ae4dfbc84d86a3f2` |
| Data da execução | 2026-10-05 (UTC), das 01:20 às 01:22 |
| `CINEDATA_MODEL` | `openrouter/free` (roteador gratuito do OpenRouter, não um modelo) |
| `CINEDATA_FALLBACK_MODELS` | vazio (e `--primary-only`) |
| `CINEDATA_REFERENCE_DATE` | 2026-10-01 |
| Limites | `CINEDATA_REQUEST_LIMIT=5`, `CINEDATA_MAX_ROWS=50`, `CINEDATA_SQL_TIMEOUT_S=30` |
| Ambiente | Windows 11, Python 3.14.3, SQLite 3.50.4, PydanticAI 2.52.0, cliente OpenAI 3.23.0 |
| Banco | `cinerocket.db`, SHA-256 `5afada60e383…` (o mesmo das execuções históricas) |
| Arquivos brutos | `evals/results/raw/smoke-openrouter-free-agent-3a7f3b05-2026-10-01.json` e o resumo `.md` ao lado |
| Política | uma execução por caso: sem retentativa manual, sem `--resume`, sem diagnóstico extra |

```bash
python -m evals.run --tier smoke --primary-only --live --out evals/results/raw/smoke-openrouter-free-agent-3a7f3b05-2026-10-01.json
```

| Caso | Categoria | Veredito | Respostas do modelo | Chamadas de ferramenta | Tempo (s) | Modelos que responderam (`models_used`) |
|---|---|---|---|---|---|---|
| `oficial_03_maior_margem` | official | **fail** (`answer_text`) | 3 | 1 | 17,8 | `inclusionai/ling-3.0-flash-sante:free`, `cohere/north-mini-code:free` |
| `oficial_06_nota_imdb_por_ano` | official | pass | 3 | 1 | 14,7 | `nvidia/nemotron-3-super-120b-a12b:free`, `liquid/lfm-2.5-2.6b:free` |
| `livre_01_top5_atores_terror` | freeform | pass | 4 | 2 | 37,2 | `liquid/lfm-2.5-2.6b:free`, `cohere/north-mini-code:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `qwen/qwen3.8-27b:free` |
| `politica_01_titulo_ambiguo` | policy | pass | 4 | 2 | 16,3 | `nvidia/nemotron-3.5-lightning:free`, `inclusionai/ling-3.0-flash-sante:free`, `nvidia/nemotron-3-super-120b-a12b:free`, `qwen/qwen3.8-27b:free` |

Resultado da execução: **3 pass e 1 fail**, com os 4 casos avaliados. Nenhuma falha de provedor
(nenhum caso não avaliado), nenhum caso pendente e nenhuma falha de protocolo ou de política: a
única falha é semântica, no texto final. Cada caso executou uma consulta SQL, bem-sucedida e lida
pelo modelo antes da resposta final.

- `oficial_03_maior_margem`: a consulta reproduziu o gabarito (as 10 linhas), mas o texto final
  parou na frase de abertura ("Aqui estão os filmes com maior margem de lucro [...] (valores em
  BRL):") e não trouxe nenhum dos 10 filmes. O pontuador marcou as 10 linhas como ausentes. É
  falha do conjunto modelo + agente e fica no denominador: o código exige que uma resposta com
  dados venha depois de um resultado lido, não que o texto o apresente.
- `oficial_06_nota_imdb_por_ano` e `livre_01_top5_atores_terror`: a consulta reproduziu o gabarito
  (13 e 5 linhas) e o texto trouxe todas as linhas exigidas. No `livre_01`, `find_entities`
  resolveu o gênero (`exact_unique`) antes da consulta.
- `politica_01_titulo_ambiguo`: a busca `find_entities` de filme, na primeira requisição, achou os
  2 homônimos (`exact_multiple`); a consulta, na segunda, trouxe os dois pelo `id_filme`, com a
  nota IMDb; a resposta final (`data_answer`) deu a nota de cada um com o ano (Elemental, 2022:
  6,7; Elemental, 2023: 7,0) e pediu o ano para uma resposta mais específica. É o segundo
  desfecho que a avaliação aceita, a resposta completa para todos os homônimos, e **não** um
  pedido de esclarecimento: o agente não escolheu um dos filmes nem afirmou dados sem consulta.

Leitura:

- O placar repete o do smoke original (3 pass e 1 fail), mas o caso que falhou é outro: antes, o
  título ambíguo (`agent_protocol`); agora, `oficial_03_maior_margem` (`answer_text`).
  Responderam os mesmos 7 modelos gratuitos do smoke original, em outras combinações por caso.
  Com `openrouter/free`, cada execução é uma amostra da composição de modelos daquele momento, e
  repetir pode dar outro desfecho.
- São 4 dos 26 casos do corpus, executados uma vez: não há taxa de acerto sobre os 26 casos.
