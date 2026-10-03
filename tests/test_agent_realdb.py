"""Agente contra o banco real, com modelo roteirizado (sem rede; pulados sem data/cinerocket.db).

Provam a ligação ferramentas -> `EntityIndex`/`SafeDatabase` na Gold real: SQL analítico fora dos
14 exemplos, teto de linhas visto pelo modelo, segurança e ausência de escrita. Os oráculos usam
uma conexão sqlite3 própria, somente leitura, com formulações diferentes das do roteiro.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from cinedata.agent import ask
from cinedata.config import Settings, load_settings
from cinedata.entities import MatchState
from cinedata.runtime import AnswerStatus, SqlStatus
from scripted import Script, final, find, returns, sql

pytestmark = pytest.mark.realdb

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "cinerocket.db"


@pytest.fixture(scope="module")
def real_db_path() -> Path:
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado (veja data/README.md)")
    return REAL_DB


@pytest.fixture(scope="module")
def oracle(real_db_path: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(f"{real_db_path.as_uri()}?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def settings_for(path: Path, **changes: object) -> Settings:
    base = load_settings(env={}, dotenv_path=path.parent / "sem.env")
    return replace(base, db_path=path, reference_date=date(2026, 10, 1), **changes)


def test_entity_lookup_and_sql_cooperate_on_the_real_gold(
    real_db_path: Path, oracle: sqlite3.Connection
) -> None:
    def count_directed(messages):  # noqa: ANN001, ANN202
        [payload] = returns(messages, "find_entities")
        assert payload["state"] == "exact_unique" and payload["resolved"]["role"] == "Diretor"
        key = payload["resolved"]["key"]
        query = f"SELECT COUNT(*) FROM bridge_movie_person WHERE sk_person_id = '{key}'"  # noqa: S608
        return [sql(query)]

    script = Script(
        [find("pessoa", "christopher nolan", "Diretor")],
        count_directed,
        [final("data_answer", "Ok.")],
    )
    outcome = ask(
        "Quantos filmes Christopher Nolan dirigiu?", settings_for(real_db_path), model=script.model
    )

    expected = oracle.execute(
        "SELECT COUNT(DISTINCT b.sk_movie_id) FROM dim_people AS p"
        " JOIN bridge_movie_person AS b ON b.sk_person_id = p.sk_person_id"
        " WHERE p.nome_pessoa = 'Christopher Nolan' AND p.tipo_pessoa = 'Diretor'"
    ).fetchone()[0]
    assert outcome.ok and expected > 0
    assert outcome.trace.entity_lookups[0].state is MatchState.EXACT_UNIQUE
    assert outcome.trace.sql_executions[0].rows == ((expected,),)


def test_a_question_outside_the_official_examples_on_real_data(
    real_db_path: Path, oracle: sqlite3.Connection
) -> None:
    generated = (
        "SELECT g.nome_genero, COUNT(DISTINCT f.sk_movie_id) AS filmes"
        " FROM fact_movies_performance AS f"
        " JOIN bridge_movie_genre AS b ON b.sk_movie_id = f.sk_movie_id"
        " JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id"
        " WHERE f.nota_imdb > 8"
        " GROUP BY g.sk_genre_id, g.nome_genero"
        " ORDER BY filmes DESC, g.nome_genero LIMIT 5"
    )
    script = Script([sql(generated)], [final("data_answer", "Ok.")])
    outcome = ask(
        "Qual gênero tem mais filmes com nota IMDb acima de 8?",
        settings_for(real_db_path),
        model=script.model,
    )

    expected = oracle.execute(
        "SELECT g.nome_genero, (SELECT COUNT(*) FROM bridge_movie_genre AS b"
        "   WHERE b.sk_genre_id = g.sk_genre_id AND EXISTS ("
        "     SELECT 1 FROM fact_movies_performance AS f"
        "     WHERE f.sk_movie_id = b.sk_movie_id AND f.nota_imdb > 8)) AS filmes"
        " FROM dim_genres AS g ORDER BY filmes DESC, g.nome_genero LIMIT 5"
    ).fetchall()
    assert outcome.ok
    assert list(outcome.trace.sql_executions[0].rows) == [tuple(row) for row in expected]


def test_the_model_sees_at_most_the_configured_rows(real_db_path: Path) -> None:
    def check(messages):  # noqa: ANN001, ANN202
        [payload] = returns(messages, "run_sql")
        assert payload["truncated"] is True and len(payload["rows"]) == 7
        return [final("data_answer", "Uma lista parcial.")]

    script = Script([sql("SELECT titulo FROM dim_movies")], check)
    outcome = ask("Liste os filmes.", settings_for(real_db_path, max_rows=7), model=script.model)

    [execution] = outcome.trace.sql_executions
    assert execution.truncated and execution.row_count == 7
    assert any("mais de 7 linhas" in notice for notice in outcome.notices)


def test_security_still_applies_and_nothing_is_written(
    real_db_path: Path, oracle: sqlite3.Connection
) -> None:
    files = [real_db_path, real_db_path.with_name(real_db_path.name + "-wal")]

    def snapshot() -> list[tuple[int, int] | None]:
        return [(f.stat().st_size, f.stat().st_mtime_ns) if f.exists() else None for f in files]

    counts = "SELECT (SELECT count(*) FROM dim_movies), (SELECT count(*) FROM movie_reviews)"
    before_counts, before_files = oracle.execute(counts).fetchone(), snapshot()
    attacks = [
        "DELETE FROM dim_movies",
        "UPDATE fact_movies_performance SET receita_brl = 0",
        "WITH x AS (SELECT 1) INSERT INTO dim_genres VALUES ('x', 'y')",
        "ATTACH DATABASE 'copia.db' AS copia",
        "PRAGMA query_only = OFF",
        "SELECT name FROM movie_reviews",
        "SELECT sql FROM sqlite_master",
    ]
    script = Script(
        *[[sql(statement)] for statement in attacks[:2]],
        [final("clarification", "Não.")],
    )
    first = ask("Apague tudo.", settings_for(real_db_path), model=script.model)
    script = Script(*[[sql(s)] for s in attacks[2:4]], [final("clarification", "Não.")])
    second = ask("Altere o banco.", settings_for(real_db_path), model=script.model)
    script = Script(*[[sql(s)] for s in attacks[4:6]], [final("clarification", "Não.")])
    third = ask("Mostre dados escondidos.", settings_for(real_db_path), model=script.model)
    script = Script([sql(attacks[6])], [final("clarification", "Não.")])
    fourth = ask("Mostre o esquema.", settings_for(real_db_path), model=script.model)

    executions = [e for o in (first, second, third, fourth) for e in o.trace.sql_executions]
    assert [e.status for e in executions] == [SqlStatus.REJECTED] * len(attacks)
    assert all(
        o.answer is not None and o.answer.status is AnswerStatus.CLARIFICATION
        for o in (first, second, third, fourth)
    )
    assert oracle.execute(counts).fetchone() == before_counts
    assert snapshot() == before_files
    assert not (Path.cwd() / "copia.db").exists()
