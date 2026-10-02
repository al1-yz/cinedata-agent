"""Testes do override por chamada de `SafeDatabase.execute(sql, max_rows=..., timeout_s=...)`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cinedata import db as db_module
from cinedata.db import (
    MAX_BULK_ROWS,
    MAX_TIMEOUT_S,
    QueryRejectedError,
    QueryTimeoutError,
    SafeDatabase,
)
from gold_db import build_gold_db


@pytest.fixture(scope="module")
def big_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_gold_db(tmp_path_factory.mktemp("bulk") / "gold.db", extra_movies=3000)


@pytest.fixture
def db(big_db_path: Path) -> Iterator[SafeDatabase]:
    with SafeDatabase(big_db_path, max_rows=50, timeout_s=30.0) as database:
        yield database


ALL_MOVIES = "SELECT sk_movie_id FROM dim_movies"


class FakeClock:
    """Relógio determinístico: cada leitura avança `step` segundos."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def test_the_default_cap_is_still_the_instance_cap(db: SafeDatabase) -> None:
    result = db.execute(ALL_MOVIES)
    assert (len(result.rows), result.truncated, result.max_rows) == (50, True, 50)


def test_a_larger_override_returns_more_rows_and_reports_the_effective_cap(
    db: SafeDatabase,
) -> None:
    result = db.execute(ALL_MOVIES, max_rows=5_000)
    assert (len(result.rows), result.truncated, result.max_rows) == (3_062, False, 5_000)


def test_a_smaller_override_also_applies(db: SafeDatabase) -> None:
    result = db.execute(ALL_MOVIES, max_rows=2)
    assert (len(result.rows), result.truncated, result.max_rows) == (2, True, 2)


def test_truncated_follows_the_override_and_the_exact_boundary(db: SafeDatabase) -> None:
    assert db.execute(ALL_MOVIES, max_rows=3_062).truncated is False
    assert db.execute(ALL_MOVIES, max_rows=3_061).truncated is True


def test_the_override_does_not_leak_into_the_next_call_or_the_instance(db: SafeDatabase) -> None:
    db.execute(ALL_MOVIES, max_rows=5_000, timeout_s=5)
    assert db.max_rows == 50
    assert db.timeout_s == 30.0
    after = db.execute(ALL_MOVIES)
    assert (len(after.rows), after.max_rows) == (50, 50)


def test_none_means_the_instance_default(db: SafeDatabase) -> None:
    result = db.execute(ALL_MOVIES, max_rows=None, timeout_s=None)
    assert result.max_rows == 50


@pytest.mark.parametrize("bad", [0, -1, True, False, "5", 1.5, [3], MAX_BULK_ROWS + 1, 10**400])
def test_invalid_row_overrides_are_clear_value_errors(db: SafeDatabase, bad: object) -> None:
    with pytest.raises(ValueError, match="max_rows"):
        db.execute("SELECT 1", max_rows=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    ["5", [5], True, False, 0, -1, MAX_TIMEOUT_S + 1, 10**400, float("nan"), float("inf")],
)
def test_invalid_timeout_overrides_are_clear_value_errors(db: SafeDatabase, bad: object) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        db.execute("SELECT 1", timeout_s=bad)  # type: ignore[arg-type]


def test_the_ceiling_itself_is_accepted(db: SafeDatabase) -> None:
    assert db.execute("SELECT 1", max_rows=MAX_BULK_ROWS).max_rows == MAX_BULK_ROWS
    assert db.execute("SELECT 1", timeout_s=MAX_TIMEOUT_S).rows == ((1,),)
    assert MAX_BULK_ROWS == 1_000_000


ATTACKS = [
    "INSERT INTO dim_genres VALUES ('x', 'y')",
    "DELETE FROM dim_genres",
    "ATTACH DATABASE ':memory:' AS x",
    "VACUUM INTO 'bulk_vacuum.db'",
    "PRAGMA query_only = OFF",
    "SELECT * FROM alembic_version",
    "SELECT name FROM movie_reviews",
    "SELECT * FROM sqlite_master",
    "SELECT CURRENT_DATE",
    "SELECT random()",
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c",
    "SELECT 1; DROP TABLE dim_genres",
]


@pytest.mark.parametrize("sql", ATTACKS)
def test_every_other_layer_still_applies_with_the_override(
    db: SafeDatabase, sql: str, tmp_path: Path
) -> None:
    with pytest.raises(QueryRejectedError):
        db.execute(sql, max_rows=MAX_BULK_ROWS, timeout_s=MAX_TIMEOUT_S)
    assert db.execute("SELECT count(*) FROM dim_genres").rows == ((19,),)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_long_cells_are_still_cut_with_the_override(big_db_path: Path) -> None:
    with SafeDatabase(big_db_path, max_cell_chars=10) as database:
        result = database.execute(
            "SELECT titulo FROM dim_movies WHERE id_filme = '2001'", max_rows=9
        )
    assert result.truncated_cells == 1
    assert result.rows[0][0].endswith("caracteres]")  # type: ignore[union-attr]


COUNTING_SQL = (
    "SELECT count(*) FROM dim_movies a, dim_movies b"
    " WHERE b.ano_lancamento = 1990 AND a.duracao_minutos > b.duracao_minutos + 1000"
)


def test_a_larger_timeout_override_lets_a_slow_query_finish(
    big_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_module, "time", FakeClock(step=0.001))
    with SafeDatabase(big_db_path, timeout_s=0.3) as database:
        with pytest.raises(QueryTimeoutError):  # o prazo da instância estoura...
            database.execute(COUNTING_SQL)
        assert database.execute(COUNTING_SQL, timeout_s=60).rows == ((0,),)  # ...o da chamada não


def test_a_smaller_timeout_override_wins_and_the_message_reports_the_effective_value(
    big_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_module, "time", FakeClock(step=0.001))
    with SafeDatabase(big_db_path, timeout_s=30) as database:
        assert database.execute(COUNTING_SQL).rows == ((0,),)  # com 30 s termina
        with pytest.raises(QueryTimeoutError) as error:
            database.execute(COUNTING_SQL, timeout_s=0.25)
        assert "0.25 s" in error.value.message
        assert "30 s" not in error.value.message
        assert database.execute(COUNTING_SQL).rows == ((0,),)  # o override não ficou gravado
    with SafeDatabase(big_db_path, timeout_s=0.5) as short:
        with pytest.raises(QueryTimeoutError) as again:  # sem override, vale o da instância
            short.execute(COUNTING_SQL)
        assert "0.5 s" in again.value.message
