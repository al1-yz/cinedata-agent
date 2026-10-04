"""Corpus da avaliação M3 e a fronteira entre produção e avaliação (offline).

- Os 14 oficiais vêm do registro do M1c, sem cópia de pergunta nem de SQL.
- O corpus vai além deles: paráfrases, perguntas livres sem caso oficial e política.
- Produção não depende de `evals` e o prompt não contém nenhuma pergunta nem SQL do corpus.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from cinedata.agent import validate_question
from cinedata.entities import normalize
from cinedata.prompt import build_instructions
from cinedata.reference import OFFICIAL_CASES, ReferenceCase
from cinedata.runtime import AnswerStatus
from evals.cases import (
    CASES_BY_ID,
    CORPUS,
    FREEFORM_CASES,
    FREEFORM_REFERENCES,
    MOVIE,
    OFFICIAL_CHECKS,
    OFFICIAL_EVAL_CASES,
    PARAPHRASE_CASES,
    POLICY_CASES,
    SMOKE_IDS,
    TIERS,
    Category,
    EvalCase,
    Quantity,
    ResultCheck,
    Shape,
    TextPolicy,
    count,
    money,
    reference_registry,
    select_cases,
)

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_IDS = [case.case_id for case in OFFICIAL_CASES]


def _norm(text: str) -> str:
    return normalize(text)


# --- integridade ------------------------------------------------------------------------------


def test_exactly_the_14_official_cases_in_m1c_order() -> None:
    assert len(OFFICIAL_CASES) == 14
    assert [case.case_id for case in OFFICIAL_EVAL_CASES] == OFFICIAL_IDS
    assert set(OFFICIAL_CHECKS) == set(OFFICIAL_IDS)
    for case, reference in zip(OFFICIAL_EVAL_CASES, OFFICIAL_CASES, strict=True):
        assert case.category is Category.OFFICIAL
        assert case.reference is reference  # o próprio objeto do M1c, não uma cópia
        assert case.question == reference.question
        assert case.expected_status == {AnswerStatus.DATA_ANSWER}


def test_ids_are_unique_and_the_corpus_goes_beyond_the_official_examples() -> None:
    ids = [case.case_id for case in CORPUS]
    assert len(ids) == len(set(ids)) == len(CASES_BY_ID)
    counts = Counter(case.category for case in CORPUS)
    assert counts == {
        Category.OFFICIAL: 14,
        Category.PARAPHRASE: 5,
        Category.FREEFORM: 4,
        Category.POLICY: 3,
    }
    assert len(CORPUS) > 14


def test_every_question_is_valid_and_distinct() -> None:
    questions = [_norm(case.question) for case in CORPUS]
    assert len(questions) == len(set(questions))
    for case in CORPUS:
        assert validate_question(case.question) == case.question


def test_paraphrases_reuse_official_oracles_with_new_wording() -> None:
    official_texts = {
        _norm(text) for case in OFFICIAL_CASES for text in (case.question, *case.paraphrases)
    }
    for case in PARAPHRASE_CASES:
        assert case.reference is not None and case.reference.official
        assert case.reference.case_id in OFFICIAL_IDS
        assert _norm(case.question) not in official_texts


def test_freeform_cases_are_new_questions_with_their_own_oracle() -> None:
    official_texts = {
        _norm(text) for case in OFFICIAL_CASES for text in (case.question, *case.paraphrases)
    }
    official_sql = {" ".join(case.sql.split()) for case in OFFICIAL_CASES}
    assert [case.reference for case in FREEFORM_CASES] == list(FREEFORM_REFERENCES)
    for case in FREEFORM_CASES:
        reference = case.reference
        assert reference is not None and not reference.official
        assert reference.case_id not in OFFICIAL_IDS
        assert _norm(case.question) not in official_texts
        assert " ".join(reference.sql.split()) not in official_sql
    shapes = {case.check.shape for case in FREEFORM_CASES}
    assert shapes == {Shape.RANKED, Shape.SET}  # ranking, conjunto e escalares


def test_freeform_oracles_extend_the_m1c_registry_without_changing_it() -> None:
    registry = reference_registry()
    assert len(registry) == 14 + len(FREEFORM_REFERENCES)
    assert registry.ids[:14] == tuple(OFFICIAL_IDS)


def test_policy_cases_define_behaviour_not_just_a_status() -> None:
    by_id = {case.case_id: case for case in POLICY_CASES}
    for case_id, status, policy in (
        ("politica_02_fora_do_escopo", AnswerStatus.OUT_OF_SCOPE, TextPolicy.REFUSAL),
        ("politica_03_capacidades", AnswerStatus.INFO, TextPolicy.CAPABILITIES),
    ):
        case = by_id[case_id]
        assert case.expected_status == {status} and case.require_no_tools
        assert case.reference is None and not case.is_data
        assert case.text_policy is policy  # o status certo sozinho não basta: o texto é conferido
    assert by_id["politica_02_fora_do_escopo"].forbidden_answers  # a previsão entregue falha
    ambiguous = by_id["politica_01_titulo_ambiguo"]
    assert ambiguous.expected_status == {AnswerStatus.CLARIFICATION, AnswerStatus.DATA_ANSWER}
    assert ambiguous.ambiguous_title == "Elemental" and not ambiguous.require_no_tools
    # o gabarito da resposta completa é só da avaliação: nem oficial, nem do registro do M1c
    reference = ambiguous.reference
    assert reference is not None and not reference.official
    assert reference.case_id not in reference_registry()
    assert ambiguous.check.shape is Shape.SET and ambiguous.check.identity == MOVIE
    assert ambiguous.check.metrics[0].column == "nota_imdb"


def test_every_check_only_uses_columns_of_its_oracle() -> None:
    for case in CORPUS:
        if case.is_data:
            assert case.check.columns <= set(case.reference.columns), case.case_id
            if case.check.shape is Shape.RANKED:  # N avaliado explícito, igual ao do gabarito
                assert case.check.top_n == case.reference.limit is not None, case.case_id
            for alternative in case.check.identity:  # filme nunca só pelo título
                assert "titulo" not in alternative or "ano_lancamento" in alternative


def test_money_metrics_carry_the_currency_of_their_column() -> None:
    money_metrics = [
        metric
        for case in CORPUS
        if case.is_data
        for metric in case.check.metrics
        if metric.currency is not None
    ]
    assert {m.column for m in money_metrics} == {
        "receita_brl",
        "lucro_medio_brl",
        "lucro_total_brl",
        "receita_total_usd",
    }
    for metric in money_metrics:
        assert metric.column.endswith("_" + metric.currency.lower()), metric.column
    for case in CORPUS:  # nenhuma coluna _brl/_usd ficou sem a moeda
        if case.is_data:
            for metric in case.check.metrics:
                if metric.column.endswith(("_brl", "_usd")):
                    assert metric.currency is not None, (case.case_id, metric.column)
    with pytest.raises(ValueError, match="moeda"):
        money("receita")


def test_metric_kinds_follow_what_the_column_measures() -> None:
    # a grandeza decide que rótulo escrito no texto vale: "nota 9,8" prova uma nota, nunca uma
    # divergência; "65 anos" nunca prova uma contagem
    for case in CORPUS:
        if not case.is_data:
            continue
        for metric in case.check.metrics:
            column = metric.column
            if column.startswith(("nota", "media")):
                assert metric.kind is Quantity.RATING, (case.case_id, column)
            elif column in ("divergencia", "popularidade"):
                assert metric.kind is Quantity.DECIMAL, (case.case_id, column)
            elif column.startswith("margem"):
                assert metric.kind is Quantity.RATIO, (case.case_id, column)
            elif column.endswith(("_brl", "_usd")):
                assert metric.kind is Quantity.MONEY, (case.case_id, column)
            else:
                assert metric.kind is Quantity.COUNT, (case.case_id, column)


def test_open_ended_official_rankings_use_the_m1c_display_n() -> None:
    for case_id in (
        "oficial_03_maior_margem",
        "oficial_05_divergencia_tmdb_imdb",
        "oficial_08_diretores_melhor_nota",
        "oficial_13_mais_avaliados",
        "oficial_14_divergencia_usuarios_imdb",
    ):
        assert CASES_BY_ID[case_id].check.top_n == 10, case_id
    assert CASES_BY_ID["parafrase_05_diretor_no_singular"].check.shape is Shape.LEADERS


def test_invalid_case_definitions_are_rejected() -> None:
    reference = OFFICIAL_CASES[0]
    check = OFFICIAL_CHECKS[reference.case_id]
    with pytest.raises(ValueError, match="andam juntos"):
        EvalCase(
            "x",
            Category.FREEFORM,
            "Pergunta?",
            "p",
            frozenset({AnswerStatus.DATA_ANSWER}),
            reference=reference,
        )
    with pytest.raises(ValueError, match="colunas fora do gabarito"):
        EvalCase(
            "x",
            Category.FREEFORM,
            "Pergunta?",
            "p",
            frozenset({AnswerStatus.DATA_ANSWER}),
            reference=reference,
            check=ResultCheck(Shape.SET, (), (count("inexistente"),)),
        )
    with pytest.raises(ValueError, match="top_n difere"):
        EvalCase(
            "x",
            Category.FREEFORM,
            "Pergunta?",
            "p",
            frozenset({AnswerStatus.DATA_ANSWER}),
            reference=reference,
            check=ResultCheck(Shape.RANKED, check.identity, check.metrics, top_n=3),
        )
    with pytest.raises(ValueError, match="exige gabarito"):
        EvalCase("x", Category.POLICY, "Pergunta?", "p", frozenset({AnswerStatus.DATA_ANSWER}))
    both = frozenset({AnswerStatus.DATA_ANSWER, AnswerStatus.CLARIFICATION})
    with pytest.raises(ValueError, match="só o título ambíguo"):
        EvalCase("x", Category.POLICY, "Pergunta?", "p", both, reference=reference, check=check)
    with pytest.raises(ValueError, match="título ambíguo exige"):
        EvalCase("x", Category.POLICY, "Pergunta?", "p", frozenset({AnswerStatus.CLARIFICATION}),
                 ambiguous_title="Elemental")  # fmt: skip
    with pytest.raises(ValueError, match="sem ferramentas"):
        EvalCase("x", Category.FREEFORM, "Pergunta?", "p", frozenset({AnswerStatus.DATA_ANSWER}),
                 reference=reference, check=check, require_no_tools=True)  # fmt: skip
    with pytest.raises(ValueError, match="top_n"):
        ResultCheck(Shape.SET, (), (count("filmes"),), top_n=3)
    with pytest.raises(ValueError, match="top_n"):  # ranking sem N avaliado explícito
        ResultCheck(Shape.RANKED, check.identity, check.metrics)
    with pytest.raises(ValueError, match="título"):
        ResultCheck(Shape.SET, (("titulo",),), (count("filmes"),))
    out = frozenset({AnswerStatus.OUT_OF_SCOPE})
    with pytest.raises(ValueError, match="exige o status dela"):  # recusa num caso de ajuda
        EvalCase("x", Category.POLICY, "Pergunta?", "p", frozenset({AnswerStatus.INFO}),
                 require_no_tools=True, text_policy=TextPolicy.REFUSAL)  # fmt: skip
    with pytest.raises(ValueError, match="exige o status dela"):  # política de texto com dados
        EvalCase("x", Category.POLICY, "Pergunta?", "p", out, text_policy=TextPolicy.REFUSAL)
    with pytest.raises(ValueError, match="forbidden_answers"):
        EvalCase("x", Category.POLICY, "Pergunta?", "p", frozenset({AnswerStatus.INFO}),
                 require_no_tools=True, text_policy=TextPolicy.CAPABILITIES,
                 forbidden_answers=("x",))  # fmt: skip


def test_fingerprint_follows_the_definition() -> None:
    case = CASES_BY_ID["oficial_01_maior_receita"]
    paraphrase = CASES_BY_ID["parafrase_02_bilheteria_informal_top5"]
    assert case.fingerprint() == CASES_BY_ID["oficial_01_maior_receita"].fingerprint()
    assert case.fingerprint() != paraphrase.fingerprint()  # outra pergunta e outro N
    from dataclasses import replace

    assert replace(case, question="Outra?").fingerprint() != case.fingerprint()
    refusal = CASES_BY_ID["politica_02_fora_do_escopo"]
    assert replace(refusal, forbidden_answers=()).fingerprint() != refusal.fingerprint()
    no_text = replace(refusal, text_policy=None, forbidden_answers=())
    assert no_text.fingerprint() not in (
        refusal.fingerprint(),
        replace(refusal, forbidden_answers=()).fingerprint(),
    )


# --- tiers ------------------------------------------------------------------------------------


def test_smoke_tier_is_small_and_semantically_varied() -> None:
    smoke = select_cases("smoke")
    assert [case.case_id for case in smoke] == list(SMOKE_IDS)
    assert len(smoke) == 4
    assert [case.case_id for case in smoke] != OFFICIAL_IDS[:4]
    categories = {case.category for case in smoke}
    assert {Category.OFFICIAL, Category.FREEFORM, Category.POLICY} <= categories
    shapes = {case.check.shape for case in smoke if case.is_data}
    assert {Shape.RANKED, Shape.SET} <= shapes


def test_tiers_and_selection() -> None:
    assert [case.case_id for case in select_cases("official")] == OFFICIAL_IDS
    assert len(select_cases("full")) == len(CORPUS)
    for name in ("paraphrase", "freeform", "policy"):
        assert {case.category.value for case in select_cases(name)} == {name}
    assert set(TIERS) == {"smoke", "official", "paraphrase", "freeform", "policy", "full"}
    picked = select_cases(ids=["politica_02_fora_do_escopo", "oficial_01_maior_receita"])
    assert [case.case_id for case in picked] == [
        "politica_02_fora_do_escopo",
        "oficial_01_maior_receita",
    ]
    assert len(select_cases("full", limit=3)) == 3
    with pytest.raises(KeyError, match="desconhecido"):
        select_cases(ids=["nao_existe"])
    with pytest.raises(KeyError, match="tier"):
        select_cases("tudo")
    with pytest.raises(ValueError):
        select_cases("smoke", limit=0)


# --- fronteira produção x avaliação ---------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_production_never_imports_the_evaluation_package() -> None:
    for path in (ROOT / "src" / "cinedata").glob("*.py"):
        assert not any(m.split(".")[0] == "evals" for m in _imports(path)), path.name


def test_running_the_agent_does_not_load_the_evaluation_package() -> None:
    code = (
        "import sys, cinedata.agent, cinedata.cli;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in ('evals', 'cinedata')))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    assert "cinedata.agent" in result.stdout and "evals" not in result.stdout


def test_prompt_contains_no_corpus_question_or_eval_sql() -> None:
    prompt = _norm(build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30))
    for case in CORPUS:
        assert _norm(case.question) not in prompt, case.case_id
        assert case.case_id not in prompt
    raw = " ".join(
        build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30).split()
    ).lower()
    own = {case.reference for case in CORPUS if case.reference and not case.reference.official}
    assert set(FREEFORM_REFERENCES) < own  # livres + o gabarito do título ambíguo
    for reference in own:
        for line in reference.sql.splitlines():
            fragment = " ".join(line.split()).lower()
            if len(fragment) >= 30:
                assert fragment not in raw, (reference.case_id, fragment)


def test_evaluation_code_does_not_retype_the_official_sql_or_questions() -> None:
    for path in (ROOT / "evals").glob("*.py"):
        source = " ".join(path.read_text(encoding="utf-8").split()).lower()
        for case in OFFICIAL_CASES:
            assert " ".join(case.sql.split()).lower() not in source, (path.name, case.case_id)
            assert case.question.lower() not in source, (path.name, case.case_id)


def test_reference_cases_used_by_the_corpus_are_m1c_objects() -> None:
    for case in CORPUS:
        assert case.reference is None or isinstance(case.reference, ReferenceCase)
