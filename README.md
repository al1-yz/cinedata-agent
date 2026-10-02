# CineData Agent

Agente que responde, em linguagem natural, perguntas sobre o catálogo de filmes da CineData
Analytics. Ele consulta a camada Gold (SQLite) por Text-to-SQL e **somente em modo leitura**.
Projeto da atividade GenAI do Rocket Lab 2026.2.

> **Status: marco M0 (fundação).** Já existem a configuração, o diagnóstico offline
> (`cinedata doctor`) e a infraestrutura de testes. O acesso seguro ao banco, o agente e a suíte
> de avaliação chegam nos próximos marcos, e este README será completado a cada um deles.

## Requisitos

- Python 3.11 ou superior
- Git
- O arquivo `cinerocket.db` da atividade e uma chave do [OpenRouter](https://openrouter.ai/keys)
  (necessários a partir dos próximos marcos)

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

## Estrutura

```text
.env.example          modelo das variáveis de ambiente (sem segredos)
constraints.txt       versões exatas das dependências (gerado por comando)
data/                 coloque aqui o cinerocket.db (não versionado)
src/cinedata/
  config.py           configuração, validação e janela móvel de datas
  cli.py              comandos --version e doctor
tests/                testes offline
```

## Segredos

O `.env` e os arquivos de banco SQLite (`*.db` e similares, em qualquer pasta) **nunca** são
versionados: estão no `.gitignore`, e qualquer exceção precisa ser explícita. Não coloque chaves
em código, testes ou no `.env.example`, que traz apenas os nomes das variáveis.
