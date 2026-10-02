"""Testes do acesso seguro ao banco (offline: bancos sintéticos com a forma da Gold)."""

from __future__ import annotations

import _thread
import hashlib
import re
import shutil
import sqlite3
import threading
import time
import tomllib
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from cinedata import db as db_module
from cinedata.config import load_settings
from cinedata.db import (
    ALLOWED_FUNCTIONS,
    GOLD_TABLES,
    DatabaseUnavailableError,
    QueryFailedError,
    QueryRejectedError,
    QueryTimeoutError,
    SafeDatabase,
    SafeDatabaseError,
)

# --- banco sintético com a forma da Gold ---------------------------------------------------------

GOLD_DDL = (
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
    "CREATE TABLE dim_genres (sk_genre_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_genero VARCHAR(50) NOT NULL UNIQUE)",
    "CREATE TABLE dim_companies (sk_company_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_produtora VARCHAR(255) NOT NULL UNIQUE)",
    "CREATE TABLE dim_people (sk_person_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_pessoa VARCHAR(255), tipo_pessoa VARCHAR(20))",
    "CREATE TABLE dim_movies (sk_movie_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " id_filme VARCHAR(50), titulo VARCHAR(500), data_lancamento DATE, ano_lancamento INTEGER,"
    " duracao_minutos INTEGER, idioma_original VARCHAR(10), status_filme VARCHAR(50),"
    " sinopse VARCHAR(4000), url_poster VARCHAR(2048), url_backdrop VARCHAR(2048))",
    "CREATE TABLE fact_movies_performance (sk_movie_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " orcamento_usd NUMERIC(18, 2), receita_usd NUMERIC(18, 2), lucro_usd NUMERIC(18, 2),"
    " orcamento_brl NUMERIC(18, 2), receita_brl NUMERIC(18, 2), lucro_brl NUMERIC(18, 2),"
    " popularidade DOUBLE, nota_tmdb DOUBLE, qtd_tmdb INTEGER, nota_imdb DOUBLE,"
    " qtd_imdb INTEGER)",
    "CREATE TABLE dim_reviews (sk_review_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " sk_movie_id VARCHAR(64), qtd_avaliacoes_usuarios INTEGER, nota_media_usuarios DOUBLE)",
    "CREATE TABLE bridge_movie_company (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_company_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_company_id))",
    "CREATE TABLE bridge_movie_genre (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_genre_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_genre_id))",
    "CREATE TABLE bridge_movie_person (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_person_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_person_id))",
    "CREATE TABLE movie_reviews (id INTEGER NOT NULL PRIMARY KEY, sk_movie_review_id VARCHAR(64),"
    " sk_movie_id VARCHAR(64), name VARCHAR(120), rating DOUBLE, text VARCHAR(4000),"
    " created_at DATETIME)",
)

LONG_SYNOPSIS = "x" * 5000

GENRES = [("g1", "Drama"), ("g2", "Ação"), ("g3", "Ficção científica"), ("g4", "日本映画")]
COMPANIES = [("c1", "Studio Ghibli"), ("c2", "Café Filmes"), ("c3", "Émile 🎬 Productions")]
PEOPLE = [
    ("p1", "Ana Souza", "ator"),
    ("p2", "Bruno Lima", "diretor"),
    ("p3", "宮崎 駿", "diretor"),
    ("p4", "Zoë Ünal", "ator"),
]


def movie(
    key: str,
    title: str | None,
    released: str | None,
    minutes: int | None,
    language: str | None = "en",
    status: str = "Lançado",
    synopsis: str | None = "Sinopse.",
) -> tuple[object, ...]:
    year = int(released[:4]) if released else None
    return (
        key,
        "tt" + key[1:],
        title,
        released,
        year,
        minutes,
        language,
        status,
        synopsis,
        "http://p/" + key,
        "http://b/" + key,
    )


def fact(
    key: str,
    budget: int | None,
    revenue: int | None,
    popularity: float,
    tmdb: float,
    tmdb_votes: int,
    imdb: float | None,
    imdb_votes: int | None,
) -> tuple[object, ...]:
    profit = None if revenue is None else revenue - (budget or 0)
    in_brl = lambda value: None if value is None else value * 5  # noqa: E731
    return (
        key, budget, revenue, profit, in_brl(budget), in_brl(revenue), in_brl(profit),
        popularity, tmdb, tmdb_votes, imdb, imdb_votes,
    )  # fmt: skip


MOVIES = [
    movie("m1", "Cidade de Deus", "2002-08-30", 130, "pt", synopsis="Favela."),
    movie("m2", "Amélie", "2001-04-25", 122, "fr", synopsis="Paris."),
    movie("m3", "千と千尋の神隠し", "2001-07-20", 125, "ja", synopsis="Banho."),
    movie("m4", "Now", "2020-05-05", 90, synopsis="Agora."),
    movie("m5", "Movie 🎬 Premiere", "2023-01-15", 100, synopsis="Estreia."),
    movie("m6", "Sem Dados", None, None, None, "Em produção", None),
    movie("m7", "O Auto da Compadecida", "2000-09-15", 104, "pt", synopsis="Sertão."),
    movie("m8", "Sinopse Longa", "2010-10-10", 95, synopsis=LONG_SYNOPSIS),
]
FACTS = [
    fact("m1", 3_300_000, 30_000_000, 12.5, 8.4, 1000, 8.6, 800),
    fact("m2", 10_000_000, 174_000_000, 20.1, 7.9, 900, 8.3, 700),
    fact("m3", 19_000_000, 395_000_000, 30.0, 8.5, 800, 8.6, 600),
    fact("m4", None, 1000, 1.0, 0.0, 0, None, None),
    fact("m5", 0, 0, 0.5, 5.0, 10, 5.1, 20),
    fact("m7", 4_000_000, 17_000_000, 9.9, 8.0, 500, 8.7, 400),
]


def build_gold_db(path: Path, *, extra_movies: int = 0, wal: bool = False) -> Path:
    """Cria um banco com as 10 tabelas da Gold (mais `alembic_version`) e poucas linhas."""
    con = sqlite3.connect(path)
    try:
        for ddl in GOLD_DDL:
            con.execute(ddl)
        con.execute("INSERT INTO alembic_version VALUES ('abc123')")
        con.executemany("INSERT INTO dim_genres VALUES (?, ?)", GENRES)
        con.executemany("INSERT INTO dim_companies VALUES (?, ?)", COMPANIES)
        con.executemany("INSERT INTO dim_people VALUES (?, ?, ?)", PEOPLE)
        con.executemany("INSERT INTO dim_movies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", MOVIES)
        con.executemany(
            "INSERT INTO fact_movies_performance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", FACTS
        )
        con.executemany(
            "INSERT INTO dim_reviews VALUES (?, ?, ?, ?)",
            [("r1", "m1", 100, 8.1), ("r2", "m2", 80, 7.7)],
        )
        con.executemany(
            "INSERT INTO bridge_movie_genre VALUES (?, ?)",
            [("m1", "g1"), ("m2", "g1"), ("m3", "g4"), ("m3", "g3"), ("m7", "g2")],
        )
        con.executemany(
            "INSERT INTO bridge_movie_company VALUES (?, ?)", [("m3", "c1"), ("m2", "c2")]
        )
        con.executemany(
            "INSERT INTO bridge_movie_person VALUES (?, ?)",
            [("m1", "p1"), ("m1", "p2"), ("m3", "p3"), ("m4", "p4")],
        )
        con.executemany(
            "INSERT INTO movie_reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "mr1", "m1", "Fulano de Tal", 9.0, "Ótimo.", "2020-01-01"),
                (2, "mr2", "m2", "Beltrana", 8.0, "Bom.", "2020-02-02"),
            ],
        )
        con.executemany(
            "INSERT INTO dim_movies (sk_movie_id, titulo, ano_lancamento, duracao_minutos)"
            " VALUES (?, ?, ?, ?)",
            [(f"x{i}", f"Extra {i}", 1990 + i % 35, 60 + i % 100) for i in range(extra_movies)],
        )
        con.commit()
        if wal:
            con.execute("PRAGMA journal_mode = WAL")
            con.commit()
    finally:
        con.close()
    return path


@pytest.fixture(scope="session")
def gold_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_gold_db(tmp_path_factory.mktemp("gold") / "gold.db")


@pytest.fixture(scope="session")
def heavy_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_gold_db(tmp_path_factory.mktemp("heavy") / "heavy.db", extra_movies=3000)


@pytest.fixture
def db(gold_db_path: Path) -> Iterator[SafeDatabase]:
    with SafeDatabase(gold_db_path, max_rows=50, timeout_s=5.0) as database:
        yield database


@pytest.fixture
def db_no_pregate(gold_db_path: Path) -> Iterator[SafeDatabase]:
    """Banco com o pré-filtro de texto desligado: só as demais camadas protegem."""
    with SafeDatabase(gold_db_path, max_rows=50, timeout_s=5.0) as database:
        database._pregate_enabled = False
        yield database


@pytest.fixture
def writable_copy(gold_db_path: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "copy.db"
    shutil.copy(gold_db_path, copy)
    return copy


def snapshot(db_file: Path) -> tuple[str, list[str], list[str]]:
    """Conteúdo do arquivo, arquivos ao lado dele e arquivos na pasta atual."""
    digest = hashlib.sha256(db_file.read_bytes()).hexdigest()
    return (
        digest,
        sorted(p.name for p in db_file.parent.iterdir()),
        sorted(p.name for p in Path.cwd().iterdir()),
    )


def assert_usable(database: SafeDatabase) -> None:
    assert database.execute("SELECT 1").rows == ((1,),)


def assert_no_path(error: BaseException, *paths: Path) -> None:
    """A mensagem (que pode ir ao modelo) nunca traz caminho absoluto nem o da pasta pai."""
    text = str(error)
    for path in paths:
        for form in {str(path), str(path.resolve()), path.as_posix(), path.resolve().as_posix()}:
            assert form not in text, f"caminho vazado na mensagem: {form}"
    assert not re.search(r"[A-Za-z]:[\\/]", text), f"caminho com unidade na mensagem: {text}"


class FakeClock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now


# --- caminho feliz --------------------------------------------------------------------------------


def test_simple_select(db: SafeDatabase) -> None:
    result = db.execute("SELECT nome_genero FROM dim_genres ORDER BY nome_genero")
    assert result.columns == ("nome_genero",)
    assert result.rows == (("Ação",), ("Drama",), ("Ficção científica",), ("日本映画",))
    assert result.truncated is False
    assert result.truncated_cells == 0
    assert result.elapsed_s >= 0


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT count(*) FROM dim_movies", ((8,),)),
        ("SELECT COUNT(*) FROM DIM_MOVIES", ((8,),)),
        ("select titulo from dim_movies where sk_movie_id = 'm2'", (("Amélie",),)),
        ("SELECT TITULO FROM DIM_MOVIES WHERE SK_MOVIE_ID = 'm2'", (("Amélie",),)),
        ('SELECT "titulo" FROM "dim_movies" WHERE "sk_movie_id" = \'m2\'', (("Amélie",),)),
        ("SELECT 1 + 1", ((2,),)),
        ("SELECT count(DISTINCT sk_movie_id) FROM bridge_movie_genre", ((4,),)),
        ("SELECT 10 / 4, CAST(10 AS REAL) / 4", ((2, 2.5),)),
        ("SELECT COALESCE(NULL, 'x'), IFNULL(NULL, 'y'), NULLIF(1, 1)", (("x", "y", None),)),
        (
            "SELECT upper(titulo), length(titulo) FROM dim_movies WHERE sk_movie_id = 'm4'",
            (("NOW", 3),),
        ),
        ("SELECT titulo FROM dim_movies WHERE titulo LIKE 'am%'", (("Amélie",),)),
        (
            "SELECT strftime('%Y', '2001-07-20'), date('2001-07-20', '+1 day')",
            (("2001", "2001-07-21"),),
        ),
        ("SELECT round(2.567, 1), abs(-3), max(1, 2), min(1, 2)", ((2.6, 3, 2, 1),)),
        (
            "SELECT sk_genre_id FROM dim_genres WHERE nome_genero IN ('Drama', 'Ação') ORDER BY 1",
            (("g1",), ("g2",)),
        ),
    ],
)
def test_typical_queries(
    db: SafeDatabase, sql: str, expected: tuple[tuple[object, ...], ...]
) -> None:
    assert db.execute(sql).rows == expected


def test_join_through_a_bridge_table(db: SafeDatabase) -> None:
    result = db.execute(
        "SELECT g.nome_genero, count(*) AS filmes FROM dim_genres g"
        " JOIN bridge_movie_genre b ON b.sk_genre_id = g.sk_genre_id"
        " GROUP BY g.nome_genero ORDER BY filmes DESC, g.nome_genero"
    )
    assert result.columns == ("nome_genero", "filmes")
    assert result.rows == (("Drama", 2), ("Ação", 1), ("Ficção científica", 1), ("日本映画", 1))


def test_cte_subquery_union_and_window(db: SafeDatabase) -> None:
    assert db.execute(
        "WITH x AS (SELECT sk_genre_id FROM dim_genres) SELECT count(*) FROM x"
    ).rows == ((4,),)
    assert db.execute(
        "SELECT titulo FROM dim_movies WHERE sk_movie_id IN"
        " (SELECT sk_movie_id FROM bridge_movie_genre WHERE sk_genre_id = 'g2')"
    ).rows == (("O Auto da Compadecida",),)
    assert db.execute("SELECT 1 UNION SELECT 2 ORDER BY 1").rows == ((1,), (2,))
    ranked = db.execute(
        "SELECT sk_movie_id, rank() OVER (ORDER BY ano_lancamento DESC) AS r FROM dim_movies"
        " WHERE ano_lancamento IS NOT NULL ORDER BY r LIMIT 2"
    )
    assert ranked.rows == (("m5", 1), ("m4", 2))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",
        "SELECT 1 -- comentário no fim",
        "/* antes */ SELECT 1",
        "-- antes\nSELECT 1",
        "\n\t  SELECT 1",
        "select 1",
        "WITH a AS (SELECT 1 x) SELECT x FROM a",
        "  with a as (select 1 x) select x from a  ;  ",
    ],
)
def test_harmless_formatting_variations_are_accepted(db: SafeDatabase, sql: str) -> None:
    assert db.execute(sql).rows == ((1,),)


@pytest.mark.parametrize(
    ("sql", "title"),
    [
        ("SELECT titulo FROM dim_movies WHERE titulo = 'Amélie'", "Amélie"),
        ("SELECT titulo FROM dim_movies WHERE titulo = '千と千尋の神隠し'", "千と千尋の神隠し"),
        ("SELECT titulo FROM dim_movies WHERE titulo = 'Movie 🎬 Premiere'", "Movie 🎬 Premiere"),
        ("SELECT titulo FROM dim_movies WHERE titulo = 'Now'", "Now"),
        ("SELECT nome_pessoa FROM dim_people WHERE nome_pessoa = 'Zoë Ünal'", "Zoë Ünal"),
        ("SELECT nome_pessoa FROM dim_people WHERE nome_pessoa = '宮崎 駿'", "宮崎 駿"),
    ],
)
def test_unicode_values_round_trip(db: SafeDatabase, sql: str, title: str) -> None:
    assert db.execute(sql).rows == ((title,),)


def test_null_and_empty_results(db: SafeDatabase) -> None:
    assert db.execute("SELECT data_lancamento FROM dim_movies WHERE sk_movie_id = 'm6'").rows == (
        (None,),
    )
    empty = db.execute("SELECT titulo FROM dim_movies WHERE 1 = 0")
    assert empty.rows == ()
    assert empty.columns == ("titulo",)
    assert empty.truncated is False


def test_result_is_immutable(db: SafeDatabase) -> None:
    result = db.execute("SELECT 1")
    with pytest.raises(FrozenInstanceError):
        result.truncated = True  # type: ignore[misc]


# --- limite de linhas e células ------------------------------------------------------------------


@pytest.fixture
def small(gold_db_path: Path) -> Iterator[SafeDatabase]:
    with SafeDatabase(gold_db_path, max_rows=3, timeout_s=5.0) as database:
        yield database


@pytest.mark.parametrize(
    ("sql", "rows", "truncated"),
    [
        ("SELECT sk_movie_id FROM dim_movies ORDER BY sk_movie_id", 3, True),
        ("SELECT sk_movie_id FROM dim_movies ORDER BY sk_movie_id LIMIT 4", 3, True),
        ("SELECT sk_movie_id FROM dim_movies ORDER BY sk_movie_id LIMIT 3", 3, False),
        ("SELECT sk_movie_id FROM dim_movies ORDER BY sk_movie_id LIMIT 2", 2, False),
        ("SELECT sk_movie_id FROM dim_movies WHERE 1 = 0", 0, False),
        ("SELECT sk_movie_id FROM dim_movies WHERE sk_movie_id <= 'm3'", 3, False),
        ("SELECT sk_movie_id FROM dim_movies WHERE sk_movie_id <= 'm4'", 3, True),
    ],
)
def test_row_limit(small: SafeDatabase, sql: str, rows: int, truncated: bool) -> None:
    result = small.execute(sql)
    assert len(result.rows) == rows
    assert result.truncated is truncated
    assert result.max_rows == 3


def test_row_limit_of_one_and_exactly_full(gold_db_path: Path) -> None:
    with SafeDatabase(gold_db_path, max_rows=1) as one:
        assert one.execute("SELECT 1 UNION SELECT 2").truncated is True
        assert one.execute("SELECT 1").truncated is False
    with SafeDatabase(gold_db_path, max_rows=8) as eight:
        full = eight.execute("SELECT sk_movie_id FROM dim_movies")
        assert (len(full.rows), full.truncated) == (8, False)


def test_the_sql_is_never_rewritten_to_add_a_limit(small: SafeDatabase) -> None:
    executed: list[str] = []
    small._con.set_trace_callback(executed.append)  # type: ignore[union-attr]
    sql = "SELECT sk_movie_id FROM dim_movies ORDER BY sk_movie_id"
    small.execute(sql)
    assert executed == [sql]
    assert all("LIMIT" not in statement.upper() for statement in executed)


def test_long_text_cells_are_cut_and_counted(gold_db_path: Path) -> None:
    with SafeDatabase(gold_db_path, max_cell_chars=100) as database:
        result = database.execute(
            "SELECT sinopse, titulo FROM dim_movies WHERE sk_movie_id IN ('m8', 'm2') ORDER BY 2"
        )
    (amelie_synopsis, _), (long_synopsis, _) = result.rows
    assert amelie_synopsis == "Paris."
    assert long_synopsis.startswith("x" * 100)
    assert long_synopsis.endswith("[+4900 caracteres]")
    assert result.truncated_cells == 1


def test_default_cell_limit_keeps_a_5000_character_text_short(db: SafeDatabase) -> None:
    value = db.execute("SELECT sinopse FROM dim_movies WHERE sk_movie_id = 'm8'").rows[0][0]
    assert isinstance(value, str)
    assert len(value) < 1100


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT x'00ff10'", "<blob de 3 bytes>"),
        ("SELECT NULL", None),
        ("SELECT 9223372036854775807", 9223372036854775807),
        ("SELECT 1.5", 1.5),
        ("SELECT 'á'", "á"),
        ("SELECT ''", ""),
    ],
)
def test_cell_values(db: SafeDatabase, sql: str, expected: object) -> None:
    assert db.execute(sql).rows == ((expected,),)


def test_infinite_float_does_not_break_the_result(db: SafeDatabase) -> None:
    assert db.execute("SELECT 1e999").rows == ((float("inf"),),)


def test_repeated_column_names_do_not_lose_data(db: SafeDatabase) -> None:
    result = db.execute(
        "SELECT a.titulo, b.titulo FROM dim_movies a JOIN dim_movies b"
        " ON a.sk_movie_id = 'm1' AND b.sk_movie_id = 'm2'"
    )
    assert result.columns == ("titulo", "titulo")
    assert result.rows == (("Cidade de Deus", "Amélie"),)


# --- pré-filtro de texto (só UX) ------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, b"SELECT 1", ["SELECT 1"], 1.5, object()])
def test_non_text_is_rejected(db: SafeDatabase, bad: object) -> None:
    with pytest.raises(QueryRejectedError, match="texto"):
        db.execute(bad)  # type: ignore[arg-type]
    assert_usable(db)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "\n\t",
        ";",
        " ; ",
        "-- só um comentário",
        "/* só */",
        "/* aberto sem fechar SELECT 1",
    ],
)
def test_empty_or_comment_only_is_rejected(db: SafeDatabase, sql: str) -> None:
    with pytest.raises(QueryRejectedError, match="vazia|não devolve"):
        db.execute(sql)
    assert_usable(db)


def test_empty_is_rejected_even_without_the_pregate(db_no_pregate: SafeDatabase) -> None:
    for sql in ("", ";", "-- x", "/* x */"):
        with pytest.raises(QueryRejectedError, match="não devolve"):
            db_no_pregate.execute(sql)


def test_nul_and_invalid_unicode_are_rejected(db: SafeDatabase) -> None:
    with pytest.raises(QueryRejectedError, match="nulo"):
        db.execute("SELECT 1\x00; DROP TABLE dim_genres")
    with pytest.raises(QueryRejectedError, match="Unicode inválidos"):
        db.execute("SELECT '\ud800'")
    assert_usable(db)


def test_sql_size_limit_is_measured_in_bytes(db: SafeDatabase) -> None:
    exactly = "SELECT 1" + " " * (20_000 - len("SELECT 1"))
    assert len(exactly.encode()) == 20_000
    assert db.execute(exactly).rows == ((1,),)
    with pytest.raises(QueryRejectedError, match="grande demais"):
        db.execute(exactly + " ")
    with pytest.raises(QueryRejectedError, match="grande demais"):
        db.execute("SELECT '" + "é" * 10_001 + "'")  # ~10 mil caracteres, mas mais de 20 mil bytes
    assert_usable(db)


def test_two_statements_are_rejected(db: SafeDatabase) -> None:
    for sql in (
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE dim_genres",
        "SELECT 1; -- ok\nSELECT 2",
    ):
        with pytest.raises(QueryRejectedError, match="uma instrução"):
            db.execute(sql)
    assert_usable(db)


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("EXPLAIN QUERY PLAN SELECT 1", "EXPLAIN"),
        ("VALUES (1)", "VALUES"),
        ("(SELECT 1)", "símbolo"),
        ("PRAGMA table_info(dim_genres)", "PRAGMA"),
        ("  \n/* c */ -- d\n DELETE FROM dim_genres", "DELETE"),
    ],
)
def test_pregate_names_the_offending_keyword(db: SafeDatabase, sql: str, keyword: str) -> None:
    with pytest.raises(QueryRejectedError, match=keyword):
        db.execute(sql)


# --- matriz adversarial ---------------------------------------------------------------------------

WRITES = [
    "INSERT INTO dim_genres VALUES ('x', 'y')",
    "INSERT OR REPLACE INTO dim_genres VALUES ('g1', 'y')",
    "REPLACE INTO dim_genres VALUES ('g1', 'y')",
    "INSERT INTO dim_genres SELECT sk_genre_id || 'x', nome_genero || 'x' FROM dim_genres",
    "UPDATE dim_genres SET nome_genero = 'x'",
    "UPDATE dim_movies SET titulo = m.titulo FROM dim_movies AS m"
    " WHERE m.sk_movie_id = dim_movies.sk_movie_id",
    "DELETE FROM dim_genres",
    "DELETE FROM dim_genres WHERE 1 = 1",
    "WITH c AS (SELECT 1) DELETE FROM dim_genres",
    "WITH c AS (SELECT 1) INSERT INTO dim_genres VALUES ('x', 'y')",
    "WITH c AS (SELECT 1) UPDATE dim_genres SET nome_genero = 'x'",
]
STRUCTURE = [
    "DROP TABLE dim_genres",
    "DROP TABLE IF EXISTS alembic_version",
    "ALTER TABLE dim_genres ADD COLUMN x TEXT",
    "ALTER TABLE dim_genres RENAME TO g2",
    "ALTER TABLE dim_genres RENAME COLUMN nome_genero TO n",
    "CREATE TABLE t (x)",
    "CREATE TEMP TABLE t (x)",
    "CREATE TABLE t AS SELECT * FROM dim_genres",
    "CREATE VIEW v AS SELECT 1",
    "CREATE TEMP VIEW v AS SELECT 1",
    "CREATE TRIGGER tr AFTER INSERT ON dim_genres BEGIN SELECT 1; END",
    "CREATE INDEX ix ON dim_genres (nome_genero)",
    "CREATE VIRTUAL TABLE v USING fts5(x)",
    "ANALYZE",
    "REINDEX",
]
FILES_AND_PRAGMAS = [
    "ATTACH DATABASE ':memory:' AS x",
    "ATTACH DATABASE 'attached.db' AS x",
    "ATTACH DATABASE 'file:attached_uri.db?mode=rwc' AS x",
    "DETACH DATABASE main",
    "VACUUM INTO 'vacuum_copy.db'",
    "PRAGMA query_only = OFF",
    "PRAGMA writable_schema = ON",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA user_version = 7",
    "PRAGMA table_info(dim_genres)",
    "PRAGMA foreign_keys = ON",
    "PRAGMA cache_size = 1",
    "PRAGMA temp_store = FILE",
]
TRANSACTIONS = ["BEGIN", "BEGIN IMMEDIATE", "COMMIT", "END", "ROLLBACK", "SAVEPOINT s", "RELEASE s"]
BASE_ATTACKS = WRITES + STRUCTURE + FILES_AND_PRAGMAS + TRANSACTIONS
# O authorizer não é consultado em VACUUM sem INTO: ele fica só nas matrizes de pilha completa.
PLAIN_VACUUM = ["VACUUM"]


def with_variations(statements: list[str]) -> list[str]:
    forms = (
        lambda s: s,
        lambda s: "/*x*/" + s,
        lambda s: "-- c\n" + s,
        lambda s: "\n\t " + s.lower(),
    )
    return [form(statement) for statement in statements for form in forms]


ALL_ATTACKS = with_variations(BASE_ATTACKS + PLAIN_VACUUM)

HIDDEN_READS = [
    "SELECT * FROM alembic_version",
    "SELECT version_num FROM alembic_version",
    "SELECT name FROM sqlite_master",
    "SELECT sql FROM sqlite_schema",
    "SELECT name FROM main.sqlite_master",
    "SELECT name FROM sqlite_temp_master",
    "SELECT * FROM pragma_table_info('dim_genres')",
    "SELECT * FROM pragma_database_list",
    "SELECT * FROM json_each('[1]')",
    "SELECT name FROM movie_reviews",
    "SELECT * FROM movie_reviews",
    "SELECT m.name FROM movie_reviews AS m",
    "SELECT count(name) FROM movie_reviews",
    "SELECT id FROM movie_reviews WHERE name LIKE 'F%'",
    "SELECT id FROM movie_reviews ORDER BY name",
    "SELECT id FROM movie_reviews GROUP BY name",
    "SELECT rowid FROM dim_genres",
    "SELECT _rowid_ FROM dim_genres",
    "SELECT oid FROM dim_genres",
    "SELECT * FROM dim_genres, alembic_version",
    "SELECT (SELECT version_num FROM alembic_version)",
    "WITH x AS (SELECT version_num FROM alembic_version) SELECT * FROM x",
]
FORBIDDEN_FUNCTIONS = [
    "SELECT load_extension('x')",
    "SELECT random()",
    "SELECT randomblob(8)",
    "SELECT zeroblob(1000000000)",
    "SELECT sqlite_version()",
    "SELECT sqlite_source_id()",
    "SELECT sqlite_compileoption_used('DQS')",
    "SELECT printf('%d', 1)",
    "SELECT format('%d', 1)",
    "SELECT hex('a')",
    "SELECT quote('a')",
    "SELECT char(65)",
    "SELECT unicode('a')",
    "SELECT json('1')",
    "SELECT json_extract('{}', '$')",
    "SELECT likely(1)",
    "SELECT unlikely(1)",
    "SELECT changes()",
    "SELECT total_changes()",
    "SELECT last_insert_rowid()",
    "SELECT sqlite_offset(1)",
    "SELECT titulo FROM dim_movies ORDER BY random()",
]
CURRENT_FORMS = [
    "SELECT CURRENT_DATE",
    "SELECT CURRENT_TIME",
    "SELECT CURRENT_TIMESTAMP",
    "SELECT current_date",
    "SELECT Current_Timestamp",
    "SELECT titulo FROM dim_movies WHERE data_lancamento <= CURRENT_DATE",
    "WITH x AS (SELECT CURRENT_DATE AS d) SELECT d FROM x",
    "SELECT (SELECT CURRENT_DATE)",
    "SELECT COALESCE(NULL, CASE WHEN 1 THEN CURRENT_TIME END)",
]
RECURSIVE = [
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT n FROM c",
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c",
]


@pytest.mark.parametrize("sql", ALL_ATTACKS)
def test_attack_is_rejected_and_nothing_changes(
    db: SafeDatabase, gold_db_path: Path, sql: str
) -> None:
    before = snapshot(gold_db_path)
    with pytest.raises(SafeDatabaseError):
        db.execute(sql)
    assert snapshot(gold_db_path) == before
    assert_usable(db)


@pytest.mark.parametrize("sql", ALL_ATTACKS)
def test_attack_is_blocked_even_without_the_pregate(
    db_no_pregate: SafeDatabase, gold_db_path: Path, sql: str
) -> None:
    before = snapshot(gold_db_path)
    with pytest.raises(SafeDatabaseError):
        db_no_pregate.execute(sql)
    assert snapshot(gold_db_path) == before
    assert_usable(db_no_pregate)
    assert db_no_pregate.execute("SELECT count(*) FROM dim_genres").rows == ((4,),)


@pytest.mark.parametrize("sql", with_variations(BASE_ATTACKS))
def test_authorizer_reports_the_denial_for_every_attack_it_can_see(
    db_no_pregate: SafeDatabase, sql: str
) -> None:
    with pytest.raises(QueryRejectedError):
        db_no_pregate.execute(sql)


@pytest.mark.parametrize("sql", HIDDEN_READS)
def test_hidden_tables_columns_and_rowid_are_denied(db: SafeDatabase, sql: str) -> None:
    with pytest.raises(QueryRejectedError):
        db.execute(sql)
    assert_usable(db)


def test_select_star_on_movie_reviews_explains_how_to_proceed(db: SafeDatabase) -> None:
    with pytest.raises(QueryRejectedError) as error:
        db.execute("SELECT * FROM movie_reviews")
    assert "movie_reviews.name" in error.value.message
    assert error.value.hint is not None
    assert "SELECT *" in error.value.hint
    safe = db.execute("SELECT id, rating, text FROM movie_reviews ORDER BY id")
    assert safe.rows == ((1, 9.0, "Ótimo."), (2, 8.0, "Bom."))


def _function_exists(sql: str) -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.execute(sql)
    except sqlite3.OperationalError as exc:
        return "no such function" not in str(exc)
    finally:
        con.close()
    return True


@pytest.mark.parametrize("sql", FORBIDDEN_FUNCTIONS)
def test_forbidden_functions_never_run(db: SafeDatabase, sql: str) -> None:
    expected = QueryRejectedError if _function_exists(sql) else QueryFailedError
    with pytest.raises(expected):
        db.execute(sql)
    assert_usable(db)


@pytest.mark.parametrize("sql", CURRENT_FORMS)
def test_current_date_time_and_timestamp_are_denied(db: SafeDatabase, sql: str) -> None:
    with pytest.raises(QueryRejectedError, match="current_(date|time|timestamp)"):
        db.execute(sql)
    assert_usable(db)


@pytest.mark.parametrize("sql", RECURSIVE)
def test_recursive_ctes_are_denied_immediately(db: SafeDatabase, sql: str) -> None:
    started = time.monotonic()
    with pytest.raises(QueryRejectedError, match="recursiv"):
        db.execute(sql)
    assert time.monotonic() - started < 1.0  # negado na preparação, não por prazo
    assert_usable(db)


def test_allowed_function_list_has_no_clock_or_dangerous_names() -> None:
    assert not [name for name in ALLOWED_FUNCTIONS if name.startswith("current_")]
    assert all(name == name.lower() for name in ALLOWED_FUNCTIONS)
    dangerous = {
        "load_extension", "random", "randomblob", "zeroblob", "hex", "quote", "printf",
        "format", "char", "unicode", "json", "json_extract", "likely", "unlikely",
        "sqlite_version", "sqlite_source_id", "readfile", "writefile", "changes",
        "total_changes", "last_insert_rowid", "sqlite_offset", "sqlite_compileoption_used",
    }  # fmt: skip
    assert not ALLOWED_FUNCTIONS & dangerous


def test_known_gap_now_based_date_functions_are_allowed_and_deferred_to_m2_m3(
    db: SafeDatabase,
) -> None:
    """Caracterização: o authorizer vê o NOME da função, não o argumento.

    `date('now')`, `date()` e formas montadas em tempo de execução passam. A regra "janela móvel
    pela data de referência, nunca pelo relógio" fica para as instruções (M2) e a avaliação (M3).
    Se isto mudar, que seja de propósito: um filtro textual por 'now' seria contornável.
    """
    for sql in (
        "SELECT date('now')",
        "SELECT datetime('now')",
        "SELECT strftime('%Y', 'now')",
        "SELECT date()",
        "SELECT datetime()",
        "SELECT date('n' || 'ow')",
    ):
        assert len(db.execute(sql).rows) == 1


def test_an_unknown_function_is_a_query_error_not_a_policy_error(db: SafeDatabase) -> None:
    with pytest.raises(QueryFailedError, match="no such function|função"):
        db.execute("SELECT funcao_inventada(1)")


def test_an_internal_failure_in_the_authorizer_denies_instead_of_allowing(
    db: SafeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("falha inesperada")

    with monkeypatch.context() as patch:
        patch.setattr(db_module, "_denial_for", broken)
        with pytest.raises(QueryRejectedError, match="Falha interna"):
            db.execute("SELECT count(*) FROM dim_genres")
    assert_usable(db)


def test_reads_outside_the_main_schema_are_denied() -> None:
    """Defesa em profundidade sem vetor por SQL (sem TEMP nem ATTACH): testada direto."""
    read = sqlite3.SQLITE_READ
    assert db_module._denial_for(read, "dim_genres", "nome_genero", "main") is None
    assert db_module._denial_for(read, "dim_genres", "", None) is None  # count(*)
    denial = db_module._denial_for(read, "dim_genres", "nome_genero", "temp")
    assert denial is not None
    assert "principal" in denial.message


def test_denial_state_does_not_leak_into_the_next_call(db: SafeDatabase) -> None:
    with pytest.raises(QueryRejectedError):
        db.execute("SELECT * FROM alembic_version")
    with pytest.raises(QueryFailedError):
        db.execute("SELECT FROM")  # erro de sintaxe, sem relação com a negação anterior
    assert_usable(db)


# --- independência das camadas --------------------------------------------------------------------


def _ro(path: Path) -> sqlite3.Connection:
    return db_module._open_readonly(path)


@pytest.mark.parametrize("sql", WRITES + ["DROP TABLE dim_genres", "CREATE TABLE t (x)"])
def test_read_only_open_alone_blocks_writes_to_the_main_file(writable_copy: Path, sql: str) -> None:
    before = hashlib.sha256(writable_copy.read_bytes()).hexdigest()
    con = _ro(writable_copy)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute(sql)
    finally:
        con.close()
    assert hashlib.sha256(writable_copy.read_bytes()).hexdigest() == before


def test_control_a_writable_connection_does_change_the_file(writable_copy: Path) -> None:
    con = sqlite3.connect(writable_copy, isolation_level=None)
    con.execute("DELETE FROM dim_genres")
    assert con.execute("SELECT count(*) FROM dim_genres").fetchone() == (0,)
    con.close()


def test_read_only_open_alone_does_not_stop_attach_which_is_why_the_limit_exists(
    writable_copy: Path, tmp_path: Path
) -> None:
    con = _ro(writable_copy)
    try:
        con.execute("ATTACH DATABASE 'file:hole.db?mode=rwc' AS x")
        con.execute("CREATE TABLE x.t (a)")
    finally:
        con.close()
    assert (tmp_path / "hole.db").exists()  # controle: sem o limite, um arquivo é criado


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE 'file:blocked.db?mode=rwc' AS x",
        "ATTACH DATABASE 'blocked_plain.db' AS x",
        "ATTACH DATABASE ':memory:' AS x",
        "VACUUM INTO 'blocked_vacuum.db'",
    ],
)
def test_attached_limit_alone_closes_attach_and_vacuum_into(
    writable_copy: Path, tmp_path: Path, sql: str
) -> None:
    con = _ro(writable_copy)
    con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    try:
        with pytest.raises(sqlite3.Error):
            con.execute(sql)
    finally:
        con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.db"]


def test_control_vacuum_into_creates_a_file_without_the_limit(
    writable_copy: Path, tmp_path: Path
) -> None:
    con = _ro(writable_copy)
    con.execute("VACUUM INTO 'ctrl_vacuum.db'")
    con.close()
    assert (tmp_path / "ctrl_vacuum.db").exists()


@pytest.mark.parametrize("sql", BASE_ATTACKS)
def test_authorizer_alone_blocks_every_attack_on_a_writable_database(
    writable_copy: Path, sql: str
) -> None:
    before = hashlib.sha256(writable_copy.read_bytes()).hexdigest()
    con = sqlite3.connect(writable_copy, isolation_level=None)
    authorizer = db_module._Authorizer()
    con.set_authorizer(authorizer)
    try:
        with pytest.raises(sqlite3.Error):
            con.execute(sql)
    finally:
        con.close()
    assert authorizer.denials, "o bloqueio deveria ter vindo do authorizer"
    assert hashlib.sha256(writable_copy.read_bytes()).hexdigest() == before


def test_query_only_alone_blocks_inserts_and_the_authorizer_blocks_turning_it_off(
    writable_copy: Path, db: SafeDatabase
) -> None:
    con = sqlite3.connect(writable_copy, isolation_level=None)
    con.execute("PRAGMA query_only = ON")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("INSERT INTO dim_genres VALUES ('x', 'y')")
    con.execute("PRAGMA query_only = OFF")  # sozinho, query_only pode ser desligado
    con.execute("INSERT INTO dim_genres VALUES ('x', 'y')")
    con.close()
    with pytest.raises(QueryRejectedError):
        db.execute("PRAGMA query_only = OFF")


def test_hardening_alone_applies_every_protection_to_a_bare_connection(
    writable_copy: Path,
) -> None:
    con = _ro(writable_copy)
    db_module._apply_hardening(con)
    assert con.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
    with pytest.raises(sqlite3.Error):
        con.execute("VACUUM INTO 'hardened_vacuum.db'")
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML) is False
    con.close()


# --- configuração aplicada ------------------------------------------------------------------------


def test_connection_configuration_is_applied(db: SafeDatabase) -> None:
    con = db._con
    assert con is not None
    con.set_authorizer(None)  # só para ler os PRAGMAs; o authorizer bloquearia a leitura
    assert con.execute("PRAGMA cache_size").fetchone() == (-65536,)
    assert con.execute("PRAGMA query_only").fetchone() == (1,)
    expected_limits = {
        sqlite3.SQLITE_LIMIT_ATTACHED: 0,
        sqlite3.SQLITE_LIMIT_LENGTH: 1_000_000,
        sqlite3.SQLITE_LIMIT_SQL_LENGTH: 20_000,
        sqlite3.SQLITE_LIMIT_COLUMN: 100,
        sqlite3.SQLITE_LIMIT_EXPR_DEPTH: 200,
        sqlite3.SQLITE_LIMIT_COMPOUND_SELECT: 20,
        sqlite3.SQLITE_LIMIT_FUNCTION_ARG: 32,
        sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH: 1_000,
    }
    assert {key: con.getlimit(key) for key in expected_limits} == expected_limits
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML) is False
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL) is False
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE) is True
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA) is False
    assert con.getconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + ", ".join(["1"] * 101),
        "SELECT " + "1+(" * 201 + "1" + ")" * 201,
        " UNION ".join(["SELECT 1"] * 22),
        "SELECT abs(" + ", ".join(["1"] * 33) + ")",
        "SELECT 'a' LIKE '" + "a" * 1_001 + "'",
    ],
)
def test_sqlite_limits_turn_into_controlled_errors(db: SafeDatabase, sql: str) -> None:
    with pytest.raises(SafeDatabaseError):
        db.execute(sql)
    assert_usable(db)


def test_a_legitimately_large_value_still_works(db: SafeDatabase) -> None:
    result = db.execute("SELECT length(sinopse) FROM dim_movies WHERE sk_movie_id = 'm8'")
    assert result.rows == ((5000,),)


def test_a_value_over_the_length_limit_is_a_controlled_error(db: SafeDatabase) -> None:
    grow = "replace(%s, 'x', 'xxxxxxxxxx')"  # cada passo multiplica o tamanho por 10
    expression = "sinopse"
    for _ in range(3):  # 5 000 -> 50 000 -> 500 000 -> 5 000 000 de caracteres (> 1 000 000)
        expression = grow % expression
    with pytest.raises(QueryFailedError, match="tamanho máximo"):
        db.execute("SELECT length(" + expression + ") FROM dim_movies WHERE sk_movie_id = 'm8'")  # noqa: S608 (expressão feita só de literais)
    assert_usable(db)


# --- endurecimento obrigatório: falha fechada -----------------------------------------------------


def fake_driver(**overrides: object) -> SimpleNamespace:
    attributes = {
        name: getattr(sqlite3, name) for name in dir(sqlite3) if name.startswith("SQLITE_")
    }
    attributes["sqlite_version"] = sqlite3.sqlite_version
    attributes["sqlite_version_info"] = sqlite3.sqlite_version_info
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


class IgnoresConfig(sqlite3.Connection):
    def setconfig(self, op: int, enable: bool = True, /) -> None:  # a build "ignora" em silêncio
        return None


class RaisesValueError(sqlite3.Connection):
    def setconfig(self, op: int, enable: bool = True, /) -> None:
        raise ValueError("unknown config 'op'")


class RaisesProgrammingError(sqlite3.Connection):
    def setconfig(self, op: int, enable: bool = True, /) -> None:
        raise sqlite3.ProgrammingError("not supported")


class LacksSetconfig(sqlite3.Connection):
    def setconfig(self, op: int, enable: bool = True, /) -> None:
        raise AttributeError("setconfig")  # como no Python 3.11, que não tem a API


class IgnoresLimits(sqlite3.Connection):
    def setlimit(self, category: int, limit: int, /) -> int:
        return 0


class IgnoresQueryOnly(sqlite3.Connection):
    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
        if sql.startswith("PRAGMA query_only = ON"):
            return super().execute("SELECT 1")
        return super().execute(sql, *args)


def memory_connection(factory: type[sqlite3.Connection] = sqlite3.Connection) -> sqlite3.Connection:
    return sqlite3.connect(":memory:", factory=factory)


def test_hardening_passes_on_a_normal_build() -> None:
    db_module._apply_hardening(memory_connection())


@pytest.mark.parametrize(
    ("factory", "missing"),
    [
        (IgnoresConfig, "SQLITE_DBCONFIG_DQS_DML"),
        (IgnoresConfig, "SQLITE_DBCONFIG_DEFENSIVE"),
        (RaisesValueError, "SQLITE_DBCONFIG_TRUSTED_SCHEMA"),
        (RaisesProgrammingError, "SQLITE_DBCONFIG_DQS_DDL"),
        (LacksSetconfig, "SQLITE_DBCONFIG_DQS_DML"),
        (IgnoresLimits, "SQLITE_LIMIT_ATTACHED"),
        (IgnoresQueryOnly, "query_only"),
    ],
)
def test_hardening_fails_closed_when_a_protection_cannot_be_verified(
    factory: type[sqlite3.Connection], missing: str
) -> None:
    with pytest.raises(DatabaseUnavailableError) as error:
        db_module._apply_hardening(memory_connection(factory))
    assert missing in error.value.message
    assert "Python 3.12" in (error.value.hint or "")
    assert error.value.recoverable is False


@pytest.mark.parametrize(
    "constant",
    [
        "SQLITE_DBCONFIG_DQS_DML",
        "SQLITE_DBCONFIG_DQS_DDL",
        "SQLITE_DBCONFIG_DEFENSIVE",
        "SQLITE_DBCONFIG_TRUSTED_SCHEMA",
        "SQLITE_LIMIT_ATTACHED",
    ],
)
def test_hardening_fails_closed_when_the_build_lacks_a_constant(constant: str) -> None:
    driver = fake_driver()
    delattr(driver, constant)
    with pytest.raises(DatabaseUnavailableError, match=constant):
        db_module._apply_hardening(memory_connection(), driver)


def test_hardening_fails_closed_on_an_old_sqlite() -> None:
    driver = fake_driver(sqlite_version="3.30.1", sqlite_version_info=(3, 30, 1))
    with pytest.raises(DatabaseUnavailableError, match=r"3\.30\.1.*3\.31\.0"):
        db_module._apply_hardening(memory_connection(), driver)


def test_hardening_lists_every_missing_protection_at_once() -> None:
    driver = fake_driver()
    delattr(driver, "SQLITE_DBCONFIG_DEFENSIVE")
    delattr(driver, "SQLITE_DBCONFIG_DQS_DML")
    with pytest.raises(DatabaseUnavailableError) as error:
        db_module._apply_hardening(memory_connection(), driver)
    assert "DEFENSIVE" in error.value.message
    assert "DQS_DML" in error.value.message


def test_a_failed_hardening_closes_the_connection_and_never_hands_out_a_database(
    gold_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    real_open = db_module._open_readonly

    def spy(path: Path) -> sqlite3.Connection:
        con = real_open(path)
        opened.append(con)
        return con

    monkeypatch.setattr(db_module, "_open_readonly", spy)
    monkeypatch.setattr(db_module, "_REQUIRED_DBCONFIG", (("SQLITE_DBCONFIG_NAO_EXISTE", True),))
    with pytest.raises(DatabaseUnavailableError, match="NAO_EXISTE"):
        SafeDatabase(gold_db_path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


class FailsToInstallAuthorizer(sqlite3.Connection):
    def set_authorizer(self, authorizer_callback: object) -> None:  # type: ignore[override]
        raise sqlite3.OperationalError("falha ao instalar o authorizer")


class ExplodesInstallingAuthorizer(sqlite3.Connection):
    def set_authorizer(self, authorizer_callback: object) -> None:  # type: ignore[override]
        raise RuntimeError("falha inesperada ao instalar o authorizer")


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (FailsToInstallAuthorizer, DatabaseUnavailableError),
        (ExplodesInstallingAuthorizer, RuntimeError),
    ],
)
def test_a_failure_installing_the_authorizer_closes_the_connection_and_hands_out_nothing(
    gold_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: type[sqlite3.Connection],
    expected: type[Exception],
) -> None:
    opened: list[sqlite3.Connection] = []

    def opener(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            factory=factory,
        )
        opened.append(con)
        return con

    monkeypatch.setattr(db_module, "_open_readonly", opener)
    with pytest.raises(expected):
        SafeDatabase(gold_db_path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_a_failure_installing_the_authorizer_does_not_leak_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = build_gold_db(tmp_path / "gold.db")

    class LeaksThePath(sqlite3.Connection):
        def set_authorizer(self, authorizer_callback: object) -> None:  # type: ignore[override]
            raise sqlite3.OperationalError(f"falha ao ler {target}")

    def opener(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, factory=LeaksThePath)

    monkeypatch.setattr(db_module, "_open_readonly", opener)
    with pytest.raises(DatabaseUnavailableError) as error:
        SafeDatabase(target)
    assert_no_path(error.value, tmp_path, target)


def test_a_too_old_sqlite_stops_the_constructor(
    gold_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_module, "MIN_SQLITE_VERSION", (99, 0, 0))
    with pytest.raises(DatabaseUnavailableError, match="anterior ao mínimo 99.0.0"):
        SafeDatabase(gold_db_path)


def test_python_floor_in_pyproject_covers_the_hardening_api() -> None:
    """`Connection.setconfig` e as constantes DBCONFIG existem a partir do Python 3.12."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requires = config["project"]["requires-python"]
    assert requires.replace(" ", "") == ">=3.12"
    assert config["tool"]["ruff"]["target-version"] == "py312"


# --- ciclo de vida e esquema ----------------------------------------------------------------------


def test_missing_file_is_reported_and_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "nao-existe.db"
    with pytest.raises(DatabaseUnavailableError, match="não encontrado") as error:
        SafeDatabase(missing)
    assert not missing.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert error.value.recoverable is False
    assert "doctor" in (error.value.hint or "")
    assert "'nao-existe.db'" in error.value.message  # só o nome do arquivo
    assert_no_path(error.value, tmp_path, missing)


def test_a_directory_in_place_of_the_file(tmp_path: Path) -> None:
    folder = tmp_path / "banco.db"
    folder.mkdir()
    with pytest.raises(DatabaseUnavailableError, match="não é um arquivo") as error:
        SafeDatabase(folder)
    assert "'banco.db'" in error.value.message
    assert_no_path(error.value, tmp_path, folder)


def test_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "vazio.db"
    empty.write_bytes(b"")
    with pytest.raises(DatabaseUnavailableError, match="vazio") as error:
        SafeDatabase(empty)
    assert empty.read_bytes() == b""
    assert "'vazio.db'" in error.value.message
    assert_no_path(error.value, tmp_path, empty)


def test_an_access_error_does_not_leak_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pasta secreta" / "banco.db"
    target.parent.mkdir()
    target.write_bytes(b"x")

    def deny(self: Path, *args: object, **kwargs: object) -> bool:
        raise PermissionError(13, "Permission denied", str(self))  # o OSError real traz o caminho

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", deny)
        with pytest.raises(DatabaseUnavailableError) as error:
            SafeDatabase(target)
    assert "'banco.db'" in error.value.message
    assert "Permission denied" in error.value.message
    assert error.value.__cause__ is None  # a causa original também carregaria o caminho
    assert_no_path(error.value, tmp_path, target.parent, target)


@pytest.mark.parametrize("reason_has_path", [True, False])
def test_an_open_failure_does_not_leak_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason_has_path: bool
) -> None:
    target = build_gold_db(tmp_path / "gold.db")

    def refuse(*args: object, **kwargs: object) -> sqlite3.Connection:
        detail = f": {target}" if reason_has_path else ""
        raise sqlite3.OperationalError(f"unable to open database file{detail}")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", refuse)
        with pytest.raises(DatabaseUnavailableError, match="abrir o banco") as error:
            SafeDatabase(target)
    assert "'gold.db'" in error.value.message
    assert_no_path(error.value, tmp_path, target)


@pytest.mark.parametrize(
    "content",
    [b"<html>erro de download</html>", b"x" * 5000, b"SQLite format 3\x00" + b"\xff" * 100],
)
def test_a_file_that_is_not_a_valid_sqlite_database(tmp_path: Path, content: bytes) -> None:
    junk = tmp_path / "lixo.db"
    junk.write_bytes(content)
    with pytest.raises(DatabaseUnavailableError) as error:
        SafeDatabase(junk)
    assert junk.read_bytes() == content
    assert_no_path(error.value, tmp_path, junk)


def test_a_database_without_the_gold_tables(tmp_path: Path) -> None:
    other = tmp_path / "outro.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE alguma_coisa (x)")
    con.commit()
    con.close()
    with pytest.raises(
        DatabaseUnavailableError, match="tabelas ausentes: bridge_movie_company"
    ) as error:
        SafeDatabase(other)
    assert_no_path(error.value, tmp_path, other)


def test_a_database_missing_a_column(tmp_path: Path) -> None:
    broken = build_gold_db(tmp_path / "quebrado.db")
    con = sqlite3.connect(broken)
    con.execute("ALTER TABLE dim_movies RENAME COLUMN titulo TO titulo_antigo")
    con.commit()
    con.close()
    with pytest.raises(
        DatabaseUnavailableError, match=r"colunas ausentes: dim_movies\.titulo"
    ) as error:
        SafeDatabase(broken)
    assert_no_path(error.value, tmp_path, broken)


def test_extra_tables_and_columns_in_the_file_are_ignored_and_stay_hidden(tmp_path: Path) -> None:
    extended = build_gold_db(tmp_path / "estendido.db")
    con = sqlite3.connect(extended)
    con.execute("CREATE TABLE tabela_extra (segredo TEXT)")
    con.execute("INSERT INTO tabela_extra VALUES ('s')")
    con.execute("ALTER TABLE dim_movies ADD COLUMN coluna_extra TEXT")
    con.commit()
    con.close()
    with SafeDatabase(extended) as database:
        assert database.execute("SELECT count(*) FROM dim_movies").rows == ((8,),)
        with pytest.raises(QueryRejectedError, match="tabela_extra"):
            database.execute("SELECT * FROM tabela_extra")
        with pytest.raises(QueryRejectedError, match="coluna_extra"):
            database.execute("SELECT coluna_extra FROM dim_movies")


def test_path_with_spaces_hash_percent_and_accents(tmp_path: Path) -> None:
    folder = tmp_path / "p 1 #x%y ç"
    folder.mkdir()
    target = build_gold_db(folder / "banco (1) #2 %41 ç.db")
    with SafeDatabase(target) as database:
        assert database.execute("SELECT count(*) FROM dim_genres").rows == ((4,),)


def test_relative_paths_are_resolved_from_the_current_directory(
    gold_db_path: Path, tmp_path: Path
) -> None:
    shutil.copy(gold_db_path, tmp_path / "relativo.db")
    with SafeDatabase("relativo.db") as database:
        assert database.execute("SELECT count(*) FROM dim_genres").rows == ((4,),)


def test_a_wal_database_is_read_without_touching_the_main_file(tmp_path: Path) -> None:
    wal_db = build_gold_db(tmp_path / "wal.db", wal=True)
    before = hashlib.sha256(wal_db.read_bytes()).hexdigest()
    with SafeDatabase(wal_db) as database:
        assert database.execute("SELECT count(*) FROM dim_movies").rows == ((8,),)
        with pytest.raises(SafeDatabaseError):
            database.execute("DELETE FROM dim_genres")
    assert hashlib.sha256(wal_db.read_bytes()).hexdigest() == before


def test_close_is_idempotent_and_execute_after_close_is_a_controlled_error(
    gold_db_path: Path,
) -> None:
    database = SafeDatabase(gold_db_path)
    database.close()
    database.close()
    with pytest.raises(DatabaseUnavailableError, match="fechada"):
        database.execute("SELECT 1")


def test_context_manager_closes_the_connection(gold_db_path: Path) -> None:
    with SafeDatabase(gold_db_path) as database:
        assert_usable(database)
    with pytest.raises(DatabaseUnavailableError, match="fechada"):
        database.execute("SELECT 1")


def test_from_settings_uses_the_configured_values(gold_db_path: Path) -> None:
    settings = load_settings(
        {
            "CINEDATA_DB_PATH": str(gold_db_path),
            "CINEDATA_MAX_ROWS": "7",
            "CINEDATA_SQL_TIMEOUT_S": "12.5",
        }
    )
    with SafeDatabase.from_settings(settings) as database:
        assert database.path.samefile(gold_db_path)
        assert database.max_rows == 7
        assert database.timeout_s == 12.5
        assert_usable(database)


@pytest.mark.parametrize("max_rows", [0, -1, True, 1.5, "5", None])
def test_invalid_max_rows(gold_db_path: Path, max_rows: object) -> None:
    with pytest.raises(ValueError, match="max_rows"):
        SafeDatabase(gold_db_path, max_rows=max_rows)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "timeout",
    [
        "5",
        None,
        object(),
        [5],
        b"5",
        5 + 0j,
        True,
        False,
        0,
        0.0,
        -1,
        -0.5,
        3601,
        3600.0001,
        10**400,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_invalid_timeout_is_always_a_clear_value_error(gold_db_path: Path, timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout_s"):  # nunca um TypeError bruto
        SafeDatabase(gold_db_path, timeout_s=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0.05, 1, 5, 3600, 3600.0])
def test_valid_timeouts_are_accepted(gold_db_path: Path, timeout: float) -> None:
    with SafeDatabase(gold_db_path, timeout_s=timeout) as database:
        assert database.timeout_s == float(timeout)
        assert_usable(database)


def test_timeout_errors_say_whether_the_type_or_the_range_is_wrong(gold_db_path: Path) -> None:
    for bad in ("5", None, object(), True):
        with pytest.raises(ValueError, match="número"):
            SafeDatabase(gold_db_path, timeout_s=bad)  # type: ignore[arg-type]
    for bad in (0, -1, 3601, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finito.*entre 0"):
            SafeDatabase(gold_db_path, timeout_s=bad)


@pytest.mark.parametrize("cells", [0, 9, True, 2.5])
def test_invalid_cell_limit(gold_db_path: Path, cells: object) -> None:
    with pytest.raises(ValueError, match="max_cell_chars"):
        SafeDatabase(gold_db_path, max_cell_chars=cells)  # type: ignore[arg-type]


# --- constantes -----------------------------------------------------------------------------------


def test_gold_tables_are_the_ten_gold_tables_without_the_hidden_ones() -> None:
    assert len(GOLD_TABLES) == 10
    assert "alembic_version" not in GOLD_TABLES
    assert not any(name.startswith("sqlite_") for name in GOLD_TABLES)
    assert "name" not in GOLD_TABLES["movie_reviews"]
    with pytest.raises(TypeError):
        GOLD_TABLES["nova"] = ("x",)  # type: ignore[index]


def test_gold_tables_match_the_synthetic_schema(gold_db_path: Path) -> None:
    con = sqlite3.connect(gold_db_path)
    for table, columns in GOLD_TABLES.items():
        actual = [row[1] for row in con.execute("SELECT * FROM pragma_table_info(?)", (table,))]
        extra = set(actual) - set(columns)
        assert set(columns) <= set(actual), table
        assert extra == ({"name"} if table == "movie_reviews" else set()), table
    con.close()


# --- mensagens ------------------------------------------------------------------------------------


def test_unknown_table_suggests_the_closest_names(db: SafeDatabase) -> None:
    with pytest.raises(QueryFailedError) as error:
        db.execute("SELECT * FROM dim_movie")
    assert "'dim_movie'" in error.value.message
    assert "dim_movies" in (error.value.hint or "")
    assert "Tabelas disponíveis" in (error.value.hint or "")


def test_unknown_column_suggests_the_closest_names_and_shows_the_model(db: SafeDatabase) -> None:
    with pytest.raises(QueryFailedError) as error:
        db.execute("SELECT titul FROM dim_movies")
    assert "titulo" in (error.value.hint or "")
    assert "dim_movies(" in (error.value.hint or "")


def test_double_quoted_text_gets_the_single_quote_hint(db: SafeDatabase) -> None:
    for sql in (
        'SELECT count(*) FROM dim_genres WHERE nome_genero = "Drama"',
        'SELECT "coluna_errada" FROM dim_genres',
    ):
        with pytest.raises(QueryFailedError, match="aspas simples"):
            db.execute(sql)
    assert db.execute("SELECT count(*) FROM dim_genres WHERE nome_genero = 'Drama'").rows == ((1,),)


def test_ambiguous_column_hint(db: SafeDatabase) -> None:
    with pytest.raises(QueryFailedError, match="ambígua") as error:
        db.execute("SELECT sk_movie_id FROM dim_movies, fact_movies_performance")
    assert "alias" in (error.value.hint or "")


def test_syntax_error_is_a_query_error(db: SafeDatabase) -> None:
    with pytest.raises(QueryFailedError, match="syntax error"):
        db.execute("SELECT FROM WHERE")


def test_error_messages_do_not_leak_the_database_path(db: SafeDatabase, gold_db_path: Path) -> None:
    attempts = [
        "SELECT FROM",
        "SELECT * FROM nada",
        "SELECT x FROM dim_movies",
        "DELETE FROM dim_genres",
    ]
    for sql in attempts:
        with pytest.raises(SafeDatabaseError) as error:
            db.execute(sql)
        assert str(gold_db_path.parent) not in str(error.value)
        assert "Traceback" not in str(error.value)


def test_errors_expose_message_hint_and_recoverability(db: SafeDatabase) -> None:
    with pytest.raises(QueryRejectedError) as error:
        db.execute("DELETE FROM dim_genres")
    assert error.value.recoverable is True
    assert str(error.value) == f"{error.value.message} {error.value.hint}"
    assert str(DatabaseUnavailableError("só mensagem")) == "só mensagem"


# --- tempo, Ctrl+C e threads ----------------------------------------------------------------------

HEAVY_SQL = (
    "SELECT count(*) FROM dim_movies a, dim_movies b, dim_movies c"
    " WHERE a.duracao_minutos + b.duracao_minutos + c.duracao_minutos < 0"
)


def test_timeout_interrupts_a_runaway_query_and_the_connection_survives(
    heavy_db_path: Path,
) -> None:
    with SafeDatabase(heavy_db_path, timeout_s=0.5) as database:
        started = time.monotonic()
        with pytest.raises(QueryTimeoutError) as error:
            database.execute(HEAVY_SQL)
        elapsed = time.monotonic() - started
        assert 0.4 <= elapsed < 3.0
        assert "0.5 s" in error.value.message
        assert "N:N" in (error.value.hint or "")
        assert error.value.recoverable is True
        assert_usable(database)
        assert database.execute("SELECT count(*) FROM dim_movies").rows == ((3008,),)
        with pytest.raises(QueryTimeoutError):  # o prazo vale por chamada
            database.execute(HEAVY_SQL)


def test_a_fast_query_after_a_timeout_is_not_poisoned_by_the_flag(heavy_db_path: Path) -> None:
    with SafeDatabase(heavy_db_path, timeout_s=0.3) as database:
        with pytest.raises(QueryTimeoutError):
            database.execute(HEAVY_SQL)
        with pytest.raises(QueryFailedError):  # erro comum, não um novo "timeout"
            database.execute("SELECT FROM")


def test_the_deadline_is_armed_only_after_the_lock_is_acquired(
    heavy_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(db_module, "time", clock)
    database = SafeDatabase(heavy_db_path, timeout_s=5)
    outcome: dict[str, object] = {}
    sql = (
        "SELECT count(*) FROM dim_movies a, dim_movies b"
        " WHERE a.duracao_minutos > b.duracao_minutos + 1000"
    )

    def worker() -> None:
        try:
            outcome["rows"] = database.execute(sql).rows
        except SafeDatabaseError as exc:
            outcome["error"] = exc

    database._lock.acquire()  # simula outra consulta ainda em andamento
    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.2)  # o worker está esperando o lock
    assert thread.is_alive()
    assert outcome == {}  # nada executou enquanto o lock estava ocupado
    clock.now = 1000.0  # a espera "durou" 1000 s, muito mais que o prazo de 5 s
    database._lock.release()
    thread.join(timeout=60)
    database.close()
    assert outcome == {"rows": ((0,),)}


def test_ctrl_c_during_a_query_reraises_keyboard_interrupt(heavy_db_path: Path) -> None:
    """Escopo: `execute` na thread que recebe o KeyboardInterrupt (a principal).

    Cancelar uma consulta que roda em thread de trabalho do agente é validado no M2.
    """
    with SafeDatabase(heavy_db_path, timeout_s=60) as database:
        timer = threading.Timer(0.3, _thread.interrupt_main)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                database.execute(HEAVY_SQL)
        finally:
            timer.cancel()
        assert time.monotonic() - started < 10
        assert database._timed_out is False
        assert_usable(database)


def test_execute_from_a_worker_thread(gold_db_path: Path) -> None:
    database = SafeDatabase(gold_db_path)  # criado na thread principal
    outcome: dict[str, object] = {}

    def worker() -> None:
        outcome["rows"] = database.execute("SELECT count(*) FROM dim_movies").rows

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)
    database.close()
    assert outcome == {"rows": ((8,),)}


def test_concurrent_calls_are_serialized_and_all_correct(gold_db_path: Path) -> None:
    errors: list[BaseException] = []
    results: list[tuple[tuple[object, ...], ...]] = []

    with SafeDatabase(gold_db_path) as database:

        def worker() -> None:
            try:
                for _ in range(25):
                    results.append(database.execute("SELECT count(*) FROM dim_genres").rows)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    assert errors == []
    assert results == [((4,),)] * 100
