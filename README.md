# CineData Agent

Agente que responde, em linguagem natural, perguntas sobre o catálogo de filmes da CineData
Analytics. Ele consulta a camada Gold (SQLite) por Text-to-SQL e **somente em modo leitura**.
Projeto da atividade GenAI do Rocket Lab 2026.2.

> **Status: marco M1a (acesso seguro ao banco).** Já existem a configuração, o diagnóstico
> offline (`cinedata doctor`), a infraestrutura de testes e o `SafeDatabase`, a única porta de
> entrada para o banco. O agente e a suíte de avaliação chegam nos próximos marcos, e este README
> será completado a cada um deles.

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

## Estrutura

```text
.env.example          modelo das variáveis de ambiente (sem segredos)
constraints.txt       versões exatas das dependências (gerado por comando)
data/                 coloque aqui o cinerocket.db (não versionado)
src/cinedata/
  config.py           configuração, validação e janela móvel de datas
  db.py               SafeDatabase: acesso somente leitura e endurecido ao banco
  cli.py              comandos --version e doctor
tests/                testes offline e testes `realdb`
```

## Segredos

O `.env` e os arquivos de banco SQLite (`*.db` e similares, em qualquer pasta) **nunca** são
versionados: estão no `.gitignore`, e qualquer exceção precisa ser explícita. Não coloque chaves
em código, testes ou no `.env.example`, que traz apenas os nomes das variáveis.
