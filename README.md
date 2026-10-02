# CineData Agent

Agente que responde, em linguagem natural, perguntas sobre o catálogo de filmes da CineData
Analytics. Ele consulta a camada Gold (SQLite) por Text-to-SQL e **somente em modo leitura**.
Projeto da atividade GenAI do Rocket Lab 2026.2.

> **Status: marco M1c (casos de referência).** Já existem a configuração, o diagnóstico
> offline (`cinedata doctor`), a infraestrutura de testes, o `SafeDatabase` (a única porta de
> entrada para o banco), o `EntityIndex`, que resolve nomes de filmes, pessoas, gêneros e
> produtoras, e os casos de referência (gabarito) para a avaliação. O agente e a suíte de
> avaliação chegam nos próximos marcos, e este README será completado a cada um deles.

## Requisitos

- Python 3.12 ou superior
- SQLite 3.31 ou superior (já vem com o Python; ver a nota abaixo)
- Git
- O arquivo `cinerocket.db` da atividade e uma chave do [OpenRouter](https://openrouter.ai/keys)
  (o banco é necessário para os testes `realdb`; a chave, a partir do agente)

**Nota sobre as versões.** O acesso ao banco usa `Connection.setconfig`, que só existe a partir do
Python 3.12, e opções do SQLite que só existem a partir da 3.31 (`DEFENSIVE` na 3.26, `DQS_*` na
3.29 e `TRUSTED_SCHEMA` na 3.31). Em vez de confiar só na versão, o `SafeDatabase` confere cada
proteção ao abrir o banco e **se recusa a funcionar** se a build não a oferecer. A execução real foi
validada em Python 3.14.3 com SQLite 3.50.4. O piso 3.12 é sustentado pela documentação oficial e
pela resolução de dependências, mas a suíte ainda não foi executada em 3.12.

## Instalação

Execute na raiz do repositório. Os comandos abaixo são para o Git Bash no Windows; as
alternativas vêm logo depois.

```bash
py -m venv .venv
source .venv/Scripts/activate
python -m pip install -e ".[dev]" -c constraints.txt
```

O `-c constraints.txt` instala exatamente as versões testadas (o arquivo é gerado por comando,
nunca editado à mão):

```bash
python -m pip install -e ".[dev]"
python -m pip freeze --exclude-editable > constraints.txt
```

Alternativas:

- **PowerShell:** ative com `.venv\Scripts\Activate.ps1`. Para gerar o `constraints.txt`, troque
  o `>` por `| Set-Content -Encoding ascii constraints.txt` (o `>` do PowerShell 5.1 grava UTF-16 e
  o pip não o lê).
- **Linux/macOS:** `python3 -m venv .venv` e `source .venv/bin/activate`. O `constraints.txt`
  gerado no Windows inclui pacotes só desse sistema (como `colorama`); o pip ignora os que não
  são necessários na sua máquina.

## Configuração

```bash
cp .env.example .env
```

Edite o `.env`. Valores vazios contam como "não definido", e variáveis do ambiente têm prioridade
sobre o `.env`. O `.env` é lido da pasta atual, por isso execute os comandos na raiz do
repositório.

| Variável | Padrão | Descrição |
|---|---|---|
| `OPENROUTER_API_KEY` | (vazio) | Chave do OpenRouter; começa com `sk-or-v1-`. |
| `CINEDATA_MODEL` | (vazio) | Id do modelo no OpenRouter (`provedor/modelo` ou `provedor/modelo:variante`). |
| `CINEDATA_FALLBACK_MODELS` | (vazio) | Até 2 modelos de fallback, separados por vírgula. |
| `CINEDATA_DB_PATH` | `data/cinerocket.db` | Caminho do banco SQLite (relativo à pasta atual ou absoluto). |
| `CINEDATA_MAX_ROWS` | `50` | Máximo de linhas devolvidas por consulta (1 a 200). |
| `CINEDATA_SQL_TIMEOUT_S` | `30` | Tempo máximo de uma consulta SQL, em segundos (1 a 300). |
| `CINEDATA_REQUEST_LIMIT` | `5` | Máximo de requisições ao modelo por pergunta (1 a 20). |
| `CINEDATA_REFERENCE_DATE` | hoje | Data de referência (`AAAA-MM-DD`) para "últimos N anos". |

**Modelo:** ainda não há modelo padrão validado. `CINEDATA_MODEL` fica vazio até os testes reais
do marco M2, e este README passará a indicar o modelo escolhido.

**Data de referência:** a janela de "últimos 5 anos" é móvel e vai de cinco anos antes da data de
referência até ela, com os dois extremos incluídos (29/fev vira 28/fev quando o ano de destino
não é bissexto). Fixe `CINEDATA_REFERENCE_DATE` para obter resultados reproduzíveis.

## Banco de dados

Coloque o banco da atividade em `data/cinerocket.db` (cerca de 581 MB, **não versionado**). Se o
download vier com um sufixo como `cinerocket (1).db`, renomeie o arquivo para `cinerocket.db`.
Detalhes em [`data/README.md`](data/README.md).

## Segurança do acesso ao banco

Todo acesso passa por `SafeDatabase` (`src/cinedata/db.py`). A segurança não depende do prompt nem
de analisar o texto do SQL: ela vem de camadas independentes, e cada uma tem testes que a exercitam
sozinha.

1. **Abertura somente leitura** (`mode=ro`), com a URI montada por `Path.as_uri()`.
2. **Limites do SQLite**, incluindo `SQLITE_LIMIT_ATTACHED=0`, que fecha `ATTACH` e `VACUUM INTO`
   (eles poderiam criar arquivos mesmo com `mode=ro`).
3. **Configuração endurecida e conferida por releitura:** aspas duplas nunca viram texto em silêncio
   (DQS desligado), `DEFENSIVE` ligado, `TRUSTED_SCHEMA` desligado, `query_only` e cache de 64 MB.
   Se a build não oferecer alguma proteção, o banco não abre.
4. **Authorizer com negação por padrão:** só leitura das 10 tabelas da Gold e de colunas
   permitidas (fica de fora `alembic_version`, `sqlite_master` e `movie_reviews.name`, o nome de
   quem avaliou) e só funções de uma lista fechada. Escrita, DDL, `PRAGMA`, `ATTACH`, transações,
   consultas recursivas e `CURRENT_DATE`/`CURRENT_TIME`/`CURRENT_TIMESTAMP` são negados.
5. **Prazo** por *progress handler* (padrão 30 s) e **limite de linhas** por `fetchmany(max_rows + 1)`,
   sem reescrever o SQL. Um Ctrl+C durante a consulta é repassado como `KeyboardInterrupt`.

Um pré-filtro de texto restringe as consultas a `SELECT` ou `WITH` e também melhora as mensagens
de erro. Mesmo sem ele, as demais camadas continuam garantindo o acesso somente leitura e
bloqueando operações fora da política de segurança.

**Lacunas conhecidas.** O authorizer vê o nome da função, não o argumento; por isso `date('now')`,
`date()` e formas parecidas passam. A regra "janela móvel pela data de referência, nunca pelo
relógio" fica para as instruções do agente (M2) e para a avaliação (M3). O cancelamento de uma
consulta rodando em thread de trabalho do agente também será validado no M2.

**Cargas internas grandes.** `execute(sql, max_rows=..., timeout_s=...)` aceita um teto de linhas
e um prazo só para aquela chamada, com limites máximos. Serve ao índice de entidades (424 mil
pessoas) na MESMA conexão, com o mesmo authorizer e as mesmas allowlists. Esses parâmetros nunca
são expostos ao modelo.

## Resolução de entidades

`EntityIndex` (`src/cinedata/entities.py`) transforma um texto livre em entidades do banco.
`find(tipo, texto, role=None)` devolve um destes estados:

| Estado | Quando | Resolve? |
|---|---|---|
| `exact_unique` | exatamente uma entidade tem o nome (sem maiúsculas, acentos nem pontuação) | sim |
| `exact_multiple` | duas ou mais têm o mesmo nome: homônimos nunca são fundidos | não |
| `partial_candidates` | sem nome exato; prefixo ou tokens do texto em outros nomes | não, nunca |
| `fuzzy_suggestions` | sem parcial; nomes a 1 ou 2 edições por token | não, só sugere |
| `none` | nada achado, ou texto vazio, longo demais ou inválido (`reason` diz qual) | |

Cada candidato traz desambiguadores: filme = título, ano e `id_filme`; pessoa = nome e papel
(`Diretor`, `Ator`, `Roteirista`); gênero e produtora = nome. Em `dim_people` cada papel é uma linha
própria, então um nome sem `role` costuma dar `exact_multiple`. Os gêneros também respondem em
português (Ação, Terror, Suspense, Ficção científica, Cinema TV...). A ordem dos candidatos é
determinística e não depende da ordem do banco. Cada tipo é carregado na primeira busca que o
usa, e uma carga que falhe, seja truncada ou venha vazia nunca deixa um índice parcial.

**Limite conhecido do fuzzy.** Ele só sugere e tem recall limitado: com 1 edição por token a
vizinhança é completa; com 2 (tokens de 6 letras ou mais), um token errado na primeira E na última
letra ao mesmo tempo não é encontrado.

## Casos de referência (gabarito)

`src/cinedata/reference.py` guarda a semântica aprovada e um SQL confiável e legível para as 14
perguntas do enunciado ("Categorias de Perguntas e Exemplos (Não exaustivo)"). Elas são **exemplos
oficiais e não exaustivos**: formam o conjunto mínimo de referência da avaliação, não a lista do
que o agente responde. O agente (M2) vai gerar SQL livre sobre o esquema Gold para qualquer
pergunta analítica válida e não depende de reconhecer um destes casos; eles servem para validar e
avaliar respostas (M3), nunca como roteador de intenções.

- Um caso (`ReferenceCase`) é um dado: id estável, pergunta, semântica, SQL, parâmetros (N de
  exibição, janela em anos) e as colunas esperadas. `run_case` executa qualquer caso pelo
  `SafeDatabase`, com o prazo normal, e recusa resultado truncado, colunas inesperadas ou chave
  repetida. Paráfrases e perguntas novas entram com `ReferenceRegistry.register`, sem mudar o
  executor (um teste registra um 15º caso).
- Rankings usam `RANK()`: os empatados no corte do top-N entram (o resultado pode passar de N
  linhas), e "o maior" devolve todos os líderes empatados.
- Métricas calculadas são arredondadas antes de ranquear (dinheiro em 2 casas, notas em 9,
  margens em 12), para que valores iguais empatem apesar do ponto flutuante. Um teste confere, no
  banco inteiro, que isso empata os valores exatamente iguais e só eles.
- "Últimos 5 anos" usa a data de referência do projeto (`CINEDATA_REFERENCE_DATE`), nunca o
  relógio do SQLite. O gabarito do banco real usa 2026-10-01 (janela de 2021-10-01 a 2026-10-01).

A pergunta de cada caso (`question`) é a redação literal do enunciado; reformulações ficam em
`paraphrases`.

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

Quando o enunciado não fixa N, o top 10 é decisão de exibição. Os empates no corte também valem
para os N do enunciado (01 e 04), embora no banco real eles não ocorram ali.

**Ressalvas encontradas no banco real.**

- O lucro da Gold existe mesmo com um lado faltando: sem orçamento, lucro = receita; sem receita,
  lucro = −orçamento. O caso 02 segue o enunciado (exige só a receita) e o 11 soma esse lucro.
- Receita e orçamento ficam ora em INTEGER, ora em REAL, e a divisão entre inteiros do SQLite
  zera a margem; por isso o `CAST(... AS REAL)`. O binário também separa valores iguais: as
  margens de "Bad Ben" e "Bad Ben: The Mandela Effect" são exatamente 1097/1100, mas o REAL difere
  na última casa (com o arredondamento, empatam em 9º lugar). Já duas margens diferentes chegam a
  diferir só na 11ª casa (7/8 e 0,87499999996), por isso margens usam 12 casas, e não 9.
- 102 notas TMDB vêm com ruído binário (6,903999999999999 no lugar de 6,904).
- A margem média por gênero (12) é dominada por receitas ínfimas: todos os gêneros têm média
  negativa, e o líder, War, tem −5,35 (−535%).
- Há `popularidade` igual a um ano do título ("La Fellinette" = 2020, "Wwe Survivor Series 2018"
  = 2018); como aprovado, esses valores ficam no caso 04.
- Linhas de `Diretor` incluem nomes que não são pessoas ("English", "Documentary", "Drama"); nenhum
  aparece nos gabaritos 08 e 09.
- Títulos se repetem (os mais avaliados são quase todos "Die Hart" com ids diferentes), por isso
  todo resultado de filme traz `id_filme` como chave. Neste banco, os 95.645 filmes têm
  `id_filme` distinto e não nulo (invariante verificado por um teste `realdb`), e `run_case`
  recusa qualquer resultado com chave repetida.
- Cruzar todo o elenco com toda a direção (caso 09) levou de 15 s a mais de 2 min, conforme a
  formulação. O SQL de referência usa uma poda exata (um par não tem mais filmes juntos do que
  cada pessoa tem sozinha) e roda em cerca de 2 s. Com o cache do sistema quente, os 14 casos
  levam de 0,02 a 2,1 s; na primeira execução, com a máquina carregada, o mais lento (07) chegou
  a 14 s, abaixo do prazo padrão de 30 s.


## Comandos

```bash
cinedata --version
cinedata doctor
python -m cinedata doctor
```

Se o shell não encontrar o comando `cinedata`, ative o ambiente virtual (veja Instalação) ou use
`python -m cinedata`.

O `doctor` é offline: ele mostra a configuração efetiva e confere o arquivo do banco (existência,
leitura e cabeçalho SQLite), sem acessar a rede e sem imprimir a chave da API. Ele separa
**pendências** (o que impede de usar o projeto) de **avisos** (o que merece conferência, como uma
chave sem o prefixo esperado, mas não bloqueia).

| Código de saída | Significado |
|---|---|
| 0 | Sem pendências (o `doctor` pode listar avisos) ou apenas `--version`/`--help`. |
| 1 | O `doctor` encontrou pendências (por exemplo, chave, modelo ou banco ausentes). |
| 2 | Uso ou configuração inválidos. |
| 130 | Interrompido com Ctrl+C. |

Se os acentos aparecerem corrompidos no terminal, defina `PYTHONUTF8=1` (ou
`PYTHONIOENCODING=utf-8`) e rode o comando de novo. A saída redirecionada já sai em UTF-8 por
padrão, e `PYTHONIOENCODING` explícito é respeitado.

## Testes e qualidade

```bash
pytest -q
ruff check .
ruff format --check .
```

Os testes são offline. Chamadas reais a modelos ficam bloqueadas por `ALLOW_MODEL_REQUESTS=False`
(um teste prova isso), e o `pytest` padrão exclui o marcador `llm`; testes que usarem um LLM real
serão executados de forma explícita (`pytest -m llm`) em marcos posteriores.

Os testes marcados `realdb` rodam contra o banco real e são **pulados com o motivo indicado** quando
`data/cinerocket.db` não existe. Use `pytest -m realdb` para rodar só eles e
`pytest -m "not realdb"` para excluí-los.

Os casos de referência têm dois arquivos de teste. `tests/test_reference.py` (offline) monta
cenários sintéticos para cada armadilha e compara os 14 SQLs, em bancos aleatórios, com um oráculo
independente em Python e aritmética exata (`tests/reference_oracle.py`).
`tests/test_reference_realdb.py` confere o gabarito no banco real, compara os 14 casos com o
mesmo oráculo sobre o banco inteiro e mede os tempos:

```bash
pytest tests/test_reference.py
pytest -m realdb tests/test_reference_realdb.py
pytest -m realdb -s tests/test_reference_realdb.py -k report
```

## Estrutura

```text
.env.example          modelo das variáveis de ambiente (sem segredos)
constraints.txt       versões exatas das dependências (gerado por comando)
data/                 coloque aqui o cinerocket.db (não versionado)
src/cinedata/
  config.py           configuração, validação e janela móvel de datas
  db.py               SafeDatabase: acesso somente leitura e endurecido ao banco
  entities.py         EntityIndex: resolução de filmes, pessoas, gêneros e produtoras
  reference.py        casos de referência (gabarito da avaliação) e executor genérico
  cli.py              comandos --version e doctor
tests/                testes offline e testes `realdb`
```

## Segredos

O `.env` e os arquivos de banco SQLite (`*.db` e similares, em qualquer pasta) **nunca** são
versionados: estão no `.gitignore`, e qualquer exceção precisa ser explícita. Não coloque chaves
em código, testes ou no `.env.example`, que traz apenas os nomes das variáveis.
