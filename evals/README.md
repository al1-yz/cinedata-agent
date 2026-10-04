# Avaliação M3 do CineData Agent

Mede, sem LLM-juiz, se o agente de Text-to-SQL livre responde corretamente perguntas sobre a
camada Gold. Os 14 exemplos do enunciado ("Categorias de Perguntas e Exemplos (Não exaustivo)")
são o **benchmark oficial mínimo**; o corpus vai além deles para medir também a generalização.

> **Estado:** só o tier `smoke` foi executado com modelo real. A primeira execução completa, com
> `openrouter/free`, teve 3 pass e 1 fail (`agent_protocol`, no título ambíguo); um diagnóstico
> separado desse caso passou com outros modelos do roteador, sem alterar o resultado do smoke.
> Não há taxa de acerto sobre os 26 casos. Detalhes e leitura em [`RESULTS.md`](RESULTS.md).

## O que é medido

| Categoria | Casos | Papel |
|---|---|---|
| `official` | 14 | Os exemplos do enunciado, lidos do registro do M1c (`cinedata.reference`), sem cópia de pergunta nem de SQL. |
| `paraphrase` | 5 | Variações de linguagem de oficiais: "faturaram", N por extenso, "bilheteria" informal sem moeda, sem acentos, ordem invertida e singular (pede o líder). |
| `freeform` | 4 | Perguntas analíticas **novas**, sem caso oficial: top 5 atores de Terror (alias de gênero, papel, duas pontes, empate), filmes dirigidos por Christopher Nolan (Tenet só como roteirista), receita em USD de Animação em 2019 e filmes dos últimos 3 anos (janela diferente da do prompt). |
| `policy` | 3 | Título ambíguo (Elemental, 2 homônimos: esclarecimento comprovado ou resposta completa), pedido fora do escopo e pergunta sobre o próprio agente (os dois sem nenhuma ferramenta de dados e com um texto mínimo conferido). |

O agente continua livre: ele não recebe nada deste pacote. `src/cinedata` não importa `evals`, o
prompt não contém nenhuma pergunta nem SQL do corpus e o agente não carrega os casos de referência
(testes em `tests/test_evals_corpus.py` e `tests/test_agent_boundaries.py`).

## Como um caso é pontuado

Tudo vem do código: o **gabarito** é recalculado na hora pelo `run_case` do M1c (casos oficiais e
paráfrases) ou por um `ReferenceCase` deste pacote (perguntas livres, conferidas contra cálculos
independentes em Python nos testes `realdb`), com a mesma data de referência do agente. O lado do
agente vem do **rastro** (`RunTrace`): status final, consultas bem-sucedidas, colunas e linhas.

1. Falha do provedor que nada diz sobre o agente (chave, crédito, moderação, modelo inexistente,
   429, 408, 5xx, conexão), banco indisponível, gabarito que não pôde ser calculado ou defeito do
   próprio pontuador: **não avaliado** (`error`), fora da taxa de acerto. Já `bad_request` (HTTP
   400/413/422, o provedor recusou a requisição que a aplicação montou para o modelo escolhido),
   falha de protocolo e limite de requisições são `fail` (`agent_protocol`): uma falha funcional
   da configuração modelo + agente, que fica no denominador.
2. Status e comportamento: perguntas de dados exigem `data_answer`. Fora do escopo e ajuda exigem
   `out_of_scope`/`info` **sem nenhuma ferramenta de dados** (nenhum `run_sql`, nem com erro, e
   nenhum `find_entities`), como manda o prompt, e um **texto mínimo** (`TextPolicy`, regras
   estreitas e determinísticas, sem interpretar a frase):
   - fora do escopo: uma recusa ou um limite de escopo ("não tenho acesso", "não consigo", "não é
     algo que eu consiga", "fora do meu escopo", "só respondo ... catálogo de filmes") e nada que
     entregue o pedido: temperatura ("30 °C", "25 graus", "máxima de 30"), chance de chuva ou
     previsão afirmada ("vai chover", "fará sol"; "não sei se vai chover" não é previsão).
     "A previsão é 30 °C e vai chover." falha, mesmo com o status certo;
   - ajuda: um verbo de capacidade não negado ("posso", "respondo", "consigo", "você pode me
     perguntar") e pelo menos dois assuntos do catálogo (filmes, catálogo, receita, gêneros,
     notas, pessoas, produtoras, rankings...), sem afirmar um número do catálogo ("o catálogo tem
     95.645 filmes", "temos 95 mil filmes"), que só uma consulta daria. "Banana azul." falha;
     "posso responder rankings, receitas, gêneros e notas do catálogo" passa.

   O título ambíguo aceita só dois desfechos:
   - `clarification` comprovado: uma busca `find_entities` de filme com o título pedido em
     `exact_multiple`, com o conjunto completo de candidatos (os mesmos `id_filme` do gabarito),
     **lida antes** do pedido de esclarecimento (uma busca pedida na mesma resposta do modelo roda,
     mas o texto já estava escrito sem ela), e um texto que distingue cada homônimo (o ano quando
     ele é único entre eles, senão o `id_filme`). "Pode esclarecer?" sem a busca falha;
   - `data_answer` completo, pontuado como qualquer caso de dados contra um gabarito só da
     avaliação (todos os filmes com o título exato, com `id_filme`, título, ano e `nota_imdb`;
     forma de conjunto; mesma identidade de filme): o SQL precisa trazer uma linha por homônimo
     e o texto, a nota de cada um. Uma linha agregada, um homônimo só ou uma nota errada ou
     omitida no texto falham.
3. Fundamentação: `data_answer` sem nenhuma consulta bem-sucedida **lida antes da resposta final**
   é `fail` (`ungrounded`), a mesma regra do validador do M2.
4. SQL: pelo menos uma consulta bem-sucedida precisa servir de **evidência**. Nunca se compara o
   texto do SQL. Uma consulta é evidência quando:
   - **foi lida antes da resposta final**: no PydanticAI 2 (`end_strategy` padrão `'graceful'`),
     um `run_sql` pedido na mesma resposta do `final_answer` é executado (antes ou depois dele,
     conforme a ordem das chamadas), mas o modelo nunca leu o resultado. A **produção** recusa
     essa resposta final (qualquer status, inclusive um esclarecimento mandado junto com a busca
     dos candidatos) e o modelo responde de novo depois de ler; a avaliação confere a mesma regra
     de forma independente: só vale uma consulta de uma requisição anterior à da resposta final
     (`ungrounded`, detalhe `not_read_before_answer`);
   - **não foi truncada**: se havia mais linhas do que o teto, as escondidas poderiam mudar a
     resposta, mesmo que as vistas confiram (falha `truncated`; outra consulta da mesma pergunta,
     sem truncamento, ainda pode servir);
   - **o modelo recebeu as linhas exigidas**: o rastro guarda o resultado inteiro, mas a
     ferramenta só manda ao modelo as `rows_shown` primeiras linhas (limite de tamanho). Uma
     linha exigida fora desse prefixo deixa a consulta inútil como fundamento (`ungrounded`,
     detalhe `rows_not_shown`); em líderes, basta o modelo ter recebido os líderes;
   - **reproduz o gabarito com o rótulo que a resposta mostra**: o resultado é mapeado **por
     valor** (aliases, colunas extras, colunas de desempate e ordem das colunas não importam) e as
     linhas são casadas como **multiconjunto** (duplicatas aparecem). Um filme se identifica pelo
     `id_filme` ou por **título + ano**, nunca só pelo título, e título + ano só vale quando é
     único dentro do gabarito: se dois filmes exigidos têm o mesmo título e o mesmo ano (como no
     caso 13), linhas iguais sem id não provam filmes distintos (falha `ambiguous_identity`).
     Identificar não basta: o resultado também precisa trazer o rótulo que a resposta mostra
     (título e ano, mais o `id_filme` quando eles se repetem), senão o texto citaria valores que
     o modelo nunca obteve; id + métrica sem título e ano não serve.

   Regras por forma de resposta:
   - conjunto (agregados por grupo, escalares): exatamente as linhas do gabarito, ordem livre;
   - ranking: todo ranking tem um N avaliado explícito, o da pergunta ou, quando ela não fixa N
     (casos 03, 05, 08, 13 e 14), o padrão de exibição 10 do M1c, uma convenção de avaliação e
     não uma exigência do enunciado. O resultado precisa ser exatamente a janela `RANK() <= N`:
     **todos os empatados no corte entram** (pode passar de N linhas) e omitir qualquer um falha.
     No banco real, o top 10 do caso 13 tem 14 linhas, porque o 8º lugar empata em 7 filmes.
     Ordem compatível com o ranking, com empatados em qualquer ordem;
   - líderes ("o maior", e a paráfrase no singular do caso 08): todos os empatados no topo,
     primeiro. Linhas abaixo deles são contexto: as que estão no gabarito precisam estar certas;
     as de fora dele ficam anotadas como não verificadas e nunca podem empatar com o líder.
5. Texto final (determinístico, sem LLM-juiz): cada linha exigida do gabarito (todas no conjunto
   e no ranking, inclusive os empatados no corte; os líderes no caso de líderes) precisa aparecer
   na resposta com a **identidade legível** e a **métrica principal**. Filme = título + ano; o
   `id_filme` só é exigido nas linhas em que título + ano se repetem dentro do próprio gabarito,
   a mesma regra que o prompt dá ao agente (nunca uma exigência geral de mostrar ids). Pessoa,
   produtora, gênero ou ano entram pelo nome ou pelo número; gênero vale também em português
   (os nomes do agente, como Guerra e Ficção científica, e os usuais Policial, Musical,
   Telefilme e Filme para TV). O número é lido em pt-BR ou en (`R$ 12.390.136.500,54`, `28.086`,
   `6,34`, `1,234.5`, `-534,88%`), com multiplicador escrito ("3 mil" é 3000, nunca 3; "R$ 12,39
   bilhões"), e comparado com a mesma tolerância e escala da métrica. Colunas auxiliares que a
   pergunta não pediu (contagens ao lado de médias, por exemplo) não são exigidas. Regras de
   leitura:
   - **localidade e exclusividade**: cada linha é procurada entre a citação dela e as citações
     vizinhas de outras linhas (numa tabela, só na própria linha da tabela), e cada trecho escrito
     (nome, ano, id, valor) serve a uma linha só; um valor citado uma vez não vale para duas
     linhas, a menos que a distribuição seja explícita (abaixo);
   - **números de posição não são valores**: `9.`, `2)`, `**3.**` no início da linha ou de uma
     célula, `8º`, `#9`, `top 10` e colunas de posição de tabela (`#`, `Posição`, `Rank`; `Nº` só
     na primeira coluna) nunca contam como métrica, ano ou id (no caso 13, o 9º item tem 9
     avaliações), nem o 10 de uma nota "6,7/10";
   - **id só escrito como id**: `id 1391481`, `id_filme 1391481`, `id do filme 1391481`, `ID:
     1391481` ou uma coluna `ID`/`id_filme` de tabela; um número solto igual ao id não conta;
   - **grandeza**: um número rotulado como outra grandeza não vale como a métrica. `id 65` nunca
     é métrica; `9%` só prova uma margem (e lido como percentual: "0,25%" é 0,0025); `R$`/`US$`
     só provam dinheiro da mesma moeda; `65 anos` (ou meses, dias...) nunca é contagem; uma nota
     escrita como nota ("nota 9", "nota IMDb 9,8", "6,7/10") só prova notas e médias de notas,
     nunca uma divergência (no caso 14, com média dos usuários 0, a divergência coincide com a
     nota IMDb) nem uma contagem; "9 avaliações", "65 filmes", "37 votos" só provam contagens. Um
     número colado a uma letra ("9th", "12,39bi") não é valor. O cabeçalho de uma coluna rotula
     as células que são só o número, como na prosa: "Nota IMDb", "Idade", "Avaliações",
     "Receita (US$)", "Margem (%)". Um número **sem rótulo** vale pelo contexto da pergunta
     ("Eric Roberts — 65" prova 65 filmes; "Eric Roberts — 65 anos" não). Um **limite** não é o
     valor: "mais de 3", "pelo menos 3", "até 3", "quase 3", "> 3" nunca provam 3 ("cerca de 3"
     prova);
   - **moeda**: em métricas de dinheiro (colunas `_brl`/`_usd`), a resposta precisa indicar a
     moeda certa uma vez (`R$`, `BRL` ou "reais"; `US$`, `USD`, "dólares" ou `$` sem R): no texto,
     num cabeçalho de tabela ou nas premissas/ressalvas. Mas as premissas só suprem a moeda de um
     número que o texto não rotula com a outra: não vale um número com a outra moeda ao lado
     ("US$ 2.900,00", "2.900,00 USD"), numa coluna "Receita (US$)", numa frase que só cita a outra
     moeda ("Receita em USD: Avatar (2009): 2.900,00") ou sob um título que só cita a outra moeda
     ("Receitas em dólares:" ou "## Receita (USD)" sobre a lista), diga o que disser a premissa.
     Apartes entre parênteses que não contêm o número não rotulam ("Valores em reais (antes
     estavam em USD): ..."), e uma ressalva em outra frase sobre a outra moeda não afeta o
     resultado;
   - **tabelas markdown**: com ou sem as barras das bordas, desde que o cabeçalho venha seguido
     de uma linha separadora com o mesmo número de células (`---`, `:---`, `---:`, `:---:`).
     Prosa com barras, sem essa linha, não é tabela (nem cria coluna de posição ou de id);
   - **ordem do ranking**: no ranking, a linha de posição menor vem antes no texto (empatados em
     qualquer ordem; conjuntos não têm ordem). Uma posição escrita junto de uma linha (`3.`,
     `3º`, `#3`, coluna de posição, ou por extenso em forma de ranking: "em segundo", "terceiro
     lugar", "na quarta posição") precisa ser a dela: no empate do 4º lugar com 4 filmes, de 4 a
     7, então tanto a numeração corrida da lista quanto "4º" para os quatro valem. "1º B; 2º A"
     com A líder falha. Com a posição escrita em **todas** as linhas, ela manda e a ordem do texto
     pode ser outra ("em 2º está B; em 1º está A" passa). Uma posição só é de uma linha quando o
     trecho (linha ou ponto e vírgula) tem tantas posições quanto linhas citadas; ordinais que não
     são posição ("o 1º filme a...", "1º de janeiro", "2º semestre") não contam. Toda posição
     escrita junto do nome de uma linha é conferida, em qualquer lugar do texto: "1. Titanic / 2.
     Avatar" sem valores, antes de uma lista certa, falha. A **numeração preguiçosa** do markdown
     (uma lista ordenada contígua com "1." em todos os itens, que o markdown renderiza como 1, 2,
     3) não é posição: vale a ordem do texto. "1., 2., 2.", "1º" e colunas de posição com 1 em
     todas as linhas continuam posições. Em líderes só se confere a posição escrita (o único
     líder escrito como "2." falha);
   - **apresentações estruturadas** (tabelas markdown, itens de lista e blocos de uma linha por
     resultado, como "Avatar (2009): R$ 2.900,00"): uma **linha de dados** (rótulo, separador
     ":", "—", "–", "=", " - ", ", com" ou o fim de "(ano)", até 3 palavras e um número compatível
     com a métrica; numa tabela, uma célula só com o número) que não é do resultado é inventada e
     falha (`unsupported`), mesmo com todas as linhas certas presentes; uma linha de dados que
     cita uma linha do resultado precisa trazer o valor dela (`contradiction`); e cada
     apresentação **completa** (unidades seguidas de um bloco, cortadas quando uma linha se
     repete) precisa provar sozinha todas as linhas exigidas, na ordem: uma lista errada não é
     apagada por outra certa, e uma lista certa repetida passa. Não são linhas do resultado:
     notas, totais e baldes ("Observação: ...", "Total: ...", "Sem gênero: ..."), comparações que
     citam duas linhas ("Avatar (2009) supera Titanic (1997) em R$ 699,50"), frases sem a forma
     de linha de dados, listas de observações à parte (sem nenhuma linha do resultado), a menos
     que tragam uma linha com a forma de filme ("Inventado (2020): ..."), e, nos líderes, linhas
     abaixo do líder (contexto, como no SQL; uma que empate ou passe dele falha). Os empatados no
     corte fazem parte da janela, não são linhas a mais;
   - **resultado vazio**: quando o gabarito certo não tem linhas, o SQL vazio é necessário mas não
     basta: o texto precisa dizer, numa mesma oração, que nada foi encontrado ("nenhum filme",
     "não há registros", "não encontrei filmes", "0 resultados", "nada foi encontrado"; "não há
     dúvida" não serve) e não pode listar linhas de dados. "Há 123 filmes chamados Inventado."
     com o SQL vazio certo falha;
   - **formas compactas aceitas**: quando várias linhas exigidas têm o mesmo título, ele pode ser
     citado uma vez ("Há dois filmes Elemental: o de 2022 tem nota 6,7 e o de 2023, 7,0"), desde
     que o ano de cada uma (ou o id, se o ano também se repete) venha junto do próprio valor;
     listas paralelas ("A e B têm X e Y") só valem com um "respectivamente" explícito, que fixa a
     correspondência por posição entre os itens escritos antes dele na mesma frase;
   - **distribuição explícita**: "cada" dá um valor a um grupo enumerado ("Drama e Comédia têm 3
     filmes cada", "cada um com 3", "Com 3 filmes cada, A e B..."); "ambos"/"ambas" (um par) ou
     "todos"/"todas" dão um rótulo ("Avatar e Top Gun, ambos de 2022") ou o valor escrito logo
     depois com "com", "têm" ou "possuem" ("Drama e Comédia, todos com 3 filmes", "A e B, ambos
     com 9 avaliações"). O grupo é a enumeração colada à expressão, na mesma linha (de
     preferência a que vem antes, o sujeito); o valor ou o rótulo só vale para os membros cujo
     valor é aquele ("ambos de 2022" não dá ano a um filme de 2009; "todos com 3" não dá 3 a quem
     tem 2). Sem essas palavras, nada é dividido: "A e B têm 3 filmes" e "A e B somam 3" falham.

Tolerâncias (absolutas, na escala do gabarito, mais alguns ulps de folga binária): contagens
exatas; dinheiro 0,01; notas, divergências e popularidade 0,005 (exibição com 2 casas); margens
5e-5 na razão, como fração ou em %. Cada métrica declara a grandeza (`count`, `money`, `rating`,
`decimal` ou `ratio`), que decide os rótulos aceitos no texto. Cada registro separa: execução,
status esperado/obtido, fundamentação, SQL (com o mapeamento de colunas e o motivo), texto
(veredito `ok`, `missing`, `wrong_order`, `contradiction` ou `unsupported`, com a ordem e o
motivo), veredito e categoria da falha (`provider`, `database`, `harness`, `agent_protocol`,
`sql_error`, `wrong_status`, `policy`, `ungrounded`, `result_mismatch`, `answer_text`). Qualquer
veredito de texto diferente de `ok` é `fail` (`answer_text`).

## Como executar

Na raiz do repositório (o pacote `evals` não é instalado; ele roda a partir dela), com o
ambiente ativado e o `.env` configurado. Os comandos são os mesmos em PowerShell, CMD, Git Bash,
Linux e macOS. Estes são offline e não falam com o provedor (o `--check-oracles` precisa do
banco):

```bash
python -m evals.run --help
python -m evals.run --tier smoke
python -m evals.run --tier full --check-oracles
```

Estes executam de verdade e consomem cota. Uma execução real exige `CINEDATA_REFERENCE_DATE`
fixada; use `2026-10-01`, a data com que os gabaritos foram conferidos no banco real.

```bash
python -m evals.run --tier smoke --primary-only --live
python -m evals.run --tier smoke --primary-only --live --resume
```

- Sem `--live` é um **dry-run**: mostra os casos, a configuração e o teto de requisições, e nada
  é enviado ao provedor (`ALLOW_MODEL_REQUESTS` é desligado). `--check-oracles` calcula os
  gabaritos no banco, ainda sem provedor.
- Tiers: `smoke` (4 casos: margem com empate no corte, nota por ano, top 5 atores de Terror e
  título ambíguo), `official`, `paraphrase`, `freeform`, `policy` e `full` (26). `--id CASO`
  (repetível) escolhe casos; `--limit N` corta a seleção; `--out ARQUIVO.json` grava em outro
  arquivo.
- `--model ID` troca o modelo principal; `--primary-only` descarta os fallbacks. Sem ele, o
  fallback seletivo do M2 continua valendo (e o teto de chamadas HTTP é multiplicado).
- Com `openrouter/free`, o roteador escolhe um modelo gratuito a cada requisição: cada caso
  registra os `models_used`, e duas execuções da mesma seleção podem ter desfechos diferentes.
  Para comparar modelos, fixe um id explícito.
- Cada pergunta usa o agente de produção com `CINEDATA_REQUEST_LIMIT` requisições, **sem
  retentativa automática**. A primeira falha de provedor ou de banco, a primeira requisição
  recusada (400/413/422) ou um defeito do pontuador interrompe a execução; os casos restantes
  ficam pendentes. Um defeito do pontuador grava o desfecho já pago, como não avaliado.
- `--resume` continua o mesmo arquivo, com as mesmas opções da execução original: casos já
  avaliados (pass/fail) com a mesma definição são pulados, e os não avaliados rodam de novo.
  Ele é recusado se mudar o modelo, os fallbacks, a
  data de referência ou os limites, a impressão digital da avaliação (`evals/cases.py`,
  `evals/scoring.py`, o executor `evals/run.py` e `src/cinedata/reference.py`; documentação não
  entra), a do agente (`agent.py`, `prompt.py`, `runtime.py`, `llm.py`, `entities.py`, `db.py`,
  `config.py`), as versões de Python, SQLite, PydanticAI e do cliente OpenAI (o laço de
  ferramentas e as requisições dependem delas) ou o SHA-256 do conteúdo do banco (mais o `-wal`,
  se houver; calculado uma vez por processo).

**Cota.** O plano mostra o máximo antes de executar: casos x `CINEDATA_REQUEST_LIMIT` requisições
do agente (x (1 + fallbacks) chamadas HTTP). O `smoke` com o `.env` padrão custa no máximo 20; o
`full`, 130. Rode tier por tier.

## Resultados

Cada caso é gravado assim que termina, em `evals/results/raw/<tier>-<modelo>-<data>.json` (bruto:
pergunta, resposta, SQL, linhas, gabarito, uso, veredito) e um resumo `.md` ao lado. O resumo diz
quantos casos do corpus a seleção cobre: uma taxa do `smoke` não é a do corpus. A pasta `raw/` é
ignorada pelo Git. Nenhum arquivo leva a chave da API (ela e qualquer texto com cara de chave são
removidos antes de gravar). Depois de revisar uma execução real, resuma-a em
[`RESULTS.md`](RESULTS.md), o único registro de resultados versionado; o JSON bruto e o resumo
gerado ficam locais, em `raw/`.

## Limitações

- O casamento por valor exige que a identidade venha numa coluna própria (`titulo` ou
  `id_filme`, nome da pessoa...) e que as métricas venham como números. Um agente que concatene
  título e ano numa coluna só, traduza nomes no SQL ou formate números como texto (`printf`)
  falha mesmo com a resposta certa; o JSON mostra o SQL para auditoria.
- Basta UMA consulta bem-sucedida, lida antes da resposta, reproduzir o gabarito; quando ela não é
  a última, o registro anota isso. O texto é sempre conferido contra o gabarito, não contra a
  consulta escolhida.
- Texto: confere-se identidade + métrica principal (e, nos rankings, a ordem e as posições
  escritas) de cada linha exigida, mais o que as apresentações estruturadas afirmam. Não se julga
  prosa: uma linha inventada escrita como frase solta ou num parágrafo próprio de uma linha só
  ("Para comparar, Inventado (2020) faturou R$ 3.000,00."), uma lista à parte, sem nenhuma linha
  do resultado, de linhas que não têm a forma de filme (de gêneros ou pessoas inventados), um
  empate afirmado sem posição nem valor ("B e C empatam"), pronomes ("os dois faturaram X e Y"),
  números auxiliares, comparações, e uma prosa que diz que nada foi encontrado e, em outra
  oração, afirma resultados ("Não encontrei filmes de 2099. Há 123 filmes chamados Inventado.").
  Num item que cita várias linhas do resultado, só o valor da linha provada é conferido, salvo
  numa apresentação completa. Escalares (sem rótulo) são conferidos por existência: um segundo
  valor diferente no texto não é julgado.
- Formas que falham mesmo com a resposta certa: arredondamento além da tolerância ("6,3" para
  6,338; "R$ 12,39 bilhões" para 12.390.136.500,54); sinal negativo separado por espaço ("Western -
  R$ 3.581.373,25" é lido como positivo); listas paralelas sem "respectivamente"; um rótulo
  distribuído a uma lista em outras linhas ("Todos de 2024:" sobre uma lista sem o ano em cada
  item; o prompt manda mostrar o ano de cada filme); id sem a palavra "id" numa tabela sem coluna
  `ID` (numa coluna "Código", por exemplo; "id 101" dentro da célula vale); uma
  linha de valor que comece com 1 a 3 dígitos seguidos de ponto ou parêntese (lida como
  posição); posição densa ("5º" para o 8º lugar empatado); numeração preguiçosa que comece em
  outro número ("3." em todos os itens); tabela sem a linha separadora quando ela depende da
  coluna `ID` (lida como prosa, os cabeçalhos não rotulam as células; sem coluna `ID`, a prosa
  costuma bastar). Nas
  apresentações estruturadas: um item com o nome de uma linha do resultado e um número sem
  rótulo, na posição de valor, que não é a métrica ("- Avatar (2009) ganhou 3 Oscars" num
  ranking de contagens) é lido como o valor dela; uma nota com rótulo fora da lista de notas
  ("Filmes considerados: 9.876") dentro do bloco do resultado é lida como linha a mais; uma
  posição escrita junto do nome de uma linha é sempre lida como a posição dela neste ranking.
- Um número **sem rótulo** que coincida com a métrica na região da linha ainda pode ser usado se
  ninguém mais o reclamar; só um rótulo explícito de outra grandeza ou um limite o exclui.
- Moeda: uma frase que cita as duas moedas fora de parênteses não rotula o número (vale a
  indicação global); "real" sozinho (adjetivo comum em português) não conta como indicação de
  reais, só "reais", "real brasileiro", `R$` e `BRL`.
- O caso 13 é o único do corpus em que o `id_filme` aparece na resposta exigida, e por causa dos
  dados, não de uma regra geral: no top 10 dele (14 linhas com os empates), 7 filmes se chamam
  "Die Hart 2: Die Harter" e 6 "Die Hart: Die Harter", todos de 2024. Sem o id, essas 13 linhas
  são indistinguíveis para o usuário; a 14ª ("Duro De Atuar 2", 2024) é única e dispensa o id.
- Linhas mostradas além dos líderes não são verificadas quando o gabarito só tem os líderes
  (casos 07, 09, 11 e 12), nem no SQL nem no texto; só se garante que não empatam com o líder.
- No pedido de esclarecimento, só os desambiguadores (ano ou `id_filme` de cada homônimo, o id
  escrito como id) são conferidos no texto; o restante da pergunta de volta não é julgado.
- Nas perguntas de política sem dados, o texto é conferido por regras estreitas: uma recusa
  que entregue a previsão em palavras fora dos padrões listados ("amanhã, céu limpo") ou uma ajuda
  que afirme outro dado do catálogo ("a nota média é 6,4") passam; um número do catálogo escrito
  sem verbo de existência ("Ao todo, 95.645 filmes estão no catálogo") também. Separar exemplo de
  afirmação em geral exigiria interpretar o texto.
- Uma chamada de ferramenta recusada antes de rodar (argumentos inválidos) não aparece no rastro;
  como nenhuma consulta ao banco aconteceu, a regra "sem ferramentas" dos casos de política não a
  conta.
- Nenhum caso do `smoke` tem uma janela maior que N (no caso 03, o empate do 9º lugar ocupa as
  posições 9 e 10 e cabe no top 10); os empates que passam de N estão no tier `official` (casos
  08, 13 e 14).
