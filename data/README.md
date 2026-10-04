# Pasta `data/`

Coloque aqui o banco da atividade com o nome **`cinerocket.db`** (`data/cinerocket.db`).

- O arquivo tem cerca de 581 MB e **não é versionado**: o `.gitignore` ignora `*.db` (e os
  auxiliares do SQLite) em qualquer pasta do repositório.
- Se o download vier com um sufixo como `cinerocket (1).db` (acontece quando o arquivo é baixado
  mais de uma vez), renomeie para `cinerocket.db` com o comando do seu terminal (tabela abaixo).
  Para usar outro nome ou outra pasta, defina `CINEDATA_DB_PATH` no `.env`. Os testes `realdb`
  não leem essa variável: eles usam sempre `data/cinerocket.db` e são pulados (não falham) quando
  o arquivo não está aqui.
- O banco usa o modo WAL. Ao abri-lo em modo somente leitura, o SQLite pode criar ao lado dele os
  arquivos auxiliares `cinerocket.db-wal` e `cinerocket.db-shm`. Isso é normal, e eles também
  estão ignorados pelo `.gitignore`.
- Para conferir o arquivo, execute `cinedata doctor` na raiz do repositório. Ele confere se o
  arquivo existe, pode ser lido e tem o cabeçalho de um banco SQLite; as tabelas da Gold só são
  conferidas ao abrir o banco (no `ask`, no `--check-oracles` da avaliação e nos testes `realdb`).

Renomear o download, na raiz do repositório:

| Terminal | Comando |
|---|---|
| PowerShell | `Rename-Item "data\cinerocket (1).db" cinerocket.db` |
| CMD | `ren "data\cinerocket (1).db" cinerocket.db` |
| Git Bash, Linux, macOS | `mv "data/cinerocket (1).db" data/cinerocket.db` |
