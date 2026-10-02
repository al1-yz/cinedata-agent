"""Testes dos casos de referência (offline: bancos sintéticos com a forma da Gold).

Cada armadilha semântica tem um cenário montado à mão com a resposta esperada escrita no teste. Os
cenários aleatórios comparam os 14 SQLs com o oráculo independente de `reference_oracle`.
"""

from __future__ import annotations

import ast
import itertools
import random
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from cinedata import db as db_module
from cinedata import reference as reference_module
from cinedata.db import MAX_BULK_ROWS, QueryRejectedError, QueryTimeoutError, SafeDatabase
from cinedata.reference import (
    OFFICIAL_CASES,
    ReferenceCase,
    ReferenceCaseError,
    ReferenceRegistry,
    ReferenceResult,
    official_registry,
    run_case,
    run_cases,
)
from gold_db import GoldScenario
from reference_oracle import ORACLES, assert_matches_oracle, load_gold

REF = date(2026, 10, 1)
CASES = {case.case_id: case for case in OFFICIAL_CASES}
_counter = itertools.count()


def run(
    tmp_path: Path,
    scenario: GoldScenario,
    case_id: str,
    *,
    ref: date | None = REF,
    **changes: object,
) -> ReferenceResult:
    case = CASES[case_id]
    if changes:
        case = replace(case, **changes)
    path = scenario.write(tmp_path / f"gold{next(_counter)}.db")
    with SafeDatabase(path) as database:
        return run_case(database, case, reference_date=ref)


def column(result: ReferenceResult, name: str) -> list[object]:
    return [record[name] for record in result.records()]


# --- o conjunto oficial e o registro --------------------------------------------------------------


def test_the_official_set_is_the_14_examples_in_order() -> None:
    assert len(OFFICIAL_CASES) == 14
    assert [case.case_id[:11] for case in OFFICIAL_CASES] == [
        f"oficial_{n:02d}_" for n in range(1, 15)
    ]
    assert all(case.official for case in OFFICIAL_CASES)
    assert len({case.question for case in OFFICIAL_CASES}) == 14


OFFICIAL_QUESTIONS = [
    "Top 10 filmes com maior receita em R$",
    "Lucro médio por gênero, considerando apenas filmes com receita informada",
    "Filmes com maior margem de lucro, entre os que possuem receita e orçamento informados",
    "Os 5 filmes mais populares",
    "Filmes com maior divergência entre a nota TMDB e a nota IMDb",
    "Nota média IMDb por ano de lançamento",
    "Ator com mais participações em filmes lançados nos últimos 5 anos",
    "Diretores com maior nota média (mínimo de 5 filmes)",
    "Dupla ator–diretor que mais trabalhou junta",
    "Quantidade de filmes por gênero",
    "Produtora com maior lucro total",
    "Gênero com maior margem de lucro média",
    "Filmes mais avaliados pelos usuários",
    "Filmes em que a nota média dos usuários mais diverge da nota IMDb",
]


def test_official_questions_keep_the_exact_assignment_wording() -> None:
    # Os 14 exemplos são o gabarito mínimo e não exaustivo: a redação do enunciado fica literal em
    # `question`, e as reformulações (que o agente também deve entender) ficam em `paraphrases`.
    assert [case.question for case in OFFICIAL_CASES] == OFFICIAL_QUESTIONS
    for case in OFFICIAL_CASES:
        assert case.paraphrases and case.question not in case.paraphrases, case.case_id
    registry = official_registry()
    registry.register(replace(OFFICIAL_CASES[0], case_id="extra_01", question="Outra pergunta?"))
    assert len(registry) == 15  # o conjunto oficial é o mínimo, não o limite


def test_display_limits_and_windows_follow_the_approved_contract() -> None:
    limits = {case_id[:10]: case.limit for case_id, case in CASES.items()}
    assert limits == {
        "oficial_01": 10,
        "oficial_02": None,
        "oficial_03": 10,
        "oficial_04": 5,
        "oficial_05": 10,
        "oficial_06": None,
        "oficial_07": None,
        "oficial_08": 10,
        "oficial_09": None,
        "oficial_10": None,
        "oficial_11": None,
        "oficial_12": None,
        "oficial_13": 10,
        "oficial_14": 10,
    }
    assert [c.case_id for c in OFFICIAL_CASES if c.window_years] == [
        "oficial_07_ator_mais_ativo_5_anos"
    ]
    assert CASES["oficial_07_ator_mais_ativo_5_anos"].window_years == 5


@pytest.mark.parametrize("case", OFFICIAL_CASES, ids=lambda case: case.case_id)
def test_the_reference_sql_never_reads_the_clock_or_cuts_with_limit(case: ReferenceCase) -> None:
    for sql in (case.sql, case.render(REF)):
        assert not re.search(r"'now'|current_(date|time|timestamp)|random\(", sql, re.I)
    if case.limit is not None:
        assert "RANK() OVER" in case.sql and "posicao <= {limit}" in case.sql
        assert "LIMIT" not in case.sql  # LIMIT N cortaria empates no corte


def test_the_registry_enumerates_and_finds_cases_generically() -> None:
    registry = official_registry()
    assert len(registry) == 14
    assert registry.ids == tuple(case.case_id for case in OFFICIAL_CASES)
    assert list(registry) == list(OFFICIAL_CASES)
    assert "oficial_09_par_ator_diretor" in registry
    assert registry.get("oficial_09_par_ator_diretor") is CASES["oficial_09_par_ator_diretor"]
    with pytest.raises(KeyError, match="desconhecido"):
        registry.get("oficial_99")
    with pytest.raises(ValueError, match="já existe"):
        registry.register(OFFICIAL_CASES[0])
    with pytest.raises(TypeError):
        registry.register("SELECT 1")  # type: ignore[arg-type]


LANGUAGE_CASE = ReferenceCase(
    case_id="extra_filmes_por_idioma",
    question="Quantos filmes há por idioma original?",
    semantics="Contagem de dim_movies por idioma_original.",
    sql=(
        "SELECT idioma_original AS idioma, COUNT(*) AS filmes FROM dim_movies"
        " GROUP BY idioma_original ORDER BY filmes DESC, idioma"
    ),
    columns=("idioma", "filmes"),
    key_columns=("idioma",),
)


def test_a_15th_case_runs_with_the_same_engine_and_no_code_change(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for title in ("A", "B", "C"):
        scenario.movie(title, genres=["Drama"], actors=["Ana"])
    registry = official_registry()
    registry.register(LANGUAGE_CASE)
    assert len(registry) == 15 and len(official_registry()) == 14 and len(OFFICIAL_CASES) == 14
    path = scenario.write(tmp_path / "gold.db")
    with SafeDatabase(path) as database:
        results = run_cases(database, registry, reference_date=REF)
    assert [r.case_id for r in results] == list(registry.ids)
    assert results[-1].rows == (("en", 3),)
    assert results[-1].parameters == {}


def test_a_paraphrase_or_a_new_top_n_is_just_another_case(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for n in range(12):
        scenario.movie(f"F{n}", receita=1000 + n)
    original = CASES["oficial_01_maior_receita"]
    paraphrase = replace(
        original,
        case_id="extra_01_faturamento",
        question="Quais filmes mais faturaram?",
        official=False,
        paraphrases=(),
    )
    top3 = replace(original, case_id="extra_01_top3", limit=3, official=False)
    registry = ReferenceRegistry([original, paraphrase, top3])
    path = scenario.write(tmp_path / "gold.db")
    with SafeDatabase(path) as database:
        first, again, three = run_cases(database, registry)
    assert first.rows == again.rows and len(first.rows) == 10
    assert three.rows == first.rows[:3] and three.parameters == {"limit": 3}
    assert "<= 3" in three.sql


def _case(**changes: object) -> ReferenceCase:
    base = {
        "case_id": "extra_teste",
        "question": "Pergunta?",
        "semantics": "Semântica.",
        "sql": "SELECT titulo FROM dim_movies",
        "columns": ("titulo",),
        "key_columns": ("titulo",),
    }
    return ReferenceCase(**{**base, **changes})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"case_id": "Com Espaço"}, "case_id"),
        ({"case_id": ""}, "case_id"),
        ({"question": "  "}, "question"),
        ({"sql": ""}, "sql"),
        ({"columns": ()}, "columns"),
        ({"columns": ("a", "a"), "key_columns": ("a",)}, "columns"),
        ({"key_columns": ("outra",)}, "key_columns"),
        ({"key_columns": ()}, "key_columns"),
        ({"limit": 0}, "limit"),
        ({"limit": True}, "limit"),
        ({"window_years": 0}, "window_years"),
        ({"max_rows": 0}, "max_rows"),
        ({"max_rows": MAX_BULK_ROWS + 1}, "max_rows"),
        ({"paraphrases": ("",)}, "paraphrases"),
        ({"paraphrases": "Outra forma?"}, "tupla"),
        ({"columns": "titulo"}, "tupla"),  # ("titulo") sem vírgula é só um texto
        ({"columns": ("",), "key_columns": ("",)}, "columns só aceita"),
        ({"columns": ("titulo", "  ")}, "columns só aceita"),
        ({"columns": ("titulo", 1)}, "columns só aceita"),
        ({"columns": (None,), "key_columns": (None,)}, "columns só aceita"),
        ({"columns": (("titulo",),), "key_columns": (("titulo",),)}, "columns só aceita"),
        ({"key_columns": ("",)}, "key_columns só aceita"),
        ({"key_columns": (1,)}, "key_columns só aceita"),
        ({"limit": 5}, "parâmetros"),  # declara {limit} mas o SQL não usa
        ({"sql": "SELECT titulo FROM dim_movies LIMIT {limit}"}, "parâmetros"),
        ({"sql": "SELECT titulo FROM dim_movies WHERE titulo = {nome}"}, "parâmetros"),
        ({"sql": "SELECT '{' AS titulo"}, "modelo"),
        ({"sql": "SELECT {start_date} AS titulo", "window_years": 5}, "parâmetros"),
    ],
)
def test_malformed_cases_are_rejected_on_construction(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _case(**changes)


def test_parameters_render_as_validated_literals() -> None:
    q1, q7 = CASES["oficial_01_maior_receita"], CASES["oficial_07_ator_mais_ativo_5_anos"]
    assert "posicao <= 10" in q1.render() and "{" not in q1.render()
    assert "posicao <= 7" in replace(q1, limit=7).render()
    rendered = q7.render(REF)
    assert "BETWEEN '2021-10-01' AND '2026-10-01'" in rendered
    assert q7.parameters(REF) == {"start_date": "2021-10-01", "end_date": "2026-10-01"}
    assert "BETWEEN '2019-02-28' AND '2024-02-29'" in q7.render(date(2024, 2, 29))


def test_a_window_case_needs_the_project_reference_date(tmp_path: Path) -> None:
    q7 = CASES["oficial_07_ator_mais_ativo_5_anos"]
    with pytest.raises(ValueError, match="reference_date"):
        q7.render()
    calls: list[str] = []
    path = GoldScenario().write(tmp_path / "gold.db")
    with SafeDatabase(path) as database:
        database.execute = lambda sql, **kw: calls.append(sql)  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="reference_date"):
            run_case(database, q7)
    assert calls == []  # falha antes de tocar no banco


# --- o executor ----------------------------------------------------------------------------------


@pytest.fixture
def small_db(tmp_path: Path) -> Iterator[SafeDatabase]:
    scenario = GoldScenario()
    scenario.movie("Duplicado", id_filme="1", receita=10)
    scenario.movie("Duplicado", id_filme="2", receita=20)
    scenario.movie("Outro", id_filme="3", receita=30)
    with SafeDatabase(scenario.write(tmp_path / "small.db")) as database:
        yield database


def test_run_case_overrides_only_the_row_cap_never_the_timeout(small_db: SafeDatabase) -> None:
    seen: list[dict[str, object]] = []
    original = small_db.execute

    def spy(sql: str, **kwargs: object) -> object:
        seen.append(kwargs)
        return original(sql, **kwargs)  # type: ignore[arg-type]

    small_db.execute = spy  # type: ignore[method-assign]
    case = replace(CASES["oficial_01_maior_receita"], max_rows=77)
    result = run_case(small_db, case)
    assert seen == [{"max_rows": 77}]
    assert [r["titulo"] for r in result.records()] == ["Outro", "Duplicado", "Duplicado"]
    assert result.keys() == (("3",), ("2",), ("1",))
    assert result.key_columns == ("id_filme",) and result.elapsed_s >= 0


def test_an_incomplete_result_is_never_a_golden_answer(small_db: SafeDatabase) -> None:
    by_id = _case(
        sql="SELECT id_filme FROM dim_movies ORDER BY id_filme",
        columns=("id_filme",),
        key_columns=("id_filme",),
    )
    with pytest.raises(ReferenceCaseError, match="incompleto"):
        run_case(small_db, replace(by_id, max_rows=2))
    assert len(run_case(small_db, replace(by_id, max_rows=3)).rows) == 3  # o teto exato basta


def test_wrong_columns_and_repeated_keys_are_errors(small_db: SafeDatabase) -> None:
    with pytest.raises(ReferenceCaseError, match="colunas"):
        run_case(small_db, _case(columns=("nome",), key_columns=("nome",)))
    with pytest.raises(ReferenceCaseError, match="se repete"):
        run_case(small_db, _case())  # "Duplicado" aparece duas vezes
    ok = run_case(
        small_db,
        _case(
            sql="SELECT id_filme, titulo FROM dim_movies ORDER BY id_filme",
            columns=("id_filme", "titulo"),
            key_columns=("id_filme",),
        ),
    )
    assert ok.keys() == (("1",), ("2",), ("3",))


def test_cut_cells_are_errors(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Um título bem mais longo do que dez caracteres")
    with SafeDatabase(scenario.write(tmp_path / "gold.db"), max_cell_chars=10) as database:
        with pytest.raises(ReferenceCaseError, match="cortada"):
            run_case(database, _case())


def test_database_errors_keep_their_type(small_db: SafeDatabase) -> None:
    hidden = _case(sql="SELECT name AS titulo FROM movie_reviews")
    with pytest.raises(QueryRejectedError):
        run_case(small_db, hidden)


class FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def test_the_instance_timeout_applies_to_reference_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = GoldScenario()
    for n in range(200):
        scenario.movie(f"F{n}", actors=[f"A{n % 7}"], directors=[f"D{n % 3}"])
    path = scenario.write(tmp_path / "gold.db")
    with SafeDatabase(path, timeout_s=1.0) as database:
        monkeypatch.setattr(db_module, "time", FakeClock(step=0.6))  # estoura na 2ª verificação
        with pytest.raises(QueryTimeoutError):
            run_case(database, CASES["oficial_09_par_ator_diretor"])


def test_the_module_never_touches_sqlite_directly() -> None:
    source = Path(reference_module.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "sqlite3" not in imported
    assert "timeout_s" not in source  # o prazo é sempre o da instância
    assert "db.execute(sql, max_rows=case.max_rows)" in source


# --- 01 maior receita ----------------------------------------------------------------------------


def test_q01_skips_missing_revenue_and_keeps_ties_at_the_cutoff(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for n in range(9):
        scenario.movie(f"Topo {n}", id_filme=f"9{n}", receita=1000 - n)
    scenario.movie("Duplicado", id_filme="20", receita=500)  # 10º, empatado com o 11º
    scenario.movie("Duplicado", id_filme="10", receita=500.0)  # REAL e INTEGER empatam
    scenario.movie("Fora", receita=499)
    scenario.movie("Sem receita", receita=None, orcamento=10**9)
    result = run(tmp_path, scenario, "oficial_01_maior_receita")
    assert column(result, "posicao") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10]
    assert result.keys()[-2:] == (("10",), ("20",))  # empate: título, depois id_filme
    assert "Fora" not in column(result, "titulo")
    assert "Sem receita" not in column(result, "titulo")


# --- 02 lucro médio por gênero -------------------------------------------------------------------


def test_q02_averages_the_literal_gold_profit_of_movies_with_revenue(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Completo", receita=100, orcamento=40, genres=["Action", "Drama"])  # 60
    scenario.movie("Sem orçamento", receita=50, genres=["Action"])  # lucro da Gold = 50
    scenario.movie("Sem receita", orcamento=30, genres=["Action", "War"])  # -30, fora
    scenario.movie("Zero", receita=0, orcamento=10, genres=["Drama"])  # receita 0 é informada
    result = run(tmp_path, scenario, "oficial_02_lucro_medio_por_genero")
    assert result.rows == (("Action", 2, 55.0), ("Drama", 2, 25.0))  # War: nenhum com receita


# --- 03 maior margem -----------------------------------------------------------------------------


def test_q03_margin_is_on_revenue_with_real_division(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Inteiros", receita=200, orcamento=50)  # 0.75, nunca 0 por divisão inteira
    scenario.movie("Metade", receita=100, orcamento=50)  # 0.5 (o ROI seria 1.0)
    scenario.movie("Prejuízo", receita=100, orcamento=300)  # -2.0
    scenario.movie("Receita zero", receita=0, orcamento=10)
    scenario.movie("Sem orçamento", receita=100)
    scenario.movie("Sem receita", orcamento=100)
    result = run(tmp_path, scenario, "oficial_03_maior_margem")
    assert [(r["titulo"], r["margem_lucro"]) for r in result.records()] == [
        ("Inteiros", 0.75),
        ("Metade", 0.5),
        ("Prejuízo", -2.0),
    ]


def test_q03_exactly_equal_margins_tie_despite_float_noise(tmp_path: Path) -> None:
    # Pares do banco real: as duas margens valem exatamente 1097/1100, mas em REAL diferem.
    assert (412907 - 1126.11) / 412907 != (355883 - 970.59) / 355883
    scenario = GoldScenario()
    for n in range(9):
        scenario.movie(f"Topo {n}", receita=100_000, orcamento=n)  # margens acima de 0.9999
    scenario.movie("Bad Ben", receita=355883, orcamento=970.59)
    scenario.movie("Bad Ben: The Mandela Effect", receita=412907, orcamento=1126.11)
    scenario.movie("Abaixo", receita=100, orcamento=50)
    result = run(tmp_path, scenario, "oficial_03_maior_margem")
    assert column(result, "posicao")[-2:] == [10, 10]
    assert column(result, "titulo")[-2:] == ["Bad Ben", "Bad Ben: The Mandela Effect"]


# --- 04 popularidade -----------------------------------------------------------------------------


def test_q04_keeps_anomalous_popularity_and_cutoff_ties(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Ano no título 2020", popularidade=2020.0)  # parece o ano, mas é o dado
    for n, value in enumerate([50.5, 40.0, 30.0]):
        scenario.movie(f"P{n}", popularidade=value)
    scenario.movie("Empate A", popularidade=10.0)
    scenario.movie("Empate B", popularidade=10.0)
    scenario.movie("Fora", popularidade=9.99)
    scenario.movie("Sem popularidade", popularidade=None)
    result = run(tmp_path, scenario, "oficial_04_mais_populares")
    assert column(result, "titulo") == [
        "Ano no título 2020",
        "P0",
        "P1",
        "P2",
        "Empate A",
        "Empate B",
    ]
    assert column(result, "posicao") == [1, 2, 3, 4, 5, 5]


# --- 05 divergência TMDB x IMDb ------------------------------------------------------------------


def test_q05_treats_tmdb_zero_without_votes_as_missing(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("TMDB 0 sem votos", nota_tmdb=0.0, qtd_tmdb=None, nota_imdb=9.0)
    scenario.movie("TMDB 0 com 0 votos", nota_tmdb=0.0, qtd_tmdb=0, nota_imdb=9.0)
    scenario.movie("TMDB 0 com votos", nota_tmdb=0.0, qtd_tmdb=3, nota_imdb=8.0)
    scenario.movie("TMDB sem nota", nota_tmdb=None, qtd_tmdb=3, nota_imdb=8.0)
    scenario.movie("IMDb 0", nota_tmdb=7.0, qtd_tmdb=5, nota_imdb=0.0)
    scenario.movie("IMDb nulo", nota_tmdb=7.0, qtd_tmdb=5, nota_imdb=None)
    scenario.movie("TMDB maior", nota_tmdb=9.0, qtd_tmdb=None, nota_imdb=2.5)  # qtd nula: nota > 0
    scenario.movie("IMDb maior", nota_tmdb=2.5, qtd_tmdb=10, nota_imdb=9.0)
    result = run(tmp_path, scenario, "oficial_05_divergencia_tmdb_imdb")
    assert [(r["titulo"], r["divergencia"], r["posicao"]) for r in result.records()] == [
        ("TMDB 0 com votos", 8.0, 1),
        ("IMDb maior", 6.5, 2),
        ("TMDB maior", 6.5, 2),
    ]


def test_q05_equal_divergences_tie_despite_float_noise(tmp_path: Path) -> None:
    assert abs(0.4 - 9.6) != abs(0.1 - 9.3)  # 9.2 e 9.200000000000001
    scenario = GoldScenario()
    for n in range(9):  # divergências de 9.9 a 9.5, todas acima do empate
        scenario.movie(f"Topo {n}", nota_tmdb=10.0, qtd_tmdb=1, nota_imdb=0.1 + n / 20)
    scenario.movie("Ruído A", nota_tmdb=0.4, qtd_tmdb=1, nota_imdb=9.6)
    scenario.movie("Ruído B", nota_tmdb=0.1, qtd_tmdb=1, nota_imdb=9.3)
    scenario.movie("Abaixo", nota_tmdb=1.0, qtd_tmdb=1, nota_imdb=2.0)
    result = run(tmp_path, scenario, "oficial_05_divergencia_tmdb_imdb")
    assert column(result, "posicao")[-2:] == [10, 10]
    assert column(result, "divergencia")[-2:] == [9.2, 9.2]


# --- 06 nota IMDb por ano ------------------------------------------------------------------------


def test_q06_averages_only_valid_imdb_without_status_or_date_filters(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("A", date="2020-01-01", nota_imdb=6.0)
    scenario.movie("B", date="2020-12-31", nota_imdb=8.0)
    scenario.movie("Zero", date="2020-05-05", nota_imdb=0.0)
    scenario.movie("Nula", date="2020-05-05", nota_imdb=None)
    scenario.movie("Planejado", date="2029-10-13", status="Planejado", nota_imdb=3.8)
    result = run(tmp_path, scenario, "oficial_06_nota_imdb_por_ano")
    assert result.rows == ((2020, 2, 7.0), (2029, 1, 3.8))


# --- 07 ator mais ativo em 5 anos ----------------------------------------------------------------


def _window_scenario() -> GoldScenario:
    scenario = GoldScenario()
    many = {"genres": ["Drama", "Comedy", "War"], "companies": ["X", "Y"]}  # ponte larga
    scenario.movie("Início exato", date="2021-10-01", actors=["Ana"], **many)
    scenario.movie("Fim exato", date="2026-10-01", actors=["Ana", "Bruno"])
    scenario.movie("Meio", date="2023-01-01", actors=["Ana"])
    scenario.movie("Véspera do início", date="2021-09-30", actors=["Bruno"])
    scenario.movie("Dia seguinte ao fim", date="2026-10-02", actors=["Bruno"])
    scenario.movie("Meio 2", date="2024-01-01", actors=["Bruno"])
    scenario.movie("Pós-produção", date="2025-01-01", status="Pós-Produção", actors=["Bruno"])
    scenario.movie("Planejado", date="2026-01-01", status="Planejado", actors=["Bruno"])
    for n in range(4):  # o mesmo nome como diretor não conta como ator
        scenario.movie(f"Dirigido {n}", date="2024-06-01", directors=["Carla"])
    scenario.movie("Atuado", date="2024-06-01", actors=["Carla"])
    return scenario


def test_q07_rolling_window_is_inclusive_date_to_date_and_released_only(tmp_path: Path) -> None:
    scenario = _window_scenario()
    assert run(tmp_path, scenario, "oficial_07_ator_mais_ativo_5_anos").rows == (("Ana", 3),)
    # Um dia depois, a janela é 2021-10-02 a 2026-10-02: Ana perde o início, Bruno ganha o fim.
    later = run(tmp_path, scenario, "oficial_07_ator_mais_ativo_5_anos", ref=date(2026, 10, 2))
    assert later.rows == (("Bruno", 3),)
    assert later.parameters == {"start_date": "2021-10-02", "end_date": "2026-10-02"}


def test_q07_returns_every_tied_leader(tmp_path: Path) -> None:
    scenario = _window_scenario()
    for n in range(3):
        scenario.movie(f"Duda {n}", date="2025-03-03", actors=["Duda"])
    result = run(tmp_path, scenario, "oficial_07_ator_mais_ativo_5_anos")
    assert result.rows == (("Ana", 3), ("Duda", 3))


def test_q07_leap_day_reference_starts_on_february_28(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Dentro", date="2019-02-28", actors=["Eva"])
    scenario.movie("Fora", date="2019-02-27", actors=["Eva", "Fábio"])
    scenario.movie("Fim", date="2024-02-29", actors=["Fábio"])
    result = run(tmp_path, scenario, "oficial_07_ator_mais_ativo_5_anos", ref=date(2024, 2, 29))
    assert result.rows == (("Eva", 1), ("Fábio", 1))


# --- 08 diretores com melhor nota ----------------------------------------------------------------


def test_q08_needs_five_directed_films_and_averages_only_valid_ratings(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for n in range(4):
        scenario.movie(f"Q{n}", directors=["Quatro"], nota_imdb=10.0)
    for n in range(6):  # o mesmo nome como ator não soma filmes dirigidos
        scenario.movie(f"Q-ator{n}", actors=["Quatro"], nota_imdb=10.0)
    for rating in (8.0, 8.0, 8.0, 0.0, None):
        scenario.movie("Cinco", directors=["Cinco"], nota_imdb=rating)
    for rating in (0.0, None, 0.0, None, 0.0):
        scenario.movie("Sem nota", directors=["Sem Nota"], nota_imdb=rating)
    for n in range(6):
        scenario.movie(f"S{n}", directors=["Seis"], nota_imdb=9.0)
    scenario.movie("Sem fato", directors=["Cinco Sem Fato"], with_fact=False)
    for n in range(4):
        scenario.movie(f"SF{n}", directors=["Cinco Sem Fato"], nota_imdb=7.0)
    result = run(tmp_path, scenario, "oficial_08_diretores_melhor_nota")
    assert result.rows == (
        (1, "Seis", 9.0, 6, 6),
        (2, "Cinco", 8.0, 3, 5),
        (3, "Cinco Sem Fato", 7.0, 4, 5),
    )


def test_q08_co_directed_films_count_for_both_and_cutoff_ties_stay(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for n in range(5):
        scenario.movie(f"Juntos {n}", directors=["Ana", "Bia"], nota_imdb=8.0)
    for n in range(5):
        scenario.movie(f"Solo {n}", directors=["Caio"], nota_imdb=9.0)
    for rating in (7.1, 8.3, 6.2, 7.2, 7.2):  # média 7.2 com somas em ordens diferentes
        scenario.movie("D", directors=["Davi"], nota_imdb=rating)
    for rating in (7.2, 7.2, 8.3, 6.2, 7.1):
        scenario.movie("E", directors=["Eli"], nota_imdb=rating)
    two = run(tmp_path, scenario, "oficial_08_diretores_melhor_nota", limit=2)
    assert [(r["posicao"], r["diretor"]) for r in two.records()] == [
        (1, "Caio"),
        (2, "Ana"),
        (2, "Bia"),
    ]
    four = run(tmp_path, scenario, "oficial_08_diretores_melhor_nota", limit=4)
    assert [(r["posicao"], r["diretor"]) for r in four.records()][3:] == [(4, "Davi"), (4, "Eli")]


# --- 09 par ator-diretor -------------------------------------------------------------------------


def test_q09_counts_shared_movies_without_cartesian_inflation(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie(
        "Grande",
        actors=["A1", "A2", "A3"],
        directors=["D1", "D2"],
        writers=["W1"],
        genres=["Drama", "War", "Comedy"],
        companies=["X", "Y"],
    )
    scenario.movie("Segundo", actors=["A1"], directors=["D1"], genres=["Drama", "War"])
    scenario.movie("Sem diretor", actors=["A2", "A3"])
    assert run(tmp_path, scenario, "oficial_09_par_ator_diretor").rows == (("A1", "D1", 2),)


def test_q09_same_name_in_both_roles_is_a_valid_pair_and_ties_all_return(
    tmp_path: Path,
) -> None:
    scenario = GoldScenario()
    for n in range(3):
        scenario.movie(f"Autodirigido {n}", actors=["Clint"], directors=["Clint"])
    for n in range(3):
        scenario.movie(f"Dupla {n}", actors=["Zoe"], directors=["Ana"])
    scenario.movie("Uma vez", actors=["Bob"], directors=["Ana"])
    result = run(tmp_path, scenario, "oficial_09_par_ator_diretor")
    assert result.rows == (("Clint", "Clint", 3), ("Zoe", "Ana", 3))


def _crowd(scenario: GoldScenario, people: int = 205, films: int = 15) -> None:
    """Pessoas mais ativas que o par vencedor, para a amostra de 200 não alcançá-lo."""
    actors = [f"Multidão {n:03d}" for n in range(people)]
    for n in range(films):
        scenario.movie(f"Multidão {n}", actors=actors)


@pytest.mark.parametrize("decoy", [False, True])
def test_q09_pruning_is_exact_when_the_sample_misses_the_winner(
    tmp_path: Path, decoy: bool
) -> None:
    scenario = GoldScenario()
    _crowd(scenario)
    if decoy:  # um diretor dentro da amostra, com um par fraco: piso = 2, abaixo do máximo real
        for n in range(20):
            actors = [f"Avulso {n}"] + (["Repete"] if n < 2 else [])
            scenario.movie(f"Isca {n}", directors=["Isca"], actors=actors)
    for n in range(12):
        scenario.movie(f"Vencedor {n}", directors=["Wanda"], actors=["Yuri"])
    scenario.movie("Outro", directors=["Wanda"], actors=["Xis"])
    result = run(tmp_path, scenario, "oficial_09_par_ator_diretor")
    assert result.rows == (("Yuri", "Wanda", 12),)


# --- 10 filmes por gênero ------------------------------------------------------------------------


def test_q10_counts_each_movie_once_per_genre_and_lists_empty_genres(tmp_path: Path) -> None:
    scenario = GoldScenario(genres=["Drama", "Comedy", "War", "Western"])
    scenario.movie("Duplicado", genres=["Drama", "Comedy"], actors=["A", "B"], companies=["X", "Y"])
    scenario.movie("Duplicado", genres=["Drama"])
    scenario.movie("Guerra", genres=["War"])
    scenario.movie("Sem gênero")
    result = run(tmp_path, scenario, "oficial_10_filmes_por_genero")
    assert result.rows == (("Drama", 2), ("Comedy", 1), ("War", 1), ("Western", 0))


# --- 11 produtora com maior lucro ----------------------------------------------------------------


def test_q11_sums_every_movie_of_each_producer(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Igual 1", receita=150, orcamento=50, companies=["Alfa"])  # 100
    scenario.movie("Igual 2", receita=150, orcamento=50, companies=["Alfa"])  # 100 de novo
    scenario.movie("Sem receita", orcamento=30, companies=["Alfa"])  # -30, sem filtro de receita
    scenario.movie("Coprodução", receita=170, companies=["Beta", "Gama"], genres=["Drama", "War"])
    result = run(tmp_path, scenario, "oficial_11_produtora_maior_lucro")
    assert result.rows == (("Alfa", 170.0, 3), ("Beta", 170.0, 1), ("Gama", 170.0, 1))


# --- 12 gênero com maior margem média ------------------------------------------------------------


def test_q12_averages_per_film_margins_instead_of_an_aggregate_margin(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("D1", receita=100, orcamento=50, genres=["Drama"])  # 0.5
    scenario.movie("D2", receita=1000, orcamento=900, genres=["Drama"])  # 0.1 -> média 0.3
    scenario.movie("C1", receita=1000, orcamento=500, genres=["Comedy"])  # agregada da Comedy
    scenario.movie("C2", receita=100, orcamento=95, genres=["Comedy"])  # seria 0.459; média 0.275
    scenario.movie("Receita zero", receita=0, orcamento=10, genres=["War"])
    scenario.movie("Sem orçamento", receita=100, genres=["War"])
    result = run(tmp_path, scenario, "oficial_12_genero_maior_margem")
    assert result.rows == (("Drama", 0.3, 2),)
    scenario.movie("Western 0.3", receita=10, orcamento=7, genres=["Western"])
    assert run(tmp_path, scenario, "oficial_12_genero_maior_margem").rows == (
        ("Drama", 0.3, 2),
        ("Western", 0.3, 1),
    )


# --- 13 mais avaliados ---------------------------------------------------------------------------


def test_q13_ranks_the_gold_review_count_with_cutoff_ties(tmp_path: Path) -> None:
    scenario = GoldScenario()
    for title, count in [("A", 5), ("B", 4), ("C", 3), ("D", 3), ("E", 2), ("Zero", 0)]:
        scenario.movie(title, reviews=(count, 7.0))
    scenario.movie("Sem avaliação")
    result = run(tmp_path, scenario, "oficial_13_mais_avaliados", limit=3)
    assert [(r["posicao"], r["titulo"]) for r in result.records()] == [
        (1, "A"),
        (2, "B"),
        (3, "C"),
        (3, "D"),
    ]
    assert len(run(tmp_path, scenario, "oficial_13_mais_avaliados").rows) == 5  # sem o zero


# --- 14 divergência usuários x IMDb --------------------------------------------------------------


def test_q14_needs_a_user_average_and_a_valid_imdb(tmp_path: Path) -> None:
    scenario = GoldScenario()
    scenario.movie("Usuários zero", reviews=(1, 0.0), nota_imdb=9.8)  # 0 de usuário é nota real
    scenario.movie("Ruído A", reviews=(1, 0.4), nota_imdb=9.6)
    scenario.movie("Ruído B", reviews=(1, 0.1), nota_imdb=9.3)
    scenario.movie("Sem média", reviews=(2, None), nota_imdb=1.0)
    scenario.movie("IMDb 0", reviews=(1, 9.0), nota_imdb=0.0)
    scenario.movie("Sem avaliação", nota_imdb=1.0)
    scenario.movie("Pequena", reviews=(3, 7.25), nota_imdb=7.0)
    result = run(tmp_path, scenario, "oficial_14_divergencia_usuarios_imdb", limit=2)
    assert [(r["posicao"], r["titulo"], r["divergencia"]) for r in result.records()] == [
        (1, "Usuários zero", 9.8),
        (2, "Ruído A", 9.2),
        (2, "Ruído B", 9.2),
    ]
    assert len(run(tmp_path, scenario, "oficial_14_divergencia_usuarios_imdb").rows) == 4


# --- cenários aleatórios contra o oráculo --------------------------------------------------------

ACTORS = ["Ana", "Bruno", "Carla", "Davi", "Eva", "Fábio", "Gil", "Hana", "Ivo", "Jade"]
DIRECTORS = ["Ana", "Lia", "Mário", "Nina", "Otto"]  # "Ana" também atua
GENRES = ["Action", "Comedy", "Drama", "War", "Western", "Horror"]
DATES = [
    "2019-02-27",
    "2019-02-28",
    "2021-09-30",
    "2021-10-01",
    "2021-10-02",
    "2023-05-05",
    "2024-02-29",
    "2026-10-01",
    "2026-10-02",
]


def random_scenario(seed: int) -> GoldScenario:
    rng = random.Random(seed)  # noqa: S311
    pick = rng.choice
    scenario = GoldScenario()
    for n in range(rng.randrange(40, 90)):
        receita = pick([None, None, 0, 100, 200, 250.5, 1000, 355883, 412907])
        orcamento = pick([None, None, 0.5, 50, 100, 300, 970.59, 1126.11])
        scenario.movie(
            pick(["Igual", "Outro", f"Filme {n}"]),
            date=pick(DATES),
            status=pick(["Lançado", "Lançado", "Lançado", "Pós-Produção", "Planejado"]),
            receita=receita,
            orcamento=orcamento,
            popularidade=pick([None, 0.0, 1.5, 2.0, 10.0, 2020.0]),
            nota_tmdb=pick([0.0, 0.0, 0.1, 0.4, 5.5, 7.3, 10.0, 6.904, 6.903999999999999]),
            qtd_tmdb=pick([None, 0, 1, 7]),
            nota_imdb=pick([None, 0.0, 0.1, 6.1, 7.3, 9.3, 9.6]),
            reviews=pick([None, (1, 0.0), (2, 0.4), (3, 7.25), (2, None), (0, None)]),
            genres=rng.sample(GENRES, rng.randrange(0, 3)),
            companies=rng.sample(["X", "Y", "Z", "W"], rng.randrange(0, 3)),
            actors=rng.sample(ACTORS, rng.randrange(0, 4)),
            directors=rng.sample(DIRECTORS, rng.randrange(0, 3)),
            writers=rng.sample(["Ana", "Rui"], rng.randrange(0, 2)),
        )
    return scenario


@pytest.mark.parametrize("seed", range(25))
def test_every_official_case_matches_the_oracle_on_random_data(tmp_path: Path, seed: int) -> None:
    ref = date(2024, 2, 29) if seed % 5 == 0 else REF
    path = random_scenario(seed).write(tmp_path / "gold.db", shuffle_seed=seed)
    with SafeDatabase(path) as database:
        data = load_gold(database)
        for case in OFFICIAL_CASES:
            if case.limit is not None and seed % 3 == 0:
                case = replace(case, limit=2)  # cortes menores criam mais empates no corte
            result = run_case(database, case, reference_date=ref)
            assert_matches_oracle(case, result.rows, ORACLES[case.case_id](data, case, ref))


def test_results_do_not_depend_on_insertion_order_or_repetition(tmp_path: Path) -> None:
    scenario = random_scenario(1234)
    outcomes = []
    for seed in (None, 1, 2):
        path = scenario.write(tmp_path / f"gold{seed}.db", shuffle_seed=seed)
        with SafeDatabase(path) as database:
            first = run_cases(database, OFFICIAL_CASES, reference_date=REF)
            second = run_cases(database, OFFICIAL_CASES, reference_date=REF)
        assert [r.rows for r in first] == [r.rows for r in second]
        outcomes.append([(r.case_id, r.sql, r.rows) for r in first])
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert all(rows for _, _, rows in outcomes[0])  # nenhum caso trivialmente vazio
