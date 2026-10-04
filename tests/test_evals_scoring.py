"""Pontuação determinística do M3, offline: gabaritos e rastros feitos à mão, sem modelo nem banco.

Cada teste ataca uma forma de o relatório mentir: valor esperado que só "aparece" no resultado,
duplicata escondida, filme identificado só pelo título, ordem ignorada, empate cortado, tolerância
frouxa, coerção de tipo, texto final que não traz o resultado e falha de provedor contada como
acerto ou erro semântico.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date

import pytest

from cinedata.entities import Candidate, EntityKind, MatchState
from cinedata.reference import ReferenceResult
from cinedata.runtime import (
    AgentAnswer,
    AgentFailure,
    AgentOutcome,
    AnswerStatus,
    EntityLookup,
    FailureKind,
    RunTrace,
    SqlExecution,
    SqlStatus,
)
from evals.cases import CASES_BY_ID, MOVIE, ResultCheck, Shape, count, decimal, money, rating, ratio
from evals.scoring import (
    FailureCategory,
    Verdict,
    check_answer,
    competition_ranks,
    match_table,
    score_case,
)

REF = date(2026, 10, 1)

TOP = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=3)
GENRES = ResultCheck(Shape.SET, (("genero",),), (count("filmes"),))
LEADER = ResultCheck(Shape.LEADERS, (("ator",),), (count("filmes"),))

TOP_COLUMNS = ("posicao", "id_filme", "titulo", "ano_lancamento", "receita_brl")
TOP_ROWS = (
    (1, "10", "Avatar", 2009, 2900.0),
    (2, "11", "Titanic", 1997, 2200.5),
    (3, "12", "Dune", 2021, 400.25),
)
GOOD_TOP = [("Avatar", 2009, 2900.0), ("Titanic", 1997, 2200.5), ("Dune", 2021, 400.25)]
GOOD_TEXT = "Avatar (2009): R$ 2.900,00\nTitanic (1997): R$ 2.200,50\nDune (2021): R$ 400,25"
GENRE_COLUMNS = ("genero", "filmes")
GENRE_ROWS = (("Drama", 3), ("Action", 2), ("War", 0))


def match(check, expected_columns, expected_rows, columns, rows, **kwargs):  # noqa: ANN001, ANN201
    return match_table(check, expected_columns, expected_rows, columns, rows, **kwargs)


def top(columns, rows, **kwargs):  # noqa: ANN001, ANN003, ANN201
    return match(TOP, TOP_COLUMNS, TOP_ROWS, columns, rows, **kwargs)


# --- mapeamento por valor: o que é inofensivo -------------------------------------------------


def test_aliases_extra_columns_and_column_order_are_harmless() -> None:
    result = top(
        ("faturamento_em_reais", "ano", "filme", "sk_movie_id"),
        [(2900.0, 2009, "Avatar", "m1"), (2200.5, 1997, "Titanic", "m2"),
         (400.25, 2021, "Dune", "m3")],
    )  # fmt: skip
    assert result.matched, result.message
    mapping = result.to_dict()["mapping"]
    assert mapping == {
        "titulo": "filme",
        "ano_lancamento": "ano",
        "receita_brl": "faturamento_em_reais",
    }


def test_identifying_rows_is_not_enough_the_displayed_label_must_be_in_the_result() -> None:
    # id + métrica identificam os filmes, mas a resposta mostra título e ano: sem eles no
    # resultado, o texto não teria de onde vir
    no_label = top(("id", "receita"), [(10, 2900.0), (11, 2200.5), (12, 400.25)])
    assert not no_label.matched and no_label.detail == "missing_columns"
    assert "titulo" in no_label.message and "ano_lancamento" in no_label.message
    rows = [(10, "Avatar", 2009, 2900.0), (11, "Titanic", 1997, 2200.5), (12, "Dune", 2021, 400.25)]
    with_label = top(("id", "t", "a", "r"), rows)  # ids inteiros valem como "10"
    assert with_label.matched and with_label.to_dict()["mapping"]["id_filme"] == "id"
    no_year = top(("id", "t", "r"), [(r[0], r[1], r[3]) for r in rows])
    assert not no_year.matched and no_year.detail == "missing_columns"


def test_case_and_repeated_spaces_in_names_do_not_matter() -> None:
    rows = [("AVATAR", 2009, 2900.0), ("titanic", 1997, 2200.5), ("  Dune ", 2021, 400.25)]
    assert top(("t", "a", "r"), rows).matched


def test_value_rounded_for_display_is_accepted() -> None:
    rows = [("Avatar", 2009, 2900), ("Titanic", 1997, 2200.5), ("Dune", 2021, 400.249)]
    assert top(("t", "a", "r"), rows).matched


# --- identidade de filme ----------------------------------------------------------------------


def test_a_movie_is_never_identified_by_its_title_alone() -> None:
    result = top(("t", "r"), [(t, r) for t, _, r in GOOD_TOP])
    assert not result.matched and result.detail == "missing_columns"
    with pytest.raises(ValueError, match="título"):
        ResultCheck(Shape.RANKED, (("titulo",),), (count("n"),), top_n=3)


def test_one_movie_repeated_cannot_stand_for_its_homonym() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (decimal("nota"),))
    expected = (("1", "Elemental", 2022, 7.0), ("2", "Elemental", 2023, 7.0))
    columns = ("id_filme", "titulo", "ano_lancamento", "nota")
    twice = [("Elemental", 2023, 7.0), ("Elemental", 2023, 7.0)]
    result = match(check, columns, expected, ("t", "a", "n"), twice)
    assert not result.matched and result.detail == "duplicate_rows"
    both = [("Elemental", 2022, 7.0), ("Elemental", 2023, 7.0)]
    assert match(check, columns, expected, ("t", "a", "n"), both).matched


def test_title_and_year_only_identify_movies_when_unique_in_the_oracle() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (count("n"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "n")
    other_years = (("1", "Die Hart", 2023, 9), ("2", "Die Hart", 2024, 9))
    rows = [("Die Hart", 2023, 9), ("Die Hart", 2024, 9)]
    assert match(check, columns, other_years, ("t", "a", "n"), rows).matched  # o ano basta
    same_year = (("1", "Die Hart", 2024, 9), ("2", "Die Hart", 2024, 9))
    twice = [("Die Hart", 2024, 9), ("Die Hart", 2024, 9)]
    no_ids = match(check, columns, same_year, ("t", "a", "n"), twice)
    assert not no_ids.matched and no_ids.detail == "ambiguous_identity"  # não prova 2 filmes
    with_ids = [("1", "Die Hart", 2024, 9), ("2", "Die Hart", 2024, 9)]
    assert match(check, columns, same_year, ("id", "t", "a", "n"), with_ids).matched
    same_id = [("1", "Die Hart", 2024, 9), ("1", "Die Hart", 2024, 9)]
    repeated = match(check, columns, same_year, ("id", "t", "a", "n"), same_id)
    assert not repeated.matched and repeated.detail == "duplicate_rows"


# --- o que precisa falhar -------------------------------------------------------------------


def test_missing_row_is_rejected() -> None:
    result = top(("t", "a", "r"), GOOD_TOP[:2])
    assert not result.matched and result.detail == "missing_rows"


def test_wrong_entity_with_the_right_value_is_rejected() -> None:
    result = top(("t", "a", "r"), [*GOOD_TOP[:2], ("Heat", 1995, 400.25)])
    assert not result.matched and result.detail == "unexpected_rows"


def test_wrong_metric_value_is_rejected() -> None:
    result = top(("t", "a", "r"), [GOOD_TOP[0], ("Titanic", 1997, 2200.0), GOOD_TOP[2]])
    assert not result.matched and result.detail == "wrong_values"


def test_one_expected_value_appearing_somewhere_is_not_enough() -> None:
    rows = [("Avatar", 2009, 2900.0), ("Heat", 1995, 1500.0), ("Her", 2013, 900.0)]
    result = top(("t", "a", "r"), rows)
    assert not result.matched and result.matched_rows == 1


def test_wrong_ranking_order_is_rejected() -> None:
    result = top(("t", "a", "r"), [GOOD_TOP[1], GOOD_TOP[0], GOOD_TOP[2]])
    assert not result.matched and result.detail == "wrong_order"


def test_rows_beyond_the_top_n_window_are_rejected() -> None:
    check = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=2)
    result = match(check, TOP_COLUMNS, TOP_ROWS[:2], ("t", "a", "r"), GOOD_TOP)
    assert not result.matched and result.detail == "unexpected_rows"


def test_duplicated_rows_are_never_hidden() -> None:
    rows = [("Drama", 3), ("Action", 2), ("War", 0), ("Drama", 3)]
    result = match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), rows)
    assert not result.matched and result.detail == "duplicate_rows"


def test_set_needs_every_row_and_nothing_else_but_order_is_free() -> None:
    shuffled = [("War", 0), ("Drama", 3), ("Action", 2)]
    assert match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), shuffled).matched
    missing = match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), shuffled[1:])
    assert not missing.matched and missing.detail == "missing_rows"
    extra = match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), [*shuffled, ("Total", 5)])
    assert not extra.matched and extra.detail == "unexpected_rows"


def test_truncated_set_is_reported_as_truncation() -> None:
    result = match(
        GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), [("Drama", 3), ("Action", 2)], truncated=True
    )
    assert not result.matched and result.detail == "truncated"


def test_empty_results() -> None:
    empty = match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), [])
    assert not empty.matched and empty.detail == "empty"
    assert match(GENRES, GENRE_COLUMNS, (), ("g", "n"), []).matched
    assert not match(GENRES, GENRE_COLUMNS, (), ("g", "n"), [("Drama", 3)]).matched


def test_missing_metric_column_is_reported() -> None:
    result = top(("t", "a"), [(t, a) for t, a, _ in GOOD_TOP])
    assert not result.matched and result.detail == "missing_columns"
    assert "receita_brl" in result.message


@pytest.mark.parametrize("value", ["2900.0", True])
def test_text_and_booleans_never_count_as_numbers(value: object) -> None:
    check = ResultCheck(Shape.SET, (), (count("filmes"),))
    result = match(check, ("filmes",), ((1,),), ("n",), [(value,)])
    assert not result.matched and result.detail == "missing_columns"


def test_float_years_match_integer_years() -> None:
    check = ResultCheck(Shape.SET, (("ano",),), (decimal("media"),))
    expected = ((2016, 6.338046142), (2017, 6.4))
    rows = [(2016.0, 6.34), (2017.0, 6.4)]
    assert match(check, ("ano", "media"), expected, ("a", "m"), rows).matched


# --- tolerâncias explícitas ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "expected", "got", "ok"),
    [
        (money("v_brl"), 100.00, 100.01, True),
        (money("v_brl"), 100.00, 100.0102, False),
        (money("v_brl"), 12390136500.54, 12390136500.55, True),  # bilhões: ainda um centavo
        (money("v_brl"), 12390136500.54, 12390136500.56, False),
        (money("v_brl"), 12390136500.54, 12390136501.54, False),
        (decimal("v"), 6.338046142, 6.34, True),
        (decimal("v"), 6.338046142, 6.33, False),  # 0,008 > 0,005
        (decimal("v"), 6.338, 6.3431, False),
        (count("v"), 65, 65.0, True),
        (count("v"), 65, 64, False),
        (count("v"), 65, 65.0000001, False),
        (ratio("v"), -5.348832873312, -5.3488, True),
        (ratio("v"), -5.348832873312, -534.88, True),  # em %
        (ratio("v"), -5.348832873312, -5.3489, False),
        (ratio("v"), 0.25, 25.006, False),
    ],
)
def test_numeric_tolerance_boundaries(metric, expected, got, ok) -> None:  # noqa: ANN001
    check = ResultCheck(Shape.SET, (), (metric,))
    assert match(check, (metric.column,), ((expected,),), ("x",), [(got,)]).matched is ok


def test_a_ratio_column_cannot_mix_fraction_and_percent() -> None:
    check = ResultCheck(Shape.SET, (("g",),), (ratio("m"),))
    expected = (("A", 0.25), ("B", 0.5))
    assert match(check, ("g", "m"), expected, ("g", "m"), [("A", 25.0), ("B", 50.0)]).matched
    assert not match(check, ("g", "m"), expected, ("g", "m"), [("A", 25.0), ("B", 0.5)]).matched


# --- empates no corte: a janela do top N é completa ------------------------------------------

REVIEW_COLUMNS = ("posicao", "id_filme", "titulo", "ano_lancamento", "qtd_avaliacoes_usuarios")
TOP3_WINDOW = (  # RANK() <= 3: o 3º lugar tem três empatados, então são 5 linhas
    (1, "1", "Die Hart", 2023, 13),
    (2, "2", "Die Hart", 2024, 12),
    (3, "3", "Alpha", 2020, 10),
    (3, "4", "Beta", 2020, 10),
    (3, "5", "Gamma", 2021, 10),
)
TOP3 = ResultCheck(Shape.RANKED, MOVIE, (count("qtd_avaliacoes_usuarios"),), top_n=3)


def window(rows):  # noqa: ANN001, ANN201
    return match(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, ("t", "a", "n"), rows)


ALL_TOP3 = [(r[2], r[3], r[4]) for r in TOP3_WINDOW]


def test_the_whole_cutoff_tie_is_required_and_may_exceed_n() -> None:
    assert window(ALL_TOP3).matched  # 5 linhas para um top 3
    for size in (3, 4):  # LIMIT 3 ou um empate pela metade
        cut = window(ALL_TOP3[:size])
        assert not cut.matched and cut.detail == "missing_rows", size


def test_tied_rows_can_come_in_any_order() -> None:
    assert window([ALL_TOP3[0], ALL_TOP3[1], ALL_TOP3[4], ALL_TOP3[2], ALL_TOP3[3]]).matched


def test_repeated_titles_are_told_apart_by_year() -> None:
    swapped = window([ALL_TOP3[1], ALL_TOP3[0], *ALL_TOP3[2:]])
    assert not swapped.matched and swapped.detail == "wrong_order"


def test_skipping_a_better_ranked_row_is_rejected() -> None:
    result = window([ALL_TOP3[0], *ALL_TOP3[2:]])
    assert not result.matched and result.detail == "missing_rows"


def test_competition_ranks() -> None:
    assert competition_ranks([13, 12, 10, 10, 10, 9, None]) == [1, 2, 3, 3, 3, 6, 7]


# --- líderes ---------------------------------------------------------------------------------

LEADER_COLUMNS = ("ator", "filmes")


def test_every_tied_leader_is_required() -> None:
    expected = (("Eric Roberts", 65), ("Outro Ator", 65))
    result = match(LEADER, LEADER_COLUMNS, expected, ("a", "n"), [("Eric Roberts", 65)])
    assert not result.matched and result.detail == "missing_rows"
    both = [("Outro Ator", 65), ("Eric Roberts", 65)]
    assert match(LEADER, LEADER_COLUMNS, expected, ("a", "n"), both).matched


def test_rows_below_the_leader_are_allowed_but_flagged_as_unverified() -> None:
    rows = [("Eric Roberts", 65), ("Alguém", 40), ("Outra Pessoa", 39)]
    result = match(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), ("a", "n"), rows)
    assert result.matched
    assert any("não verificada" in note for note in result.notes)


def test_an_extra_row_tied_with_the_leader_is_rejected() -> None:
    rows = [("Eric Roberts", 65), ("Impostor", 65)]
    result = match(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), ("a", "n"), rows)
    assert not result.matched and result.detail == "unexpected_rows"


def test_the_leader_must_come_first() -> None:
    rows = [("Alguém", 40), ("Eric Roberts", 65)]
    assert not match(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), ("a", "n"), rows).matched


def test_rows_of_the_oracle_window_shown_after_the_leader_must_be_right() -> None:
    expected = (("Scott", 9.34), ("Jun", 9.1875), ("Yuichiro", 9.1875))
    check = ResultCheck(Shape.LEADERS, (("diretor",),), (decimal("media"),))
    good = [("Scott", 9.34), ("Yuichiro", 9.19)]
    assert match(check, ("diretor", "media"), expected, ("d", "m"), good).matched
    wrong = match(
        check, ("diretor", "media"), expected, ("d", "m"), [("Scott", 9.34), ("Jun", 8.0)]
    )
    assert not wrong.matched and wrong.detail == "wrong_values"


def test_a_negative_leader_still_bounds_the_unverified_rows() -> None:
    check = ResultCheck(Shape.LEADERS, (("genero",),), (ratio("margem"),))
    expected = (("War", -5.348832873312),)
    below = [("War", -534.88), ("Drama", -600.0)]
    assert match(check, ("genero", "margem"), expected, ("g", "m"), below).matched
    above = [("War", -534.88), ("Drama", -100.0)]
    assert not match(check, ("genero", "margem"), expected, ("g", "m"), above).matched


def test_a_zero_valued_leader_is_not_treated_as_missing() -> None:
    rows = [("Alguém", 0), ("Outro", -1)]
    assert match(LEADER, LEADER_COLUMNS, (("Alguém", 0),), ("a", "n"), rows).matched


def test_pair_identity_needs_both_columns() -> None:
    check = ResultCheck(Shape.LEADERS, (("ator", "diretor"),), (count("juntos"),))
    columns = ("ator", "diretor", "juntos")
    expected = (("Joe Anoa'i", "Kevin Dunn", 37),)
    right = match(check, columns, expected, ("d", "a", "n"), [("Kevin Dunn", "Joe Anoa'i", 37)])
    assert right.matched
    wrong = match(check, columns, expected, ("d", "a", "n"), [("Outro", "Joe Anoa'i", 37)])
    assert not wrong.matched


# --- fidelidade do texto final ----------------------------------------------------------------


def answer(check, columns, rows, text):  # noqa: ANN001, ANN201
    return check_answer(check, columns, rows, text).verdict


def test_every_ranked_row_needs_title_year_and_metric() -> None:
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, GOOD_TEXT) == "ok"
    no_year = GOOD_TEXT.replace(" (2009)", "")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, no_year) == "missing"
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, GOOD_TEXT.replace("2.200,50", "2.100,50"))
    assert (
        result.verdict == "missing" and len(result.missing) == 1 and "Titanic" in result.missing[0]
    )


def test_real_ptbr_money_rendering_passes() -> None:  # como na saída do smoke real com Qwen
    columns = ("posicao", "id_filme", "titulo", "ano_lancamento", "receita_brl")
    rows = (
        (1, "76600", "Avatar: The Way Of Water", 2022, 12390136500.54),
        (2, "299534", "Avengers: Endgame", 2019, 11972234345.65),
    )
    check = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=2)
    text = (
        "Os filmes com maior faturamento são:\n\n"
        "1. **Avatar: The Way Of Water** (2022) – R$ 12.390.136.500,54\n"
        "2. **Avengers: Endgame** (2019) – R$ 11.972.234.345,65"
    )
    assert answer(check, columns, rows, text) == "ok"
    rounded = text.replace("12.390.136.500,54", "12,39 bilhões")  # abreviação não é o valor
    assert answer(check, columns, rows, rounded) == "missing"


@pytest.mark.parametrize(
    "rendering",
    ["R$ 2.900,00", "R$2900", "2,900.00", "2900.0", "2.900"],
)
def test_number_renderings(rendering: str) -> None:
    check = ResultCheck(Shape.SET, (), (money("receita_brl"),))
    text = f"Total em reais: {rendering}."
    assert answer(check, ("receita_brl",), ((2900.0,),), text) == "ok"


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Lucro médio: R$ -3.581.373,25", True),
        ("Lucro médio: -R$ 3.581.373,25", True),
        ("Lucro médio em reais: −3.581.373,25", True),
        ("Western - R$ 3.581.373,25", False),  # hífen de separação não é sinal
        ("Lucro médio: R$ 3.581.373,25", False),
    ],
)
def test_negative_money(text: str, ok: bool) -> None:
    check = ResultCheck(Shape.SET, (), (money("lucro_brl"),))
    assert (answer(check, ("lucro_brl",), ((-3581373.25,),), text) == "ok") is ok


def test_ratio_as_percent_or_fraction_and_genre_in_portuguese() -> None:
    check = ResultCheck(Shape.LEADERS, (("genero",),), (ratio("margem_media"),))
    rows = (("War", -5.348832873312),)
    assert answer(check, ("genero", "margem_media"), rows, "Guerra: -534,88%") == "ok"
    assert answer(check, ("genero", "margem_media"), rows, "War, margem -5,3488") == "ok"
    assert answer(check, ("genero", "margem_media"), rows, "Guerra: -535%") == "missing"


@pytest.mark.parametrize(
    ("genre", "written"),
    [("Crime", "Policial"), ("Tv Movie", "Filme para TV"), ("Tv Movie", "Telefilme"),
     ("Music", "Musical"), ("War", "Guerra"), ("Science Fiction", "Ficção científica")],
)  # fmt: skip
def test_genres_may_be_written_with_their_usual_portuguese_names(genre: str, written: str) -> None:
    rows = ((genre, 3902), ("Drama", 28086))
    assert answer(GENRES, GENRE_COLUMNS, rows, f"{written}: 3.902 filmes; Drama: 28.086") == "ok"
    assert answer(GENRES, GENRE_COLUMNS, rows, "Drama: 28.086 filmes") == "missing"


def test_exhaustive_breakdowns_need_every_group() -> None:
    years = ResultCheck(Shape.SET, (("ano_lancamento",),), (rating("nota_media_imdb"),))
    columns = ("ano_lancamento", "filmes_com_nota", "nota_media_imdb")
    rows = ((2016, 10381, 6.338046142), (2017, 11000, 6.4), (2018, 9000, 6.512))
    full = "| Ano | Nota |\n|---|---|\n| 2016 | 6,34 |\n| 2017 | 6,40 |\n| 2018 | 6,51 |"
    assert answer(years, columns, rows, full) == "ok"  # a contagem auxiliar não é exigida
    assert answer(years, columns, rows, full.replace("| 2018 | 6,51 |", "")) == "missing"
    genres = "Drama: 3 filmes; Action: 2 filmes; War: 0 filmes."
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, genres) == "ok"
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, genres.replace(" War: 0 filmes.", "")) == (
        "missing"
    )


def test_a_value_belongs_to_the_row_it_is_written_next_to() -> None:
    swapped = "Drama: 2 filmes; Action: 3 filmes; War: 0 filmes."
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, swapped) == "missing"


def test_year_and_metric_must_be_distinct_numbers() -> None:
    check = ResultCheck(Shape.RANKED, MOVIE, (decimal("popularidade"),), top_n=1)
    columns = ("posicao", "id_filme", "titulo", "ano_lancamento", "popularidade")
    rows = ((1, "809905", "La Fellinette", 2020, 2020.0),)
    assert answer(check, columns, rows, "La Fellinette (2020), popularidade 2020,0") == "ok"
    assert answer(check, columns, rows, "La Fellinette, de 2020") == "missing"
    title_with_year = ((1, "557809", "Wwe Survivor Series 2018", 2018, 2018.0),)
    text = "Wwe Survivor Series 2018 (2018): 2018,0"
    assert answer(check, columns, title_with_year, text) == "ok"
    assert answer(check, columns, title_with_year, "Wwe Survivor Series 2018") == "missing"


def test_a_title_inside_a_longer_title_does_not_count() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (money("receita_brl"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "receita_brl")
    rows = (("1", "Avatar", 2009, 2900.0), ("2", "Avatar: The Way Of Water", 2022, 2300.0))
    both = "Avatar (2009): R$ 2.900,00; Avatar: The Way Of Water (2022): R$ 2.300,00"
    assert answer(check, columns, rows, both) == "ok"
    only_long = "Avatar: The Way Of Water (2022): R$ 2.300,00 e R$ 2.900,00 em 2009"
    assert answer(check, columns, rows, only_long) == "missing"


def test_repeated_title_and_year_need_the_id() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (count("n"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "n")
    rows = (("1391481", "Die Hart 2", 2024, 13), ("1376602", "Die Hart 2", 2024, 12))
    plain = "Die Hart 2 (2024): 13 avaliações\nDie Hart 2 (2024): 12 avaliações"
    assert answer(check, columns, rows, plain) == "missing"
    with_ids = (
        "Die Hart 2 (2024, id 1391481): 13 avaliações\nDie Hart 2 (2024, id 1376602): 12 avaliações"
    )
    assert answer(check, columns, rows, with_ids) == "ok"


def test_the_answer_rule_matches_the_prompt_rule_for_movie_ids() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (count("n"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "n")
    # mesmo título, anos diferentes: o ano basta
    years = (("1", "Elemental", 2022, 9), ("2", "Elemental", 2023, 8))
    assert answer(check, columns, years, "Elemental (2022): 9\nElemental (2023): 8") == "ok"
    # título + ano únicos: nenhum id exigido
    unique = (("3", "Dunkirk", 2017, 7),)
    assert answer(check, columns, unique, "Dunkirk (2017): 7") == "ok"
    # só as linhas que colidem precisam do id
    mixed = (*unique, ("4", "Die Hart", 2024, 6), ("5", "Die Hart", 2024, 5))
    text = "Dunkirk (2017): 7\nDie Hart (2024, id 4): 6\nDie Hart (2024, id 5): 5"
    assert answer(check, columns, mixed, text) == "ok"
    no_ids = "Dunkirk (2017): 7\nDie Hart (2024): 6\nDie Hart (2024): 5"
    result = check_answer(check, columns, mixed, no_ids)
    assert result.verdict == "missing" and len(result.missing) == 2


def test_pairs_and_scalars() -> None:
    pair = ResultCheck(Shape.LEADERS, (("ator", "diretor"),), (count("juntos"),))
    rows = (("Joe Anoa'i", "Kevin Dunn", 37),)
    columns = ("ator", "diretor", "juntos")
    assert answer(pair, columns, rows, "Joe Anoa'i e Kevin Dunn: 37 filmes") == "ok"
    assert answer(pair, columns, rows, "Joe Anoa'i: 37 filmes") == "missing"
    scalar = ResultCheck(Shape.SET, (), (count("filmes"),))
    assert answer(scalar, ("filmes",), ((3486,),), "São 3.486 filmes.") == "ok"
    assert answer(scalar, ("filmes",), ((3486,),), "São 13486 filmes.") == "missing"
    assert answer(scalar, ("filmes",), ((3486,),), "São cerca de 3 mil filmes.") == "missing"


# --- o caso inteiro --------------------------------------------------------------------------

Q01 = CASES_BY_ID["oficial_01_maior_receita"]
Q13 = CASES_BY_ID["oficial_13_mais_avaliados"]
AMBIGUOUS = CASES_BY_ID["politica_01_titulo_ambiguo"]
OUT_OF_SCOPE = CASES_BY_ID["politica_02_fora_do_escopo"]


def execution(columns, rows, *, index=1, status=SqlStatus.OK, truncated=False, shown=None):  # noqa: ANN001, ANN201
    """Como o `run_sql` grava: `rows_shown` = linhas que o modelo recebeu (todas, por padrão)."""
    ok = status is SqlStatus.OK
    rows = tuple(tuple(r) for r in rows) if ok else ()
    return SqlExecution(
        index, index, f"SELECT {index}", status,
        columns=tuple(columns) if ok else (),
        rows=rows,
        truncated=truncated,
        rows_shown=len(rows) if shown is None else shown,
        error=None if ok else "erro",
    )  # fmt: skip


def outcome(status="data_answer", text=GOOD_TEXT, executions=(), failure=None, lookups=(),
            question="pergunta", assumptions=(), responses=None):  # noqa: ANN001, ANN201  # fmt: skip
    """Como o agente grava: a resposta final vem na resposta do modelo seguinte à última
    ferramenta (`responses` muda isso: a resposta final é a `responses`-ésima)."""
    trace = RunTrace(reference_date=REF, max_rows=50, request_limit=5)
    trace.sql_executions.extend(executions)
    trace.entity_lookups.extend(lookups)
    steps = [e.step for e in executions] + [lookup.step for lookup in lookups]
    trace.model_responses = max(steps, default=0) + 1 if responses is None else responses
    answer_ = (
        None
        if failure
        else AgentAnswer(status=AnswerStatus(status), answer=text, assumptions=list(assumptions))
    )
    return AgentOutcome(question, answer_, failure, trace)


EXPECTED_TOP = ReferenceResult("q01", "SQL", {}, TOP_COLUMNS, ("id_filme",), TOP_ROWS, 0.0)


def good_query(index: int = 1) -> SqlExecution:
    return execution(("t", "a", "r"), GOOD_TOP, index=index)


def test_a_grounded_correct_answer_passes() -> None:
    score = score_case(Q01, outcome(executions=[good_query()]), EXPECTED_TOP)
    assert score.verdict is Verdict.PASS and score.category is None
    assert score.chosen_query == 1 and score.answer.verdict == "ok" and score.answer.required == 3


def test_correct_sql_with_a_useless_answer_fails() -> None:
    score = score_case(Q01, outcome(text="Feito.", executions=[good_query()]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert score.chosen_query == 1  # o SQL estava certo


def test_correct_entity_with_a_wrong_metric_in_the_answer_fails() -> None:
    text = GOOD_TEXT.replace("2.900,00", "2.950,00")
    score = score_case(Q01, outcome(text=text, executions=[good_query()]), EXPECTED_TOP)
    assert score.category is FailureCategory.ANSWER_TEXT and "Avatar" in score.reason


def test_wrong_sql_is_a_result_mismatch_with_the_detail() -> None:
    reversed_rows = execution(("t", "a", "r"), list(reversed(GOOD_TOP)))
    score = score_case(Q01, outcome(executions=[reversed_rows]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL
    assert score.category is FailureCategory.RESULT_MISMATCH and score.detail == "wrong_order"


def test_an_earlier_matching_query_passes_but_is_noted() -> None:
    queries = [good_query(), execution(("n",), [(3,)], index=2)]
    score = score_case(Q01, outcome(executions=queries), EXPECTED_TOP)
    assert score.verdict is Verdict.PASS and score.chosen_query == 1
    assert any("não a última" in note for note in score.notes)


def test_wrong_status_and_sql_errors() -> None:
    clarification = score_case(Q01, outcome(status="clarification"), EXPECTED_TOP)
    assert clarification.category is FailureCategory.WRONG_STATUS
    failed = outcome(
        status="clarification", executions=[execution((), [], status=SqlStatus.FAILED)]
    )
    assert score_case(Q01, failed, EXPECTED_TOP).category is FailureCategory.SQL_ERROR


def test_ungrounded_data_answer_is_never_a_pass() -> None:
    for executions in ((), [execution((), [], status=SqlStatus.FAILED)]):
        score = score_case(Q01, outcome(executions=executions), EXPECTED_TOP)
        assert score.verdict is Verdict.FAIL and score.category is FailureCategory.UNGROUNDED


@pytest.mark.parametrize(
    ("kind", "verdict", "category"),
    [
        (FailureKind.RATE_LIMITED, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.AUTH, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.PAYMENT, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.MODEL_UNAVAILABLE, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.PROVIDER_UNAVAILABLE, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.BAD_REQUEST, Verdict.FAIL, FailureCategory.AGENT_PROTOCOL),
        (FailureKind.FORBIDDEN, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.PROVIDER_ERROR, Verdict.ERROR, FailureCategory.PROVIDER),
        (FailureKind.DATABASE, Verdict.ERROR, FailureCategory.DATABASE),
        (FailureKind.AGENT_PROTOCOL, Verdict.FAIL, FailureCategory.AGENT_PROTOCOL),
        (FailureKind.REQUEST_LIMIT, Verdict.FAIL, FailureCategory.AGENT_PROTOCOL),
    ],
)
def test_failures_are_classified_and_provider_errors_are_not_evaluated(
    kind: FailureKind, verdict: Verdict, category: FailureCategory
) -> None:
    # Mesmo com um SQL certo no rastro, falha de provedor não vira pass nem fail.
    result = outcome(executions=[good_query()], failure=AgentFailure(kind, "detalhe"))
    for case in (Q01, OUT_OF_SCOPE):
        score = score_case(case, result, EXPECTED_TOP)
        assert (score.verdict, score.category) == (verdict, category)
        assert score.evaluated is (verdict is not Verdict.ERROR)


# Janela real do Q13 no banco: 14 linhas para o top 10, porque o 8º lugar empata em 7 filmes.
Q13_RANKS = (1, 2, 3, 4, 4, 4, 4, 8, 8, 8, 8, 8, 8, 8)
Q13_COUNTS = (13, 12, 11, 10, 10, 10, 10, 9, 9, 9, 9, 9, 9, 9)
Q13_ROWS = tuple(
    (rank, str(1000 + i), "Die Hart 2: Die Harter", 2024, n)
    for i, (rank, n) in enumerate(zip(Q13_RANKS, Q13_COUNTS, strict=True))
)
Q13_EXPECTED = ReferenceResult("q13", "SQL", {}, REVIEW_COLUMNS, ("id_filme",), Q13_ROWS, 0.0)
Q13_TEXT = "\n".join(f"{r[2]} ({r[3]}, id {r[1]}): {r[4]} avaliações" for r in Q13_ROWS)


def q13_query(rows) -> SqlExecution:  # noqa: ANN001
    return execution(("titulo", "ano", "id", "n"), [(r[2], r[3], r[1], r[4]) for r in rows])


def test_q13_needs_the_complete_top_10_window_with_its_ties() -> None:
    full = score_case(Q13, outcome(text=Q13_TEXT, executions=[q13_query(Q13_ROWS)]), Q13_EXPECTED)
    assert full.verdict is Verdict.PASS, full.reason
    for size in (1, 2, 5, 10):  # os primeiros k corretos, inclusive um LIMIT 10 no meio do empate
        score = score_case(
            Q13, outcome(text=Q13_TEXT, executions=[q13_query(Q13_ROWS[:size])]), Q13_EXPECTED
        )
        assert score.verdict is Verdict.FAIL, size
        assert score.category is FailureCategory.RESULT_MISMATCH and score.detail == "missing_rows"


def test_q13_without_ids_cannot_pass_on_coinciding_titles_years_and_counts() -> None:
    no_ids = execution(("titulo", "ano", "n"), [(r[2], r[3], r[4]) for r in Q13_ROWS])
    score = score_case(Q13, outcome(text=Q13_TEXT, executions=[no_ids]), Q13_EXPECTED)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.RESULT_MISMATCH
    assert score.detail == "ambiguous_identity"


def test_every_ranking_case_has_a_fixed_evaluated_n() -> None:
    for case in CASES_BY_ID.values():
        if case.is_data and case.check.shape is Shape.RANKED:
            assert case.check.top_n == case.reference.limit is not None, case.case_id


CAPABILITIES = CASES_BY_ID["politica_03_capacidades"]
NOT_FOUND = EntityLookup(1, "filme", "Chuva", None, MatchState.NONE)
REFUSAL_TEXT = (
    "Não tenho acesso à previsão do tempo; posso responder perguntas sobre o catálogo de filmes."
)
CAPABILITIES_TEXT = "Posso responder rankings, receitas, gêneros e notas do catálogo."


def test_policy_statuses() -> None:
    wrong = score_case(OUT_OF_SCOPE, outcome(status="info", text=CAPABILITIES_TEXT))
    assert wrong.verdict is Verdict.FAIL and wrong.category is FailureCategory.POLICY
    data = score_case(OUT_OF_SCOPE, outcome(executions=[execution(("n",), [(1,)])]))
    assert data.category is FailureCategory.POLICY


@pytest.mark.parametrize(
    ("case", "status", "text"),
    [(OUT_OF_SCOPE, "out_of_scope", REFUSAL_TEXT), (CAPABILITIES, "info", CAPABILITIES_TEXT)],
    ids=["fora_do_escopo", "capacidades"],
)
def test_answers_without_data_must_not_touch_the_database(case, status: str, text: str) -> None:  # noqa: ANN001
    assert score_case(case, outcome(status=status, text=text)).verdict is Verdict.PASS
    sql_ok = outcome(status=status, text=text, executions=[execution(("n",), [(1,)])])
    sql_failed = outcome(
        status=status, text=text, executions=[execution((), [], status=SqlStatus.FAILED)]
    )
    lookup = outcome(status=status, text=text, lookups=[NOT_FOUND])
    for result, why in ((sql_ok, "run_sql"), (sql_failed, "run_sql com erro"), (lookup, "busca")):
        score = score_case(case, result)
        assert score.verdict is Verdict.FAIL, why
        assert score.category is FailureCategory.POLICY and "ferramentas" in score.reason, why


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        (REFUSAL_TEXT, Verdict.PASS),
        ("Desculpe, isso está fora do meu escopo: só respondo sobre o catálogo de filmes.",
         Verdict.PASS),
        ("Só consigo responder perguntas sobre filmes da CineData.", Verdict.PASS),
        ("Não sei se vai chover amanhã: não tenho dados de clima.", Verdict.PASS),
        ("A previsão é 30 °C e vai chover.", Verdict.FAIL),  # entrega o pedido, sem recusar
        ("Não tenho acesso a isso, mas a previsão é 30 °C.", Verdict.FAIL),  # recusa e entrega
        ("Não posso confirmar, mas vai chover amanhã.", Verdict.FAIL),
        ("Não tenho acesso. Há 80% de chance de chuva.", Verdict.FAIL),
        ("Amanhã faz 25 graus em São Paulo.", Verdict.FAIL),
        ("Banana azul.", Verdict.FAIL),
        ("Posso responder perguntas sobre o catálogo de filmes.", Verdict.FAIL),  # sem recusa
    ],
)  # fmt: skip
def test_out_of_scope_text_must_refuse_without_answering(text: str, verdict: Verdict) -> None:
    score = score_case(OUT_OF_SCOPE, outcome(status="out_of_scope", text=text))
    assert score.verdict is verdict, score.reason
    if verdict is Verdict.FAIL:
        assert score.category is FailureCategory.POLICY


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        (CAPABILITIES_TEXT, Verdict.PASS),
        ("Posso responder perguntas sobre o catálogo de filmes.", Verdict.PASS),
        ("Respondo perguntas analíticas: por exemplo, os 10 filmes de maior receita ou a nota "
         "média por gênero.", Verdict.PASS),
        ("Você pode me perguntar sobre atores, diretores e bilheteria.", Verdict.PASS),
        ("Banana azul.", Verdict.FAIL),
        ("O catálogo tem 95.645 filmes.", Verdict.FAIL),  # um dado, não uma capacidade
        ("Posso responder sobre filmes: o catálogo tem 95.645 filmes.", Verdict.FAIL),
        ("Posso ajudar.", Verdict.FAIL),  # nenhum assunto do catálogo
        ("Não posso responder nada sobre filmes nem sobre o catálogo.", Verdict.FAIL),
    ],
)  # fmt: skip
def test_capabilities_text_must_say_what_the_agent_answers(text: str, verdict: Verdict) -> None:
    score = score_case(CAPABILITIES, outcome(status="info", text=text))
    assert score.verdict is verdict, score.reason
    if verdict is Verdict.FAIL:
        assert score.category is FailureCategory.POLICY


# --- título ambíguo: esclarecimento comprovado ou resposta completa -------------------------

ELEMENTAL_COLUMNS = ("id_filme", "titulo", "ano_lancamento", "nota_imdb")
ELEMENTAL_ROWS = (("1002355", "Elemental", 2022, 6.7), ("976573", "Elemental", 2023, 7.0))
ELEMENTAL = ReferenceResult(
    "elemental", "SQL", {}, ELEMENTAL_COLUMNS, ("id_filme",), ELEMENTAL_ROWS, 0.0
)
CLARIFY = "Há dois filmes chamados Elemental: o de 2022 e o de 2023. De qual você fala?"
FULL = (
    "Há dois filmes com esse título:\n"
    "- Elemental (2022): nota IMDb 6,7\n"
    "- Elemental (2023): nota IMDb 7,0"
)


def _homonyms(*, complete: bool = True, text: str = "Elemental", ids=("1002355", "976573"),
              years=(2022, 2023)) -> EntityLookup:  # noqa: ANN001  # fmt: skip
    candidates = tuple(
        Candidate(EntityKind.FILME, f"k{movie_id}", "Elemental", year, movie_id)
        for movie_id, year in zip(ids, years, strict=True)
    )
    total = len(candidates) if complete else len(candidates) + 1
    return EntityLookup(1, "filme", text, None, MatchState.EXACT_MULTIPLE,
                        total_matches=total, candidates=candidates)  # fmt: skip


def ambiguous(status: str = "clarification", text: str = CLARIFY, executions=(), lookups=(),
              responses=None):  # noqa: ANN001, ANN201  # fmt: skip
    return outcome(status=status, text=text, executions=executions, lookups=lookups,
                   question=AMBIGUOUS.question, responses=responses)  # fmt: skip


def test_clarification_after_finding_every_homonym_and_naming_them_passes() -> None:
    score = score_case(AMBIGUOUS, ambiguous(lookups=[_homonyms()]), ELEMENTAL)
    assert score.verdict is Verdict.PASS, score.reason


@pytest.mark.parametrize(
    ("text", "lookups", "why"),
    [
        ("Pode esclarecer?", (), "sem busca nem desambiguador"),
        (CLARIFY, (), "texto certo, mas sem a busca"),
        (CLARIFY, (_homonyms(complete=False),), "busca com candidatos fora da lista"),
        (CLARIFY, (_homonyms(text="Elementais"),), "busca de outro título"),
        ("Há mais de um Elemental; o de 2023 é o mais recente. Qual?", (_homonyms(),), "um ano só"),
        ("Pode esclarecer?", (_homonyms(),), "busca certa, texto sem desambiguador"),
    ],
)
def test_clarification_without_evidence_fails(text: str, lookups: tuple, why: str) -> None:
    score = score_case(AMBIGUOUS, ambiguous(text=text, lookups=lookups), ELEMENTAL)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.POLICY, why


def test_same_year_homonyms_are_told_apart_by_id_in_the_clarification() -> None:
    rows = (("111", "Elemental", 2024, 6.0), ("222", "Elemental", 2024, 7.0))
    oracle = ReferenceResult("elemental", "SQL", {}, ELEMENTAL_COLUMNS, ("id_filme",), rows, 0.0)
    lookup = _homonyms(ids=("111", "222"), years=(2024, 2024))
    by_year = ambiguous(text="Há dois Elemental de 2024. Qual?", lookups=[lookup])
    assert score_case(AMBIGUOUS, by_year, oracle).verdict is Verdict.FAIL
    bare = ambiguous(text="Há dois Elemental de 2024: 111 e 222. Qual?", lookups=[lookup])
    assert score_case(AMBIGUOUS, bare, oracle).verdict is Verdict.FAIL
    by_id = ambiguous(text="Há dois Elemental de 2024 (id 111 e id 222). Qual?", lookups=[lookup])
    assert score_case(AMBIGUOUS, by_id, oracle).verdict is Verdict.PASS


def _elemental_sql(columns: tuple[str, ...], rows) -> SqlExecution:  # noqa: ANN001
    query = "SELECT * FROM fact_movies_performance WHERE sk_movie_id IN ('k1002355', 'k976573')"
    rows = tuple(rows)
    return SqlExecution(1, 2, query, SqlStatus.OK, columns=columns, rows=rows, rows_shown=len(rows))


BOTH = _elemental_sql(("id", "titulo", "ano", "nota"), ELEMENTAL_ROWS)


def test_a_complete_answer_for_every_homonym_passes() -> None:
    covered = ambiguous("data_answer", FULL, executions=[BOTH], lookups=[_homonyms()])
    assert score_case(AMBIGUOUS, covered, ELEMENTAL).verdict is Verdict.PASS
    by_title = ambiguous("data_answer", FULL, executions=[_elemental_sql(
        ("titulo", "ano", "nota"), [r[1:] for r in ELEMENTAL_ROWS])])  # fmt: skip
    assert score_case(AMBIGUOUS, by_title, ELEMENTAL).verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("columns", "rows", "text", "category"),
    [
        (("media",), [(6.85,)], FULL, FailureCategory.RESULT_MISMATCH),  # agregado sobre os dois
        (BOTH.columns, ELEMENTAL_ROWS[:1], FULL, FailureCategory.RESULT_MISMATCH),
        (("id", "titulo", "ano", "nota"), ELEMENTAL_ROWS, FULL.replace("6,7", "6,9"),
         FailureCategory.ANSWER_TEXT),  # nota errada no texto
        (("id", "titulo", "ano", "nota"), ELEMENTAL_ROWS, FULL.replace(": nota IMDb 6,7", ""),
         FailureCategory.ANSWER_TEXT),  # nota de um homônimo omitida
    ],
    ids=["linha_agregada", "um_homonimo_so", "nota_errada", "nota_omitida"],
)  # fmt: skip
def test_anything_between_clarifying_and_a_complete_answer_fails(
    columns: tuple[str, ...], rows, text: str, category: FailureCategory  # noqa: ANN001
) -> None:  # fmt: skip
    result = ambiguous("data_answer", text, executions=[_elemental_sql(columns, rows)],
                       lookups=[_homonyms()])  # fmt: skip
    score = score_case(AMBIGUOUS, result, ELEMENTAL)
    assert score.verdict is Verdict.FAIL and score.category is category, score.reason


# --- evidência: truncamento, o que o modelo recebeu e o rótulo exibido ----------------------

Q07 = CASES_BY_ID["oficial_07_ator_mais_ativo_5_anos"]
Q07_EXPECTED = ReferenceResult(
    "q07", "SQL", {}, ("ator", "filmes"), ("ator",), (("Eric Roberts", 65),), 0.0
)
Q07_ROWS = [("Eric Roberts", 65), ("Alguém", 40), ("Outra Pessoa", 39)]


def test_a_truncated_result_never_proves_the_answer() -> None:
    exact_set = match(GENRES, GENRE_COLUMNS, GENRE_ROWS, ("g", "n"), GENRE_ROWS, truncated=True)
    assert not exact_set.matched and exact_set.detail == "truncated"
    exact_window = top(("t", "a", "r"), GOOD_TOP, truncated=True)
    assert not exact_window.matched and exact_window.detail == "truncated"
    leader = match(LEADER, LEADER_COLUMNS, Q07_EXPECTED.rows, ("a", "n"), Q07_ROWS, truncated=True)
    assert not leader.matched and leader.detail == "truncated"
    assert match(LEADER, LEADER_COLUMNS, Q07_EXPECTED.rows, ("a", "n"), Q07_ROWS).matched


def test_an_untruncated_query_of_the_same_run_can_still_be_the_evidence() -> None:
    truncated = execution(("t", "a", "r"), GOOD_TOP, truncated=True)
    only = score_case(Q01, outcome(executions=[truncated]), EXPECTED_TOP)
    assert only.verdict is Verdict.FAIL and only.category is FailureCategory.RESULT_MISMATCH
    assert only.detail == "truncated"
    both = [truncated, execution(("t", "a", "r"), GOOD_TOP, index=2)]
    score = score_case(Q01, outcome(executions=both), EXPECTED_TOP)
    assert score.verdict is Verdict.PASS and score.chosen_query == 2


def test_rows_the_model_never_received_do_not_ground_the_answer() -> None:
    cut = execution(("t", "a", "r"), GOOD_TOP, shown=2)  # o rastro tem 3, o modelo recebeu 2
    score = score_case(Q01, outcome(executions=[cut]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.UNGROUNDED
    assert score.detail == "rows_not_shown"
    seen = execution(("t", "a", "r"), GOOD_TOP, shown=3)
    assert score_case(Q01, outcome(executions=[seen]), EXPECTED_TOP).verdict is Verdict.PASS


def test_leaders_only_need_the_leaders_to_have_been_received() -> None:
    text = "O ator com mais filmes é Eric Roberts, com 65."
    leader_seen = outcome(text=text, executions=[execution(("a", "n"), Q07_ROWS, shown=1)])
    assert score_case(Q07, leader_seen, Q07_EXPECTED).verdict is Verdict.PASS
    nothing = outcome(text=text, executions=[execution(("a", "n"), Q07_ROWS, shown=0)])
    score = score_case(Q07, nothing, Q07_EXPECTED)
    assert score.category is FailureCategory.UNGROUNDED and score.detail == "rows_not_shown"


def test_the_answer_label_must_come_from_the_evidence_query() -> None:
    ids_only = execution(("id", "r"), [(10, 2900.0), (11, 2200.5), (12, 400.25)])
    score = score_case(Q01, outcome(executions=[ids_only]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.RESULT_MISMATCH
    assert score.detail == "missing_columns"
    # a consulta do smoke real (título + ano + receita, linhas únicas) continua valendo
    real_style = execution(("titulo", "ano_lancamento", "receita_brl"), GOOD_TOP)
    assert score_case(Q01, outcome(executions=[real_style]), EXPECTED_TOP).verdict is Verdict.PASS


def test_a_colliding_window_needs_id_title_year_and_metric_in_the_query() -> None:
    columns = ("titulo", "ano", "id", "n")
    rows = [(r[2], r[3], r[1], r[4]) for r in Q13_ROWS]
    for drop in range(3):
        kept = columns[:drop] + columns[drop + 1 :]
        partial = execution(kept, [row[:drop] + row[drop + 1 :] for row in rows])
        score = score_case(Q13, outcome(text=Q13_TEXT, executions=[partial]), Q13_EXPECTED)
        assert score.verdict is Verdict.FAIL, columns[drop]
    full = score_case(
        Q13, outcome(text=Q13_TEXT, executions=[execution(columns, rows)]), Q13_EXPECTED
    )
    assert full.verdict is Verdict.PASS, full.reason


# --- números de lista e ids explícitos -------------------------------------------------------

LIST_CHECK = ResultCheck(Shape.RANKED, MOVIE, (count("qtd_avaliacoes_usuarios"),), top_n=3)
LIST_ROWS = ((1, "1", "Alpha", 2001, 10), (2, "2", "Beta", 2002, 7), (3, "3", "Gamma", 2003, 3))
LIST_TEXT = (
    "1. Alpha (2001): 10 avaliações\n2. Beta (2002): 7 avaliações\n3. Gamma (2003): 3 avaliações"
)


@pytest.mark.parametrize(
    "gamma", ["3. Gamma (2003)", "3) Gamma (2003)", "**3.** Gamma (2003)", "3º Gamma (2003)",
              "#3 Gamma (2003)"],
)  # fmt: skip
def test_list_numbers_never_stand_for_a_metric(gamma: str) -> None:
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, LIST_TEXT) == "ok"
    omitted = LIST_TEXT.replace("3. Gamma (2003): 3 avaliações", gamma)
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, omitted) == "missing"


def test_rank_columns_of_a_table_never_stand_for_a_metric() -> None:
    table = (
        "| # | Filme | Ano | Avaliações |\n|---|---|---|---|\n| 1 | Alpha | 2001 | 10 |\n"
        "| 2 | Beta | 2002 | 7 |\n| 3 | Gamma | 2003 | |"
    )
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, table) == "missing"
    filled = table.replace("| 2003 | |", "| 2003 | 3 |")
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, filled) == "ok"


def test_a_q13_numbered_list_cannot_borrow_the_ordinal_nine() -> None:
    lines = [
        f"{n}. {r[2]} ({r[3]}, id {r[1]}): {r[4]} avaliações" for n, r in enumerate(Q13_ROWS, 1)
    ]
    full = "\n".join(lines)
    good = score_case(Q13, outcome(text=full, executions=[q13_query(Q13_ROWS)]), Q13_EXPECTED)
    assert good.verdict is Verdict.PASS, good.reason
    assert Q13_ROWS[8][4] == 9  # o 9º item tem 9 avaliações, como no corte real do caso 13
    lines[8] = lines[8].removesuffix(": 9 avaliações")
    omitted = outcome(text="\n".join(lines), executions=[q13_query(Q13_ROWS)])
    score = score_case(Q13, omitted, Q13_EXPECTED)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert len(score.answer.missing) == 1 and Q13_ROWS[8][1] in score.answer.missing[0]


def test_ids_only_count_when_written_as_ids() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (count("n"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "n")
    rows = (("1391481", "Die Hart 2", 2024, 13), ("1376602", "Die Hart 2", 2024, 12))
    bare = "Die Hart 2 (2024) 1391481: 13 avaliações\nDie Hart 2 (2024) 1376602: 12 avaliações"
    assert answer(check, columns, rows, bare) == "missing"
    for form in ("id 1391481", "id_filme 1391481", "id do filme 1391481", "ID: 1391481",
                 "id=1391481"):  # fmt: skip
        other = form.replace("1391481", "1376602")
        text = (
            f"Die Hart 2 (2024, {form}): 13 avaliações\nDie Hart 2 (2024, {other}): 12 avaliações"
        )
        assert answer(check, columns, rows, text) == "ok", form
    table = (
        "| Título | Ano | ID | Avaliações |\n|---|---|---|---|\n"
        "| Die Hart 2 | 2024 | 1391481 | 13 |\n| Die Hart 2 | 2024 | 1376602 | 12 |"
    )
    assert answer(check, columns, rows, table) == "ok"


# --- moeda -----------------------------------------------------------------------------------

USD = ResultCheck(Shape.SET, (), (money("receita_total_usd"),))


def test_brl_money_labeled_as_usd_fails() -> None:
    as_usd = GOOD_TEXT.replace("R$", "US$")
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, as_usd)
    assert result.verdict == "missing" and result.missing[0].startswith("moeda")
    # citar reais em outro lugar não salva números rotulados em dólar
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, "Valores em reais.\n" + as_usd) == "missing"


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Receita total: US$ 1.234,50", True),
        ("Receita total: 1.234,50 dólares", True),
        ("Receita total (USD): 1,234.50", True),
        ("Receita total: $1,234.50", True),
        ("Receita total: R$ 1.234,50", False),
        ("Receita total: 1.234,50 reais", False),
        ("Receita total: 1.234,50", False),
    ],
)
def test_usd_money_needs_a_usd_indication(text: str, ok: bool) -> None:
    assert (answer(USD, ("receita_total_usd",), ((1234.5,),), text) == "ok") is ok


def test_currency_may_be_stated_once_in_a_header_or_in_the_assumptions() -> None:
    header = (
        "| Filme | Ano | Receita (BRL) |\n|---|---|---|\n| Avatar | 2009 | 2.900,00 |\n"
        "| Titanic | 1997 | 2.200,50 |\n| Dune | 2021 | 400,25 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, header) == "ok"
    plain = GOOD_TEXT.replace("R$ ", "")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, plain) == "missing"
    stated = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, plain, context="Valores em reais (BRL).")
    assert stated.verdict == "ok"
    no_currency = score_case(Q01, outcome(text=plain, executions=[good_query()]), EXPECTED_TOP)
    assert no_currency.category is FailureCategory.ANSWER_TEXT
    default_brl = outcome(
        text=plain,
        executions=[good_query()],
        assumptions=["Sem moeda na pergunta, os valores estão em reais (BRL)."],
    )
    assert score_case(Q01, default_brl, EXPECTED_TOP).verdict is Verdict.PASS


# --- formas compactas: título comum citado uma vez e "respectivamente" ----------------------

ELEMENTAL_CHECK = ResultCheck(Shape.SET, MOVIE, (rating("nota_imdb"),))
TOP2 = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=2)


def test_a_shared_title_may_be_written_once() -> None:
    compact = "Há dois filmes Elemental: o de 2022 tem nota 6,7 e o de 2023, 7,0."
    assert answer(ELEMENTAL_CHECK, ELEMENTAL_COLUMNS, ELEMENTAL_ROWS, compact) == "ok"
    for text in (
        "Há dois filmes Elemental: o de 2022 tem nota 7,0 e o de 2023, 6,7.",  # trocados
        "Há dois filmes: o de 2022 tem nota 6,7 e o de 2023, 7,0.",  # sem o título
        "Há dois filmes Elemental: o de 2022 tem nota 6,7 e o de 2023 também.",  # um valor só
        "Há dois filmes Elemental, de 2022 e de 2023, com notas 6,7 e 7,0.",  # lista sem posição
    ):
        assert answer(ELEMENTAL_CHECK, ELEMENTAL_COLUMNS, ELEMENTAL_ROWS, text) == "missing", text
    # títulos diferentes não ganham o atalho
    two_titles = "Avatar e Titanic: o de 2009 tem R$ 2.900,00 e o de 1997, R$ 2.200,50."
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], two_titles) == "missing"


def test_the_compact_elemental_answer_passes_the_policy_case() -> None:
    compact = "Há dois filmes Elemental: o de 2022 tem nota 6,7 e o de 2023, 7,0."
    result = ambiguous("data_answer", compact, executions=[BOTH], lookups=[_homonyms()])
    assert score_case(AMBIGUOUS, result, ELEMENTAL).verdict is Verdict.PASS


def test_parallel_lists_need_an_explicit_respectivamente() -> None:
    ok = "Avatar (2009) e Titanic (1997) têm R$ 2.900,00 e R$ 2.200,50, respectivamente."
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], ok) == "ok"
    first = "Avatar (2009) e Titanic (1997) têm, respectivamente, R$ 2.900,00 e R$ 2.200,50."
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], first) == "ok"
    swapped = ok.replace("R$ 2.900,00 e R$ 2.200,50", "R$ 2.200,50 e R$ 2.900,00")
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], swapped) == "missing"
    unlabeled = ok.replace(", respectivamente", "")
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], unlabeled) == "missing"
    genres = "Drama, Action e War têm 3, 2 e 0 filmes, respectivamente."
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, genres) == "ok"
    wrong = genres.replace("3, 2 e 0", "2, 3 e 0")
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, wrong) == "missing"
    shared = "Os filmes Elemental de 2022 e de 2023 têm notas 6,7 e 7,0, respectivamente."
    assert answer(ELEMENTAL_CHECK, ELEMENTAL_COLUMNS, ELEMENTAL_ROWS, shared) == "ok"


def test_score_serialization_is_json_ready() -> None:
    score = score_case(Q01, outcome(executions=[good_query()]), EXPECTED_TOP)
    data = json.loads(json.dumps(score.to_dict()))
    assert data["verdict"] == "pass" and data["sql_check"]["chosen_query"] == 1
    assert data["sql_check"]["queries"][0]["mapping"]["receita_brl"] == "r"
    assert data["answer_check"] == {
        "verdict": "ok",
        "required_rows": 3,
        "missing": [],
        "reason": "",
        "order": "",
    }


# --- ordem do ranking no texto final -----------------------------------------------------------

SWAPPED_TEXT = "Titanic (1997): R$ 2.200,50\nAvatar (2009): R$ 2.900,00\nDune (2021): R$ 400,25"
REVERSED_TEXT = "Dune (2021): R$ 400,25\nTitanic (1997): R$ 2.200,50\nAvatar (2009): R$ 2.900,00"


def test_a_ranking_must_be_written_in_order() -> None:
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, GOOD_TEXT) == "ok"
    prose = (
        "O maior é Avatar (2009), com R$ 2.900,00, seguido de Titanic (1997), com R$ 2.200,50, "
        "e de Dune (2021), com R$ 400,25."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, prose) == "ok"
    swapped = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, SWAPPED_TEXT)
    assert swapped.verdict == "wrong_order" and swapped.missing == ()
    assert "Titanic" in swapped.order and "Avatar" in swapped.order
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, REVERSED_TEXT) == "wrong_order"


def test_tied_rows_may_be_written_in_any_order_but_the_cutoff_tie_stays_complete() -> None:
    lines = [f"{r[2]} ({r[3]}): {r[4]} avaliações" for r in TOP3_WINDOW]
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, "\n".join(lines)) == "ok"
    ties = [lines[0], lines[1], lines[4], lines[2], lines[3]]  # o 3º lugar em outra ordem
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, "\n".join(ties)) == "ok"
    swapped = [lines[1], lines[0], *lines[2:]]  # 1º e 2º trocados (mesmo título, outro ano)
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, "\n".join(swapped)) == "wrong_order"
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, "\n".join(lines[:4])) == "missing"


def test_explicit_positions_are_checked_and_may_override_the_prose_order() -> None:
    labeled_wrong = (
        "1º Titanic (1997): R$ 2.200,50; 2º Avatar (2009): R$ 2.900,00; 3º Dune (2021): R$ 400,25"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, labeled_wrong) == "wrong_order"
    numbered = "\n".join(f"{n}. {line}" for n, line in enumerate(SWAPPED_TEXT.splitlines(), 1))
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, numbered) == "wrong_order"
    # o texto na ordem certa, mas com posições escritas erradas
    relabeled = (
        GOOD_TEXT.replace("Avatar", "1. Avatar")
        .replace("Titanic", "3. Titanic")
        .replace("Dune", "2. Dune")
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, relabeled) == "wrong_order"
    # posições certas escritas em ordem inversa: valem as posições
    reverse = (
        "Em 3º, Dune (2021), com R$ 400,25; em 2º, Titanic (1997), com R$ 2.200,50; "
        "em 1º, Avatar (2009), com R$ 2.900,00."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, reverse) == "ok"
    countdown = "\n".join(
        f"{4 - n}. {line}" for n, line in enumerate(REVERSED_TEXT.splitlines(), 1)
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, countdown) == "ok"
    pair = "Em 2º está Titanic (1997), com R$ 2.200,50; em 1º está Avatar (2009), com R$ 2.900,00."
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], pair) == "ok"
    # só uma linha com posição: vale a ordem do texto
    partial = SWAPPED_TEXT.replace("Titanic", "2º Titanic")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, partial) == "wrong_order"


def test_an_ordinal_that_is_not_a_position_is_not_checked_as_one() -> None:
    chatty = (
        "Avatar (2009) lidera com R$ 2.900,00; Titanic (1997), o 1º filme a passar de 1 bilhão, "
        "vem depois com R$ 2.200,50; Dune (2021), lançado no 2º semestre, fecha com R$ 400,25."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, chatty) == "ok"
    ranked = chatty.replace("o 1º filme a passar de 1 bilhão", "o 1º colocado")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, ranked) == "wrong_order"  # isso é uma posição


def test_tables_follow_the_same_order_rules() -> None:
    header = "| Filme | Ano | Receita (R$) |\n|---|---|---|\n"
    rows = {
        "Avatar": "| Avatar | 2009 | 2.900,00 |",
        "Titanic": "| Titanic | 1997 | 2.200,50 |",
        "Dune": "| Dune | 2021 | 400,25 |",
    }
    ordered = header + "\n".join(rows[name] for name in ("Avatar", "Titanic", "Dune"))
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, ordered) == "ok"
    upside_down = header + "\n".join(rows[name] for name in ("Dune", "Titanic", "Avatar"))
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, upside_down) == "wrong_order"
    ranked = (
        "| # | Filme | Ano | Receita (R$) |\n|---|---|---|---|\n| 3 | Dune | 2021 | 400,25 |\n"
        "| 2 | Titanic | 1997 | 2.200,50 |\n| 1 | Avatar | 2009 | 2.900,00 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, ranked) == "ok"
    mislabeled = ranked.replace("| 3 | Dune", "| 1 | Dune").replace("| 1 | Avatar", "| 3 | Avatar")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, mislabeled) == "wrong_order"


def test_sets_and_tied_leaders_ignore_the_written_order() -> None:
    reversed_set = "War: 0 filmes; Action: 2 filmes; Drama: 3 filmes."
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, reversed_set) == "ok"
    numbered = "1. War: 0 filmes\n2. Action: 2 filmes\n3. Drama: 3 filmes"  # só numeração
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, numbered) == "ok"
    tied = (("Eric Roberts", 65), ("Outro Ator", 65))
    for text in (
        "Outro Ator: 65 filmes\nEric Roberts: 65 filmes",
        "1. Outro Ator: 65 filmes\n2. Eric Roberts: 65 filmes",
    ):
        assert answer(LEADER, LEADER_COLUMNS, tied, text) == "ok", text
    second = "1. Alguém: 40 filmes\n2. Eric Roberts: 65 filmes"  # o único líder escrito como 2º
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), second) == "wrong_order"


def test_a_reversed_ranking_fails_the_case_even_with_the_right_sql() -> None:
    score = score_case(Q01, outcome(text=REVERSED_TEXT, executions=[good_query()]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert score.chosen_query == 1 and score.answer.verdict == "wrong_order"
    assert "ordem" in score.reason


def test_a_wrong_complete_list_is_never_erased_by_a_right_one_and_checks_stay_fast() -> None:
    items = [f"{r[2]} ({r[3]}, id {r[1]}): {r[4]} avaliações" for r in Q13_ROWS]
    numbered = "\n".join(f"{n}. {item}" for n, item in enumerate(items, 1))
    reversed_bullets = "\n".join(f"- {item}" for item in reversed(items))
    in_order = "\n".join(f"- {item}" for item in items)
    table = "\n".join(
        ["| # | Filme | Ano | ID | Avaliações |", "|---|---|---|---|---|"]
        + [f"| {n} | {r[2]} | {r[3]} | {r[1]} | {r[4]} |" for n, r in enumerate(Q13_ROWS, 1)]
    )
    started = time.perf_counter()
    # apresentações certas repetidas (lista numerada, bullets e tabela) valem
    assert answer(Q13.check, REVIEW_COLUMNS, Q13_ROWS, f"{numbered}\n\n{in_order}\n\n{table}") == (
        "ok"
    )
    # uma lista completa invertida contradiz as certas, venha antes ou depois delas
    for text in (
        f"{reversed_bullets}\n\n{numbered}\n\n{table}",
        f"{reversed_bullets}\n\n{in_order}",
        f"{numbered}\n\n{reversed_bullets}",
    ):
        result = check_answer(Q13.check, REVIEW_COLUMNS, Q13_ROWS, text)
        assert result.verdict == "contradiction", text
        assert "apresentação completa" in result.reason and "fora de ordem" in result.reason
    thrice = "\n\n".join([reversed_bullets] * 3)  # só leituras invertidas
    assert answer(Q13.check, REVIEW_COLUMNS, Q13_ROWS, thrice) == "wrong_order"
    assert time.perf_counter() - started < 5


def test_q13_positions_may_be_list_positions_or_competition_ranks() -> None:
    by_rank = "\n".join(f"{r[0]}º {r[2]} ({r[3]}, id {r[1]}): {r[4]} avaliações" for r in Q13_ROWS)
    score = score_case(Q13, outcome(text=by_rank, executions=[q13_query(Q13_ROWS)]), Q13_EXPECTED)
    assert score.verdict is Verdict.PASS, score.reason
    dense = by_rank.replace("8º", "5º")  # posição densa: o 8º lugar empatado não é o 5º
    score = score_case(Q13, outcome(text=dense, executions=[q13_query(Q13_ROWS)]), Q13_EXPECTED)
    assert score.verdict is Verdict.FAIL and score.answer.verdict == "wrong_order"


# --- tabelas markdown com e sem as barras das bordas ------------------------------------------

BARE_TABLE = (
    "# | Filme | Ano | Avaliações\n---|---|---|---\n1 | Alpha | 2001 | 10\n"
    "2 | Beta | 2002 | 7\n3 | Gamma | 2003 |"
)


@pytest.mark.parametrize(
    "table",
    [
        BARE_TABLE,
        BARE_TABLE.replace("---|---|---|---", ":---|:---:|---:|---"),
        BARE_TABLE.replace("# |", "Posição |"),
    ],
    ids=["sem_barras", "alinhamento", "posicao"],
)
def test_rank_columns_without_outer_pipes_never_stand_for_a_metric(table: str) -> None:
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, table) == "missing"
    filled = table.replace("| 2003 |", "| 2003 | 3")
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, filled) == "ok"


def test_id_columns_count_with_or_without_outer_pipes() -> None:
    check = ResultCheck(Shape.SET, MOVIE, (count("n"),))
    columns = ("id_filme", "titulo", "ano_lancamento", "n")
    rows = (("1391481", "Die Hart 2", 2024, 13), ("1376602", "Die Hart 2", 2024, 12))
    bare = (
        "Título | Ano | ID | Avaliações\n---|---|---|---\n"
        "Die Hart 2 | 2024 | 1391481 | 13\nDie Hart 2 | 2024 | 1376602 | 12"
    )
    assert answer(check, columns, rows, bare) == "ok"
    # prosa com barras não é tabela: sem a linha separadora logo abaixo do cabeçalho, ou com
    # outro número de colunas nela, a coluna "ID" não existe e o número solto não é id
    assert answer(check, columns, rows, bare.replace("---|---|---|---\n", "")) == "missing"
    assert answer(check, columns, rows, bare.replace("---|---|---|---", "---|---")) == "missing"
    assert answer(check, columns, rows, bare.replace("ID", "Código")) == "missing"


@pytest.mark.parametrize(
    "table",
    [
        "| Filme | Ano | Avaliações | Nota |\n|---|---|---|---|\n| Alpha | 2001 | 10 | 6,1 |\n"
        "| Beta | 2002 | 7 | 3,0 |\n| Gamma | 2003 | | 5,5 |",
        "Filme | Ano | Avaliações | Nota\n---|---|---|---\nAlpha | 2001 | 10 | 6,1\n"
        "Beta | 2002 | 7 | 3,0\nGamma | 2003 | | 5,5",
    ],
    ids=["com_barras", "sem_barras"],
)
def test_a_table_row_never_borrows_a_value_from_another_row(table: str) -> None:
    # a nota 3,0 do Beta não é a contagem que falta na linha do Gamma
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, table) == "missing"
    filled = table.replace("| | 5,5", "| 3 | 5,5")
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, filled) == "ok"


# --- valor distribuído com "cada", rótulo distribuído com "ambos"/"todos" --------------------

TIED_GENRES = (("Drama", 3), ("Comedy", 3), ("War", 1))


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Drama e Comédia têm 3 filmes cada; Guerra tem 1.", "ok"),
        ("Drama e Comédia têm, cada um, 3 filmes; Guerra tem 1.", "ok"),
        ("Com 3 filmes cada, Drama e Comédia lideram; Guerra tem 1.", "ok"),
        ("Drama e Comédia têm 3 filmes; Guerra tem 1.", "missing"),  # sem "cada", não divide
        ("Drama e Comédia: 3; Guerra: 1.", "missing"),
    ],
)
def test_cada_shares_one_value_with_an_explicit_group(text: str, verdict: str) -> None:
    assert answer(GENRES, GENRE_COLUMNS, TIED_GENRES, text) == verdict


def test_cada_never_gives_a_wrong_value_or_reaches_outside_the_group() -> None:
    text = "Drama e Comédia têm 3 filmes cada; Guerra tem 1."
    other = (("Drama", 3), ("Comedy", 4), ("War", 1))
    assert answer(GENRES, GENRE_COLUMNS, other, text) == "missing"
    three = (("Drama", 3), ("Comedy", 3), ("War", 3))
    outside = "Drama e Comédia têm 3 filmes cada; Guerra também."
    assert answer(GENRES, GENRE_COLUMNS, three, outside) == "missing"


PAIR = ResultCheck(Shape.SET, MOVIE, (money("receita_brl"),))
PAIR_COLUMNS = ("id_filme", "titulo", "ano_lancamento", "receita_brl")
PAIR_ROWS = (
    ("76600", "Avatar: The Way Of Water", 2022, 12390136500.54),
    ("361743", "Top Gun: Maverick", 2022, 7160804869.01),
)
PAIR_TEXT = (
    "Avatar: The Way Of Water e Top Gun: Maverick, ambos de 2022, faturaram "
    "R$ 12.390.136.500,54 e R$ 7.160.804.869,01, respectivamente."
)


def test_ambos_shares_the_stated_year_with_an_explicit_pair() -> None:
    assert answer(PAIR, PAIR_COLUMNS, PAIR_ROWS, PAIR_TEXT) == "ok"
    first = PAIR_TEXT.replace("Avatar: The Way Of Water e Top Gun: Maverick, ambos de 2022,", (
        "Ambos de 2022, Avatar: The Way Of Water e Top Gun: Maverick"))  # fmt: skip
    assert answer(PAIR, PAIR_COLUMNS, PAIR_ROWS, first) == "ok"
    without = PAIR_TEXT.replace(", ambos de 2022,", "")
    assert answer(PAIR, PAIR_COLUMNS, PAIR_ROWS, without) == "missing"
    wrong_year = PAIR_TEXT.replace("2022", "2021")
    assert answer(PAIR, PAIR_COLUMNS, PAIR_ROWS, wrong_year) == "missing"
    differing = ((*PAIR_ROWS[0][:2], 2009, PAIR_ROWS[0][3]), PAIR_ROWS[1])  # anos diferentes
    assert answer(PAIR, PAIR_COLUMNS, differing, PAIR_TEXT) == "missing"


TRIO = ResultCheck(Shape.SET, MOVIE, (count("n"),))
TRIO_COLUMNS = ("id_filme", "titulo", "ano_lancamento", "n")
TRIO_ROWS = (("1", "Alpha", 2020, 10), ("2", "Beta", 2020, 10), ("3", "Gamma", 2020, 10))


def test_todos_shares_a_label_and_cada_a_value_with_the_whole_group() -> None:
    text = "Alpha, Beta e Gamma, todos de 2020, têm 10 avaliações cada."
    assert answer(TRIO, TRIO_COLUMNS, TRIO_ROWS, text) == "ok"
    assert answer(TRIO, TRIO_COLUMNS, TRIO_ROWS, text.replace("todos", "ambos")) == "missing"
    assert answer(TRIO, TRIO_COLUMNS, TRIO_ROWS, text.replace(" cada", "")) == "missing"
    gamma_2021 = (*TRIO_ROWS[:2], ("3", "Gamma", 2021, 10))
    assert answer(TRIO, TRIO_COLUMNS, gamma_2021, text) == "missing"


def test_the_group_before_the_expression_is_its_subject() -> None:
    rows = (*PAIR_ROWS, ("346698", "Barbie", 2023, 6856159007.38))
    text = PAIR_TEXT.removesuffix(".") + "; Barbie (2023) faturou R$ 6.856.159.007,38."
    assert answer(PAIR, PAIR_COLUMNS, rows, text) == "ok"
    later = (
        "Avatar: The Way Of Water e Top Gun: Maverick, ambos de 2022, superaram Barbie (2023), "
        "com R$ 6.856.159.007,38; os dois faturaram R$ 12.390.136.500,54 e R$ 7.160.804.869,01."
    )
    assert answer(PAIR, PAIR_COLUMNS, rows, later) == "missing"  # "os dois" não fixa os valores
    genres = (("Drama", 3), ("Comedy", 3), ("War", 2), ("Action", 2))
    two = "Drama e Comédia têm 3 filmes cada, mais que Guerra e Ação, com 2 cada."
    assert answer(GENRES, GENRE_COLUMNS, genres, two) == "ok"


def test_respectivamente_pairs_only_the_list_written_before_it() -> None:
    text = (
        "Avatar (2009) e Titanic (1997) têm R$ 2.900,00 e R$ 2.200,50, respectivamente; "
        "Dune (2021) tem R$ 400,25."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, text) == "ok"
    same_sentence = text.replace("; Dune", ", e Dune")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, same_sentence) == "ok"
    names = ResultCheck(Shape.SET, (("ator",),), (count("filmes"),))
    rows = (("Robert Downey Jr.", 10), ("Chris Evans", 8))
    abbreviated = "Robert Downey Jr. e Chris Evans têm 10 e 8 filmes, respectivamente."
    assert answer(names, ("ator", "filmes"), rows, abbreviated) == "ok"


def test_positions_written_in_table_cells_or_in_words() -> None:
    cells = (
        "| Filme | Ano | Receita (R$) |\n|---|---|---|\n| 3. Dune | 2021 | 400,25 |\n"
        "| 2. Titanic | 1997 | 2.200,50 |\n| 1. Avatar | 2009 | 2.900,00 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, cells) == "ok"  # a posição em cada linha manda
    swapped = cells.replace("| 3. Dune", "| 1. Dune").replace("| 1. Avatar", "| 3. Avatar")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, swapped) == "wrong_order"
    # o número da posição na célula não é a métrica que falta
    gamma = (
        "| Filme | Ano | Avaliações |\n|---|---|---|\n| 1. Alpha | 2001 | 10 |\n"
        "| 2. Beta | 2002 | 7 |\n| 3. Gamma | 2003 | |"
    )
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, gamma) == "missing"
    words = (
        "Em terceiro lugar, Dune (2021), com R$ 400,25; em segundo, Titanic (1997), com "
        "R$ 2.200,50; em primeiro, Avatar (2009), com R$ 2.900,00."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, words) == "ok"
    wrong = words.replace("Em terceiro lugar", "Em primeiro lugar")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, wrong) == "wrong_order"
    according = "Segundo o banco, Avatar (2009) tem R$ 2.900,00; Titanic (1997), R$ 2.200,50"
    assert answer(TOP2, TOP_COLUMNS, TOP_ROWS[:2], according) == "ok"  # "segundo o" não é posição


# --- números rotulados como outra grandeza não valem como a métrica ---------------------------


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Eric Roberts — 65", "ok"),
        ("Eric Roberts — 65 filmes", "ok"),
        ("Eric Roberts — _65_ filmes", "ok"),  # ênfase markdown com sublinhado
        ("Eric Roberts — __65__", "ok"),
        ("Eric Roberts — _65 anos_", "missing"),
        ("Eric Roberts, com 65 participações", "ok"),
        ("Eric Roberts — 65 anos", "missing"),
        ("Eric Roberts — 65%", "missing"),
        ("Eric Roberts — R$ 65,00", "missing"),
        ("Eric Roberts (id 65)", "missing"),
        ("Eric Roberts — 65/100", "missing"),
        ("Eric Roberts — 65 mil filmes", "missing"),
        ("Eric Roberts — 65th", "missing"),
    ],
)
def test_a_number_labeled_as_another_quantity_is_not_the_count(text: str, verdict: str) -> None:
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), text) == verdict


def test_q07_an_age_is_not_a_film_count() -> None:
    query = execution(("a", "n"), Q07_ROWS)
    age = score_case(Q07, outcome(text="Eric Roberts — 65 anos", executions=[query]), Q07_EXPECTED)
    assert age.verdict is Verdict.FAIL and age.category is FailureCategory.ANSWER_TEXT
    bare = score_case(Q07, outcome(text="Eric Roberts — 65", executions=[query]), Q07_EXPECTED)
    assert bare.verdict is Verdict.PASS, bare.reason


@pytest.mark.parametrize(
    ("gamma", "verdict"),
    [
        ("3. Gamma (2003): 3 avaliações", "ok"),
        ("3. Gamma (2003): 3", "ok"),
        ("3. Gamma (2003, id 3)", "missing"),
        ("3. Gamma (2003): 3%", "missing"),
        ("3. Gamma (2003): R$ 3,00", "missing"),
        ("3. Gamma (2003): 3/10", "missing"),
    ],
)
def test_review_counts_need_a_number_that_is_a_count(gamma: str, verdict: str) -> None:
    text = LIST_TEXT.replace("3. Gamma (2003): 3 avaliações", gamma)
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, text) == verdict


def test_a_rating_out_of_ten_is_a_rating_and_its_scale_is_not_a_value() -> None:
    rated = "Elemental (2022): nota 6,7/10\nElemental (2023): nota 7,0/10"
    assert answer(ELEMENTAL_CHECK, ELEMENTAL_COLUMNS, ELEMENTAL_ROWS, rated) == "ok"
    alpha = LIST_TEXT.replace("Alpha (2001): 10 avaliações", "Alpha (2001): nota 6,7/10")
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, alpha) == "missing"
    labeled = LIST_TEXT.replace("Gamma (2003): 3 avaliações", "Gamma (2003): nota 3")
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, labeled) == "missing"  # nota não conta


def test_a_rating_never_stands_for_a_divergence() -> None:
    # caso 14 real: com média dos usuários 0,0, a divergência é igual à nota IMDb (9,8)
    check = ResultCheck(Shape.RANKED, MOVIE, (decimal("divergencia"),), top_n=1)
    columns = ("posicao", "id_filme", "titulo", "ano_lancamento", "divergencia")
    rows = ((1, "1150168", "The Moon Child", 2021, 9.8),)
    rating_only = "The Moon Child (2021): média dos usuários 0,0 e nota IMDb 9,8."
    assert answer(check, columns, rows, rating_only) == "missing"
    assert answer(check, columns, rows, "The Moon Child (2021): 9,8/10 no IMDb.") == "missing"
    stated = "The Moon Child (2021): divergência de 9,8 (nota IMDb 9,8, usuários 0,0)."
    assert answer(check, columns, rows, stated) == "ok"


def test_a_column_header_labels_its_cells_like_prose_does() -> None:
    divergence = ResultCheck(Shape.RANKED, MOVIE, (decimal("divergencia"),), top_n=1)
    columns = ("posicao", "id_filme", "titulo", "ano_lancamento", "divergencia")
    rows = ((1, "1150168", "The Moon Child", 2021, 9.8),)
    no_divergence = (
        "| # | Filme | Ano | Média dos usuários | Nota IMDb |\n|---|---|---|---|---|\n"
        "| 1 | The Moon Child | 2021 | 0,0 | 9,8 |"
    )
    assert answer(divergence, columns, rows, no_divergence) == "missing"
    full = (
        no_divergence.replace("| Nota IMDb |", "| Nota IMDb | Divergência |").replace(
            "|---|---|---|---|---|", "|---|---|---|---|---|---|"
        )
        + " 9,8 |"
    )
    assert answer(divergence, columns, rows, full) == "ok"
    # sem barras nas bordas, a mesma coisa
    bare = "Filme | Ano | Nota IMDb\n---|---|---\nThe Moon Child | 2021 | 9,8"
    assert answer(divergence, columns, rows, bare) == "missing"
    ages = "| Ator | Idade |\n|---|---|\n| Eric Roberts | 65 |"
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), ages) == "missing"
    films = "| Ator | Filmes |\n|---|---|\n| Eric Roberts | 65 |"
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), films) == "ok"
    dollars = "| Filme | Ano | Receita (US$) |\n|---|---|---|\n| Avatar | 2009 | 2.900,00 |"
    top1 = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=1)
    assert answer(top1, TOP_COLUMNS, TOP_ROWS[:1], dollars + "\nValores em reais.") == "missing"
    rated = (
        "| Filme | Ano | Nota |\n|---|---|---|\n| Elemental | 2022 | 6,7 |\n"
        "| Elemental | 2023 | 7,0 |"
    )
    assert answer(ELEMENTAL_CHECK, ELEMENTAL_COLUMNS, ELEMENTAL_ROWS, rated) == "ok"
    counted = (
        "| Diretor | Média IMDb | Filmes com nota |\n|---|---|---|\n| Scott Wozniak | 9,34 | 5 |"
    )
    leader = ResultCheck(Shape.LEADERS, (("diretor",),), (rating("media_imdb"),))
    assert answer(leader, ("diretor", "media_imdb"), (("Scott Wozniak", 9.34),), counted) == "ok"
    # o cabeçalho só rotula a célula que é só o número: o ano dentro do título continua ano
    titles = "| Filmes | Receita |\n|---|---|\n| Avatar (2009) | R$ 2.900,00 |"
    assert answer(top1, TOP_COLUMNS, TOP_ROWS[:1], titles) == "ok"


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Margem: 25%", "ok"),
        ("Margem: 25 por cento", "ok"),
        ("Margem: 0,25", "ok"),
        ("Margem: 0,25%", "missing"),  # 0,25% é 0,0025
        ("Margem: 25 filmes", "missing"),
        ("Margem: R$ 0,25", "missing"),
    ],
)
def test_percent_only_proves_ratios_and_only_read_as_percent(text: str, verdict: str) -> None:
    check = ResultCheck(Shape.SET, (), (ratio("margem"),))
    assert answer(check, ("margem",), ((0.25,),), text) == verdict


def test_money_and_counts_never_prove_each_other() -> None:
    scalar = ResultCheck(Shape.SET, (), (count("filmes"),))
    assert answer(scalar, ("filmes",), ((3486,),), "São R$ 3.486,00.") == "missing"
    assert answer(scalar, ("filmes",), ((3486,),), "São 3.486 filmes.") == "ok"
    usd = ("receita_total_usd",)
    assert answer(USD, usd, ((1234.5,),), "Receita total (USD): 1.234,50 filmes") == "missing"
    assert answer(USD, usd, ((1234.5,),), "Receita total (USD): 1.234,50%") == "missing"
    assert answer(USD, usd, ((1234.5,),), "Receita total: US$ 1.234,50") == "ok"


def test_written_multipliers_change_the_value() -> None:
    scalar = ResultCheck(Shape.SET, (), (count("filmes"),))
    assert answer(scalar, ("filmes",), ((3,),), "São 3 mil filmes.") == "missing"
    assert answer(scalar, ("filmes",), ((3000,),), "São 3 mil filmes.") == "ok"
    brl = ResultCheck(Shape.SET, (), (money("receita_brl"),))
    text = "Receita: R$ 12,39 bilhões."
    assert answer(brl, ("receita_brl",), ((12_390_000_000.0,),), text) == "ok"
    assert answer(brl, ("receita_brl",), ((12.39,),), text) == "missing"


# --- a evidência precisa ter sido lida antes da resposta final --------------------------------


def test_a_query_run_with_the_final_answer_was_never_read() -> None:
    # No PydanticAI 2 (end_strategy 'graceful'), run_sql e final_answer na MESMA resposta rodam
    # os dois: a consulta executa, mas o texto já estava escrito sem o resultado dela.
    other = execution(("n",), [(3,)], index=1)
    late = execution(("t", "a", "r"), GOOD_TOP, index=2)
    same = score_case(Q01, outcome(executions=[other, late], responses=2), EXPECTED_TOP)
    assert same.verdict is Verdict.FAIL and same.category is FailureCategory.UNGROUNDED
    assert same.detail == "not_read_before_answer"
    read = score_case(Q01, outcome(executions=[other, late], responses=3), EXPECTED_TOP)
    assert read.verdict is Verdict.PASS and read.chosen_query == 2
    only_late = score_case(Q01, outcome(executions=[late], responses=2), EXPECTED_TOP)
    assert only_late.verdict is Verdict.FAIL and only_late.category is FailureCategory.UNGROUNDED
    assert "antes da resposta final" in only_late.reason


def test_a_clarification_must_come_after_the_lookup_result() -> None:
    same = score_case(AMBIGUOUS, ambiguous(lookups=[_homonyms()], responses=1), ELEMENTAL)
    assert same.verdict is Verdict.FAIL and same.category is FailureCategory.POLICY
    later = score_case(AMBIGUOUS, ambiguous(lookups=[_homonyms()], responses=2), ELEMENTAL)
    assert later.verdict is Verdict.PASS, later.reason


# --- linhas estruturadas inventadas -------------------------------------------------------------

INVENTED = "Inventado (2020): R$ 3.000,00"
BULLETS = "\n".join(f"- {line}" for line in GOOD_TEXT.splitlines())
NUMBERED = "\n".join(f"{n}. {line}" for n, line in enumerate(GOOD_TEXT.splitlines(), 1))


@pytest.mark.parametrize(
    ("good", "invented"),
    [
        (GOOD_TEXT, GOOD_TEXT.replace("\nTitanic", f"\n{INVENTED}\nTitanic")),
        (BULLETS, BULLETS.replace("\n- Titanic", f"\n- {INVENTED}\n- Titanic")),
        (NUMBERED, NUMBERED.replace("\n2. Titanic", f"\n- {INVENTED}\n2. Titanic")),
    ],
    ids=["linhas", "bullets", "numerada"],
)
def test_an_invented_structured_row_fails_even_when_every_real_row_is_there(
    good: str, invented: str
) -> None:
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, good) == "ok"
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, invented)
    assert result.verdict == "unsupported", invented
    assert "Inventado" in result.reason
    score = score_case(Q01, outcome(text=invented, executions=[good_query()]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert "não estão no resultado" in score.reason


def test_an_invented_table_row_fails() -> None:
    table = (
        "| Filme | Ano | Receita (R$) |\n|---|---|---|\n| Avatar | 2009 | 2.900,00 |\n"
        "| Titanic | 1997 | 2.200,50 |\n| Dune | 2021 | 400,25 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, table) == "ok"
    extra = table.replace("| Titanic", "| Inventado | 2020 | 3.000,00 |\n| Titanic")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, extra) == "unsupported"


def test_notes_totals_and_prose_are_not_invented_rows() -> None:
    notes = [
        "Os 3 filmes de maior receita, em reais:",  # introdução: sem a forma rótulo: valor
        "Observação: 2 filmes do catálogo não têm receita informada.",
        "Total: R$ 5.500,75",
        "Valores em reais, sem correção pela inflação de 2024.",
    ]
    text = "\n".join([notes[0], GOOD_TEXT, *notes[1:]])
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, text) == "ok"
    # uma frase explicativa fora do bloco de resultado nunca vira linha
    prose = f"{GOOD_TEXT}\n\nPara comparar, Inventado (2020) faturou R$ 3.000,00 em outra fonte."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, prose) == "ok"
    noted = f"{BULLETS}\n- Observação: valores em reais (R$ 1,00 = 1 real)."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, noted) == "ok"
    genres = (
        "Drama: 3 filmes\nAction: 2 filmes\nWar: 0 filmes\nTotal: 5 filmes\nSem gênero: 4 filmes"
    )
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, genres) == "ok"
    invented_genre = genres.replace("Total: 5 filmes", "Western: 9 filmes")
    assert answer(GENRES, GENRE_COLUMNS, GENRE_ROWS, invented_genre) == "unsupported"


def test_cutoff_ties_are_expected_rows_not_extra_ones() -> None:
    lines = [f"{r[2]} ({r[3]}): {r[4]} avaliações" for r in TOP3_WINDOW]
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, "\n".join(lines)) == "ok"  # 5 linhas no top 3
    bullets = "\n".join(f"- {line}" for line in lines)
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, bullets) == "ok"


def test_rows_below_a_leader_are_context_but_never_beat_it() -> None:
    below = "1. Eric Roberts: 65 filmes\n2. Outro Ator: 40 filmes"
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), below) == "ok"
    beating = "- Eric Roberts: 65 filmes\n- Outro Ator: 70 filmes"
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), beating) == "unsupported"
    tied = "- Eric Roberts: 65 filmes\n- Outro Ator: 65 filmes"
    assert answer(LEADER, LEADER_COLUMNS, (("Eric Roberts", 65),), tied) == "unsupported"


# --- apresentações completas não se contradizem -------------------------------------------------


def test_a_wrong_complete_list_followed_by_a_right_one_fails() -> None:
    text = f"{SWAPPED_TEXT}\n\n{GOOD_TEXT}"
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, text)
    assert result.verdict == "contradiction" and "fora de ordem" in result.reason
    score = score_case(Q01, outcome(text=text, executions=[good_query()]), EXPECTED_TOP)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert "contradiz" in score.reason
    # o mesmo com bullets, com a lista errada depois da certa e numa tabela
    swapped = "\n".join(f"- {line}" for line in SWAPPED_TEXT.splitlines())
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{BULLETS}\n\n{swapped}") == "contradiction"
    table = (
        "| Filme | Ano | Receita (R$) |\n|---|---|---|\n| Titanic | 1997 | 2.200,50 |\n"
        "| Avatar | 2009 | 2.900,00 |\n| Dune | 2021 | 400,25 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{table}\n\n{BULLETS}") == "contradiction"


def test_a_complete_list_with_a_wrong_value_fails_next_to_a_right_one() -> None:
    wrong = GOOD_TEXT.replace("2.900,00", "2.500,00")
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{wrong}\n\n{GOOD_TEXT}")
    assert result.verdict == "contradiction" and "Avatar" in result.reason
    # uma linha estruturada isolada com outro valor também contradiz o resultado
    partial = "- Avatar (2009): R$ 2.500,00"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{partial}\n\n{GOOD_TEXT}") == "contradiction"


def test_repeated_right_presentations_and_partial_summaries_pass() -> None:
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{GOOD_TEXT}\n\n{GOOD_TEXT}") == "ok"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{NUMBERED}\n\n{BULLETS}") == "ok"
    summary = "O destaque é Avatar (2009), com R$ 2.900,00."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{summary}\n\n{GOOD_TEXT}") == "ok"
    bullet_summary = "- Destaque: Avatar (2009), com R$ 2.900,00"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{bullet_summary}\n\n{GOOD_TEXT}") == "ok"
    # prosa casual fora de ordem não é uma apresentação estruturada
    casual = "Titanic (1997) e Avatar (2009) dominam a lista."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{casual}\n\n{GOOD_TEXT}") == "ok"


def test_explicit_positions_in_reverse_prose_order_still_follow_the_position_rule() -> None:
    reverse = (
        "Em 3º, Dune (2021), com R$ 400,25; em 2º, Titanic (1997), com R$ 2.200,50; "
        "em 1º, Avatar (2009), com R$ 2.900,00."
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, reverse) == "ok"
    countdown = (
        "3. Dune (2021): R$ 400,25\n2. Titanic (1997): R$ 2.200,50\n1. Avatar (2009): R$ 2.900,00"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{countdown}\n\n{GOOD_TEXT}") == "ok"
    mislabeled = countdown.replace("3. Dune", "1. Dune").replace("1. Avatar", "3. Avatar")
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{mislabeled}\n\n{GOOD_TEXT}") == "contradiction"


# --- gabarito vazio ---------------------------------------------------------------------------

EMPTY_CHECK = ResultCheck(Shape.SET, MOVIE, (money("receita_brl"),))
EMPTY_COLUMNS = ("id_filme", "titulo", "ano_lancamento", "receita_brl")
EMPTY_EXPECTED = ReferenceResult("vazio", "SQL", {}, EMPTY_COLUMNS, ("id_filme",), (), 0.0)


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Nenhum filme foi encontrado com esse critério.", "ok"),
        ("Não há filmes chamados Inventado no catálogo.", "ok"),
        ("Não encontrei nenhum resultado.", "ok"),
        ("A consulta devolveu 0 filmes.", "ok"),
        ("Zero resultados para essa busca.", "ok"),
        ("O catálogo não tem filmes de 2099.", "ok"),
        ("Não há, no catálogo, filmes de 2099.", "ok"),
        ("A consulta não retornou nenhuma linha.", "ok"),
        ("Nada foi encontrado.", "ok"),
        ("Há 123 filmes chamados Inventado.", "missing"),
        ("Não há dúvida: há 123 filmes chamados Inventado.", "missing"),  # negação de outra coisa
        ("Nenhum problema: há 123 filmes chamados Inventado.", "missing"),
        ("Inventado (2020): R$ 3.000,00", "missing"),
        ("Nenhum filme encontrado.\n- Inventado (2020): R$ 3.000,00", "unsupported"),
        ("Não há resultado exato.\n\n| Filme | Ano | Receita |\n|---|---|---|\n"
         "| Inventado | 2020 | 3.000,00 |", "unsupported"),
    ],
)  # fmt: skip
def test_an_empty_result_needs_an_answer_saying_nothing_matched(text: str, verdict: str) -> None:
    assert answer(EMPTY_CHECK, EMPTY_COLUMNS, (), text) == verdict


def test_an_empty_correct_query_with_a_fabricated_answer_fails_the_case() -> None:
    case = replace(Q01, case_id="teste_vazio", check=EMPTY_CHECK)
    empty = execution(("id", "titulo", "ano", "receita"), [])
    fabricated = outcome(text="Há 123 filmes chamados Inventado.", executions=[empty])
    score = score_case(case, fabricated, EMPTY_EXPECTED)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert score.chosen_query == 1 and "nada foi encontrado" in score.reason
    honest = outcome(text="Não há filmes chamados Inventado no catálogo.", executions=[empty])
    passed = score_case(case, honest, EMPTY_EXPECTED)
    assert passed.verdict is Verdict.PASS, passed.reason
    assert passed.answer.verdict == "ok" and passed.answer.required == 0
    wrong_sql = outcome(text="Não há filmes.", executions=[good_query()])  # o SQL achou linhas
    assert score_case(case, wrong_sql, EMPTY_EXPECTED).category is (FailureCategory.RESULT_MISMATCH)


# --- valor distribuído com "ambos"/"todos" ------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "text", "verdict"),
    [
        ((("Drama", 9), ("Comedy", 9)), "Drama e Comédia, ambos com 9 filmes.", "ok"),
        ((("Drama", 9), ("Comedy", 9)), "Drama e Comédia, ambas têm 9 filmes.", "ok"),
        ((("Drama", 9), ("Comedy", 8)), "Drama e Comédia, ambos com 9 filmes.", "missing"),
        ((("Drama", 3), ("Comedy", 3)), "Drama e Comédia, todos com 3 filmes.", "ok"),
        ((("Drama", 3), ("Comedy", 3), ("War", 3)), "Drama, Comédia e Guerra, todos com 3 filmes.",
         "ok"),
        ((("Drama", 3), ("Comedy", 3), ("War", 2)), "Drama, Comédia e Guerra, todos com 3 filmes.",
         "missing"),
        ((("Drama", 3), ("Comedy", 3), ("War", 3)), "Drama, Comédia e Guerra, ambos com 3 filmes.",
         "missing"),  # "ambos" é um par
        ((("Drama", 3), ("Comedy", 3)), "Drama e Comédia, com 3 filmes.", "missing"),
        ((("Drama", 3), ("Comedy", 3)), "Drama e Comédia somam 3 filmes.", "missing"),
    ],
)  # fmt: skip
def test_ambos_and_todos_share_an_explicit_value(rows, text: str, verdict: str) -> None:  # noqa: ANN001
    assert answer(GENRES, GENRE_COLUMNS, rows, text) == verdict


def test_shared_values_keep_labels_and_spans_exclusive() -> None:
    trio = "Alpha, Beta e Gamma, todos de 2020, todos com 10 avaliações."
    assert answer(TRIO, TRIO_COLUMNS, TRIO_ROWS, trio) == "ok"
    # sem o ano de cada um, o valor distribuído não basta
    assert answer(TRIO, TRIO_COLUMNS, TRIO_ROWS, "Alpha, Beta e Gamma, todos com 10.") == "missing"
    names = ResultCheck(Shape.SET, (("ator",),), (count("filmes"),))
    pair = "Ana Lima e Bia Souza, ambas com 9 avaliações"
    assert answer(names, ("ator", "filmes"), (("Ana Lima", 9), ("Bia Souza", 9)), pair) == "ok"
    differing = (("Ana Lima", 9), ("Bia Souza", 7))
    assert answer(names, ("ator", "filmes"), differing, pair) == "missing"


# --- numeração preguiçosa do markdown ---------------------------------------------------------


def test_lazy_markdown_numbering_follows_the_text_order() -> None:
    lazy = "\n".join(f"1. {line}" for line in GOOD_TEXT.splitlines())
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, lazy) == "ok"
    lazy_swapped = "\n".join(f"1. {line}" for line in SWAPPED_TEXT.splitlines())
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, lazy_swapped) == "wrong_order"
    loose = "\n\n".join(f"1) {line}" for line in GOOD_TEXT.splitlines())  # lista solta
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, loose) == "ok"
    # o "1." preguiçoso nunca vale como valor
    review = "\n".join(f"1. {r[2]} ({r[3]}): {r[4]} avaliações" for r in LIST_ROWS[:2])
    assert answer(LIST_CHECK, REVIEW_COLUMNS, LIST_ROWS, f"{review}\n1. Gamma (2003)") == "missing"


def test_repeated_positions_outside_the_lazy_pattern_stay_positions() -> None:
    lines = GOOD_TEXT.splitlines()
    repeated_two = f"1. {lines[0]}\n2. {lines[1]}\n2. {lines[2]}"  # não é tudo 1
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, repeated_two) == "wrong_order"
    ordinals = "\n".join(f"1º {line}" for line in lines)  # "1º" é posição escrita
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, ordinals) == "wrong_order"
    # uma lista de um item só, separada por um parágrafo, não é preguiçosa: o "1." dela é posição
    split = f"1. {lines[1]}\n\nTexto no meio.\n\n1. {lines[0]}\n1. {lines[2]}"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, split) == "wrong_order"
    alone = f"1. {lines[0]}\n\nTexto no meio.\n\n1. {lines[1]}\n1. {lines[2]}"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, alone) == "ok"  # Avatar é mesmo o 1º
    table = (
        "| # | Filme | Ano | Receita (R$) |\n|---|---|---|---|\n| 1 | Avatar | 2009 | 2.900,00 |\n"
        "| 1 | Titanic | 1997 | 2.200,50 |\n| 1 | Dune | 2021 | 400,25 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, table) == "wrong_order"  # coluna de posição


# --- moeda no escopo do resultado -------------------------------------------------------------

TOP1 = ResultCheck(Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=1)


@pytest.mark.parametrize(
    ("text", "context", "verdict"),
    [
        ("Receita em USD: Avatar (2009): 2.900,00", "Moeda: BRL", "missing"),
        ("Receitas em dólares:\n- Avatar (2009): 2.900,00", "Valores em reais (BRL).", "missing"),
        ("## Receita (USD)\n\n- Avatar (2009): 2.900,00", "Moeda: BRL", "missing"),
        ("Avatar (2009): 2.900,00 USD\nValores em reais.", "", "missing"),
        ("Avatar (2009), em dólares: 2.900,00", "Moeda: BRL", "missing"),
        ("Avatar (2009): 2.900,00", "Sem moeda na pergunta: reais (BRL).", "ok"),
        ("Receitas em reais:\n- Avatar (2009): 2.900,00", "", "ok"),
        ("| Filme | Ano | Receita (BRL) |\n|---|---|---|\n| Avatar | 2009 | 2.900,00 |", "", "ok"),
        ("Avatar (2009): R$ 2.900,00 (cerca de US$ 580,00).", "", "ok"),
        ("Avatar (2009): R$ 2.900,00.\nNão consultei os valores em dólares.", "", "ok"),
        ("Avatar (2009): 2.900,00.\nObs.: os valores em USD não foram usados.", "Moeda: BRL",
         "ok"),  # a ressalva sobre dólares não rotula o resultado
    ],
)  # fmt: skip
def test_a_wrong_currency_next_to_the_result_is_not_rescued(
    text: str, context: str, verdict: str
) -> None:
    result = check_answer(TOP1, TOP_COLUMNS, TOP_ROWS[:1], text, context=context)
    assert result.verdict == verdict, (text, result)


@pytest.mark.parametrize(
    ("text", "context", "verdict"),
    [
        ("Receita total em reais: 1.234,50", "Moeda: USD", "missing"),
        ("Em BRL:\n- Receita total: 1.234,50", "Moeda: USD", "missing"),
        ("Receita total: R$ 1.234,50\nValores em dólares.", "", "missing"),
        ("Receita total: 1.234,50", "Valores em dólares (USD).", "ok"),
        ("Receita total (USD): 1.234,50", "", "ok"),
        ("Em dólares:\n- Receita total: 1.234,50", "", "ok"),
    ],
)
def test_usd_mirrors_the_currency_scope_rules(text: str, context: str, verdict: str) -> None:
    result = check_answer(USD, ("receita_total_usd",), ((1234.5,),), text, context=context)
    assert result.verdict == verdict, (text, result)


# --- passada adversarial: formas que escapavam das regras acima --------------------------------


def test_an_invented_row_written_with_a_verb_is_still_a_data_row() -> None:
    verbs = "\n".join(
        f"- {title} ({year}) faturou R$ {value}"
        for title, year, value in (
            ("Avatar", 2009, "2.900,00"),
            ("Inventado", 2020, "3.000,00"),
            ("Titanic", 1997, "2.200,50"),
            ("Dune", 2021, "400,25"),
        )
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, verbs) == "unsupported"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, verbs.replace("- Inventado", "- Avatar")) == (
        "contradiction"  # o mesmo título com outro ano e outro valor
    )
    honest = "\n".join(line for line in verbs.splitlines() if "Inventado" not in line)
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, honest) == "ok"


def test_a_comparison_between_result_rows_is_not_a_row_of_either() -> None:
    compared = f"{GOOD_TEXT}\nAvatar (2009) supera Titanic (1997) em R$ 699,50."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, compared) == "ok"
    bullets = f"{BULLETS}\n- Avatar (2009) supera Titanic (1997) em R$ 699,50."
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, bullets) == "ok"


def test_a_written_position_anywhere_must_be_the_rows_position() -> None:
    lying = "1. Titanic\n2. Avatar\n3. Dune\n\n" + GOOD_TEXT  # nomes sem valor, posições erradas
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, lying)
    assert result.verdict == "contradiction" and "Titanic / 1997 aparece como 1º" in result.reason
    honest = "1. Avatar\n2. Titanic\n3. Dune\n\n" + GOOD_TEXT
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, honest) == "ok"
    prose = "Em primeiro lugar ficou Titanic.\n\n" + GOOD_TEXT
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, prose) == "contradiction"
    # posição densa num empate continua a regra de competição (o 4º lugar empatado vai de 4 a 7)
    tied = "\n".join(
        f"{rank}º {r[2]} ({r[3]}): {r[4]} avaliações" for rank, r in zip(
            (1, 2, 3, 3, 3), TOP3_WINDOW, strict=True)
    )  # fmt: skip
    assert answer(TOP3, REVIEW_COLUMNS, TOP3_WINDOW, tied) == "ok"


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Drama: 3 filmes", "ok"),
        ("Drama: cerca de 3 filmes", "ok"),  # aproximação do próprio valor
        ("Drama: mais de 3 filmes", "missing"),
        ("Drama: pelo menos 3 filmes", "missing"),
        ("Drama: até 3 filmes", "missing"),
        ("Drama: quase 3 filmes", "missing"),
        ("Drama: > 3 filmes", "missing"),
    ],
)
def test_a_bound_is_not_the_value(text: str, verdict: str) -> None:
    assert answer(GENRES, GENRE_COLUMNS, (("Drama", 3),), text) == verdict


def test_a_bound_never_becomes_a_shared_or_money_value() -> None:
    rows = (("Drama", 3), ("Comedy", 3))
    assert answer(GENRES, GENRE_COLUMNS, rows, "Drama e Comédia, todos com mais de 3 filmes.") == (
        "missing"
    )
    assert answer(GENRES, GENRE_COLUMNS, rows, "Drama e Comédia têm mais de 3 filmes cada.") == (
        "missing"
    )
    over = "Avatar (2009): mais de R$ 2.900,00"
    assert answer(TOP1, TOP_COLUMNS, TOP_ROWS[:1], over) == "missing"


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Previsão do tempo não é algo que eu consiga responder; sou focado no catálogo de filmes.",
         Verdict.PASS),
        ("Desculpe, não lido com clima: respondo só perguntas sobre filmes.", Verdict.PASS),
        ("Não tenho acesso, mas amanhã a máxima será de 30.", Verdict.FAIL),
        ("Não tenho acesso a isso. Amanhã fará sol em São Paulo.", Verdict.FAIL),
        ("Não sei se fará sol amanhã: não tenho acesso a dados de clima.", Verdict.PASS),
    ],
)  # fmt: skip
def test_refusals_in_other_words_and_forecasts_in_the_future(text: str, verdict: Verdict) -> None:
    score = score_case(OUT_OF_SCOPE, outcome(status="out_of_scope", text=text))
    assert score.verdict is verdict, score.reason


@pytest.mark.parametrize(
    "text",
    [
        "Posso responder sobre filmes e o catálogo. Temos 95 mil filmes.",
        "O catálogo conta com 95.645 filmes; posso responder sobre receitas e notas.",
    ],
)
def test_catalog_sizes_in_other_words_are_still_data(text: str) -> None:
    score = score_case(CAPABILITIES, outcome(status="info", text=text))
    assert score.verdict is Verdict.FAIL and "sem consultar o banco" in score.reason
    examples = "Posso listar os 10 filmes do catálogo com maior receita."
    assert score_case(CAPABILITIES, outcome(status="info", text=examples)).verdict is Verdict.PASS


def test_a_wrong_value_for_another_row_inside_a_proven_row_breaks_a_complete_presentation() -> None:
    multi = "Avatar (2009): R$ 2.900,00, Titanic (1997): R$ 9.999,00\nDune (2021): R$ 400,25"
    result = check_answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{multi}\n\n{GOOD_TEXT}")
    assert result.verdict == "contradiction" and "Titanic" in result.reason
    # uma comparação junto da linha provada, com as outras linhas certas, continua valendo
    ahead = (
        "1. Avatar (2009): R$ 2.900,00, à frente de Titanic (1997) por R$ 699,50\n"
        "2. Titanic (1997): R$ 2.200,50\n3. Dune (2021): R$ 400,25"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, ahead) == "ok"


@pytest.mark.parametrize(
    ("text", "context", "verdict"),
    [
        ("Valores em USD (a pergunta pediu reais): Avatar (2009): 2.900,00", "Moeda: BRL",
         "missing"),  # o aparte entre parênteses não desfaz o rótulo da frase
        ("Valores em reais (antes estavam em USD): Avatar (2009): 2.900,00", "", "ok"),
        ("Avatar (receita em USD: 2.900,00)", "Moeda: BRL", "missing"),  # o parêntese do número
    ],
)  # fmt: skip
def test_currency_asides_in_parentheses_do_not_label_the_result(
    text: str, context: str, verdict: str
) -> None:
    result = check_answer(TOP1, TOP_COLUMNS, TOP_ROWS[:1], text, context=context)
    assert result.verdict == verdict, (text, result)


def test_a_separate_notes_list_is_not_a_result_block() -> None:
    notes = (
        f"{NUMBERED}\n\nObservações:\n- Filmes considerados: 9.876\n"
        "- Período (2023 a 2026): 3 anos\n- Moeda: reais (BRL)"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, notes) == "ok"


def test_invented_film_rows_fail_in_loose_lists_and_in_separate_lists_or_tables() -> None:
    loose = "\n\n".join(f"- {line}" for line in GOOD_TEXT.replace(
        "\nTitanic", f"\n{INVENTED}\nTitanic").splitlines())  # fmt: skip
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, loose) == "unsupported"
    separate = f"{NUMBERED}\n\nOutros destaques:\n- {INVENTED}"
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, separate) == "unsupported"
    table = (
        f"{NUMBERED}\n\nOutros:\n\n| Filme | Ano | Receita |\n|---|---|---|\n"
        "| Inventado | 2020 | R$ 3.000,00 |"
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, table) == "unsupported"


def test_note_labels_may_carry_a_currency_but_not_a_film_year() -> None:
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, f"{GOOD_TEXT}\nTotal (BRL): R$ 5.500,75") == "ok"
    film = f"{GOOD_TEXT}\nTotal Recall (1990): R$ 9,00"  # um filme, não um total
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, film) == "unsupported"


def test_a_semicolon_inside_parentheses_does_not_split_a_row() -> None:
    rows = "\n".join(
        f"- {title} ({year}; id {movie_id}): R$ {value}"
        for title, year, movie_id, value in (
            ("Avatar", 2009, 10, "2.900,00"),
            ("Titanic", 1997, 11, "2.200,50"),
            ("Dune", 2021, 12, "400,25"),
        )
    )
    assert answer(TOP, TOP_COLUMNS, TOP_ROWS, rows) == "ok"
