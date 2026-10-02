"""Resolução de entidades contra o banco real (pulados quando data/cinerocket.db não existe).

Os oráculos vêm do próprio banco, por SQL, e não do código sob teste. Para ver o relatório de
tempo e memória: `pytest -m realdb -s tests/test_entities_realdb.py -k report`.
"""

from __future__ import annotations

import time
import tracemalloc
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

from cinedata import entities as entities_module
from cinedata.db import SafeDatabase
from cinedata.entities import EntityIndex, EntityKind, MatchState, normalize

pytestmark = pytest.mark.realdb

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "cinerocket.db"
FILME, PESSOA, GENERO, PRODUTORA = (
    EntityKind.FILME,
    EntityKind.PESSOA,
    EntityKind.GENERO,
    EntityKind.PRODUTORA,
)
EXACT = {MatchState.EXACT_UNIQUE, MatchState.EXACT_MULTIPLE}


@pytest.fixture(scope="module")
def real_db() -> Iterator[SafeDatabase]:
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado (veja data/README.md)")
    with SafeDatabase(REAL_DB, max_rows=50, timeout_s=30.0) as database:
        yield database


@pytest.fixture(scope="module")
def index(real_db: SafeDatabase) -> EntityIndex:
    return EntityIndex(real_db)


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def keys(match) -> set[str]:  # noqa: ANN001
    return {candidate.key for candidate in match.candidates}


def test_loaded_row_counts_match_the_database(index: EntityIndex, real_db: SafeDatabase) -> None:
    index.preload()
    for kind, table in (
        (FILME, "dim_movies"),
        (PESSOA, "dim_people"),
        (GENERO, "dim_genres"),
        (PRODUTORA, "dim_companies"),
    ):
        expected = real_db.execute("SELECT count(*) FROM " + table).rows[0][0]  # noqa: S608
        stats = index.stats()[kind]
        assert (stats.rows, stats.skipped) == (expected, 0), kind
    assert [index.stats()[k].rows for k in (FILME, PESSOA, GENERO, PRODUTORA)] == [
        95645,
        424656,
        19,
        45941,
    ]


def test_every_real_genre_resolves_by_its_own_name_and_every_alias_by_alias(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    names = [row[0] for row in real_db.execute("SELECT nome_genero FROM dim_genres").rows]
    assert len(names) == 19
    for name in names:
        match = index.find(GENERO, name)
        assert match.state is MatchState.EXACT_UNIQUE and match.resolved is not None, name
        assert (match.resolved.label, match.resolved.matched_via) == (name, "exact")
    real = {normalize(name): name for name in names}
    for alias, target in entities_module._GENRE_ALIASES.items():
        assert normalize(target) in real, f"alias {alias!r} aponta para um gênero que não existe"
        match = index.find(GENERO, alias)
        assert match.state is MatchState.EXACT_UNIQUE and match.resolved is not None, alias
        assert match.resolved.label == real[normalize(target)]
        assert match.resolved.matched_via == "alias"


def test_a_name_in_three_roles_is_ambiguous_and_each_role_resolves_it(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    name = real_db.execute(
        "SELECT nome_pessoa FROM dim_people WHERE nome_pessoa NOT GLOB '*[^ -~]*'"
        " AND nome_pessoa NOT LIKE '%''%' GROUP BY nome_pessoa HAVING count(*) = 3"
        " ORDER BY nome_pessoa LIMIT 1"
    ).rows[0][0]
    truth = dict(
        real_db.execute(
            "SELECT tipo_pessoa, sk_person_id FROM dim_people WHERE nome_pessoa = "  # noqa: S608
            + sql_literal(name)
        ).rows
    )
    assert set(truth) == {"Ator", "Diretor", "Roteirista"}
    match = index.find(PESSOA, name)
    assert match.state is MatchState.EXACT_MULTIPLE and match.resolved is None
    assert set(truth.values()) <= keys(match)
    for role, sk in truth.items():
        by_role = index.find(PESSOA, name.upper(), role=role)
        assert by_role.state in EXACT and sk in keys(by_role), (name, role)
        assert all(c.role == role for c in by_role.candidates)


def test_the_most_repeated_title_stays_ambiguous_with_ids_and_chronological_order(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    title, count = real_db.execute(
        "SELECT titulo, count(*) AS c FROM dim_movies GROUP BY titulo"
        " ORDER BY c DESC, titulo LIMIT 1"
    ).rows[0]
    assert count >= 36
    truth = {
        row[0]
        for row in real_db.execute(
            "SELECT id_filme FROM dim_movies WHERE titulo = " + sql_literal(title)  # noqa: S608
        ).rows
    }
    match = index.find(FILME, title)
    assert match.state is MatchState.EXACT_MULTIPLE and match.resolved is None
    assert match.total_matches >= count
    assert len(match.candidates) == 10
    years = [c.year for c in match.candidates]
    assert years == sorted(years)
    assert len({c.movie_id for c in match.candidates}) == 10
    assert all(c.movie_id is not None and c.year is not None for c in match.candidates)
    if match.total_matches == count:
        assert {c.movie_id for c in match.candidates} <= truth
    capped = index.find(FILME, title, max_candidates=3)
    assert (len(capped.candidates), capped.total_matches) == (3, match.total_matches)


def test_accents_and_case_do_not_matter_checked_against_an_independent_oracle(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    sk, name = real_db.execute(
        "SELECT sk_person_id, nome_pessoa FROM dim_people WHERE nome_pessoa LIKE '%é%'"
        " AND nome_pessoa NOT LIKE '%''%' ORDER BY nome_pessoa LIMIT 1"
    ).rows[0]
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn"
    )
    assert stripped != name
    for variant in (name, name.lower(), name.upper(), stripped, stripped.lower(), f"  {name}  "):
        match = index.find(PESSOA, variant)
        assert match.state in EXACT and sk in keys(match), variant


def test_symbol_only_titles_in_the_real_data_are_findable(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    rows = real_db.execute(
        "SELECT sk_movie_id, titulo FROM dim_movies WHERE titulo IN ('★', '________')"
    ).rows
    assert {title for _, title in rows} == {"★", "________"}
    for sk, title in rows:
        match = index.find(FILME, title)
        assert match.state in EXACT and sk in keys(match), title


def test_zero_width_and_bom_characters_do_not_hide_people(
    index: EntityIndex, real_db: SafeDatabase
) -> None:
    rows = real_db.execute(
        "SELECT sk_person_id, nome_pessoa FROM dim_people"
        " WHERE nome_pessoa LIKE '%​%' OR nome_pessoa LIKE '%﻿%' LIMIT 5"
    ).rows
    assert rows
    for sk, name in rows:
        clean = name.replace("​", "").replace("﻿", "")
        for query in (clean, name):
            match = index.find(PESSOA, query)
            assert match.state in EXACT and sk in keys(match), repr(name)


def test_partial_and_fuzzy_on_real_names(index: EntityIndex, real_db: SafeDatabase) -> None:
    exists = real_db.execute(
        "SELECT count(*) FROM dim_people WHERE nome_pessoa = 'Christopher Nolan'"
    ).rows[0][0]
    if not exists:
        pytest.skip("Christopher Nolan não existe nesta cópia do banco")
    partial = index.find(PESSOA, "christopher nol")
    assert partial.state is MatchState.PARTIAL_CANDIDATES and partial.resolved is None
    assert "Christopher Nolan" in {c.label for c in partial.candidates}
    fuzzy = index.find(PESSOA, "Christoper Nolan")
    assert fuzzy.state is MatchState.FUZZY_SUGGESTIONS and fuzzy.resolved is None
    assert "Christopher Nolan" in {c.label for c in fuzzy.candidates}


def test_search_speed_after_loading(index: EntityIndex) -> None:
    index.preload()
    timings = {}
    for label, call in {
        "exato": lambda: index.find(PESSOA, "Christopher Nolan"),
        "parcial": lambda: index.find(PESSOA, "nolan"),
        "fuzzy": lambda: index.find(PESSOA, "Christoper Nolan"),
        "fuzzy sem resultado": lambda: index.find(PESSOA, "qzxwvkj plmnb"),
    }.items():
        started = time.perf_counter()
        call()
        timings[label] = time.perf_counter() - started
    assert timings["exato"] < 0.5
    assert timings["parcial"] < 10 and timings["fuzzy"] < 20 and timings["fuzzy sem resultado"] < 20


@pytest.mark.parametrize(
    "query",
    ["a b", "de la", "the", "mr", "john smith", "de", "the the the", "e e e e e e e e", "a" * 200],
)
def test_hostile_and_very_common_queries_finish_quickly_and_cleanly(
    index: EntityIndex, query: str
) -> None:
    for kind in (FILME, PESSOA, PRODUTORA):
        started = time.perf_counter()
        match = index.find(kind, query)
        assert time.perf_counter() - started < 20
        assert match.total_matches >= len(match.candidates) <= 10
        assert match.resolved is None or match.state is MatchState.EXACT_UNIQUE


def test_a_fresh_index_gives_the_same_answers_on_the_real_data(real_db: SafeDatabase) -> None:
    battery = [
        (FILME, "Avatar"),
        (FILME, "star wars"),
        (FILME, "Avtar"),
        (FILME, "★"),
        (GENERO, "Terror"),
        (GENERO, "sci"),
        (PRODUTORA, "warner bros"),
    ]
    runs = []
    for _ in range(2):
        fresh = EntityIndex(real_db)
        runs.append([fresh.find(kind, query) for kind, query in battery])
    assert runs[0] == runs[1]


def test_load_time_per_kind_report(real_db: SafeDatabase) -> None:
    report = []
    for kind in EntityKind:
        fresh = EntityIndex(real_db)
        started = time.perf_counter()
        fresh.preload(kind)
        elapsed = time.perf_counter() - started
        report.append(f"{kind.value}={elapsed:.2f}s ({fresh.stats()[kind].rows} linhas)")
        assert elapsed < 60  # folga generosa: a máquina do avaliador pode ser lenta
    print("\nTEMPO DE CARGA:", "; ".join(report))


def test_memory_report(real_db: SafeDatabase) -> None:
    """Mede e REPORTA a memória do índice (Python heap); sem teto dependente da máquina."""
    fresh = EntityIndex(real_db)
    tracemalloc.start()
    try:
        fresh.preload()
        exact_mb = tracemalloc.get_traced_memory()[0] / 1e6
        fresh.find(PESSOA, "nolan")  # constrói os índices de tokens
        fresh.find(FILME, "star wars")
        fresh.find(PESSOA, "Christoper Nolan")  # e a vizinhança do fuzzy
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(
        f"\nMEMÓRIA (heap Python): índices exatos de 4 tipos = {exact_mb:.0f} MB; "
        f"com tokens e fuzzy = {current / 1e6:.0f} MB; pico durante a medição = {peak / 1e6:.0f} MB"
    )
    assert current > 0
    assert current / 1e6 < 2_000  # só pega explosões absurdas (>2 GB), não um limite de projeto
