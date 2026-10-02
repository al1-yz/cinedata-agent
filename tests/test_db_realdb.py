"""Testes do acesso seguro contra o banco real (pulados quando data/cinerocket.db não existe).

São somente leitura. Só estes: `pytest -m realdb`. Sem eles: `pytest -m "not realdb"`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from cinedata.db import (
    GOLD_TABLES,
    QueryRejectedError,
    QueryTimeoutError,
    SafeDatabase,
)

pytestmark = pytest.mark.realdb

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "cinerocket.db"


@pytest.fixture(scope="module")
def real_db_path() -> Path:
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado (veja data/README.md)")
    return REAL_DB


@pytest.fixture
def real_db(real_db_path: Path) -> Iterator[SafeDatabase]:
    with SafeDatabase(real_db_path, max_rows=50, timeout_s=10.0) as database:
        yield database


def test_gold_tables_match_the_real_schema(real_db_path: Path) -> None:
    con = sqlite3.connect(f"{real_db_path.as_uri()}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables - {"alembic_version"} == set(GOLD_TABLES)
        hidden: set[tuple[str, str]] = set()
        for table, allowed in GOLD_TABLES.items():
            actual = [row[1] for row in con.execute("SELECT * FROM pragma_table_info(?)", (table,))]
            assert set(allowed) <= set(actual), table
            hidden |= {(table, column) for column in actual if column not in allowed}
        assert hidden == {("movie_reviews", "name")}
    finally:
        con.close()


def test_inventory_sanity(real_db: SafeDatabase) -> None:
    assert real_db.execute("SELECT count(*) FROM dim_genres").rows == ((19,),)
    assert real_db.execute("SELECT count(*) FROM dim_movies").rows == ((95645,),)


def test_a_typical_join_with_aggregation(real_db: SafeDatabase) -> None:
    result = real_db.execute(
        "SELECT g.nome_genero, count(*) AS filmes FROM dim_genres g"
        " JOIN bridge_movie_genre b ON b.sk_genre_id = g.sk_genre_id"
        " GROUP BY g.nome_genero ORDER BY filmes DESC, g.nome_genero LIMIT 3"
    )
    assert result.columns == ("nome_genero", "filmes")
    assert len(result.rows) == 3
    assert all(count > 0 for _, count in result.rows)


def test_row_limit_stops_early_on_a_large_table(real_db: SafeDatabase) -> None:
    result = real_db.execute("SELECT sk_movie_id FROM dim_movies")
    assert len(result.rows) == 50
    assert result.truncated is True
    assert result.elapsed_s < 1.0  # não materializa as 95 645 linhas


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM alembic_version",
        "SELECT name FROM sqlite_master",
        "SELECT name FROM movie_reviews",
        "SELECT * FROM movie_reviews",
        "SELECT titulo FROM dim_movies WHERE data_lancamento <= CURRENT_DATE",
    ],
)
def test_hidden_data_is_denied_on_the_real_database(real_db: SafeDatabase, sql: str) -> None:
    with pytest.raises(QueryRejectedError):
        real_db.execute(sql)
    assert real_db.execute("SELECT count(*) FROM dim_genres").rows == ((19,),)


def test_reviews_are_readable_without_the_author_name(real_db: SafeDatabase) -> None:
    result = real_db.execute("SELECT id, rating FROM movie_reviews ORDER BY id LIMIT 3")
    assert len(result.rows) == 3


def test_timeout_interrupts_a_heavy_query_and_the_connection_survives(
    real_db_path: Path,
) -> None:
    sql = (
        "SELECT a.titulo, b.titulo FROM dim_movies a JOIN dim_movies b"
        " ON a.ano_lancamento = b.ano_lancamento ORDER BY a.sinopse, b.sinopse"
    )
    with SafeDatabase(real_db_path, timeout_s=2.0) as database:
        started = time.monotonic()
        with pytest.raises(QueryTimeoutError):
            database.execute(sql)
        assert time.monotonic() - started < 2.0 + 1.0  # excesso medido: até ~0,3 s
        assert database.execute("SELECT count(*) FROM dim_genres").rows == ((19,),)


def test_the_conservative_memory_profile_is_applied(real_db: SafeDatabase) -> None:
    con = real_db._con
    assert con is not None
    con.set_authorizer(None)  # só para ler os PRAGMAs
    assert con.execute("PRAGMA cache_size").fetchone() == (-65536,)
    assert con.execute("PRAGMA query_only").fetchone() == (1,)


def test_opening_and_validating_the_schema_is_fast(real_db_path: Path) -> None:
    started = time.monotonic()
    with SafeDatabase(real_db_path):
        pass
    assert time.monotonic() - started < 2.0


def test_the_database_file_is_never_modified(real_db_path: Path) -> None:
    before = real_db_path.stat()
    with SafeDatabase(real_db_path) as database:
        database.execute("SELECT count(*) FROM dim_movies")
        with pytest.raises(QueryRejectedError):
            database.execute("SELECT * FROM movie_reviews")
    after = real_db_path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
