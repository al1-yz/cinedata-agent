"""Testes da resolução de entidades (offline: banco sintético e bancos falsos)."""

from __future__ import annotations

import ast
import json
import os
import random
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cinedata import entities as entities_module
from cinedata.db import MAX_BULK_ROWS, QueryResult, QueryTimeoutError, SafeDatabase
from cinedata.entities import (
    BULK_TIMEOUT_S,
    EntityIndex,
    EntityIndexError,
    EntityKind,
    MatchState,
    Reason,
    Role,
    normalize,
)
from gold_db import DEFAULT_GENRES, build_gold_db

FILME, PESSOA, GENERO, PRODUTORA = (
    EntityKind.FILME,
    EntityKind.PESSOA,
    EntityKind.GENERO,
    EntityKind.PRODUTORA,
)


@pytest.fixture(scope="module")
def gold_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_gold_db(tmp_path_factory.mktemp("entities") / "gold.db")


@pytest.fixture
def index(gold_path: Path) -> Iterator[EntityIndex]:
    with SafeDatabase(gold_path) as database:
        yield EntityIndex(database)


def labels(match) -> list[str]:  # noqa: ANN001
    return [candidate.label for candidate in match.candidates]


# --- normalize ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Amélie", "amelie"),
        ("AMÉLIE", "amelie"),
        ("Zoë Saldaña", "zoe saldana"),
        ("O'Brien", "o brien"),
        ("O’Brien", "o brien"),
        ("Spider-Man", "spider man"),
        ("S.W.A.T.", "s w a t"),
        ("Fast & Furious", "fast furious"),
        ("  Padded\t Name\n", "padded name"),
        ("Alexander Chard​", "alexander chard"),
        ("﻿BOM Name", "bom name"),
        ("Bjørk", "bjork"),
        ("BJØRK", "bjork"),
        ("Straße", "strasse"),
        ("Æon Flux", "aeon flux"),
        ("Œuvre", "oeuvre"),
        ("Łódź", "lodz"),
        ("Ðorđe", "dorde"),
        ("Þór", "thor"),
        ("İstanbul", "istanbul"),
        ("ＡＢＣ　１２３", "abc 123"),
        ("Movie 🎬 Premiere", "movie premiere"),
        ("Blade Runner 2049", "blade runner 2049"),
        ("a b", "a b"),
        ("́abc", "abc"),
        ("e" + "́" * 50, "e"),
        ("Ελλάδα", "ελλάδα"),
        ("Йосиф", "йосиф"),
        ("Иосиф", "иосиф"),
        ("がぎぐ", "がぎぐ"),
        ("ｶﾞ", "ガ"),
        ("千と千尋の神隠し", "千と千尋の神隠し"),
        ("№5", "no5"),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    assert normalize(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("★", "★"),
        (" ★ ", "★"),
        ("★️", "★"),
        ("________", "________"),
        ("...", "..."),
        ("$$$", "$$$"),
        ("🎬", "🎬"),
        ("", ""),
        ("   \t\n", ""),
        ("​", ""),
        ("﻿‍", ""),
        ("́", ""),
        ("\x00", ""),
    ],
)
def test_symbol_only_text_keeps_a_useful_key_and_empty_only_when_nothing_is_left(
    text: str, expected: str
) -> None:
    assert normalize(text) == expected


def test_scripts_with_essential_marks_keep_them() -> None:
    hebrew = "שָׁלוֹם"
    marks = sum(unicodedata.category(c) == "Mn" for c in unicodedata.normalize("NFD", hebrew))
    assert marks > 0
    assert sum(unicodedata.category(c) == "Mn" for c in normalize(hebrew)) == marks
    assert normalize(hebrew) != normalize("שלום")
    assert normalize("がぎぐ") != normalize("かきく")
    assert normalize("Йосиф") != normalize("Иосиф")


def test_marks_follow_the_script_of_the_letter_before_them() -> None:
    assert normalize("का") != normalize("क")  # vogal indiana (Mc): faz parte da palavra
    assert normalize("9́") == "9"  # depois de dígito, a marca some como numa letra latina
    assert normalize("あ ゙") == "あ"  # marca solta depois de espaço não é de letra nenhuma
    nameless = next(
        (
            chr(code)
            for code in range(0x17000, 0x18900)
            if unicodedata.category(chr(code)).startswith("L")
            and not unicodedata.name(chr(code), "")
        ),
        None,
    )
    if nameless is None:
        pytest.skip("esta versão do Unicode dá nome a todas as letras do intervalo testado")
    assert normalize(nameless + "́") != normalize(nameless)  # na dúvida, preserva a marca


def test_a_zero_width_character_inside_a_word_joins_it() -> None:
    assert normalize("Chard​onnay") == "chardonnay"


@pytest.mark.parametrize("separator", ["\t", "\n", " ", " ", "　", "  "])
def test_symbols_separated_by_any_whitespace_collapse_to_one_space(separator: str) -> None:
    assert normalize(f"★{separator}☆") == "★ ☆"


@pytest.mark.parametrize(
    ("low", "high"),
    [
        (0x3040, 0x30FF),  # hiragana e katakana
        (0x3400, 0x4DBF),  # ideogramas, extensão A
        (0x4E00, 0x9FFF),  # ideogramas unificados
        (0xAC00, 0xD7AF),  # sílabas hangul
        (0x1100, 0x11FF),  # jamo
        (0xFF66, 0xFF9F),  # katakana de meia largura
    ],
)
def test_the_cjk_ranges_are_exact(low: int, high: int) -> None:
    has_cjk = entities_module._has_cjk
    assert has_cjk(chr(low)) and has_cjk(chr(high)) and has_cjk("a" + chr((low + high) // 2))
    assert not has_cjk(chr(low - 1)) and not has_cjk(chr(high + 1))


def test_the_ascii_shortcut_agrees_with_the_unicode_path() -> None:
    rng = random.Random(7)  # noqa: S311
    pool = "abcXYZ019 -_.,;:'\"!?&/\\()[]{}+*#@%$\t\n"
    for _ in range(2000):
        text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 15)))
        assert normalize(text) == entities_module._normalize_unicode(text)


def test_normalize_is_idempotent_on_tricky_input() -> None:
    rng = random.Random(20261002)  # noqa: S311
    pool = list("aAbZ09 -_.'\"!&é ß ø İ ς ★ ☆ 🎬 ＡＢ １ ガ ｶ ゙ שָׁ й Ё ё 千 한 ​ ́   ﻿ 　 ﷽ ǅ ´ № Ⅻ ∑")
    for _ in range(4000):
        text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 12)))
        once = normalize(text)
        assert normalize(once) == once, repr(text)


def test_normalize_stays_fast_and_bounded_on_hostile_input() -> None:
    started = time.perf_counter()
    for text in ("a" + "́" * 100_000, "ﷺ" * 5_000, "​" * 100_000, "é" * 100_000):
        assert len(normalize(text)) < 700_000
    assert time.perf_counter() - started < 10


# --- validação de argumentos ----------------------------------------------------------------------


def test_kind_and_role_accept_enums_strings_accents_and_aliases(index: EntityIndex) -> None:
    assert index.find("GÊNERO", "action").state is MatchState.EXACT_UNIQUE
    assert index.find("Pessoa", "Wes Anderson", role="DIRETOR").state is MatchState.EXACT_UNIQUE
    assert index.find(PESSOA, "Larry Rosen", role="writer").resolved.role == "Roteirista"  # type: ignore[union-attr]
    assert index.find(PESSOA, "Larry Rosen", role=Role.ATOR).resolved.role == "Ator"  # type: ignore[union-attr]
    assert index.find(PESSOA, "Larry Rosen", role="actress").resolved.role == "Ator"  # type: ignore[union-attr]


ROLE_ALIASES = {
    "diretor": "Diretor",
    "Diretora": "Diretor",
    "director": "Diretor",
    "ator": "Ator",
    "atriz": "Ator",
    "actor": "Ator",
    "Actress": "Ator",
    "roteirista": "Roteirista",
    "writer": "Roteirista",
    "Screenwriter": "Roteirista",
}


@pytest.mark.parametrize(("alias", "role"), list(ROLE_ALIASES.items()))
def test_every_role_alias_maps_to_its_role(index: EntityIndex, alias: str, role: str) -> None:
    match = index.find(PESSOA, "Christopher Nolan", role=alias)
    assert match.resolved is not None and match.resolved.role == role


def test_the_role_alias_table_is_fully_covered() -> None:
    covered = {normalize(alias): role for alias, role in ROLE_ALIASES.items()}
    assert covered == {alias: role.value for alias, role in entities_module._ROLE_NAMES.items()}


@pytest.mark.parametrize("kind", ["xyz", "", None, 5, "filmes"])
def test_unknown_kind_lists_the_valid_ones(index: EntityIndex, kind: object) -> None:
    with pytest.raises(ValueError, match="Valores aceitos: filme, pessoa, genero, produtora"):
        index.find(kind, "x")  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["xyz", "", 5, "produtor"])
def test_unknown_role_lists_the_valid_ones(index: EntityIndex, role: object) -> None:
    with pytest.raises(ValueError, match="Valores aceitos: Diretor, Ator, Roteirista"):
        index.find(PESSOA, "x", role=role)  # type: ignore[arg-type]


def test_role_only_applies_to_people(index: EntityIndex) -> None:
    with pytest.raises(ValueError, match="só se aplica ao tipo pessoa"):
        index.find(FILME, "Avatar", role="ator")


@pytest.mark.parametrize("limit", [0, -1, 26, True, 1.5, "5"])
def test_invalid_max_candidates(index: EntityIndex, limit: object) -> None:
    with pytest.raises(ValueError, match="max_candidates"):
        index.find(FILME, "Avatar", max_candidates=limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_candidates"):
        EntityIndex(index._db, max_candidates=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("query", [None, 123, b"Avatar", ["Avatar"], 1.5])
def test_a_non_text_query_is_a_type_error(index: EntityIndex, query: object) -> None:
    with pytest.raises(TypeError, match="texto"):
        index.find(FILME, query)  # type: ignore[arg-type]


# --- entradas vazias, curtas, longas e estranhas --------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t", "​", "﻿", "́", "​ ‍"])
def test_empty_means_there_is_no_useful_key(index: EntityIndex, query: str) -> None:
    match = index.find(FILME, query)
    assert (match.state, match.reason, match.normalized_query) == (
        MatchState.NONE,
        Reason.EMPTY,
        "",
    )
    assert match.resolved is None


@pytest.mark.parametrize("query", ["★", " ★ ", "★️", "________"])
def test_symbol_only_titles_are_findable(index: EntityIndex, query: str) -> None:
    match = index.find(FILME, query)
    assert match.state is MatchState.EXACT_UNIQUE
    assert match.resolved is not None
    assert match.resolved.label in {"★", "________"}


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("!!!", Reason.NO_MATCH),
        ("???", Reason.NO_MATCH),
        ("☆", Reason.TOO_SHORT_FOR_PARTIAL),  # 1 caractere: só a busca exata se aplica
        ("🎭", Reason.TOO_SHORT_FOR_PARTIAL),
    ],
)
def test_symbol_only_queries_that_match_nothing_are_not_reported_as_empty(
    index: EntityIndex, query: str, reason: Reason
) -> None:
    match = index.find(FILME, query)
    assert (match.state, match.reason) == (MatchState.NONE, reason)
    assert match.normalized_query != ""


def test_too_long_input_is_reported_not_truncated(index: EntityIndex) -> None:
    ok = index.find(FILME, "a" * 200)
    assert ok.reason is Reason.NO_MATCH
    too_long = index.find(FILME, "a" * 201)
    assert (too_long.state, too_long.reason) == (MatchState.NONE, Reason.TOO_LONG)
    assert len(too_long.query) == 200  # só o texto de exibição é cortado
    expands = index.find(FILME, "ﷺ" * 30)  # 30 caracteres que viram mais de 400 depois do NFKD
    assert expands.reason is Reason.TOO_LONG


def test_the_normalized_length_limit_is_exact(index: EntityIndex) -> None:
    at_limit = index.find(FILME, "㍿" * 100)  # cada ㍿ vira 4 caracteres: 400, no limite
    assert len(at_limit.normalized_query) == 400 and at_limit.reason is Reason.NO_MATCH
    over = index.find(FILME, "㍿" * 100 + "a")
    assert len(over.normalized_query) == 401 and over.reason is Reason.TOO_LONG


@pytest.mark.parametrize("query", ["abc\x00def", "ab\ud800", "\x00"])
def test_invalid_text_is_rejected_like_in_the_database_layer(
    index: EntityIndex, query: str
) -> None:
    match = index.find(FILME, query)
    assert (match.state, match.reason) == (MatchState.NONE, Reason.INVALID_TEXT)


def test_one_character_queries_only_match_exactly(index: EntityIndex) -> None:
    exact = index.find(FILME, "9")
    assert exact.state is MatchState.EXACT_UNIQUE
    assert exact.resolved is not None and exact.resolved.movie_id == "9999"
    short = index.find(FILME, "a")
    assert (short.state, short.reason) == (MatchState.NONE, Reason.TOO_SHORT_FOR_PARTIAL)
    assert index.find(FILME, "na").reason is Reason.TOO_SHORT_FOR_PARTIAL


def test_two_character_cjk_queries_can_be_partial(index: EntityIndex) -> None:
    match = index.find(PESSOA, "宮崎")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert [c.role for c in match.candidates] == ["Diretor", "Roteirista"]


def test_a_single_cjk_character_is_too_short_for_partial(index: EntityIndex) -> None:
    match = index.find(FILME, "千")
    assert (match.state, match.reason) == (MatchState.NONE, Reason.TOO_SHORT_FOR_PARTIAL)


# --- os estados: filmes ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["Avatar", "avatar", "AVATAR", "  Avatar  ", "Ávatar"])
def test_exact_unique_resolves_with_every_disambiguator(index: EntityIndex, query: str) -> None:
    match = index.find(FILME, query)
    assert match.state is MatchState.EXACT_UNIQUE
    assert match.resolved is not None and match.candidates == (match.resolved,)
    resolved = match.resolved
    assert (resolved.label, resolved.year, resolved.movie_id) == ("Avatar", 2009, "14564")
    assert (resolved.kind, resolved.key, resolved.matched_via) == (FILME, "m01", "exact")
    assert match.total_matches == 1 and match.reason is None


def test_an_exact_title_wins_over_longer_titles_that_contain_it(index: EntityIndex) -> None:
    assert labels(index.find(FILME, "Her")) == ["Her"]


def test_homonym_films_stay_ambiguous_and_ordered_by_year_then_id(index: EntityIndex) -> None:
    match = index.find(FILME, "dune")
    assert match.state is MatchState.EXACT_MULTIPLE
    assert match.resolved is None
    assert [(c.year, c.movie_id) for c in match.candidates] == [
        (1984, "100"),
        (2021, "101"),
        (2021, "102"),
    ]
    assert match.total_matches == 3


def test_titles_that_only_differ_by_accent_are_not_collapsed(index: EntityIndex) -> None:
    for query in ("Amélie", "amelie", "AMELIE"):
        match = index.find(FILME, query)
        assert match.state is MatchState.EXACT_MULTIPLE
        assert labels(match) == ["Amélie", "Amelie"]  # mesmo ano: o id numérico desempata


def test_titles_that_differ_only_by_punctuation_stay_separate_entities(index: EntityIndex) -> None:
    match = index.find(FILME, "spider man")
    assert match.state is MatchState.EXACT_MULTIPLE
    assert [c.year for c in match.candidates] == [2002, 2017]


def test_a_title_repeated_many_times_is_capped_but_counted(index: EntityIndex) -> None:
    match = index.find(FILME, "Silence")
    assert match.state is MatchState.EXACT_MULTIPLE
    assert (len(match.candidates), match.total_matches) == (10, 40)
    years = [c.year for c in match.candidates]
    assert years == sorted(years)
    assert len({c.movie_id for c in match.candidates}) == 10  # o ano repete; o id não
    assert [c.movie_id for c in match.candidates[:4]] == ["s000", "s010", "s020", "s030"]
    small = index.find(FILME, "Silence", max_candidates=2)
    assert (len(small.candidates), small.total_matches) == (2, 40)
    assert len(index.find(FILME, "Silence", max_candidates=25).candidates) == 25


def test_partial_never_resolves_even_with_a_single_candidate(index: EntityIndex) -> None:
    for query in ("godard", "miyazaki"):
        match = index.find(PESSOA, query)
        assert match.state is MatchState.PARTIAL_CANDIDATES
        assert len(match.candidates) == 1 and match.total_matches == 1
        assert match.resolved is None


def test_partial_ranks_prefix_then_fewer_extra_tokens_then_shorter(index: EntityIndex) -> None:
    star = index.find(FILME, "star war")
    assert labels(star)[0].startswith("Star Wars: Episode IV")
    assert [c.matched_via for c in star.candidates] == ["prefix", "prefix"]
    blade = index.find(FILME, "blade")
    assert labels(blade) == ["Blade Runner", "Blade Runner 2049"]
    spider = index.find(FILME, "spider")
    assert [c.year for c in spider.candidates] == [2002, 2017]


def test_partial_matches_tokens_in_any_order(index: EntityIndex) -> None:
    match = index.find(FILME, "wars star")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert len(match.candidates) == 2
    assert {c.matched_via for c in match.candidates} == {"tokens"}


# --- os estados: pessoas --------------------------------------------------------------------------


def test_partial_accepts_a_prefix_of_any_token_not_only_the_last(index: EntityIndex) -> None:
    match = index.find(PESSOA, "chris nolan")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert labels(match) == ["Christopher Nolan"] * 3
    assert {c.matched_via for c in match.candidates} == {"tokens"}


def test_partial_results_are_capped_but_counted_and_ordered(index: EntityIndex) -> None:
    match = index.find(FILME, "silenc")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert (len(match.candidates), match.total_matches) == (10, 40)
    years = [c.year for c in match.candidates]
    assert years == sorted(years)
    assert len(index.find(FILME, "silenc", max_candidates=3).candidates) == 3


def test_movie_ids_are_ordered_numerically_not_alphabetically(index: EntityIndex) -> None:
    match = index.find(FILME, "heat")
    assert match.state is MatchState.EXACT_MULTIPLE
    assert [(c.year, c.movie_id) for c in match.candidates] == [(1995, "8"), (1995, "70")]


def test_the_same_name_in_several_roles_is_ambiguous_without_a_role(index: EntityIndex) -> None:
    match = index.find(PESSOA, "christopher nolan")
    assert match.state is MatchState.EXACT_MULTIPLE
    assert [c.role for c in match.candidates] == ["Ator", "Diretor", "Roteirista"]
    assert match.resolved is None


@pytest.mark.parametrize(
    ("role", "key"), [("diretor", "p01"), ("Roteirista", "p02"), (Role.ATOR, "p03")]
)
def test_a_role_filter_resolves_the_homonym(index: EntityIndex, role: object, key: str) -> None:
    match = index.find(PESSOA, "Christopher Nolan", role=role)  # type: ignore[arg-type]
    assert match.state is MatchState.EXACT_UNIQUE
    assert match.resolved is not None and match.resolved.key == key


def test_a_name_that_exists_only_in_other_roles_says_so(index: EntityIndex) -> None:
    match = index.find(PESSOA, "Hayao Miyazaki", role="ator")
    assert (match.state, match.reason) == (MatchState.NONE, Reason.EXISTS_IN_OTHER_ROLE)
    assert match.detail == "existe como: Diretor"
    both = index.find(PESSOA, "Larry Rosen", role="diretor")
    assert both.state is MatchState.EXACT_UNIQUE


def test_distinct_people_with_the_same_name_in_the_same_role_are_never_merged(
    index: EntityIndex,
) -> None:
    for query in ("zoe saldana", "Zoë Saldaña", "ZOE SALDAÑA"):
        match = index.find(PESSOA, query, role="ator")
        assert match.state is MatchState.EXACT_MULTIPLE
        assert [c.key for c in match.candidates] == ["p07", "p06"]


def test_labels_keep_the_exact_database_spelling(index: EntityIndex) -> None:
    assert index.find(PESSOA, "alexander chard").resolved.label == "Alexander Chard​"  # type: ignore[union-attr]
    assert index.find(PESSOA, "padded name").resolved.label == "  Padded   Name "  # type: ignore[union-attr]
    assert index.find(PESSOA, "BJORK").resolved.label == "Bjørk"  # type: ignore[union-attr]
    assert index.find(PESSOA, "o brien").resolved.label == "O'Brien"  # type: ignore[union-attr]


def test_non_latin_names_are_not_collapsed(index: EntityIndex) -> None:
    assert index.find(PESSOA, "がぎぐ").resolved.key == "p15"  # type: ignore[union-attr]
    assert index.find(PESSOA, "かきく").resolved.key == "p16"  # type: ignore[union-attr]
    assert index.find(PESSOA, "Йосиф").resolved.key == "p17"  # type: ignore[union-attr]
    assert index.find(PESSOA, "Иосиф").resolved.key == "p18"  # type: ignore[union-attr]
    assert index.find(PESSOA, "שָׁלוֹם").resolved.key == "p14"  # type: ignore[union-attr]
    assert index.find(PESSOA, "שלום").resolved is None
    assert index.find(PESSOA, "宮崎 駿", role="diretor").resolved.key == "p09"  # type: ignore[union-attr]


def test_partial_ranking_and_the_role_filter(index: EntityIndex) -> None:
    match = index.find(PESSOA, "nolan")
    assert match.state is MatchState.PARTIAL_CANDIDATES and match.total_matches == 5
    assert labels(match) == [
        "Nolan Gould",
        "Jonathan Nolan",
        "Christopher Nolan",
        "Christopher Nolan",
        "Christopher Nolan",
    ]
    assert [c.matched_via for c in match.candidates][:2] == ["prefix", "tokens"]
    only_writers = index.find(PESSOA, "nolan", role="roteirista")
    assert labels(only_writers) == ["Jonathan Nolan", "Christopher Nolan"]


def test_first_name_prefixes_find_longer_names(index: EntityIndex) -> None:
    match = index.find(PESSOA, "wes")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert set(labels(match)) == {"Wes Anderson", "Wes Craven", "Wesley Snipes"}
    assert match.candidates[0].label in {"Wes Anderson", "Wes Craven"}  # "wes" inteiro vem antes


# --- fuzzy ----------------------------------------------------------------------------------------


def test_fuzzy_only_suggests_and_reports_the_edits(index: EntityIndex) -> None:
    match = index.find(PESSOA, "Cristopher Nolan")
    assert match.state is MatchState.FUZZY_SUGGESTIONS
    assert match.resolved is None
    assert labels(match) == ["Christopher Nolan"] * 3
    assert {(c.matched_via, c.edits) for c in match.candidates} == {("fuzzy", 1)}
    assert index.find(PESSOA, "Cristopher Nolan", role="diretor").total_matches == 1


@pytest.mark.parametrize(
    "query", ["avtar", "avatra", "avater", "avvatar", "Avatr", "zvatar", "vatar"]
)
def test_fuzzy_handles_substitution_deletion_insertion_and_transposition(
    index: EntityIndex, query: str
) -> None:
    match = index.find(FILME, query)
    assert match.state is MatchState.FUZZY_SUGGESTIONS
    assert match.candidates[0].label == "Avatar"
    assert match.candidates[0].edits == 1


def test_fuzzy_can_fix_more_than_one_token(index: EntityIndex) -> None:
    match = index.find(PESSOA, "chrstopher nolen")
    assert match.state is MatchState.FUZZY_SUGGESTIONS
    assert match.candidates[0].label == "Christopher Nolan"
    assert match.candidates[0].edits == 2


def test_nothing_close_is_none(index: EntityIndex) -> None:
    for query in ("qqqqqqqq", "xyzzy plugh"):
        match = index.find(FILME, query)
        assert (match.state, match.reason) == (MatchState.NONE, Reason.NO_MATCH)
    assert index.find(FILME, "swa").reason is Reason.NO_MATCH  # curto demais para o fuzzy
    two_edits = index.find(FILME, "avtor")  # 5 letras toleram 1 edição; "avatar" está a 2
    assert (two_edits.state, two_edits.reason) == (MatchState.NONE, Reason.NO_MATCH)
    assert index.find(FILME, "avatorr").state is MatchState.FUZZY_SUGGESTIONS  # 7 letras: até 2
    hor = index.find(FILME, "hor")  # a 1 edição de "her", mas 3 caracteres não vão ao fuzzy
    assert (hor.state, hor.reason) == (MatchState.NONE, Reason.NO_MATCH)


def test_fuzzy_size_boundaries(index: EntityIndex) -> None:
    four = index.find(FILME, "heet")  # 4 caracteres: o menor texto que vai ao fuzzy
    assert four.state is MatchState.FUZZY_SUGGESTIONS and labels(four) == ["Heat", "Heat"]
    three_letter_token = index.find(FILME, "star wrs")  # um token de 3 letras já é corrigido
    assert three_letter_token.state is MatchState.FUZZY_SUGGESTIONS
    assert labels(three_letter_token)[0].startswith("Star Wars: Episode IV")
    two_letter_token = index.find(FILME, "star wars ix")  # um de 2 letras só casa inteiro
    assert (two_letter_token.state, two_letter_token.reason) == (MatchState.NONE, Reason.NO_MATCH)
    six = index.find(FILME, "avtaor")  # 6 letras já toleram 2 edições (transposição + troca)
    assert six.state is MatchState.FUZZY_SUGGESTIONS
    assert (six.candidates[0].label, six.candidates[0].edits) == ("Avatar", 2)


def test_short_tokens_match_whole_and_longer_ones_also_as_prefix(index: EntityIndex) -> None:
    whole = index.find(FILME, "wars iv")  # "iv" tem 2 letras: só casa inteiro, e existe inteiro
    assert whole.state is MatchState.PARTIAL_CANDIDATES
    assert labels(whole) == ["Star Wars: Episode IV - A New Hope"]
    cut = index.find(FILME, "wars ep")  # "ep" não vale como prefixo de "episode"
    assert (cut.state, cut.reason) == (MatchState.NONE, Reason.NO_MATCH)
    prefix = index.find(FILME, "wars epi")  # 3 letras já valem como prefixo
    assert prefix.state is MatchState.PARTIAL_CANDIDATES and len(prefix.candidates) == 2


# --- gêneros e produtoras -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "label", "via"),
    [
        ("Action", "Action", "exact"),
        ("action", "Action", "exact"),
        ("TV MOVIE", "Tv Movie", "exact"),
        ("Science Fiction", "Science Fiction", "exact"),
        ("Crime", "Crime", "exact"),
        ("Ação", "Action", "alias"),
        ("acao", "Action", "alias"),
        ("Aventura", "Adventure", "alias"),
        ("Terror", "Horror", "alias"),
        ("Suspense", "Thriller", "alias"),
        ("Ficção Científica", "Science Fiction", "alias"),
        ("sci-fi", "Science Fiction", "alias"),
        ("SciFi", "Science Fiction", "alias"),
        ("Cinema TV", "Tv Movie", "alias"),
        ("Filme de TV", "Tv Movie", "alias"),
        ("Faroeste", "Western", "alias"),
        ("Guerra", "War", "alias"),
        ("Música", "Music", "alias"),
        ("Mistério", "Mystery", "alias"),
    ],
)
def test_genres_resolve_by_english_name_or_portuguese_alias(
    index: EntityIndex, query: str, label: str, via: str
) -> None:
    match = index.find(GENERO, query)
    assert match.state is MatchState.EXACT_UNIQUE
    assert match.resolved is not None
    assert (match.resolved.label, match.resolved.matched_via) == (label, via)


def test_every_alias_points_to_one_of_the_real_genres() -> None:
    real = {normalize(name) for _, name in DEFAULT_GENRES}
    for alias, target in entities_module._GENRE_ALIASES.items():
        assert normalize(target) in real, alias


def test_genre_partials_dedupe_the_alias_and_the_real_name(index: EntityIndex) -> None:
    sci = index.find(GENERO, "sci")
    assert (sci.total_matches, labels(sci)) == (1, ["Science Fiction"])
    fic = index.find(GENERO, "fic")
    assert labels(fic) == ["Science Fiction"] and fic.candidates[0].matched_via == "alias"
    fantas = index.find(GENERO, "fantas")
    assert labels(fantas) == ["Fantasy"] and fantas.candidates[0].matched_via == "prefix"


def test_an_alias_whose_target_is_missing_in_the_database_is_ignored(tmp_path: Path) -> None:
    path = build_gold_db(
        tmp_path / "sem_western.db", genres=[g for g in DEFAULT_GENRES if g[1] != "Western"]
    )
    with SafeDatabase(path) as database:
        index = EntityIndex(database)
        assert index.find(GENERO, "Faroeste").state is MatchState.NONE
        assert index.find(GENERO, "Guerra").state is MatchState.EXACT_UNIQUE


def test_two_aliases_with_the_same_key_do_not_make_a_genre_ambiguous(
    gold_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aliases = {**entities_module._GENRE_ALIASES, "Sci Fi": "Science Fiction"}
    monkeypatch.setattr(entities_module, "_GENRE_ALIASES", aliases)
    with SafeDatabase(gold_path) as database:
        match = EntityIndex(database).find(GENERO, "sci fi")
    assert match.state is MatchState.EXACT_UNIQUE


def test_an_alias_equal_to_a_real_genre_name_never_steals_it(
    gold_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities_module, "_GENRE_ALIASES", {"Drama": "Comedy"})
    with SafeDatabase(gold_path) as database:
        resolved = EntityIndex(database).find(GENERO, "drama").resolved
    assert resolved is not None and (resolved.label, resolved.matched_via) == ("Drama", "exact")


def test_companies_keep_homonyms_apart(index: EntityIndex) -> None:
    cafe = index.find(PRODUTORA, "cafe filmes")
    assert cafe.state is MatchState.EXACT_MULTIPLE
    assert labels(cafe) == ["CAFE FILMES", "Café Filmes"]
    warner = index.find(PRODUTORA, "Warner Bros")
    assert warner.state is MatchState.EXACT_MULTIPLE
    assert labels(warner) == ["Warner Bros", "Warner Bros."]
    assert index.find(PRODUTORA, "emile productions").resolved.label == "Émile 🎬 Productions"  # type: ignore[union-attr]
    assert index.find(PRODUTORA, "pixar").state is MatchState.EXACT_UNIQUE


# --- ranking e varredura em bancos sob medida -----------------------------------------------------


@pytest.fixture
def films(tmp_path: Path) -> Iterator[Callable[[list[str]], EntityIndex]]:
    """Fábrica de índices de filmes: um banco novo por chamada, só com os títulos dados."""
    opened: list[SafeDatabase] = []

    def make(titles: list[str]) -> EntityIndex:
        movies = [(f"m{n:03d}", str(n + 1), title, 2000) for n, title in enumerate(titles)]
        database = SafeDatabase(build_gold_db(tmp_path / f"films{len(opened)}.db", movies=movies))
        opened.append(database)
        return EntityIndex(database)

    yield make
    for database in opened:
        database.close()


def test_a_short_token_never_matches_as_a_prefix_even_when_it_exists(
    films: Callable[[list[str]], EntityIndex],
) -> None:
    index = films(["Star Wars Ep IV", "Ivan Wars", "Rocky IV", "Rambo IV", "Alien IV"])
    # "wars" é mais seletivo que "iv", então as candidatas vêm dele e "Ivan" (prefixo de "iv")
    # chega à checagem; "iv" tem 2 letras e só pode casar inteiro.
    assert labels(index.find(FILME, "wars iv")) == ["Star Wars Ep IV"]


def test_partial_ranks_whole_tokens_before_prefixed_ones(
    films: Callable[[list[str]], EntityIndex],
) -> None:
    index = films(["Runner Blade Extra", "Runner Bla Extra Words"])
    match = index.find(FILME, "bla runner")
    assert match.state is MatchState.PARTIAL_CANDIDATES
    assert labels(match) == [
        "Runner Bla Extra Words",
        "Runner Blade Extra",
    ]  # inteiro antes de prefixo


def test_partial_ranks_fewer_extra_tokens_before_a_shorter_title(
    films: Callable[[list[str]], EntityIndex],
) -> None:
    index = films(["A Star B Wars", "Star Wars Superlongwordhere"])
    match = index.find(FILME, "wars star")
    assert labels(match) == ["Star Wars Superlongwordhere", "A Star B Wars"]


def test_the_scan_starts_from_the_most_selective_token(
    films: Callable[[list[str]], EntityIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities_module, "SCAN_ROW_LIMIT", 5)
    index = films(["Silence"] * 40 + ["Silence Zebra"])
    match = index.find(FILME, "zebra silence")  # "zebra" tem 1 linha; "silence", 41
    assert labels(match) == ["Silence Zebra"]
    assert match.total_matches == 1 and match.total_is_lower_bound is False


@pytest.mark.parametrize(("count", "cut"), [(4, False), (5, False), (6, True)])
def test_the_scan_limit_is_exact_and_the_cut_is_applied(
    films: Callable[[list[str]], EntityIndex],
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    cut: bool,
) -> None:
    monkeypatch.setattr(entities_module, "SCAN_ROW_LIMIT", 5)
    match = films(["Silence Night"] * count).find(FILME, "night silence")
    assert match.total_matches == min(count, 5)
    assert match.total_is_lower_bound is cut


def test_a_token_prefix_with_too_many_expansions_stops_being_a_prefix(
    films: Callable[[list[str]], EntityIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    index = films(["Alpha Zed", "Alpha Yak", "Alphabet One", "Alphanumeric Three"])
    # "alpha" expande para 3 tokens (alpha, alphabet, alphanumeric); "one" é o mais seletivo.
    monkeypatch.setattr(entities_module, "PREFIX_EXPANSION_CAP", 3)
    within = index.find(FILME, "one alpha")
    assert within.state is MatchState.PARTIAL_CANDIDATES and labels(within) == ["Alphabet One"]
    monkeypatch.setattr(entities_module, "PREFIX_EXPANSION_CAP", 2)
    over = index.find(FILME, "one alpha")
    assert (over.state, over.reason) == (MatchState.NONE, Reason.NO_MATCH)


def test_fuzzy_counts_the_cheapest_token_of_the_row_per_query_token(
    films: Callable[[list[str]], EntityIndex],
) -> None:
    # "testor" está a 1 edição de "tester" e a 2 de "testes", e os dois estão na mesma linha
    match = films(["Tester Testes"]).find(FILME, "testor")
    assert match.state is MatchState.FUZZY_SUGGESTIONS
    assert (match.candidates[0].label, match.candidates[0].edits) == ("Tester Testes", 1)


def test_fuzzy_prefers_more_exact_tokens_when_the_total_edits_tie(
    films: Callable[[list[str]], EntityIndex],
) -> None:
    index = films(["Plastik Monstar", "Plastic Xxmonster"])  # as duas custam 2 edições no total
    match = index.find(FILME, "plastic monster")
    assert match.state is MatchState.FUZZY_SUGGESTIONS
    assert [c.edits for c in match.candidates] == [2, 2]
    # a segunda tem um token exato e é mais longa; mesmo assim vem primeiro
    assert labels(match) == ["Plastic Xxmonster", "Plastik Monstar"]


def test_fuzzy_keeps_the_closest_neighbors_when_there_are_too_many(
    films: Callable[[list[str]], EntityIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities_module, "FUZZY_MAX_NEIGHBORS", 1)
    # "zonkey" está a 1 edição de "monkey"; "aaonkey", a 2 (e vem antes na ordem alfabética)
    match = films(["Aaonkey", "Zonkey"]).find(FILME, "monkey")
    assert labels(match) == ["Zonkey"] and match.candidates[0].edits == 1


def test_tokens_under_three_letters_get_no_edit_tolerance(index: EntityIndex) -> None:
    # "he" estaria a 1 edição de "her" e de "the", mas só casaria se existisse inteiro
    match = index.find(FILME, "he smell")
    assert (match.state, match.reason) == (MatchState.NONE, Reason.NO_MATCH)


# --- carga sob demanda, falhas e concorrência -----------------------------------------------------


class CountingDB:
    """Embrulha um SafeDatabase real e conta as consultas (com uma pausa que abre a corrida)."""

    def __init__(self, database: SafeDatabase, pause: float = 0.0) -> None:
        self._database = database
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._pause = pause
        self._lock = threading.Lock()

    @property
    def timeout_s(self) -> float:
        return self._database.timeout_s

    def execute(self, sql: str, **overrides: object) -> QueryResult:
        with self._lock:
            self.calls.append((sql, overrides))
        time.sleep(self._pause)
        return self._database.execute(sql, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def counting(gold_path: Path) -> Iterator[CountingDB]:
    with SafeDatabase(gold_path) as database:
        yield CountingDB(database)


def test_nothing_is_read_until_the_first_search_and_each_kind_loads_once(
    counting: CountingDB,
) -> None:
    index = EntityIndex(counting)  # type: ignore[arg-type]
    assert counting.calls == [] and index.stats() == {}
    index.find(GENERO, "Action")
    index.find(GENERO, "Drama")
    assert len(counting.calls) == 1 and set(index.stats()) == {GENERO}
    index.find(FILME, "Avatar")
    index.find(FILME, "Dune")
    assert len(counting.calls) == 2 and set(index.stats()) == {GENERO, FILME}


@pytest.mark.parametrize("query", ["", "   ", "\x00x", "x" * 201, "​"])
def test_unusable_queries_never_trigger_a_load(counting: CountingDB, query: str) -> None:
    index = EntityIndex(counting)  # type: ignore[arg-type]
    index.find(PESSOA, query)
    assert counting.calls == []


def test_invalid_arguments_never_trigger_a_load(counting: CountingDB) -> None:
    index = EntityIndex(counting)  # type: ignore[arg-type]
    for call in (
        lambda: index.find("xyz", "a"),
        lambda: index.find(PESSOA, "a", role="xyz"),
        lambda: index.find(FILME, "a", role="ator"),
        lambda: index.find(FILME, None),  # type: ignore[arg-type]
    ):
        with pytest.raises((ValueError, TypeError)):
            call()
    assert counting.calls == []


def test_preload_and_stats(counting: CountingDB) -> None:
    index = EntityIndex(counting)  # type: ignore[arg-type]
    index.preload("pessoa")
    assert set(index.stats()) == {PESSOA}
    index.preload()
    assert {kind: stats.rows for kind, stats in index.stats().items()} == {
        FILME: 62,
        PESSOA: 26,
        GENERO: 19,
        PRODUTORA: 7,
    }
    assert all(stats.seconds >= 0 and stats.skipped == 0 for stats in index.stats().values())
    assert len(counting.calls) == 4


def test_the_bulk_load_uses_the_per_call_overrides(counting: CountingDB) -> None:
    EntityIndex(counting).find(GENERO, "Action")  # type: ignore[arg-type]
    ((_, overrides),) = counting.calls
    assert overrides == {"max_rows": MAX_BULK_ROWS, "timeout_s": BULK_TIMEOUT_S}


def test_a_larger_database_timeout_is_never_lowered(gold_path: Path) -> None:
    with SafeDatabase(gold_path, timeout_s=120) as database:
        counting = CountingDB(database)
        EntityIndex(counting).find(GENERO, "Action")  # type: ignore[arg-type]
        assert counting.calls[0][1]["timeout_s"] == 120.0


def test_the_first_concurrent_searches_load_once(counting: CountingDB) -> None:
    slow = CountingDB(counting._database, pause=0.05)
    index = EntityIndex(slow)  # type: ignore[arg-type]
    barrier = threading.Barrier(8)
    outcomes: list[object] = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(index.find(PESSOA, "Larry Rosen", role="diretor").resolved)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(slow.calls) == 1
    assert len(outcomes) == 8 and len({o.key for o in outcomes}) == 1  # type: ignore[union-attr]


def test_concurrent_partial_and_fuzzy_searches_agree(gold_path: Path) -> None:
    with SafeDatabase(gold_path) as database:
        index = EntityIndex(database)
        barrier = threading.Barrier(8)
        results: list[tuple[object, object]] = []

        def worker() -> None:
            barrier.wait()  # todos pedem os índices de tokens e de vizinhança ao mesmo tempo
            results.append((index.find(PESSOA, "nolan"), index.find(PESSOA, "Cristopher Nolan")))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    assert len(results) == 8 and all(result == results[0] for result in results)


class FakeDB:
    """Banco de mentira: devolve linhas fixas ou falha, para testar a carga."""

    def __init__(self, rows=None, *, truncated=False, cut=0, errors=()) -> None:  # noqa: ANN001
        self.timeout_s = 30.0
        self.rows = rows if rows is not None else [("g1", "Action"), ("g2", "Drama")]
        self.truncated, self.cut, self.errors = truncated, cut, list(errors)
        self.calls = 0

    def execute(self, sql: str, *, max_rows=None, timeout_s=None) -> QueryResult:  # noqa: ANN001
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return QueryResult((), tuple(self.rows), self.truncated, max_rows or 0, 0.0, self.cut)


def test_a_truncated_load_is_an_error_never_a_partial_index() -> None:
    database = FakeDB(truncated=True)
    index = EntityIndex(database)  # type: ignore[arg-type]
    with pytest.raises(EntityIndexError, match="incompleto"):
        index.find(GENERO, "Action")
    assert index.stats() == {}
    database.truncated = False  # nova tentativa: carrega do zero
    assert index.find(GENERO, "Action").state is MatchState.EXACT_UNIQUE


def test_a_cut_value_is_an_error_because_the_key_would_be_wrong() -> None:
    with pytest.raises(EntityIndexError, match="cortado"):
        EntityIndex(FakeDB(cut=2)).find(GENERO, "Action")  # type: ignore[arg-type]


def test_an_empty_table_and_a_table_without_usable_names_are_errors() -> None:
    with pytest.raises(EntityIndexError, match="vazia"):
        EntityIndex(FakeDB(rows=[])).find(GENERO, "Action")  # type: ignore[arg-type]
    with pytest.raises(EntityIndexError, match="utilizável"):
        EntityIndex(FakeDB(rows=[("g1", None), ("g2", "  ")])).find(  # type: ignore[arg-type]
            GENERO, "Action"
        )


def test_a_database_error_becomes_an_index_error_and_a_retry_recovers() -> None:
    original = QueryTimeoutError("A consulta passou de 60 s e foi interrompida.")
    database = FakeDB(errors=[original])
    index = EntityIndex(database)  # type: ignore[arg-type]
    with pytest.raises(EntityIndexError, match="carregar o índice de genero") as error:
        index.find(GENERO, "Action")
    assert error.value.__cause__ is original
    assert index.find(GENERO, "Action").state is MatchState.EXACT_UNIQUE
    assert database.calls == 2


def test_rows_without_a_key_or_a_usable_name_are_skipped_and_counted() -> None:
    rows = [
        ("g1", "Action"),
        ("g2", None),
        (None, "Drama"),
        ("", "Comedy"),
        ("g5", "​"),
        ("g6", 7),
        (5, "Western"),
    ]
    index = EntityIndex(FakeDB(rows=rows))  # type: ignore[arg-type]
    assert index.find(GENERO, "Action").state is MatchState.EXACT_UNIQUE
    assert index.find(GENERO, "Drama").state is MatchState.NONE
    assert index.find(GENERO, "Western").state is MatchState.NONE  # chave que não é texto
    stats = index.stats()[GENERO]
    assert (stats.rows, stats.skipped) == (7, 6)


def test_film_ties_are_ordered_by_year_then_numeric_id_then_text_id_then_key() -> None:
    rows = [
        ("s5", "abc", "Same", 2000),
        ("s6", None, "Same", 2000),
        ("s4", "10", "Same", 2000),
        ("s3", "9", "Same", 2000),
        ("s2", "5", "Same", None),
        ("s1", "5", "Same", 1999),
        ("s8", "7", "Same", 2000),
        ("s7", "7", "Same", 2000),
    ]
    match = EntityIndex(FakeDB(rows=rows)).find(FILME, "same", max_candidates=25)  # type: ignore[arg-type]
    assert match.state is MatchState.EXACT_MULTIPLE
    assert [c.key for c in match.candidates] == ["s1", "s7", "s8", "s3", "s4", "s6", "s5", "s2"]


def test_odd_database_types_are_normalized_in_film_rows() -> None:
    rows = [("s1", 5, "Typed", True), ("s2", "6", "Typed", 1999)]
    match = EntityIndex(FakeDB(rows=rows)).find(FILME, "typed")  # type: ignore[arg-type]
    by_key = {candidate.key: candidate for candidate in match.candidates}
    assert (by_key["s1"].movie_id, by_key["s1"].year) == (
        "5",
        None,
    )  # id vira texto; bool não é ano
    assert (by_key["s2"].movie_id, by_key["s2"].year) == ("6", 1999)


def test_a_role_that_is_not_text_becomes_none_and_still_sorts() -> None:
    rows = [("p3", "Ann", "Ator"), ("p2", "Ann", 5), ("p1", "Ann", None)]
    match = EntityIndex(FakeDB(rows=rows)).find(PESSOA, "ann")  # type: ignore[arg-type]
    assert match.state is MatchState.EXACT_MULTIPLE
    assert [(c.key, c.role) for c in match.candidates] == [
        ("p1", None),
        ("p2", None),
        ("p3", "Ator"),
    ]


# --- determinismo e arquitetura -------------------------------------------------------------------

BATTERY = [
    (FILME, "dune", None),
    (FILME, "silence", None),
    (FILME, "amelie", None),
    (FILME, "spider", None),
    (FILME, "star", None),
    (FILME, "avtar", None),
    (FILME, "★", None),
    (PESSOA, "christopher nolan", None),
    (PESSOA, "nolan", None),
    (PESSOA, "nolan", "roteirista"),
    (PESSOA, "wes", None),
    (PESSOA, "cristopher nolan", None),
    (PESSOA, "zoe saldana", "ator"),
    (GENERO, "sci", None),
    (GENERO, "terror", None),
    (PRODUTORA, "warner bros", None),
]
HASH_CHECK = """
import json, sys, tempfile
from pathlib import Path
from cinedata.db import SafeDatabase
from cinedata.entities import EntityIndex
from gold_db import build_gold_db
battery = json.loads(sys.argv[2])
with tempfile.TemporaryDirectory() as folder:
    path = build_gold_db(Path(folder) / "gold.db", shuffle_seed=int(sys.argv[1]))
    with SafeDatabase(path) as database:
        index = EntityIndex(database)
        out = []
        for kind, query, role in battery:
            match = index.find(kind, query, role=role)
            out.append([match.state.value, match.total_matches, [
                [c.key, c.label, c.matched_via, c.edits] for c in match.candidates
            ]])
print(json.dumps(out))
"""


def _run_battery(insertion_seed: int, hash_seed: int) -> str:
    env = dict(os.environ, PYTHONHASHSEED=str(hash_seed))
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", HASH_CHECK, str(insertion_seed), json.dumps(BATTERY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
        env=env,
        cwd=Path(__file__).parent,
    )
    return result.stdout


def test_results_do_not_depend_on_insertion_order_or_on_the_hash_seed() -> None:
    first = _run_battery(insertion_seed=1, hash_seed=0)
    second = _run_battery(insertion_seed=2, hash_seed=12345)
    assert first == second
    assert json.loads(first)[0][2][0][1] == "Dune"  # e a saída não é vazia por engano


def test_shuffled_load_order_gives_identical_matches(tmp_path: Path) -> None:
    results = []
    for seed in (None, 11, 12):
        path = build_gold_db(tmp_path / f"s{seed}.db", shuffle_seed=seed)
        with SafeDatabase(path) as database:
            index = EntityIndex(database)
            results.append([index.find(k, q, role=r) for k, q, r in BATTERY])
    assert results[0] == results[1] == results[2]


def test_a_scan_cut_is_flagged_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities_module, "SCAN_ROW_LIMIT", 3)
    outcomes = []
    for seed in (5, 6):
        path = build_gold_db(tmp_path / f"cut{seed}.db", shuffle_seed=seed)
        with SafeDatabase(path) as database:
            match = EntityIndex(database).find(FILME, "silenc")
            outcomes.append((match.total_is_lower_bound, [c.movie_id for c in match.candidates]))
    assert outcomes[0] == outcomes[1] and outcomes[0][0] is True


def test_the_module_never_touches_sqlite_directly() -> None:
    source = Path(entities_module.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "sqlite3" not in imported
    assert "execute" in source and "SafeDatabase" in source


# --- distância de edição --------------------------------------------------------------------------


def _reference_osa(a: str, b: str) -> int:
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


@pytest.mark.parametrize(
    ("a", "b", "limit", "expected"),
    [
        ("avatar", "avtar", 1, 1),
        ("abc", "abc", 2, 0),
        ("ab", "ba", 1, 1),
        ("kitten", "sitting", 3, 3),
        ("kitten", "sitting", 2, None),
        ("a", "abcd", 2, None),
        ("christopher", "cristopher", 2, 1),
    ],
)
def test_edit_distance_known_cases(a: str, b: str, limit: int, expected: int | None) -> None:
    assert entities_module._edit_distance(a, b, limit) == expected


def test_edit_distance_matches_a_reference_implementation() -> None:
    rng = random.Random(99)  # noqa: S311
    for _ in range(1500):
        a = "".join(rng.choice("abc") for _ in range(rng.randrange(1, 7)))
        b = "".join(rng.choice("abc") for _ in range(rng.randrange(1, 7)))
        limit = rng.randrange(1, 4)
        reference = _reference_osa(a, b)
        assert entities_module._edit_distance(a, b, limit) == (
            reference if reference <= limit else None
        ), (a, b, limit)
