# Pasta `data/`

Coloque aqui o banco da atividade com o nome **`cinerocket.db`** (`data/cinerocket.db`).

- O arquivo tem cerca de 581 MB e **não é versionado**: o `.gitignore` ignora `*.db` (e os
  auxiliares do SQLite) em qualquer pasta do repositório.
- Se o download vier com um sufixo como `cinerocket (1).db` (acontece quando o arquivo é baixado
  mais de uma vez), renomeie para `cinerocket.db`. Para usar outro nome ou outra pasta, defina
  `CINEDATA_DB_PATH` no `.env`.
- O banco usa o modo WAL. Ao abri-lo em modo somente leitura, o SQLite pode criar ao lado dele os
  arquivos auxiliares `cinerocket.db-wal` e `cinerocket.db-shm`. Isso é normal, e eles também
  estão ignorados pelo `.gitignore`.
- Para conferir o arquivo, execute `cinedata doctor` na raiz do repositório.
