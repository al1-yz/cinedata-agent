# CineData Agent

Agente que responde, em linguagem natural, perguntas analíticas sobre o catálogo de filmes da
CineData Analytics. Ele gera SQL livre (Text-to-SQL) sobre a camada Gold, um banco SQLite, executa
a consulta **somente em modo leitura** e responde em português. É uma ferramenta de linha de
comando, sem frontend. Projeto da atividade GenAI do Rocket Lab 2026.2.

- **Text-to-SQL livre:** o modelo recebe o esquema da Gold e escreve o SQL de cada pergunta. Não há
  lista de perguntas aceitas, roteamento por intenção nem SQL pronto.
- **Somente leitura por construção:** todo acesso ao banco passa pelo `SafeDatabase`, que abre o
  arquivo em modo leitura e nega por padrão tudo que não seja ler as tabelas da Gold.
- **Respostas fundamentadas:** uma resposta com dados só é aceita depois de o modelo ter lido o
  resultado de uma consulta real; um nome ambíguo vira pedido de esclarecimento.
- **Rastreável:** `--show-sql` e `--json` mostram o SQL executado, as linhas, os tempos e os
  modelos que responderam, a partir do rastro da aplicação (nunca do texto do modelo).
- **Avaliação determinística:** 26 casos pontuados por código, sem LLM-juiz. Só o tier `smoke`
  (4 casos) foi executado com modelo real; não há taxa sobre o corpus (ver [Avaliação](#avaliação)).

## Escopo

O enunciado traz 14 perguntas em "Categorias de Perguntas e Exemplos (Não exaustivo)". Elas são
tratadas como o que são: **exemplos oficiais e não exaustivos**.

- O agente responde perguntas analíticas livres sobre a Gold. Ele não foi construído como 14
  intenções nem como 14 handlers de SQL e não precisa reconhecer nenhuma delas.
- Os 14 exemplos têm uma semântica aprovada e um SQL de referência (`src/cinedata/reference.py`),
  usados **só na avaliação**, como benchmark oficial mínimo. Testes garantem que o agente não
  carrega esse módulo e que nenhuma pergunta ou SQL de referência entra nas instruções do modelo.
- A avaliação acrescenta paráfrases, perguntas novas e casos de comportamento, para medir
  generalização além dos exemplos.

## Arquitetura

```text
pergunta em linguagem natural
        │
        ▼
agente PydanticAI ──── instruções: esquema Gold, relações, semântica do domínio, data de referência
        │
        ├── find_entities ──► EntityIndex: filmes, pessoas, gêneros e produtoras
        │                     (só um nome exato e único resolve; o resto volta como candidatos)
        │
        ├── run_sql ────────► SafeDatabase: validação e execução somente leitura
        │                              │
        │                              ▼
        │                     cinerocket.db (camada Gold, SQLite)
        ▼
final_answer ──── validada em código (fundamentação e ambiguidade)
        │
        ▼
resposta em português + rastro da aplicação (SQL, linhas, tempos, modelo, uso)
```

| Componente | Arquivo | Responsabilidade |
|---|---|---|
| Configuração | `src/cinedata/config.py` | Lê o ambiente e o `.env`, valida os valores e calcula a janela de datas. |
| `SafeDatabase` | `src/cinedata/db.py` | Único caminho até o banco: abertura somente leitura, authorizer e limites. |
| `EntityIndex` | `src/cinedata/entities.py` | Resolução de nomes com estados explícitos (exato, homônimos, parcial, fuzzy). |
| Instruções | `src/cinedata/prompt.py` | Esquema gerado da allowlist do `SafeDatabase`, mais a semântica do domínio. |
| Agente | `src/cinedata/agent.py` | Ferramentas `find_entities` e `run_sql`, validação da resposta e limites por pergunta. |
| Rastro | `src/cinedata/runtime.py` | Resposta tipada do modelo, rastro escrito só pelo código, falhas e avisos. |
| Provedor | `src/cinedata/llm.py` | Cliente do OpenRouter, fallback seletivo e tradução das falhas HTTP. |
| CLI | `src/cinedata/cli.py` | Comandos `doctor` e `ask`. |
| Referência | `src/cinedata/reference.py` | Gabarito dos 14 exemplos oficiais (só avaliação e testes). |
| Avaliação | `evals/` | Corpus, pontuação determinística e executor (fora do pacote instalado). |

Como uma pergunta é respondida:

1. O modelo recebe a pergunta e as instruções. O esquema vem da própria allowlist do
   `SafeDatabase`, então colunas ocultas nunca aparecem para ele.
2. Para nomes citados na pergunta, ele chama `find_entities`. Só `exact_unique` resolve; homônimos,
   correspondências parciais e sugestões fuzzy voltam como candidatos, com ano e `id_filme` (filmes)
   ou papel (pessoas).
3. Ele escreve o SQL e chama `run_sql(sql)`. A ferramenta só tem esse parâmetro: teto de linhas,
   prazo, conexão e authorizer vêm da configuração, nunca do modelo.
4. Ele termina com `final_answer`: um status (`data_answer`, `clarification`, `info` ou
   `out_of_scope`), o texto, premissas e ressalvas. O código recusa a resposta, e devolve o motivo
   ao modelo dentro do orçamento da pergunta, quando:
   - um `data_answer` não tem uma consulta bem-sucedida lida numa requisição anterior;
   - a resposta final veio na mesma resposta do modelo que pediu `run_sql` ou `find_entities` (o
     texto foi escrito sem ver o resultado);
   - uma consulta usou um candidato de uma busca não resolvida sem que o código prove a escolha
     (um ano ou `id_filme` escrito na pergunta que identifique um candidato só ou, entre
     homônimos, uma resposta que cubra todos). Sugestão fuzzy nunca vale. O caminho, então, é
     pedir esclarecimento.
5. A aplicação anexa avisos próprios (resultado truncado, zero linhas, candidato escolhido entre
   homônimos) e o rastro (`RunTrace`), que o modelo não escreve.

A fundamentação prova que a resposta veio depois de dados reais, não que o SQL era o certo. Isso
é o que a avaliação mede.

## Segurança e limites

A segurança do banco não depende do prompt nem de analisar o texto do SQL. Ela vem de camadas
independentes do `SafeDatabase`, e cada uma tem testes que a exercitam sozinha:

1. **Abertura somente leitura** (`mode=ro`), com `query_only` como reforço.
2. **Configuração endurecida e conferida por releitura:** `DEFENSIVE` ligado, `TRUSTED_SCHEMA`
   desligado e aspas duplas que nunca viram texto (DQS desligado). Se a build do SQLite não
   oferecer alguma dessas proteções, o banco não abre.
3. **Limites do SQLite:** `ATTACH` fechado (`SQLITE_LIMIT_ATTACHED=0`, que também bloqueia
   `VACUUM INTO`), SQL de até 20 KB e tetos de colunas, profundidade de expressão e `UNION`s.
4. **Authorizer com negação por padrão:** só leitura das 10 tabelas da Gold, das colunas
   permitidas e de funções de uma lista fechada. Escrita, DDL, `PRAGMA`, `ATTACH`, transações,
   consultas recursivas e `CURRENT_DATE`/`CURRENT_TIME`/`CURRENT_TIMESTAMP` são negados. Ficam
   ocultos `sqlite_master`, `alembic_version` e o nome de quem avaliou (`movie_reviews.name`).
5. **Prazo e tamanho do resultado:** prazo por consulta (padrão 30 s), teto de linhas (padrão 50)
   e de tamanho de célula, sem reescrever o SQL. Um resultado truncado é marcado e avisado.

Além disso, um pré-filtro aceita só `SELECT` e `WITH`, e cada pergunta tem orçamento próprio:
requisições ao modelo (`CINEDATA_REQUEST_LIMIT`), 6 consultas, 8 buscas de entidade e 2 prazos
estourados; um SQL idêntico a um que já falhou não roda de novo.

**Provedor.** O cliente do OpenRouter tem prazo de 120 s por requisição e **nenhuma retentativa
automática** do SDK. Os modelos de fallback, se configurados, entram só em falhas transitórias
(HTTP 408, 429, 5xx ou falha de conexão); 400, 401, 402, 403, 404, 413 e 422 viram erro claro.
Cabeçalhos que o SDK da OpenAI herdaria do ambiente são removidos: a requisição leva só a chave do
OpenRouter.

**Segredos.** A chave fica no `.env`, ignorado pelo Git, e nunca aparece em `repr`, mensagens de
erro, `doctor` ou arquivos da avaliação. Bancos SQLite (`*.db`, `-wal`, `-shm`) e resultados
brutos da avaliação também são ignorados. O `.env.example` traz só nomes e valores padrão.

**O que não é garantido.**

- O authorizer vê o nome da função, não o argumento: `date('now')` passa pelo `SafeDatabase`. O
  agente recusa as formas comuns de ler o relógio do SQLite, mas esse filtro de texto é
  incompleto e serve à reprodutibilidade, não à segurança (ler o relógio não escreve nada).
- As linhas consultadas vão para o provedor do modelo, como em qualquer Text-to-SQL com LLM remoto.
- Não há pretensão de sandbox perfeito: as garantias são as das camadas acima e dos testes que as
  exercitam.

## Requisitos

- **Python 3.12 ou superior.** Desenvolvido e testado com Python 3.14.3 e SQLite 3.50.4; as
  versões 3.12 e 3.13 não foram exercitadas. O piso vem de `sqlite3.Connection.setconfig`, e o
  SQLite embutido precisa ser 3.31 ou superior (o `SafeDatabase` confere cada proteção ao abrir).
- **O banco da atividade,** `cinerocket.db` (cerca de 581 MB, não versionado).
- **Uma chave do [OpenRouter](https://openrouter.ai/keys), só para chamadas reais ao modelo:**
  `cinedata ask`, `pytest -m llm` e `python -m evals.run --live`. Instalação, `doctor`, testes
  padrão e o dry-run da avaliação não precisam dela.
- Acesso ao PyPI durante a instalação.

## Instalação

Na raiz do repositório, crie o ambiente virtual:

```bash
python -m venv .venv
```

Ative-o conforme o shell:

| Shell | Comando |
|---|---|
| PowerShell (Windows) | `.venv\Scripts\Activate.ps1` |
| Git Bash (Windows) | `source .venv/Scripts/activate` |
| bash ou zsh (Linux, macOS) | `source .venv/bin/activate` |

Com o ambiente ativo, instale, crie o `.env` e confira:

```bash
python -m pip install -e ".[dev]" -c constraints.txt
cp .env.example .env
cinedata doctor
```

- No Windows, se `python` não for encontrado ou abrir a Microsoft Store, crie o ambiente com
  `py -m venv .venv`; depois de ativado, `python` já é o do ambiente. No Linux e no macOS, use
  `python3` se esse for o nome do Python 3. No `cmd.exe`, troque `cp` por `copy`.
- Se o PowerShell recusar o script de ativação ("execução de scripts foi desabilitada"), libere-o
  só na sessão atual com `Set-ExecutionPolicy -Scope Process RemoteSigned` e ative de novo.
- `-c constraints.txt` instala exatamente as versões testadas. O arquivo foi gerado no Windows com
  `pip freeze --exclude-editable`; o pip ignora pacotes que a sua plataforma não usa (como
  `colorama`).
- `[dev]` acrescenta `pytest` e `ruff`. Para só usar o agente, `python -m pip install -e . -c
  constraints.txt` basta.
- Se o shell não encontrar `cinedata`, ative o ambiente ou use `python -m cinedata`.

### Banco de dados

Coloque o banco em `data/cinerocket.db` ou aponte `CINEDATA_DB_PATH` para ele. Se o download vier
como `cinerocket (1).db`, renomeie o arquivo (o `doctor` aponta isso). Ao abrir o banco, o SQLite
pode criar `cinerocket.db-wal` e `cinerocket.db-shm` ao lado dele; é normal, e o Git os ignora. O
arquivo do banco nunca é modificado (um teste `realdb` confere). Mais em
[`data/README.md`](data/README.md).

## Configuração

O `.env` é lido da pasta atual, então execute os comandos na raiz do repositório.

| Variável | Necessária para | Padrão | Descrição |
|---|---|---|---|
| `OPENROUTER_API_KEY` | chamadas reais ao modelo | (vazio) | Chave do OpenRouter; começa com `sk-or-v1-`. |
| `CINEDATA_MODEL` | chamadas reais ao modelo | (vazio) | Id no OpenRouter (`provedor/modelo` ou `provedor/modelo:variante`), com suporte a tool calling. O `.env.example` recomenda `openrouter/free`. |
| `CINEDATA_FALLBACK_MODELS` | opcional | (vazio) | Até 2 modelos, separados por vírgula, usados só em falhas transitórias do provedor. |
| `CINEDATA_DB_PATH` | opcional | `data/cinerocket.db` | Caminho do banco (relativo à pasta atual ou absoluto). |
| `CINEDATA_MAX_ROWS` | opcional | `50` | Máximo de linhas devolvidas por consulta (1 a 200). |
| `CINEDATA_SQL_TIMEOUT_S` | opcional | `30` | Prazo de uma consulta, em segundos (1 a 300). |
| `CINEDATA_REQUEST_LIMIT` | opcional | `5` | Máximo de requisições ao modelo por pergunta (1 a 20). |
| `CINEDATA_REFERENCE_DATE` | avaliação real | data de hoje | Data (`AAAA-MM-DD`) que ancora "últimos N anos". |

- Variáveis do ambiente têm prioridade sobre o `.env`. Valores vazios contam como "não definido",
  então uma variável de ambiente vazia **não** anula um valor do `.env`.
- O código não tem modelo padrão: qualquer id do [catálogo](https://openrouter.ai/models) com
  suporte a *tool calling* serve (o agente só funciona chamando ferramentas). Há duas formas de
  configurar:
  - **`openrouter/free` (recomendado, custo zero):** não é um modelo, e sim o roteador gratuito do
    OpenRouter. Cada requisição pode ser atendida por um modelo gratuito compatível diferente, o
    que dá mais disponibilidade do que depender de um endpoint gratuito só; em troca, a
    composição de modelos varia entre requisições e entre execuções.
  - **Um modelo fixo** (por exemplo `qwen/qwen3.8-27b:free`): use quando a reprodutibilidade ou a
    comparação no nível do modelo importar. Um endpoint gratuito específico pode estar com limite
    de uso (HTTP 429) com mais frequência.

  Em qualquer caso, o rastro registra os modelos que de fato responderam (`models_used`).
  Capacidade anunciada no catálogo não garante qualidade como agente: meça com a avaliação.
- Com fallback, cada requisição do agente pode virar até 1 + (nº de fallbacks) chamadas HTTP. Deixe
  `CINEDATA_FALLBACK_MODELS` vazio num primeiro uso controlado.
- "Últimos N anos" é uma janela móvel que termina na data de referência, com os dois extremos
  incluídos (29/fev vira 28/fev quando o ano de destino não é bissexto). Sem
  `CINEDATA_REFERENCE_DATE`, vale a data de hoje e as respostas mudam com o tempo; fixe uma data
  para resultados reproduzíveis.

### Diagnóstico offline

`cinedata doctor` mostra a configuração efetiva e confere o arquivo do banco (existência, leitura e
cabeçalho SQLite), sem acessar a rede e sem imprimir a chave. Ele separa **pendências**, que
impedem o uso (código de saída 1), de **avisos**, que pedem conferência mas não bloqueiam (código
0). Um valor inválido, como `CINEDATA_MAX_ROWS=abc`, é reportado com o nome da variável e código 2.

## Uso

```bash
cinedata --help
cinedata ask --help
cinedata doctor
cinedata ask "Quais diretores têm mais filmes de Animação lançados a partir de 2010?"
cinedata ask --show-sql "Qual é a receita média em reais dos filmes de Terror por década?"
cinedata ask --json "Qual é a nota IMDb do filme Elemental?"
```

Cada `ask` consome cota do provedor. A saída traz a resposta, as premissas, as ressalvas, os
avisos do sistema e um rodapé com o tipo da resposta, o modelo que respondeu e o número de
respostas recebidas do modelo.

- `--show-sql` lista o SQL **do rastro da aplicação**, com status, linhas e tempo de cada
  consulta, nunca um SQL escrito no texto do modelo.
- `--json` separa `answer` (escrito pelo modelo) de `runtime` e `notices` (escritos pela
  aplicação): consultas, buscas de entidade, modelos, tokens e tempo.
- Caracteres de controle vindos do banco ou do modelo são removidos da saída do terminal.

Outros exemplos de perguntas (ilustrativos, não uma lista do que é aceito): "Top 10 filmes com
maior receita em R$", "Quais atores mais trabalharam com Christopher Nolan?", "Quantos filmes
lançados nos últimos 3 anos têm nota IMDb acima de 7?" e "O que você sabe responder?".

| Código de saída | Significado |
|---|---|
| 0 | `doctor` sem pendências (pode listar avisos), `--version`/`--help`, ou o `ask` respondeu (inclusive com pedido de esclarecimento ou recusa fora do escopo). |
| 1 | O `doctor` encontrou pendências, ou o `ask` não conseguiu responder (provedor, banco, limites do agente). |
| 2 | Uso ou configuração inválidos (inclusive chave ou modelo ausentes no `ask`). |
| 130 | Interrompido com Ctrl+C. |

Se os acentos aparecerem corrompidos no terminal, defina `PYTHONUTF8=1` (ou
`PYTHONIOENCODING=utf-8`) e rode o comando de novo. A saída redirecionada já sai em UTF-8.

## Avaliação

### Gabarito dos exemplos oficiais

`src/cinedata/reference.py` guarda, para cada um dos 14 exemplos, a pergunta literal do enunciado,
a semântica aprovada e um SQL legível que serve de gabarito. O executor é genérico: um caso é um
dado, e paráfrases ou perguntas novas entram por registro, sem código novo. Os 14 SQLs são
conferidos offline contra um oráculo independente em Python, com aritmética exata, em bancos
sintéticos aleatórios (`tests/test_reference.py`), e no banco real inteiro
(`tests/test_reference_realdb.py`).

| # | Pergunta (enunciado) | Decisões principais |
|---|---|---|
| 01 | Top 10 filmes com maior receita em R$ | `receita_brl`; receita ≈ faturamento ≈ bilheteria |
| 02 | Lucro médio por gênero, considerando apenas filmes com receita informada | `lucro_brl` literal; orçamento não é exigido |
| 03 | Filmes com maior margem de lucro, entre os que possuem receita e orçamento informados | (receita − orçamento) / receita, em REAL; receita > 0; top 10 |
| 04 | Os 5 filmes mais populares | `popularidade` literal, sem remover valores estranhos |
| 05 | Filmes com maior divergência entre a nota TMDB e a nota IMDb | IMDb > 0; TMDB 0 sem votos = sem nota, com votos = nota; top 10 |
| 06 | Nota média IMDb por ano de lançamento | IMDb > 0, sem filtro de status ou data |
| 07 | Ator com mais participações em filmes lançados nos últimos 5 anos | janela móvel inclusiva, `data_lancamento`, só `Lançado` |
| 08 | Diretores com maior nota média (mínimo de 5 filmes) | 5 filmes dirigidos no total; média só de IMDb > 0; top 10 |
| 09 | Dupla ator–diretor que mais trabalhou junta | filmes em comum pela ponte; nomes iguais não são excluídos |
| 10 | Quantidade de filmes por gênero | filmes distintos por gênero, inclusive gêneros com zero |
| 11 | Produtora com maior lucro total | soma de `lucro_brl`, sem filtro de receita e sem DISTINCT |
| 12 | Gênero com maior margem de lucro média | média simples das margens por filme (não ponderada) |
| 13 | Filmes mais avaliados pelos usuários | `dim_reviews.qtd_avaliacoes_usuarios`; top 10 |
| 14 | Filmes em que a nota média dos usuários mais diverge da nota IMDb | média de usuários existente e IMDb > 0; top 10 |

Convenções comuns: quando o enunciado não fixa N, o top 10 é decisão de exibição; rankings usam
`RANK()`, então os empatados no corte entram e "o maior" devolve todos os líderes empatados;
métricas calculadas são arredondadas antes de ranquear (dinheiro em 2 casas, notas em 9, margens
em 12), para que valores iguais empatem apesar do ponto flutuante; "últimos 5 anos" usa a data de
referência do projeto, nunca o relógio do SQLite.

Particularidades do banco real que orientaram essas decisões:

- O lucro da Gold existe mesmo com um lado faltando (sem orçamento, lucro = receita; sem receita,
  lucro = −orçamento). O caso 02 exige a receita, como o enunciado; o 11 soma esse lucro.
- Receita e orçamento ficam ora em INTEGER, ora em REAL, e a divisão inteira do SQLite zeraria a
  margem; por isso o `CAST(... AS REAL)`.
- Títulos se repetem (os filmes mais avaliados são quase todos "Die Hart", com ids diferentes), por
  isso todo resultado de filme carrega `id_filme` como chave.
- Há `popularidade` igual ao ano do título e linhas de `Diretor` que não são pessoas
  ("Documentary", "Drama"); os dados são usados como estão.
- A margem média por gênero (caso 12) é negativa em todos os gêneros, dominada por receitas ínfimas.

### Corpus e pontuação

`evals/` mede se o agente responde certo, sem LLM-juiz e sem gastar cota para pontuar. A
especificação completa da pontuação está em [`evals/README.md`](evals/README.md).

| Categoria | Casos | Papel |
|---|---|---|
| `official` | 14 | Os exemplos do enunciado, lidos do gabarito acima, sem cópia. |
| `paraphrase` | 5 | Variações de linguagem de exemplos oficiais (sinônimo, registro informal, sem acentos, singular). |
| `freeform` | 4 | Perguntas analíticas novas, sem caso oficial: atores de Terror, filmes de um diretor, receita em USD, janela de 3 anos. |
| `policy` | 3 | Título ambíguo, pedido fora do escopo e pergunta sobre o próprio agente. |

- **Gabarito na hora:** calculado no mesmo banco e com a mesma data de referência do agente. As
  perguntas livres têm gabaritos próprios, conferidos contra cálculos independentes em Python.
- **SQL por resultado, não por texto:** o lado do agente vem do rastro. Uma consulta conta se
  reproduz o gabarito por valor (aliases, colunas extras e ordem das colunas não importam; linhas
  como multiconjunto; filme por `id_filme` ou título + ano; tolerâncias numéricas explícitas), se
  o modelo a leu antes da resposta final e se não foi truncada. Rankings exigem todos os
  empatados no corte do top N.
- **Texto final:** cada linha exigida precisa aparecer com identidade e métrica principal, na
  ordem do ranking, sem linhas inventadas em listas ou tabelas; um resultado vazio precisa ser dito.
- **Política:** fora do escopo e ajuda sem nenhuma ferramenta de dados e com um texto mínimo;
  título ambíguo com esclarecimento comprovado pela busca dos homônimos ou com a resposta completa
  para todos eles.
- **Falha do provedor não é falha semântica:** chave, crédito, moderação, modelo inexistente,
  HTTP 408, 429, 5xx e falhas de conexão, assim como banco indisponível ou defeito do
  pontuador, são **não avaliado** e ficam fora da taxa. Uma requisição recusada pelo provedor
  (400, 413, 422), o limite de requisições ou uma falha de protocolo contam como falha do
  conjunto modelo + agente.

### Como executar

Na raiz do repositório (o pacote `evals` não é instalado; ele roda a partir dela):

```bash
python -m evals.run --help
python -m evals.run --tier smoke
python -m evals.run --tier full --check-oracles
python -m evals.run --tier smoke --primary-only --live
python -m evals.run --tier smoke --primary-only --live --resume
```

- Sem `--live`, é um **dry-run**: mostra os casos, a configuração e o teto de requisições, e nada é
  enviado ao provedor. `--check-oracles` calcula os gabaritos no banco, ainda sem provedor.
- Tiers: `smoke` (4 casos: margem com empate no corte, nota IMDb por ano, top 5 atores de Terror e
  título ambíguo), `official`, `paraphrase`, `freeform`, `policy` e `full` (26). `--id CASO`
  (repetível) e `--limit N` escolhem casos; `--model` troca o modelo principal; `--out` escolhe o
  arquivo.
- `--primary-only` descarta os fallbacks, para que tentativas extras não multipliquem o gasto.
- `--live` exige chave, modelo e `CINEDATA_REFERENCE_DATE` fixada (os gabaritos foram conferidos
  com `2026-10-01`). Não há retentativa automática: a primeira falha de provedor ou de banco, uma
  requisição recusada ou um defeito do pontuador interrompe a execução, e os casos restantes ficam
  pendentes.
- `--resume` continua o mesmo arquivo, com as mesmas opções: pula os casos já avaliados e roda de
  novo os não avaliados.
- **Cota:** cada caso usa no máximo `CINEDATA_REQUEST_LIMIT` requisições do agente; com o `.env`
  padrão, o `smoke` custa até 20 e o `full` até 130. O plano mostra o teto antes de executar.

### Resultados e reprodutibilidade

Cada caso é gravado assim que termina, em `evals/results/raw/<tier>-<modelo>-<data>.json` (pergunta,
resposta, SQL, linhas, gabarito, uso e veredito), com um resumo `.md` ao lado. A pasta `raw/` é
ignorada pelo Git, e a chave (ou qualquer texto com cara de chave) é removida antes de gravar.
Cada registro guarda os `models_used` do caso, o que importa com `openrouter/free`, em que o
roteador escolhe o modelo a cada requisição. Execuções reais revisadas são resumidas em
[`evals/RESULTS.md`](evals/RESULTS.md), o único registro de resultados versionado.

Cada arquivo registra o que precisa ser igual para dois resultados conviverem: modelo, fallbacks,
data de referência e limites; o SHA-256 do código da avaliação (`evals/cases.py`,
`evals/scoring.py`, `evals/run.py`, `src/cinedata/reference.py`) e do agente (`agent.py`,
`prompt.py`, `runtime.py`, `llm.py`, `entities.py`, `db.py`, `config.py`); as versões de Python,
SQLite, PydanticAI e do cliente OpenAI; e o SHA-256 do conteúdo do banco. O `--resume` recusa
qualquer diferença em vez de misturar resultados. Documentação não entra nessa impressão digital.

### Estado atual

A primeira execução real completa do `smoke` (4 casos, `openrouter/free`, data de referência
2026-10-01) teve **3 pass e 1 fail**: o caso do título ambíguo falhou por `agent_protocol`. Um
diagnóstico separado desse caso, com o mesmo código, configuração e pergunta, passou com outros
modelos escolhidos pelo roteador; ele não altera o resultado do smoke. Os `models_used` diferentes
mostram a variação esperada de um roteador gratuito: resultados reais variam entre execuções,
enquanto o gabarito e a pontuação continuam determinísticos e validados offline. Não há taxa de
acerto sobre os 26 casos. Detalhes em [`evals/RESULTS.md`](evals/RESULTS.md).

## Testes

```bash
pytest -q
pytest -m realdb
pytest -m "not realdb"
ruff check .
ruff format --check .
```

- **`pytest` padrão é offline** e não fala com o OpenRouter: o `conftest.py` desliga as
  requisições a modelos (`ALLOW_MODEL_REQUESTS=False`), bloqueia a resolução de nomes fora do
  loopback, remove `OPENROUTER_API_KEY` e `CINEDATA_*` do ambiente e roda cada teste numa pasta
  temporária, sem o `.env`. `tests/test_model_requests_blocked.py` prova os bloqueios. O agente
  é testado com modelos roteirizados (`FunctionModel`).
- **`realdb`:** testes contra o banco real, somente leitura. Eles usam sempre `data/cinerocket.db`
  (não leem `CINEDATA_DB_PATH`) e são pulados, com o motivo indicado, quando o arquivo não existe.
  Assim, um clone sem o banco passa na suíte padrão.
- **`llm`:** chamadas reais, **fora** do `pytest` padrão e só de propósito. Elas só rodam quando a
  expressão `-m` cita `llm`; com qualquer outra (como `-m "not realdb"`), são puladas com o motivo
  indicado. Consomem cota, usam `OPENROUTER_API_KEY` e `CINEDATA_MODEL` do `.env` e só o modelo
  principal (4 perguntas, cada uma com no máximo `CINEDATA_REQUEST_LIMIT` requisições):

```bash
pytest -m llm tests/test_agent_llm.py -v -s
```

| Área | Arquivos em `tests/` |
|---|---|
| Configuração e CLI | `test_config.py`, `test_cli.py`, `test_cli_ask.py` |
| Banco seguro | `test_db.py`, `test_db_bulk.py`, `test_db_realdb.py` |
| Entidades | `test_entities.py`, `test_entities_realdb.py` |
| Gabarito | `test_reference.py`, `test_reference_realdb.py`, `reference_oracle.py` |
| Agente e provedor | `test_agent.py`, `test_agent_boundaries.py`, `test_llm.py`, `test_agent_realdb.py`, `test_agent_llm.py` |
| Avaliação | `test_evals_corpus.py`, `test_evals_scoring.py`, `test_evals_run.py`, `test_evals_realdb.py` |
| Isolamento | `conftest.py`, `test_model_requests_blocked.py` |

## Limitações conhecidas

- **Provedor externo:** disponibilidade, limites de uso e a oferta de modelos gratuitos do
  OpenRouter estão fora do controle do projeto, e não há retentativa automática.
- **A correção depende do modelo:** o código garante que a resposta veio depois de dados reais e
  que nomes ambíguos não são escolhidos às cegas, não que o SQL é o certo. A correção é medida pela
  avaliação, só nos casos do corpus; com modelo real, só o `smoke` foi executado.
- **Uma pergunta por vez:** não há memória de conversa. Depois de um pedido de esclarecimento,
  faça uma nova pergunta mais específica (por exemplo, com o ano do filme).
- **Resolução de nomes:** só o nome exato e único resolve; o fuzzy só sugere e tem recall limitado
  (um token errado na primeira e na última letra ao mesmo tempo não é encontrado). A política de
  ambiguidade enxerga as chaves `sk_*` usadas no SQL: um modelo que filtre direto pelo texto do
  título, contra as instruções, não é detectado por ela.
- **Respostas `info` não são verificadas:** o rodapé mostra "sem dados do banco" e o `--json`
  mostra zero consultas, mas o texto de uma resposta `info` não é conferido.
- **Pontuador deliberadamente estreito:** a leitura do texto final é determinística, não um juiz de
  linguagem. Formas incomuns de uma resposta certa podem falhar e prosa solta não é julgada; a
  lista está em [`evals/README.md`](evals/README.md#limitações).
- **Modelo variável por requisição:** com `openrouter/free` ou com fallback, requisições de uma
  mesma pergunta podem ser respondidas por modelos diferentes, e duas execuções da mesma pergunta
  podem ter desfechos diferentes (o rastro registra cada modelo). `model_responses` conta respostas
  recebidas, não chamadas HTTP, e não mede consumo exato de cota.
- **Uso como biblioteca:** `ask()` cria o próprio laço de eventos; em código assíncrono, use
  `ask_async()`.

## Estrutura

```text
.env.example          variáveis de ambiente (sem segredos)
constraints.txt       versões exatas testadas (gerado por pip freeze)
pyproject.toml        pacote, comando `cinedata`, pytest e ruff
data/                 lugar do cinerocket.db (não versionado)
src/cinedata/
  config.py           configuração, validação e janela móvel de datas
  db.py               SafeDatabase: acesso somente leitura e endurecido ao banco
  entities.py         EntityIndex: resolução de filmes, pessoas, gêneros e produtoras
  prompt.py           instruções do agente: esquema, relações, semântica e regras de SQL
  agent.py            o agente: ferramentas, validação da resposta e limites
  runtime.py          resposta tipada, rastro da execução, falhas e avisos
  llm.py              cliente do OpenRouter, fallback seletivo e falhas do provedor
  cli.py              comandos --version, doctor e ask
  reference.py        gabarito dos 14 exemplos oficiais (avaliação)
evals/                avaliação (fora do pacote; o agente nunca a importa)
  cases.py            corpus, regras de conferência e tiers
  scoring.py          pontuação determinística (sem LLM-juiz)
  run.py              executor: dry-run, --live, --resume, JSON e resumo
  RESULTS.md          execuções reais revisadas
  results/raw/        resultados brutos (não versionados)
tests/                testes offline, `realdb` e `llm` (opt-in)
```
