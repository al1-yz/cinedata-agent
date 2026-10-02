"""Casos de referência contra o banco real (pulados quando data/cinerocket.db não existe).

As respostas exatas abaixo são o gabarito do M3 para a data de referência 2026-10-01. Elas não são
uma foto do primeiro resultado: `test_every_case_matches_the_oracle_on_the_full_database` refaz os
14 casos em Python, com aritmética exata, sobre o banco inteiro. Para ver o relatório de tempos:
`pytest -m realdb -s tests/test_reference_realdb.py -k report`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from cinedata.config import load_settings
from cinedata.db import MAX_BULK_ROWS, SafeDatabase
from cinedata.reference import OFFICIAL_CASES, ReferenceResult, official_registry, run_case
from reference_oracle import (
    ORACLES,
    GoldData,
    assert_matches_oracle,
    load_gold,
    margin,
    mean,
    rating,
    valid_imdb,
)

pytestmark = pytest.mark.realdb

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "cinerocket.db"
REF = date(2026, 10, 1)


@pytest.fixture(scope="module")
def real_db() -> Iterator[SafeDatabase]:
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado (veja data/README.md)")
    with SafeDatabase(REAL_DB) as database:  # padrões do projeto: 50 linhas e 30 s de prazo
        yield database


@pytest.fixture(scope="module")
def results(real_db: SafeDatabase) -> dict[str, ReferenceResult]:
    return {case.case_id: run_case(real_db, case, reference_date=REF) for case in OFFICIAL_CASES}


@pytest.fixture(scope="module")
def gold(real_db: SafeDatabase) -> GoldData:
    return load_gold(real_db)


def col(result: ReferenceResult, name: str) -> list[object]:
    return [record[name] for record in result.records()]


def scalar(db: SafeDatabase, sql: str) -> object:
    return db.execute(sql).rows[0][0]


# --- fatos da Gold que a semântica assume -------------------------------------------------------


def test_the_gold_facts_behind_the_semantics(real_db: SafeDatabase) -> None:
    counts = {
        table: scalar(real_db, f"SELECT count(*) FROM {table}")  # noqa: S608
        for table in ("dim_movies", "fact_movies_performance", "dim_genres", "dim_people")
    }
    assert counts == {
        "dim_movies": 95645,
        "fact_movies_performance": 95645,
        "dim_genres": 19,
        "dim_people": 424656,
    }
    assert scalar(real_db, "SELECT count(*) FROM dim_companies") == 45941
    roles = real_db.execute("SELECT DISTINCT tipo_pessoa FROM dim_people ORDER BY 1").rows
    assert roles == (("Ator",), ("Diretor",), ("Roteirista",))
    # BETWEEN em texto só vale com datas ISO puras
    not_iso = scalar(
        real_db,
        "SELECT count(*) FROM dim_movies WHERE data_lancamento IS NOT NULL"
        " AND data_lancamento NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'",
    )
    released = scalar(real_db, "SELECT count(*) FROM dim_movies WHERE status_filme = 'Lançado'")
    assert not_iso == 0 and released > 90000
    # receita nunca é 0 (receita > 0 = receita informada); o lucro existe com um lado ausente
    zero_revenue = scalar(
        real_db, "SELECT count(*) FROM fact_movies_performance WHERE receita_brl = 0"
    )
    profit_without_revenue = scalar(
        real_db,
        "SELECT count(*) FROM fact_movies_performance"
        " WHERE receita_brl IS NULL AND orcamento_brl IS NOT NULL AND lucro_brl <> 0",
    )
    assert zero_revenue == 0 and profit_without_revenue > 1000
    # TMDB 0 é quase sempre ausência; uma minoria tem votos e é nota real
    zero_without_votes, zero_with_votes = real_db.execute(
        "SELECT sum(coalesce(qtd_tmdb, 0) = 0), sum(qtd_tmdb > 0)"
        " FROM fact_movies_performance WHERE nota_tmdb = 0"
    ).rows[0]
    assert zero_without_votes > 30000 and 0 < zero_with_votes < 1000
    # Algumas notas TMDB vêm com ruído binário (6.903999999999999 no lugar de 6.904)
    noisy = scalar(
        real_db,
        "SELECT count(*) FROM fact_movies_performance WHERE nota_tmdb <> ROUND(nota_tmdb, 3)",
    )
    assert noisy > 0


def test_id_filme_identifies_every_movie_in_this_dataset(real_db: SafeDatabase) -> None:
    # Invariante verificado neste banco: é o que permite usar id_filme como chave do gabarito.
    total, not_null, distinct = real_db.execute(
        "SELECT count(*), count(id_filme), count(DISTINCT id_filme) FROM dim_movies"
    ).rows[0]
    assert total == not_null == distinct == 95645


def test_bridge_keys_rule_out_duplicated_participations(real_db: SafeDatabase) -> None:
    for table, column in (
        ("bridge_movie_person", "sk_person_id"),
        ("bridge_movie_genre", "sk_genre_id"),
        ("bridge_movie_company", "sk_company_id"),
    ):
        duplicated = scalar(
            real_db,
            f"SELECT count(*) FROM (SELECT sk_movie_id, {column} FROM {table}"  # noqa: S608
            f" GROUP BY sk_movie_id, {column} HAVING count(*) > 1)",
        )
        assert duplicated == 0, table


# --- gabarito ------------------------------------------------------------------------------------


def test_q01_q04_top_revenue_and_popularity(results: dict[str, ReferenceResult]) -> None:
    q01 = results["oficial_01_maior_receita"]
    assert col(q01, "id_filme") == [
        "76600", "299534", "634649", "299536", "361743",
        "346698", "502356", "420818", "330457", "351286",
    ]  # fmt: skip
    assert col(q01, "posicao") == list(range(1, 11))
    assert q01.rows[0][2:] == ("Avatar: The Way Of Water", 2022, 12390136500.54)
    q04 = results["oficial_04_mais_populares"]
    assert col(q04, "id_filme") == ["565770", "980489", "809905", "658829", "557809"]
    assert col(q04, "popularidade") == [2994.357, 2680.593, 2020.0, 2019.0, 2018.0]


def test_q02_q06_q10_aggregates(results: dict[str, ReferenceResult]) -> None:
    q02 = results["oficial_02_lucro_medio_por_genero"]
    assert len(q02.rows) == 19
    assert q02.rows[0] == ("Science Fiction", 227, 520783718.33)
    assert q02.rows[-1] == ("Western", 20, -3581373.25)
    q06 = results["oficial_06_nota_imdb_por_ano"]
    assert col(q06, "ano_lancamento") == [*range(2016, 2028), 2029]
    assert q06.rows[0][:2] == (2016, 10381)
    assert q06.rows[0][2] == pytest.approx(6.338046142, abs=1e-9)
    q10 = results["oficial_10_filmes_por_genero"]
    assert len(q10.rows) == 19
    assert (q10.rows[0], q10.rows[-1]) == (("Drama", 28086), ("Western", 355))
    assert sum(col(q10, "filmes")) == 121521  # = linhas da ponte de gêneros


def test_q03_bad_ben_films_tie_at_the_ninth_place(results: dict[str, ReferenceResult]) -> None:
    q03 = results["oficial_03_maior_margem"]
    assert col(q03, "id_filme") == [
        "787459", "1676901", "464003", "1009615", "893086",
        "608439", "441889", "1028194", "881502", "882052",
    ]  # fmt: skip
    assert col(q03, "posicao") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 9]
    assert col(q03, "titulo")[-2:] == ["Bad Ben", "Bad Ben: The Mandela Effect"]


def test_q05_q14_divergences_keep_real_tmdb_zeros_and_cutoff_ties(
    results: dict[str, ReferenceResult],
) -> None:
    q05 = results["oficial_05_divergencia_tmdb_imdb"]
    assert col(q05, "posicao") == [1, 2, 3, 3, 3, 6, 7, 8, 8, 8]
    assert col(q05, "id_filme")[:2] == ["992506", "1012763"]
    zeros = [r for r in q05.records() if r["nota_tmdb"] == 0.0]
    assert len(zeros) == 4 and all(r["qtd_tmdb"] for r in zeros)  # TMDB 0 com votos é nota
    q14 = results["oficial_14_divergencia_usuarios_imdb"]
    assert col(q14, "posicao") == [1, 2, 3, 4, 4, 6, 7, 7, 7, 7, 7, 7]  # 12 linhas por empate
    first = q14.records()[0]
    assert (first["id_filme"], first["nota_media_usuarios"], first["divergencia"]) == (
        "1150168",
        0.0,  # média 0 de usuário é nota real
        9.8,
    )


def test_q07_q08_q09_people(results: dict[str, ReferenceResult]) -> None:
    assert results["oficial_07_ator_mais_ativo_5_anos"].rows == (("Eric Roberts", 65),)
    assert results["oficial_07_ator_mais_ativo_5_anos"].parameters == {
        "start_date": "2021-10-01",
        "end_date": "2026-10-01",
    }
    q08 = results["oficial_08_diretores_melhor_nota"]
    assert col(q08, "posicao") == [1, 2, 2, 4, 5, 6, 7, 8, 9, 10, 10]  # 11 linhas por empate
    assert q08.rows[:4] == (
        (1, "Scott Wozniak", 9.34, 5, 5),
        (2, "Jun Shishido", 9.1875, 8, 8),
        (2, "Yūichirō Hayashi", 9.1875, 8, 8),
        (4, "Trevor L. Allen", 9.15, 6, 8),  # média sobre 6 dos 8 filmes dirigidos
    )
    assert col(q08, "diretor")[-2:] == ["Don Thacker", "John D. Boswell"]
    assert all(row[4] >= 5 and 0 < row[3] <= row[4] for row in q08.rows)
    assert results["oficial_09_par_ator_diretor"].rows == (("Joe Anoa'i", "Kevin Dunn", 37),)


def test_q11_q12_q13_leaders_and_counts(results: dict[str, ReferenceResult]) -> None:
    assert results["oficial_11_produtora_maior_lucro"].rows == (
        ("Marvel Studios", 63936626417.88, 44),
    )
    # A média simples de margens por filme é dominada por receitas ínfimas: todo gênero é
    # negativo, e o "maior" é o menos negativo.
    assert results["oficial_12_genero_maior_margem"].rows == (("War", -5.348832873312, 57),)
    q13 = results["oficial_13_mais_avaliados"]
    assert col(q13, "posicao") == [1, 2, 3, 4, 4, 4, 4, 8, 8, 8, 8, 8, 8, 8]  # 14 por empate
    assert col(q13, "qtd_avaliacoes_usuarios") == [13, 12, 11, 10, 10, 10, 10, 9, 9, 9, 9, 9, 9, 9]
    assert q13.rows[0][1] == "1391481"


# --- o gabarito vem da semântica, não do SQL ---------------------------------------------------


def test_every_case_matches_the_oracle_on_the_full_database(
    gold: GoldData, results: dict[str, ReferenceResult]
) -> None:
    for case in OFFICIAL_CASES:
        expected = ORACLES[case.case_id](gold, case, REF)
        assert_matches_oracle(case, results[case.case_id].rows, expected)


def _partition(pairs: list[tuple[str, object]]) -> set[frozenset[str]]:
    groups: dict[object, set[str]] = defaultdict(set)
    for key, value in pairs:
        groups[value].add(key)
    return {frozenset(group) for group in groups.values()}


MARGIN_SQL = (
    "SELECT sk_movie_id, ROUND(CAST(receita_brl - orcamento_brl AS REAL) / receita_brl, {})"
    " FROM fact_movies_performance WHERE receita_brl > 0 AND orcamento_brl IS NOT NULL"
)


def test_rounding_never_merges_or_splits_exact_values_on_real_data(
    real_db: SafeDatabase, gold: GoldData
) -> None:
    """No banco inteiro, o arredondamento de cada métrica empata os valores exatamente iguais, e só
    eles: margens em 12 casas, notas (divergências e médias) em 9."""
    directed: dict[str, list[str]] = defaultdict(list)
    for movie, people in gold.movie_people.items():
        for person in people:
            if gold.people[person][1] == "Diretor":
                directed[person].append(movie)

    def director_mean(sk: str) -> Fraction:
        perf = gold.perf
        return mean([rating(perf[m].nota_imdb) for m in directed[sk] if valid_imdb(perf.get(m))])

    checks = {
        MARGIN_SQL.format(12): lambda sk: margin(gold.perf[sk]),
        "SELECT sk_movie_id, ROUND(ABS(nota_tmdb - nota_imdb), 9) FROM fact_movies_performance"
        " WHERE nota_imdb > 0 AND nota_tmdb IS NOT NULL AND (nota_tmdb <> 0 OR qtd_tmdb > 0)": (
            lambda sk: abs(rating(gold.perf[sk].nota_tmdb) - rating(gold.perf[sk].nota_imdb))
        ),
        "SELECT r.sk_movie_id, ROUND(ABS(r.nota_media_usuarios - f.nota_imdb), 9)"
        " FROM dim_reviews r JOIN fact_movies_performance f ON f.sk_movie_id = r.sk_movie_id"
        " WHERE r.nota_media_usuarios IS NOT NULL AND f.nota_imdb > 0": (
            lambda sk: abs(rating(gold.reviews[sk][1]) - rating(gold.perf[sk].nota_imdb))
        ),
        "SELECT b.sk_person_id, ROUND(AVG(f.nota_imdb), 9) FROM bridge_movie_person b"
        " JOIN dim_people p ON p.sk_person_id = b.sk_person_id"
        " JOIN fact_movies_performance f ON f.sk_movie_id = b.sk_movie_id"
        " WHERE p.tipo_pessoa = 'Diretor' AND f.nota_imdb > 0 GROUP BY b.sk_person_id": (
            director_mean
        ),
    }
    for sql, exact in checks.items():
        rows = real_db.execute(sql, max_rows=MAX_BULK_ROWS).rows
        assert len(rows) > 1000
        rounded = _partition([(sk, value) for sk, value in rows])
        exact_groups = _partition([(sk, Fraction(exact(sk))) for sk, _ in rows])
        assert rounded == exact_groups, sql
    # Com 9 casas, margens diferentes empatariam (7/8 e 0,87499999996, por exemplo).
    rows = real_db.execute(MARGIN_SQL.format(9), max_rows=MAX_BULK_ROWS).rows
    assert len(_partition(list(rows))) < len(
        _partition([(sk, margin(gold.perf[sk])) for sk, _ in rows])
    )


# --- data de referência, desempenho e integridade ------------------------------------------------


def test_q07_uses_the_project_reference_date(real_db: SafeDatabase, tmp_path: Path) -> None:
    settings = load_settings(
        {"CINEDATA_REFERENCE_DATE": "2026-10-01"}, dotenv_path=tmp_path / "sem.env"
    )
    q07 = official_registry().get("oficial_07_ator_mais_ativo_5_anos")
    result = run_case(real_db, q07, reference_date=settings.reference_date)
    assert result.rows == (("Eric Roberts", 65),)
    assert "BETWEEN '2021-10-01' AND '2026-10-01'" in result.sql


def test_execution_time_report(real_db: SafeDatabase) -> None:
    """Mede cada caso de novo (cache aquecido) e REPORTA; o teto é o prazo normal da instância."""
    report = []
    for case in OFFICIAL_CASES:
        result = run_case(real_db, case, reference_date=REF)
        report.append((case.case_id, result.elapsed_s, len(result.rows)))
        assert result.elapsed_s < real_db.timeout_s
    width = max(len(name) for name, _, _ in report)
    print(f"\nTEMPO DOS CASOS DE REFERÊNCIA (banco real, prazo de {real_db.timeout_s:g} s)")
    for name, seconds, rows in report:
        print(f"  {name:<{width}}  {seconds:6.2f} s  {rows:3d} linhas")


def test_the_database_file_is_never_modified(real_db: SafeDatabase) -> None:
    before = REAL_DB.stat()
    with SafeDatabase(REAL_DB) as database:
        run_case(database, official_registry().get("oficial_09_par_ator_diretor"))
    after = REAL_DB.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
