# Solução de problemas

Problemas comuns de instalação e configuração. Os comandos de instalação de cada terminal estão no
[README](../README.md#instalação), e os ambientes validados, em
[Ambientes validados](../README.md#ambientes-validados).

- [Qual Python estou usando?](#qual-python-estou-usando)
- [`python` abre a Microsoft Store](#python-abre-a-microsoft-store)
- [`py` não é reconhecido](#py-não-é-reconhecido)
- [A `.venv` tem `bin` em vez de `Scripts`](#a-venv-tem-bin-em-vez-de-scripts)
- [Python do MSYS2](#python-do-msys2)
- [A ativação da `.venv` falhou](#a-ativação-da-venv-falhou)
- [Python mais antigo que 3.12](#python-mais-antigo-que-312)
- [Banco de dados ausente](#banco-de-dados-ausente)
- [Chave da API ausente](#chave-da-api-ausente)
- [Chave expirada ou inválida (HTTP 401)](#chave-expirada-ou-inválida-http-401)
- [Limite de uso do provedor gratuito (HTTP 429)](#limite-de-uso-do-provedor-gratuito-http-429)
- [O que o doctor confere](#o-que-o-doctor-confere)
- [Acentos corrompidos no terminal](#acentos-corrompidos-no-terminal)
- [`cinedata` não é reconhecido](#cinedata-não-é-reconhecido)

## Qual Python estou usando?

No Windows, o ambiente validado é o CPython oficial do python.org, chamado pelo `py`. Para conferir
qual Python o `py` usa, rode no PowerShell, no CMD ou no Git Bash (no Linux e no macOS, troque
`py` por `python3`):

```text
py -c "import sys, sysconfig; print(sys.executable); print(sys.version); print(sysconfig.get_platform())"
```

No Python oficial do Windows, a saída se parece com esta:

```text
C:\Users\voce\AppData\Local\Python\pythoncore-3.14-64\python.exe
3.14.3 (tags/v3.14.3:323c59a, Feb  3 2026, 16:04:56) [MSC v.1944 64 bit (AMD64)]
win-amd64
```

- A versão cita `MSC` (o compilador da Microsoft, usado no Python oficial) e a última linha é
  `win-amd64` (ou `win-arm64`). O caminho varia com o instalador.
- Um caminho dentro de `C:\msys64`, uma versão compilada com `GCC` ou `Clang` ou uma última linha
  que começa com `mingw`, `msys` ou `cygwin` indicam um Python do MSYS2 ou do Cygwin (veja
  [Python do MSYS2](#python-do-msys2)).

Depois de ativar a `.venv`, confira se o `python` é o dela; a saída deve terminar em `.venv`:

```text
python -c "import sys; print(sys.prefix)"
```

## `python` abre a Microsoft Store

No Windows, quando nenhum Python oficial está no PATH, o comando `python` pode ser um atalho do
sistema que abre a Microsoft Store ou que só imprime uma sugestão de instalar pela loja.

- Instale o Python oficial do [python.org](https://www.python.org/downloads/) e crie a `.venv` com
  `py`, como na [Instalação](../README.md#instalação).
- Depois de ativar a `.venv`, `python` passa a ser o dela, e o atalho deixa de importar.
- O Python da Microsoft Store não é necessário: ele fica fora da matriz validada e não traz o `py`.

## `py` não é reconhecido

Sintoma: "O termo 'py' não é reconhecido como nome de cmdlet..." (PowerShell), "'py' não é
reconhecido como um comando interno ou externo..." (CMD) ou `py: command not found` (Git Bash).

Causas comuns: o Python oficial não está instalado; foi instalado sem o launcher `py` (uma opção
do instalador); veio da Microsoft Store, que não traz o `py`; ou o terminal foi aberto antes da
instalação.

- Instale o Python oficial do python.org, mantendo o `py`, e abra um terminal novo.
- Confira com o teste de [Qual Python estou usando?](#qual-python-estou-usando).
- Se só houver o `python` e o teste (com `python` no lugar de `py`) mostrar `MSC` e `win-amd64`,
  ele é o oficial, e `python -m venv .venv` serve do mesmo jeito.

## A `.venv` tem `bin` em vez de `Scripts`

Sintoma, no Windows: o script de ativação não é encontrado, e a pasta `.venv` tem uma subpasta
`bin` em vez de `Scripts`.

Causa: a `.venv` foi criada por um Python que segue a convenção do Linux: do MSYS2, do Cygwin ou
do WSL. O Python oficial do Windows sempre cria `Scripts`.

Solução: apague a `.venv` (ela é descartável: tudo nela é reinstalado) e crie de novo com `py`,
seguindo a [Instalação](../README.md#instalação) desde o primeiro comando. Se ela estiver ativa,
rode `deactivate` antes.

| Terminal | Apagar a `.venv` |
|---|---|
| PowerShell | `Remove-Item -Recurse -Force .venv` |
| CMD | `rmdir /s /q .venv` |
| Git Bash | `rm -rf .venv` |

## Python do MSYS2

Sintoma: o teste de [Qual Python estou usando?](#qual-python-estou-usando) mostra um caminho
dentro de `C:\msys64`, uma versão compilada com `GCC` ou `Clang` ou uma plataforma que começa com
`mingw`, `msys` ou `cygwin`. Costuma vir junto com a `.venv` que tem `bin`.

O MSYS2 traz o próprio Python, diferente do oficial. Ele não faz parte da matriz validada: pode
funcionar, mas não foi testado, e o PyPI costuma não ter pacotes binários para ele, então o `pip`
pode tentar compilar dependências como `pydantic-core`.

Solução: não é preciso desinstalar o MSYS2. Instale o Python oficial e crie a `.venv` com `py`,
inclusive no Git Bash: por padrão, o `py` escolhe um Python oficial instalado. Se a `.venv` já foi
criada pelo Python do MSYS2, apague-a antes (item anterior).

## A ativação da `.venv` falhou

**PowerShell: "a execução de scripts foi desabilitada neste sistema".** O Windows vem com a
política de execução `Restricted`, que bloqueia todo script `.ps1`, inclusive o `Activate.ps1`.
Libere scripts só na janela atual (vale até fechá-la e não precisa de administrador) e ative de
novo:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

- Para não repetir em cada janela, o mesmo comando com `-Scope CurrentUser` vale só para o seu
  usuário e fica gravado. `RemoteSigned` continua bloqueando scripts baixados da internet sem
  assinatura.
- Não use `Unrestricted` nem `Bypass` e não mude a política da máquina inteira (`LocalMachine`).
- Se a política vier de uma política de grupo da empresa, o comando não tem efeito: use o CMD ou
  rode sem ativar (tabela abaixo).

**"não é reconhecido" ou "No such file or directory".** A `.venv` não existe na pasta atual (rode
os comandos na raiz do repositório, onde estão o `README.md` e o `pyproject.toml`), o comando não
é o do seu terminal ou a `.venv` tem `bin` em vez de `Scripts` (item acima).

| Terminal | Ativar | Usar sem ativar |
|---|---|---|
| PowerShell | `.\.venv\Scripts\Activate.ps1` | `.\.venv\Scripts\python.exe -m cinedata doctor` |
| CMD | `.venv\Scripts\activate.bat` | `.venv\Scripts\python.exe -m cinedata doctor` |
| Git Bash | `source .venv/Scripts/activate` | `.venv/Scripts/python.exe -m cinedata doctor` |
| Linux, macOS | `source .venv/bin/activate` | `.venv/bin/python -m cinedata doctor` |

Com a `.venv` ativa, o prompt mostra `(.venv)` e o teste de `sys.prefix` de
[Qual Python estou usando?](#qual-python-estou-usando) termina em `.venv`.

## Python mais antigo que 3.12

Sintoma: o `pip install` falha com "requires a different Python" e cita `>=3.12`.

Solução: instale o Python 3.12 ou mais novo, apague a `.venv` e crie de novo escolhendo a versão,
por exemplo `py -3.14 -m venv .venv`. No Linux, use um `python3` 3.12 ou mais novo; no Debian e no
Ubuntu, o `venv` pode exigir o pacote `python3-venv` do sistema.

## Banco de dados ausente

Sintoma: o `doctor` lista o banco como `NÃO ENCONTRADO` (pendência, código 1), o `ask` responde
"Banco de dados indisponível" e o `pytest` mostra os testes `realdb` como pulados
(`data/cinerocket.db não encontrado`).

Solução: coloque o arquivo em `data/cinerocket.db` ou aponte `CINEDATA_DB_PATH` para ele no
`.env`. O [`data/README.md`](../data/README.md) tem o comando de cada terminal para renomear um
download como `cinerocket (1).db`. Sem o banco, os testes `realdb` são pulados de propósito: o
resto da suíte continua valendo.

## Chave da API ausente

Sintoma: o `doctor` lista `OPENROUTER_API_KEY ausente` como pendência, e o `ask` termina com
"Erro de configuração: OPENROUTER_API_KEY não configurada" (código 2).

Solução: crie uma chave em [openrouter.ai/keys](https://openrouter.ai/keys) e cole no `.env`, sem
aspas e sem espaços, no formato `OPENROUTER_API_KEY=sk-or-v1-...`. A instalação, o `doctor`, os
testes e o dry-run da avaliação funcionam sem a chave.

## Chave expirada ou inválida (HTTP 401)

Sintoma: o `doctor` diz que a chave está presente, mas o `ask` termina com "(HTTP 401): o
OpenRouter recusou a chave da API" (código 1).

Causa: o `doctor` é offline e só confere se a chave existe e começa com `sk-or-v1-`. Quem valida a
chave é o OpenRouter, na primeira chamada real.

Solução: gere uma chave nova em [openrouter.ai/keys](https://openrouter.ai/keys) e troque-a no
`.env`. Uma `OPENROUTER_API_KEY` definida no ambiente do terminal tem prioridade sobre o `.env`;
se uma chave antiga estiver lá, remova-a da sessão:

| Terminal | Comando |
|---|---|
| PowerShell | `Remove-Item Env:OPENROUTER_API_KEY -ErrorAction SilentlyContinue` |
| CMD | `set OPENROUTER_API_KEY=` |
| Git Bash, Linux, macOS | `unset OPENROUTER_API_KEY` |

Se ela voltar num terminal novo, está definida nas variáveis de ambiente do Windows ou no perfil do
shell; remova-a de lá.

## Limite de uso do provedor gratuito (HTTP 429)

Sintoma: o `ask` termina com "(HTTP 429): limite de uso do provedor atingido" (código 1). Na
avaliação real, o caso fica "não avaliado", e os seguintes, pendentes.

Causa: modelos gratuitos têm limites de uso (por minuto e por dia), e um endpoint gratuito
específico pode estar saturado. Não é falha do agente nem da instalação.

Solução:

- espere alguns minutos e tente de novo;
- prefira `CINEDATA_MODEL=openrouter/free`, que distribui as requisições entre modelos gratuitos;
- ou configure até 2 modelos em `CINEDATA_FALLBACK_MODELS`, que só entram em falhas transitórias
  como esta (cada fallback pode virar mais uma chamada HTTP).

O projeto não repete requisições automaticamente, de propósito, para não gastar cota sem controle.
Na avaliação, `--resume` roda de novo os casos não avaliados.

## O que o doctor confere

`cinedata doctor` é offline: não faz nenhuma chamada de rede e nunca mostra a chave.

Ele confere:

- a versão do Python e o sistema operacional;
- se há um `.env` na pasta atual;
- se cada variável tem formato e faixa válidos (modelo, fallbacks, limites e data de referência);
- se a chave existe e começa com `sk-or-v1-`;
- se o arquivo do banco existe, pode ser lido e tem o cabeçalho de um banco SQLite.

Ele não confere:

- se a chave é válida, está ativa ou tem crédito (HTTP 401 e 402 aparecem no `ask`);
- se o modelo existe, suporta tool calling ou está com limite de uso (HTTP 404 e 429 aparecem no
  `ask`);
- a conexão com a internet e com o OpenRouter;
- se o banco tem as tabelas da Gold e se o SQLite tem as proteções exigidas: isso é conferido ao
  abrir o banco, no `ask`, no `python -m evals.run --check-oracles` e nos testes `realdb`;
- se o Python é o da matriz validada ou se a `.venv` está ativa (use o teste de
  [Qual Python estou usando?](#qual-python-estou-usando)).

Códigos de saída: 0 sem pendências (pode listar avisos), 1 com pendências e 2 com um valor inválido
na configuração.

## Acentos corrompidos no terminal

Sintoma: letras como "ç" e "ã" aparecem trocadas por outros símbolos. Ative o modo UTF-8 do
Python na sessão e rode o comando de novo:

| Terminal | Comando |
|---|---|
| PowerShell | `$env:PYTHONUTF8 = "1"` |
| CMD | `set PYTHONUTF8=1` |
| Git Bash, Linux, macOS | `export PYTHONUTF8=1` |

## `cinedata` não é reconhecido

Ative a `.venv` ([A ativação da `.venv` falhou](#a-ativação-da-venv-falhou)) ou use o Python dela
diretamente, como na coluna "Usar sem ativar" da tabela daquela seção.
