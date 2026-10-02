"""Casos de referência (golden set) para avaliar consultas analíticas sobre a camada Gold.

Os 14 casos oficiais reproduzem os exemplos do enunciado ("Categorias de Perguntas e Exemplos (Não
exaustivo)"). Eles são o conjunto MÍNIMO de referência da avaliação, não a lista de perguntas que o
agente aceita: o agente (M2) gera SQL livre sobre o esquema Gold para qualquer pergunta analítica
válida e nunca depende de reconhecer um destes casos. Aqui ficam a semântica aprovada de cada
exemplo e um SQL confiável e legível, que a avaliação (M3) usa como gabarito.

Um caso é um dado (`ReferenceCase`), não um handler. O executor `run_case` é genérico, e um caso
novo, seja uma paráfrase ou uma pergunta inédita, entra por `ReferenceRegistry.register` sem
nenhuma mudança neste módulo.

Execução: só por `SafeDatabase.execute` (este módulo não importa `sqlite3`), com o prazo normal da
instância. O único override é o teto de linhas da chamada (`max_rows`), porque um gabarito precisa
vir completo. Resultado truncado, célula cortada, colunas diferentes das declaradas ou chave de
linha repetida viram `ReferenceCaseError`. A janela "últimos N anos" sai da data de referência do
projeto (`config.rolling_window`), nunca do relógio do SQLite.

Convenções dos SQLs:

- Rankings usam RANK(): `posicao <= N` mantém todos os empatados no corte, e `posicao = 1` devolve
  todos os líderes empatados. Depois da métrica, a ordem desempata por nome e chave.
- Métricas calculadas são arredondadas antes de ranquear, para que valores matematicamente iguais
  empatem apesar do ruído do ponto flutuante binário: dinheiro em centavos (2 casas), notas
  (médias e divergências) em 9 casas e margens em 12. As notas da Gold têm no máximo 3 casas. As
  margens são razões entre valores em dinheiro, e no banco real duas margens diferentes chegam a
  diferir só na 11ª casa (7/8 e 0,87499999996), enquanto o ruído binário aparece a partir da 14ª.
  Um teste `realdb` confere, no banco inteiro, que esses arredondamentos empatam os valores
  exatamente iguais e só eles. Valores armazenados (receita, popularidade, contagens) são
  comparados como estão.
- Contas entre colunas de dinheiro usam REAL (`CAST`): a Gold guarda parte delas como INTEGER, e a
  divisão entre inteiros do SQLite truncaria a margem para 0.
"""

from __future__ import annotations

import re
import string
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from cinedata.config import rolling_window
from cinedata.db import MAX_BULK_ROWS, SafeDatabase

DEFAULT_MAX_ROWS = 1_000  # teto de linhas de um gabarito; acima disso o caso falha, nunca corta
PARAM_LIMIT = "limit"
PARAM_START_DATE = "start_date"
PARAM_END_DATE = "end_date"
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")


class ReferenceCaseError(Exception):
    """Um caso de referência é inválido ou produziu um resultado que não serve de gabarito."""


@dataclass(frozen=True)
class ReferenceCase:
    """Uma pergunta de referência: a semântica aprovada e o SQL que a responde.

    `sql` é um modelo com, no máximo, estes parâmetros: `{limit}` (obrigatório quando `limit` é
    definido) e `{start_date}`/`{end_date}` (obrigatórios quando `window_years` é definido). Os
    valores entram já validados (um inteiro e datas ISO entre aspas), nunca texto livre. Para
    variar um parâmetro, use `dataclasses.replace(caso, limit=20)`; a validação roda de novo.
    """

    case_id: str
    question: str
    semantics: str
    sql: str
    columns: tuple[str, ...]  # colunas esperadas do resultado, na ordem
    key_columns: tuple[str, ...]  # identidade de uma linha (única no resultado)
    limit: int | None = None  # N de exibição dos rankings
    window_years: int | None = None  # janela móvel até a data de referência
    official: bool = False  # um dos exemplos do enunciado
    paraphrases: tuple[str, ...] = ()  # outras formas da mesma pergunta (avaliação)
    max_rows: int = DEFAULT_MAX_ROWS

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not _CASE_ID.fullmatch(self.case_id):
            raise ValueError(
                f"case_id inválido: {self.case_id!r} (use minúsculas, dígitos, '_', '.' ou '-')."
            )
        for name in ("question", "semantics", "sql"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{self.case_id}: {name} não pode ser vazio.")
        for name in ("columns", "key_columns", "paraphrases"):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{self.case_id}: {name} deve ser uma tupla.")
        for name in ("columns", "key_columns"):
            if any(not isinstance(item, str) or not item.strip() for item in getattr(self, name)):
                raise ValueError(f"{self.case_id}: {name} só aceita nomes de coluna não vazios.")
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError(f"{self.case_id}: columns deve ter nomes únicos e não vazios.")
        if not self.key_columns or not set(self.key_columns) <= set(self.columns):
            raise ValueError(f"{self.case_id}: key_columns deve ser um subconjunto de columns.")
        for name in ("limit", "window_years"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{self.case_id}: {name} deve ser um inteiro >= 1 ou None.")
        if (
            isinstance(self.max_rows, bool)
            or not isinstance(self.max_rows, int)
            or not 1 <= self.max_rows <= MAX_BULK_ROWS
        ):
            raise ValueError(f"{self.case_id}: max_rows deve estar entre 1 e {MAX_BULK_ROWS}.")
        if any(not isinstance(text, str) or not text.strip() for text in self.paraphrases):
            raise ValueError(f"{self.case_id}: paraphrases não aceita textos vazios.")
        try:
            used = {field for _, field, _, _ in string.Formatter().parse(self.sql) if field}
        except ValueError as exc:
            raise ValueError(f"{self.case_id}: modelo de SQL inválido ({exc}).") from None
        if used != self._declared_parameters():
            raise ValueError(
                f"{self.case_id}: o SQL usa os parâmetros {sorted(used)}, mas o caso declara "
                f"{sorted(self._declared_parameters())}."
            )

    def _declared_parameters(self) -> set[str]:
        declared = set()
        if self.limit is not None:
            declared.add(PARAM_LIMIT)
        if self.window_years is not None:
            declared |= {PARAM_START_DATE, PARAM_END_DATE}
        return declared

    def parameters(self, reference_date: date | None = None) -> Mapping[str, object]:
        """Valores dos parâmetros desta execução (datas em ISO)."""
        values: dict[str, object] = {}
        if self.limit is not None:
            values[PARAM_LIMIT] = self.limit
        if self.window_years is not None:
            if not isinstance(reference_date, date):
                raise ValueError(
                    f"{self.case_id} usa uma janela de {self.window_years} anos: informe "
                    "reference_date (a data de referência do projeto, Settings.reference_date)."
                )
            start, end = rolling_window(reference_date, self.window_years)
            values[PARAM_START_DATE] = start.isoformat()
            values[PARAM_END_DATE] = end.isoformat()
        return MappingProxyType(values)

    def render(self, reference_date: date | None = None) -> str:
        """O SQL pronto para executar, com os parâmetros já como literais SQL."""
        literals = {
            name: (f"'{value}'" if name in (PARAM_START_DATE, PARAM_END_DATE) else str(value))
            for name, value in self.parameters(reference_date).items()
        }
        return self.sql.format_map(literals)


@dataclass(frozen=True)
class ReferenceResult:
    """Resultado completo de um caso: o gabarito que a avaliação compara."""

    case_id: str
    sql: str  # o SQL executado, já com os parâmetros
    parameters: Mapping[str, object]
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    elapsed_s: float

    def records(self) -> tuple[dict[str, object], ...]:
        """As linhas como dicionários coluna -> valor."""
        return tuple(dict(zip(self.columns, row, strict=True)) for row in self.rows)

    def keys(self) -> tuple[tuple[object, ...], ...]:
        """A identidade de cada linha (valores de `key_columns`), na ordem do resultado."""
        positions = [self.columns.index(column) for column in self.key_columns]
        return tuple(tuple(row[i] for i in positions) for row in self.rows)


class ReferenceRegistry:
    """Coleção de casos indexada por `case_id`, na ordem de registro."""

    def __init__(self, cases: Iterable[ReferenceCase] = ()) -> None:
        self._cases: dict[str, ReferenceCase] = {}
        for case in cases:
            self.register(case)

    def register(self, case: ReferenceCase) -> ReferenceCase:
        if not isinstance(case, ReferenceCase):
            raise TypeError("só objetos ReferenceCase podem ser registrados")
        if case.case_id in self._cases:
            raise ValueError(f"já existe um caso com o id {case.case_id!r}")
        self._cases[case.case_id] = case
        return case

    def get(self, case_id: str) -> ReferenceCase:
        try:
            return self._cases[case_id]
        except KeyError:
            raise KeyError(f"caso de referência desconhecido: {case_id!r}") from None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._cases)

    def __iter__(self) -> Iterator[ReferenceCase]:
        return iter(tuple(self._cases.values()))

    def __len__(self) -> int:
        return len(self._cases)

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._cases


def run_case(
    db: SafeDatabase, case: ReferenceCase, *, reference_date: date | None = None
) -> ReferenceResult:
    """Executa um caso qualquer pelo `SafeDatabase` e confere que o resultado é um gabarito.

    Usa o prazo normal da instância; só o teto de linhas é da chamada (`case.max_rows`). Erros do
    banco (`SafeDatabaseError`) sobem como estão.
    """
    sql = case.render(reference_date)
    started = time.perf_counter()
    result = db.execute(sql, max_rows=case.max_rows)
    elapsed = time.perf_counter() - started
    if result.truncated:
        raise ReferenceCaseError(
            f"{case.case_id}: o resultado passou de {case.max_rows} linhas e ficaria incompleto."
        )
    if result.truncated_cells:
        raise ReferenceCaseError(
            f"{case.case_id}: {result.truncated_cells} célula(s) cortada(s) por tamanho."
        )
    if result.columns != case.columns:
        raise ReferenceCaseError(
            f"{case.case_id}: colunas {list(result.columns)} diferentes das declaradas "
            f"{list(case.columns)}."
        )
    outcome = ReferenceResult(
        case_id=case.case_id,
        sql=sql,
        parameters=case.parameters(reference_date),
        columns=result.columns,
        key_columns=case.key_columns,
        rows=result.rows,
        elapsed_s=elapsed,
    )
    keys = outcome.keys()
    if len(set(keys)) != len(keys):
        raise ReferenceCaseError(
            f"{case.case_id}: a chave {list(case.key_columns)} se repete no resultado."
        )
    return outcome


def run_cases(
    db: SafeDatabase, cases: Iterable[ReferenceCase], *, reference_date: date | None = None
) -> tuple[ReferenceResult, ...]:
    """Executa vários casos, em ordem, com a mesma data de referência."""
    return tuple(run_case(db, case, reference_date=reference_date) for case in cases)


# --- os 14 exemplos oficiais (não exaustivos) -----------------------------------------------------
# `question` traz a redação literal do enunciado; reformulações ficam em `paraphrases`.

# `id_filme` é a chave das linhas de filme porque títulos se repetem. A unicidade é um invariante
# verificado neste banco (95.645 filmes, 95.645 `id_filme` distintos e não nulos; um teste realdb
# confere), e `run_case` recusa qualquer resultado com chave repetida.
_MOVIE_TOP_COLUMNS = ("posicao", "id_filme", "titulo", "ano_lancamento")

_SQL_MAIOR_RECEITA = """
WITH ranking AS (
    SELECT sk_movie_id, receita_brl,
           RANK() OVER (ORDER BY receita_brl DESC) AS posicao
    FROM fact_movies_performance
    WHERE receita_brl IS NOT NULL
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento, r.receita_brl
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

_SQL_LUCRO_MEDIO_POR_GENERO = """
-- (filme, gênero) é a chave da ponte: cada filme entra uma vez em cada um dos seus gêneros
SELECT g.nome_genero AS genero,
       COUNT(*) AS filmes,
       ROUND(AVG(f.lucro_brl), 2) AS lucro_medio_brl
FROM fact_movies_performance AS f
JOIN bridge_movie_genre AS b ON b.sk_movie_id = f.sk_movie_id
JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id
WHERE f.receita_brl IS NOT NULL
GROUP BY g.sk_genre_id, g.nome_genero
ORDER BY lucro_medio_brl DESC, genero, g.sk_genre_id
"""

_SQL_MAIOR_MARGEM = """
WITH margens AS (
    SELECT sk_movie_id, receita_brl, orcamento_brl,
           ROUND(CAST(receita_brl - orcamento_brl AS REAL) / receita_brl, 12) AS margem_lucro
    FROM fact_movies_performance
    WHERE receita_brl > 0
      AND orcamento_brl IS NOT NULL
),
ranking AS (
    SELECT sk_movie_id, receita_brl, orcamento_brl, margem_lucro,
           RANK() OVER (ORDER BY margem_lucro DESC) AS posicao
    FROM margens
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento,
       r.receita_brl, r.orcamento_brl, r.margem_lucro
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

_SQL_MAIS_POPULARES = """
WITH ranking AS (
    SELECT sk_movie_id, popularidade,
           RANK() OVER (ORDER BY popularidade DESC) AS posicao
    FROM fact_movies_performance
    WHERE popularidade IS NOT NULL
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento, r.popularidade
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

_SQL_DIVERGENCIA_TMDB_IMDB = """
WITH notas AS (
    SELECT sk_movie_id, nota_tmdb, qtd_tmdb, nota_imdb,
           ROUND(ABS(nota_tmdb - nota_imdb), 9) AS divergencia
    FROM fact_movies_performance
    WHERE nota_imdb > 0                       -- IMDb 0 ou nulo = sem nota
      AND nota_tmdb IS NOT NULL
      AND (nota_tmdb <> 0 OR qtd_tmdb > 0)    -- TMDB 0 sem votos = sem nota; com votos, é nota
),
ranking AS (
    SELECT sk_movie_id, nota_tmdb, qtd_tmdb, nota_imdb, divergencia,
           RANK() OVER (ORDER BY divergencia DESC) AS posicao
    FROM notas
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento,
       r.nota_tmdb, r.qtd_tmdb, r.nota_imdb, r.divergencia
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

_SQL_NOTA_IMDB_POR_ANO = """
SELECT m.ano_lancamento,
       COUNT(*) AS filmes_com_nota,
       ROUND(AVG(f.nota_imdb), 9) AS nota_media_imdb
FROM fact_movies_performance AS f
JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id
WHERE f.nota_imdb > 0                         -- IMDb 0 ou nulo = sem nota
GROUP BY m.ano_lancamento
ORDER BY m.ano_lancamento
"""

_SQL_ATOR_MAIS_ATIVO = """
WITH filmes_na_janela AS (
    SELECT sk_movie_id
    FROM dim_movies
    WHERE status_filme = 'Lançado'
      AND data_lancamento BETWEEN {start_date} AND {end_date}
),
participacoes AS (
    -- conta por linha de pessoa só nos filmes da janela; o papel é filtrado depois
    SELECT sk_person_id, COUNT(DISTINCT sk_movie_id) AS filmes
    FROM bridge_movie_person
    WHERE sk_movie_id IN filmes_na_janela
    GROUP BY sk_person_id
),
ranking AS (
    SELECT pa.sk_person_id, p.nome_pessoa AS ator, pa.filmes,
           RANK() OVER (ORDER BY pa.filmes DESC) AS posicao
    FROM participacoes AS pa
    JOIN dim_people AS p ON p.sk_person_id = pa.sk_person_id
    WHERE p.tipo_pessoa = 'Ator'
)
SELECT ator, filmes
FROM ranking
WHERE posicao = 1
ORDER BY ator, sk_person_id
"""

_SQL_DIRETORES_MELHOR_NOTA = """
WITH filmes_por_pessoa AS (
    -- (filme, pessoa) é a chave da ponte: uma linha por filme. Cada papel é uma linha de
    -- dim_people, então numa linha de diretor isto é o total de filmes dirigidos.
    SELECT sk_person_id, COUNT(*) AS filmes
    FROM bridge_movie_person
    GROUP BY sk_person_id
    HAVING COUNT(*) >= 5                      -- mínimo de 5 filmes dirigidos (com ou sem nota)
),
notas AS (
    SELECT p.sk_person_id, p.nome_pessoa AS diretor,
           fp.filmes AS filmes_dirigidos,
           COUNT(f.nota_imdb) AS filmes_com_nota,
           ROUND(AVG(f.nota_imdb), 9) AS media_imdb
    FROM filmes_por_pessoa AS fp
    JOIN dim_people AS p ON p.sk_person_id = fp.sk_person_id
    JOIN bridge_movie_person AS b ON b.sk_person_id = fp.sk_person_id
    LEFT JOIN fact_movies_performance AS f
           ON f.sk_movie_id = b.sk_movie_id
          AND f.nota_imdb > 0                 -- a média usa só notas válidas
    WHERE p.tipo_pessoa = 'Diretor'
    GROUP BY p.sk_person_id, p.nome_pessoa, fp.filmes
),
ranking AS (
    SELECT sk_person_id, diretor, media_imdb, filmes_com_nota, filmes_dirigidos,
           RANK() OVER (ORDER BY media_imdb DESC) AS posicao
    FROM notas
    WHERE filmes_com_nota > 0
)
SELECT posicao, diretor, media_imdb, filmes_com_nota, filmes_dirigidos
FROM ranking
WHERE posicao <= {limit}
ORDER BY posicao, diretor, sk_person_id
"""

# Cruzar todo o elenco (~530 mil linhas) com toda a direção (~94 mil) levou de 15 s a mais de 2 min
# no banco real, conforme a formulação. A poda abaixo é exata: dois parceiros nunca têm mais filmes
# juntos do que cada um tem sozinho.
_SQL_PAR_ATOR_DIRETOR = """
-- Poda exata (nunca muda a resposta, só o tempo):
-- 1) piso = o maior par entre os diretores das 200 pessoas com mais filmes. É um par real,
--    então o máximo verdadeiro é >= piso. Sem par na amostra, piso = 1 e nada é podado.
-- 2) o par vencedor tem >= piso filmes juntos, logo cada um tem >= piso filmes. Só essas
--    pessoas entram na contagem, que é feita por inteiro.
WITH filmes_por_pessoa AS (
    -- (filme, pessoa) é a chave da ponte: uma linha por filme de cada linha de pessoa
    SELECT sk_person_id, COUNT(*) AS filmes
    FROM bridge_movie_person
    GROUP BY sk_person_id
),
amostra AS (
    SELECT p.sk_person_id
    FROM (
        SELECT sk_person_id
        FROM filmes_por_pessoa
        ORDER BY filmes DESC, sk_person_id
        LIMIT 200
    ) AS mais_ativas
    JOIN dim_people AS p ON p.sk_person_id = mais_ativas.sk_person_id
    WHERE p.tipo_pessoa = 'Diretor'
),
piso AS (
    SELECT COALESCE(MAX(filmes_juntos), 1) AS filmes
    FROM (
        SELECT COUNT(*) AS filmes_juntos
        FROM amostra AS d
        JOIN bridge_movie_person AS bd ON bd.sk_person_id = d.sk_person_id
        JOIN bridge_movie_person AS ba ON ba.sk_movie_id = bd.sk_movie_id
        JOIN dim_people AS a ON a.sk_person_id = ba.sk_person_id
        WHERE a.tipo_pessoa = 'Ator'
        GROUP BY bd.sk_person_id, ba.sk_person_id
    )
),
candidatos AS (
    SELECT p.sk_person_id, p.nome_pessoa, p.tipo_pessoa
    FROM filmes_por_pessoa AS fp
    JOIN dim_people AS p ON p.sk_person_id = fp.sk_person_id
    WHERE fp.filmes >= (SELECT filmes FROM piso)
      AND p.tipo_pessoa IN ('Ator', 'Diretor')
),
pares AS (
    -- uma linha por filme em comum: (filme, diretor) e (filme, ator) são únicos na ponte
    SELECT a.sk_person_id AS sk_ator, a.nome_pessoa AS ator,
           d.sk_person_id AS sk_diretor, d.nome_pessoa AS diretor,
           COUNT(*) AS filmes_juntos
    FROM candidatos AS d
    JOIN bridge_movie_person AS bd ON bd.sk_person_id = d.sk_person_id
    JOIN bridge_movie_person AS ba ON ba.sk_movie_id = bd.sk_movie_id
    JOIN candidatos AS a ON a.sk_person_id = ba.sk_person_id
    WHERE d.tipo_pessoa = 'Diretor'
      AND a.tipo_pessoa = 'Ator'
    GROUP BY a.sk_person_id, a.nome_pessoa, d.sk_person_id, d.nome_pessoa
),
ranking AS (
    SELECT sk_ator, ator, sk_diretor, diretor, filmes_juntos,
           RANK() OVER (ORDER BY filmes_juntos DESC) AS posicao
    FROM pares
)
SELECT ator, diretor, filmes_juntos
FROM ranking
WHERE posicao = 1
ORDER BY ator, diretor, sk_ator, sk_diretor
"""

_SQL_FILMES_POR_GENERO = """
WITH contagem AS (
    SELECT sk_genre_id, COUNT(DISTINCT sk_movie_id) AS filmes
    FROM bridge_movie_genre
    GROUP BY sk_genre_id
)
SELECT g.nome_genero AS genero, COALESCE(c.filmes, 0) AS filmes
FROM dim_genres AS g
LEFT JOIN contagem AS c ON c.sk_genre_id = g.sk_genre_id
ORDER BY filmes DESC, genero, g.sk_genre_id
"""

_SQL_PRODUTORA_MAIOR_LUCRO = """
WITH lucro_por_produtora AS (
    -- (filme, produtora) é a chave da ponte: o lucro de um filme entra uma vez em cada
    -- produtora dele. Filmes diferentes com o mesmo lucro somam os dois (nada de DISTINCT).
    SELECT b.sk_company_id,
           ROUND(SUM(f.lucro_brl), 2) AS lucro_total_brl,
           COUNT(*) AS filmes
    FROM bridge_movie_company AS b
    JOIN fact_movies_performance AS f ON f.sk_movie_id = b.sk_movie_id
    GROUP BY b.sk_company_id
),
ranking AS (
    SELECT l.sk_company_id, c.nome_produtora AS produtora, l.lucro_total_brl, l.filmes,
           RANK() OVER (ORDER BY l.lucro_total_brl DESC) AS posicao
    FROM lucro_por_produtora AS l
    JOIN dim_companies AS c ON c.sk_company_id = l.sk_company_id
)
SELECT produtora, lucro_total_brl, filmes
FROM ranking
WHERE posicao = 1
ORDER BY produtora, sk_company_id
"""

_SQL_GENERO_MAIOR_MARGEM = """
WITH margens AS (
    SELECT sk_movie_id,
           CAST(receita_brl - orcamento_brl AS REAL) / receita_brl AS margem
    FROM fact_movies_performance
    WHERE receita_brl > 0
      AND orcamento_brl IS NOT NULL
),
por_genero AS (
    -- média simples das margens por filme (não é a margem agregada do gênero)
    SELECT b.sk_genre_id, ROUND(AVG(mg.margem), 12) AS margem_media, COUNT(*) AS filmes
    FROM margens AS mg
    JOIN bridge_movie_genre AS b ON b.sk_movie_id = mg.sk_movie_id
    GROUP BY b.sk_genre_id
),
ranking AS (
    SELECT pg.sk_genre_id, g.nome_genero AS genero, pg.margem_media, pg.filmes,
           RANK() OVER (ORDER BY pg.margem_media DESC) AS posicao
    FROM por_genero AS pg
    JOIN dim_genres AS g ON g.sk_genre_id = pg.sk_genre_id
)
SELECT genero, margem_media, filmes
FROM ranking
WHERE posicao = 1
ORDER BY genero, sk_genre_id
"""

_SQL_MAIS_AVALIADOS = """
WITH ranking AS (
    SELECT sk_movie_id, qtd_avaliacoes_usuarios,
           RANK() OVER (ORDER BY qtd_avaliacoes_usuarios DESC) AS posicao
    FROM dim_reviews
    WHERE qtd_avaliacoes_usuarios > 0
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento, r.qtd_avaliacoes_usuarios
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

_SQL_DIVERGENCIA_USUARIOS_IMDB = """
WITH notas AS (
    SELECT r.sk_movie_id, r.nota_media_usuarios, r.qtd_avaliacoes_usuarios, f.nota_imdb,
           ROUND(ABS(r.nota_media_usuarios - f.nota_imdb), 9) AS divergencia
    FROM dim_reviews AS r
    JOIN fact_movies_performance AS f ON f.sk_movie_id = r.sk_movie_id
    WHERE r.nota_media_usuarios IS NOT NULL
      AND f.nota_imdb > 0                     -- IMDb 0 ou nulo = sem nota
),
ranking AS (
    SELECT sk_movie_id, nota_media_usuarios, qtd_avaliacoes_usuarios, nota_imdb, divergencia,
           RANK() OVER (ORDER BY divergencia DESC) AS posicao
    FROM notas
)
SELECT r.posicao, m.id_filme, m.titulo, m.ano_lancamento,
       r.nota_media_usuarios, r.qtd_avaliacoes_usuarios, r.nota_imdb, r.divergencia
FROM ranking AS r
JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id
WHERE r.posicao <= {limit}
ORDER BY r.posicao, m.titulo, m.id_filme
"""

OFFICIAL_CASES: tuple[ReferenceCase, ...] = (
    ReferenceCase(
        case_id="oficial_01_maior_receita",
        question="Top 10 filmes com maior receita em R$",
        semantics=(
            "Receita = fact_movies_performance.receita_brl (receita, faturamento e bilheteria "
            "são sinônimos). Só filmes com receita informada; sem filtro de status nem de data. "
            "N = 10, do enunciado; empates no corte entram."
        ),
        sql=_SQL_MAIOR_RECEITA,
        columns=(*_MOVIE_TOP_COLUMNS, "receita_brl"),
        key_columns=("id_filme",),
        limit=10,
        official=True,
        paraphrases=(
            "Quais são os 10 filmes com maior receita em BRL?",
            "Quais são os 10 filmes com maior faturamento em reais?",
            "Quais os 10 filmes de maior bilheteria em BRL?",
        ),
    ),
    ReferenceCase(
        case_id="oficial_02_lucro_medio_por_genero",
        question="Lucro médio por gênero, considerando apenas filmes com receita informada",
        paraphrases=(
            "Qual é o lucro médio por gênero, considerando apenas filmes com receita informada?",
        ),
        semantics=(
            "Média do lucro_brl da Gold, literal, por gênero (via ponte), só para filmes com "
            "receita_brl não nula. O orçamento não é exigido: a Gold materializa o lucro mesmo "
            "sem orçamento (aí lucro = receita). Valor em centavos."
        ),
        sql=_SQL_LUCRO_MEDIO_POR_GENERO,
        columns=("genero", "filmes", "lucro_medio_brl"),
        key_columns=("genero",),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_03_maior_margem",
        question=(
            "Filmes com maior margem de lucro, entre os que possuem receita e orçamento informados"
        ),
        paraphrases=(
            "Quais filmes têm a maior margem de lucro, entre os que têm receita e orçamento "
            "informados?",
        ),
        semantics=(
            "Margem sobre a receita (não é ROI): (receita_brl - orcamento_brl) / receita_brl, "
            "em REAL, exigindo orçamento informado e receita > 0. N = 10 é decisão de exibição "
            "(o enunciado não fixa N); empates no corte entram."
        ),
        sql=_SQL_MAIOR_MARGEM,
        columns=(*_MOVIE_TOP_COLUMNS, "receita_brl", "orcamento_brl", "margem_lucro"),
        key_columns=("id_filme",),
        limit=10,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_04_mais_populares",
        question="Os 5 filmes mais populares",
        paraphrases=("Quais são os 5 filmes mais populares?",),
        semantics=(
            "popularidade da Gold, literal; valores que parecem anômalos não são removidos. "
            "N = 5, do enunciado; empates no corte entram."
        ),
        sql=_SQL_MAIS_POPULARES,
        columns=(*_MOVIE_TOP_COLUMNS, "popularidade"),
        key_columns=("id_filme",),
        limit=5,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_05_divergencia_tmdb_imdb",
        question="Filmes com maior divergência entre a nota TMDB e a nota IMDb",
        paraphrases=("Quais filmes têm a maior divergência entre a nota do TMDB e a do IMDb?",),
        semantics=(
            "Divergência = |nota_tmdb - nota_imdb|. IMDb vale só com nota_imdb > 0. TMDB 0 sem "
            "votos (qtd_tmdb nulo ou 0) é ausência de nota; TMDB 0 com votos é nota real. "
            "N = 10 é decisão de exibição; empates no corte entram."
        ),
        sql=_SQL_DIVERGENCIA_TMDB_IMDB,
        columns=(*_MOVIE_TOP_COLUMNS, "nota_tmdb", "qtd_tmdb", "nota_imdb", "divergencia"),
        key_columns=("id_filme",),
        limit=10,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_06_nota_imdb_por_ano",
        question="Nota média IMDb por ano de lançamento",
        paraphrases=("Qual é a nota média do IMDb por ano de lançamento?",),
        semantics=(
            "Média de nota_imdb por ano_lancamento, só com notas válidas (nota_imdb > 0). Sem "
            "filtro oculto de status ou de data: filmes ainda não lançados com nota entram."
        ),
        sql=_SQL_NOTA_IMDB_POR_ANO,
        columns=("ano_lancamento", "filmes_com_nota", "nota_media_imdb"),
        key_columns=("ano_lancamento",),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_07_ator_mais_ativo_5_anos",
        question="Ator com mais participações em filmes lançados nos últimos 5 anos",
        paraphrases=("Qual ator apareceu em mais filmes lançados nos últimos 5 anos?",),
        semantics=(
            "Janela móvel de 5 anos pela data de referência do projeto, de data a data e "
            "inclusiva nas duas pontas (2026-10-01 -> 2021-10-01 a 2026-10-01), sobre "
            "data_lancamento e com status_filme = 'Lançado'. Conta filmes distintos de cada "
            "linha de ator; devolve todos os líderes empatados."
        ),
        sql=_SQL_ATOR_MAIS_ATIVO,
        columns=("ator", "filmes"),
        key_columns=("ator",),
        window_years=5,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_08_diretores_melhor_nota",
        question="Diretores com maior nota média (mínimo de 5 filmes)",
        paraphrases=("Quais diretores têm a maior nota média, com no mínimo 5 filmes?",),
        semantics=(
            "Mínimo de 5 filmes dirigidos no total (com ou sem nota). A média usa só notas "
            "IMDb válidas (nota_imdb > 0); o resultado mostra os filmes dirigidos e os filmes "
            "com nota usados na média. Diretores sem nenhuma nota válida ficam de fora. N = 10 "
            "é decisão de exibição; empates no corte entram."
        ),
        sql=_SQL_DIRETORES_MELHOR_NOTA,
        columns=("posicao", "diretor", "media_imdb", "filmes_com_nota", "filmes_dirigidos"),
        key_columns=("diretor",),
        limit=10,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_09_par_ator_diretor",
        question="Dupla ator–diretor que mais trabalhou junta",
        paraphrases=("Qual par ator-diretor trabalhou junto mais vezes?",),
        semantics=(
            "Pares (linha de ator, linha de diretor) de dim_people, contando os filmes em comum "
            "pela ponte, sem duplicação cartesiana. Nomes iguais nos dois papéis não são "
            "excluídos: o dado não prova que são a mesma pessoa. Devolve todos os pares "
            "empatados no topo. O SQL tem uma poda exata que só reduz o tempo."
        ),
        sql=_SQL_PAR_ATOR_DIRETOR,
        columns=("ator", "diretor", "filmes_juntos"),
        key_columns=("ator", "diretor"),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_10_filmes_por_genero",
        question="Quantidade de filmes por gênero",
        paraphrases=("Quantos filmes existem por gênero?",),
        semantics=(
            "Filmes distintos por gênero, pela ponte; um filme com vários gêneros conta uma vez "
            "em cada um. Todos os gêneros aparecem, inclusive com zero."
        ),
        sql=_SQL_FILMES_POR_GENERO,
        columns=("genero", "filmes"),
        key_columns=("genero",),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_11_produtora_maior_lucro",
        question="Produtora com maior lucro total",
        paraphrases=("Qual produtora teve o maior lucro total?",),
        semantics=(
            "Soma do lucro_brl da Gold por produtora, sem filtro de receita. Um filme com várias "
            "produtoras entra no total de cada uma; filmes diferentes com o mesmo lucro somam "
            "os dois. Devolve todos os líderes empatados (valor em centavos)."
        ),
        sql=_SQL_PRODUTORA_MAIOR_LUCRO,
        columns=("produtora", "lucro_total_brl", "filmes"),
        key_columns=("produtora",),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_12_genero_maior_margem",
        question="Gênero com maior margem de lucro média",
        paraphrases=("Qual gênero tem a maior margem de lucro média?",),
        semantics=(
            "Mesma margem por filme do caso 03 (receita > 0 e orçamento informado); por gênero, "
            "a média simples dessas margens, não a margem agregada ponderada pela receita. "
            "Devolve todos os líderes empatados."
        ),
        sql=_SQL_GENERO_MAIOR_MARGEM,
        columns=("genero", "margem_media", "filmes"),
        key_columns=("genero",),
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_13_mais_avaliados",
        question="Filmes mais avaliados pelos usuários",
        paraphrases=("Quais são os filmes com mais avaliações?",),
        semantics=(
            "Contagem agregada da Gold (dim_reviews.qtd_avaliacoes_usuarios); filmes sem "
            "avaliação não entram. N = 10 é decisão de exibição; empates no corte entram."
        ),
        sql=_SQL_MAIS_AVALIADOS,
        columns=(*_MOVIE_TOP_COLUMNS, "qtd_avaliacoes_usuarios"),
        key_columns=("id_filme",),
        limit=10,
        official=True,
    ),
    ReferenceCase(
        case_id="oficial_14_divergencia_usuarios_imdb",
        question="Filmes em que a nota média dos usuários mais diverge da nota IMDb",
        paraphrases=(
            "Quais filmes têm a maior divergência entre a nota média dos usuários e a nota do "
            "IMDb?",
        ),
        semantics=(
            "Divergência = |nota_media_usuarios - nota_imdb|, exigindo média de usuários "
            "(dim_reviews) e IMDb válido (nota_imdb > 0). N = 10 é decisão de exibição; empates "
            "no corte entram."
        ),
        sql=_SQL_DIVERGENCIA_USUARIOS_IMDB,
        columns=(
            *_MOVIE_TOP_COLUMNS,
            "nota_media_usuarios",
            "qtd_avaliacoes_usuarios",
            "nota_imdb",
            "divergencia",
        ),
        key_columns=("id_filme",),
        limit=10,
        official=True,
    ),
)


def official_registry() -> ReferenceRegistry:
    """Um registro novo com os 14 exemplos oficiais; casos extras podem ser acrescentados."""
    return ReferenceRegistry(OFFICIAL_CASES)


__all__ = [
    "DEFAULT_MAX_ROWS",
    "OFFICIAL_CASES",
    "ReferenceCase",
    "ReferenceCaseError",
    "ReferenceRegistry",
    "ReferenceResult",
    "official_registry",
    "run_case",
    "run_cases",
]
