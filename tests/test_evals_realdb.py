"""Avaliação M3 contra o banco real, sem modelo (pulados sem data/cinerocket.db).

- Todo gabarito do corpus roda no banco real com a data de referência 2026-10-01.
- O pontuador aceita o próprio gabarito e recusa perturbações dele (linha a menos, valor errado,
  ordem invertida, duplicata).
- SQLs escritos de outro jeito, como um agente escreveria (aliases, ROUND, %, LIMIT), passam; SQLs
  com semântica errada conhecida falham.
- Os gabaritos das perguntas livres conferem com cálculos independentes em Python.
- As premissas da política e das perguntas livres valem no banco (homônimos, papéis, aliases).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cinedata.config import rolling_window
from cinedata.db import MAX_BULK_ROWS, SafeDatabase
from cinedata.entities import EntityIndex, MatchState
from cinedata.reference import ReferenceResult
from cinedata.runtime import (
    AgentAnswer,
    AgentOutcome,
    AnswerStatus,
    RunTrace,
    SqlExecution,
    SqlStatus,
)
from evals.cases import CASES_BY_ID, CORPUS, EvalCase, Metric, Quantity, Shape, expected_result
from evals.scoring import (
    CaseScore,
    FailureCategory,
    Verdict,
    check_answer,
    competition_ranks,
    display_columns,
    required_rows,
    score_case,
)

pytestmark = pytest.mark.realdb

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "cinerocket.db"
REF = date(2026, 10, 1)
DATA_CASES = [case for case in CORPUS if case.is_data]


@pytest.fixture(scope="module")
def real_db() -> Iterator[SafeDatabase]:
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado (veja data/README.md)")
    with SafeDatabase(REAL_DB, max_rows=50, timeout_s=120.0) as database:  # 50 = teto do agente
        yield database


@pytest.fixture(scope="module")
def expected(real_db: SafeDatabase) -> dict[str, ReferenceResult]:
    return {case.case_id: _oracle(real_db, case.case_id) for case in DATA_CASES}


def _bulk(db: SafeDatabase, sql: str) -> tuple[tuple[object, ...], ...]:
    result = db.execute(sql, max_rows=MAX_BULK_ROWS, timeout_s=600)
    assert not result.truncated and not result.truncated_cells, sql
    return result.rows


_ORACLES: dict[str, ReferenceResult] = {}  # o banco não muda entre testes


def _oracle(db: SafeDatabase, case_id: str) -> ReferenceResult:
    if case_id not in _ORACLES:
        _ORACLES[case_id] = expected_result(db, CASES_BY_ID[case_id], REF)
    return _ORACLES[case_id]


def _ptbr(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _render(case: EvalCase, result: ReferenceResult) -> str:
    """Uma resposta como um modelo escreveria: uma linha por linha do gabarito, em pt-BR."""
    metric = case.check.metrics[0]
    lines = []
    for record in result.records():
        value = record[metric.column]
        if isinstance(value, int):
            shown = _ptbr(value, 0)
        elif metric.scales != (1.0,):  # razão em %
            shown = _ptbr(value * 100, 2) + "%"
        else:
            shown = _ptbr(value, 2)
        if metric.currency is not None:
            shown = {"BRL": "R$ ", "USD": "US$ "}[metric.currency] + shown
        label = case.check.label
        if label == ("titulo", "ano_lancamento"):
            name = f"**{record['titulo']}** ({record['ano_lancamento']}, id {record['id_filme']})"
        else:
            name = " e ".join(str(record[column]) for column in label)
        lines.append(f"- {name}: {shown}" if name else f"Resultado: {shown}")
    return "\n".join(lines)


def _score(
    db: SafeDatabase, case_id: str, columns, rows, *, truncated: bool = False, text: str = ""
) -> CaseScore:  # noqa: ANN001
    case = CASES_BY_ID[case_id]
    trace = RunTrace(reference_date=REF, max_rows=db.max_rows, request_limit=5)
    rows = tuple(tuple(row) for row in rows)
    trace.sql_executions.append(
        SqlExecution(
            1,
            1,
            "SQL",
            SqlStatus.OK,
            columns=tuple(columns),
            rows=rows,
            truncated=truncated,
            rows_shown=len(rows),  # como o run_sql grava quando tudo coube no que o modelo recebe
        )
    )
    trace.model_responses = 2  # a consulta na 1ª resposta do modelo, o texto final na 2ª
    expected = _oracle(db, case_id)
    answer = AgentAnswer(status=AnswerStatus.DATA_ANSWER, answer=text or _render(case, expected))
    return score_case(case, AgentOutcome(case.question, answer, None, trace), expected)


def _score_sql(db: SafeDatabase, case_id: str, sql: str) -> CaseScore:
    result = db.execute(sql)  # o mesmo teto de linhas do agente
    return _score(db, case_id, result.columns, result.rows, truncated=result.truncated)


# --- gabaritos ------------------------------------------------------------------------------


def test_every_oracle_runs_on_the_real_gold(expected: dict[str, ReferenceResult]) -> None:
    for case in DATA_CASES:
        result = expected[case.case_id]
        assert result.rows, case.case_id
        if case.check.top_n is not None:  # no banco real há pelo menos N linhas
            assert len(result.rows) >= case.check.top_n, case.case_id
    assert expected["livre_01_top5_atores_terror"].rows == (
        (1, "Shawn C. Phillips", 60),
        (2, "Felissa Rose", 38),
        (2, "Julie Anne Prescott", 38),
        (4, "Eric Roberts", 33),
        (5, "Jennifer Nangle", 27),
    )
    nolan = expected["livre_02_filmes_dirigidos_nolan"]
    assert [(r[1], r[3]) for r in nolan.rows] == [("Dunkirk", 7.8), ("Oppenheimer", 8.2)]
    assert expected["livre_03_receita_usd_animacao_2019"].rows[0][0] > 0
    assert expected["livre_04_filmes_ultimos_3_anos"].rows == ((3486,),)
    assert expected["livre_04_filmes_ultimos_3_anos"].parameters == {
        "start_date": "2023-10-01",
        "end_date": "2026-10-01",
    }


# --- o pontuador é coerente com o gabarito --------------------------------------------------


@pytest.mark.parametrize("case", DATA_CASES, ids=lambda case: case.case_id)
def test_the_oracle_scores_as_correct_and_its_perturbations_do_not(
    real_db: SafeDatabase, expected: dict[str, ReferenceResult], case
) -> None:  # noqa: ANN001
    result = expected[case.case_id]
    columns, rows = result.columns, [list(row) for row in result.rows]
    assert _score(real_db, case.case_id, columns, rows).verdict is Verdict.PASS

    metric = case.check.metrics[0].column
    position = columns.index(metric)
    perturbations = {
        "sem a primeira linha": rows[1:],
        "valor errado": [
            [*rows[0][:position], rows[0][position] + 1, *rows[0][position + 1 :]],
            *rows[1:],
        ],
        "linha duplicada": [*rows, rows[-1]],
    }
    if (
        case.check.shape is not Shape.SET
        and len(set(competition_ranks([row[position] for row in rows]))) > 1
    ):
        perturbations["ordem invertida"] = list(reversed(rows))
    for name, changed in perturbations.items():
        score = _score(real_db, case.case_id, columns, changed)
        assert score.verdict is Verdict.FAIL, (case.case_id, name)
        assert score.category is FailureCategory.RESULT_MISMATCH, (case.case_id, name)


# --- SQL de agente: formas corretas diferentes passam, semânticas erradas falham ------------

CORRECT = {
    "oficial_01_maior_receita": (
        "SELECT m.titulo AS filme, f.receita_brl AS faturamento, m.ano_lancamento AS ano"
        " FROM fact_movies_performance AS f JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id"
        " WHERE f.receita_brl IS NOT NULL ORDER BY f.receita_brl DESC, m.titulo LIMIT 10"
    ),
    "oficial_03_maior_margem": (  # em %, com 2 casas
        "SELECT m.titulo, m.ano_lancamento,"
        " ROUND(100.0 * (f.receita_brl - f.orcamento_brl) / f.receita_brl, 2) AS margem_pct"
        " FROM fact_movies_performance AS f JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id"
        " WHERE f.receita_brl > 0 AND f.orcamento_brl IS NOT NULL"
        " ORDER BY CAST(f.receita_brl - f.orcamento_brl AS REAL) / f.receita_brl DESC, m.titulo"
        " LIMIT 10"
    ),
    "oficial_06_nota_imdb_por_ano": (
        "SELECT m.ano_lancamento AS ano, ROUND(AVG(f.nota_imdb), 2) AS media"
        " FROM fact_movies_performance AS f JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id"
        " WHERE f.nota_imdb > 0 GROUP BY m.ano_lancamento ORDER BY ano"
    ),
    "oficial_07_ator_mais_ativo_5_anos": (  # mostra um ranking; só o líder é verificável
        "SELECT p.nome_pessoa AS ator, COUNT(DISTINCT m.sk_movie_id) AS filmes"
        " FROM dim_people AS p JOIN bridge_movie_person AS b ON b.sk_person_id = p.sk_person_id"
        " JOIN dim_movies AS m ON m.sk_movie_id = b.sk_movie_id"
        " WHERE p.tipo_pessoa = 'Ator' AND m.status_filme = 'Lançado'"
        " AND m.data_lancamento BETWEEN '2021-10-01' AND '2026-10-01'"
        " GROUP BY p.sk_person_id ORDER BY filmes DESC, ator LIMIT 5"
    ),
    "oficial_10_filmes_por_genero": (
        "SELECT g.nome_genero, COUNT(DISTINCT b.sk_movie_id) AS total FROM dim_genres AS g"
        " LEFT JOIN bridge_movie_genre AS b ON b.sk_genre_id = g.sk_genre_id"
        " GROUP BY g.sk_genre_id ORDER BY total DESC"
    ),
    "oficial_12_genero_maior_margem": (  # em %, com os 3 primeiros
        "WITH mg AS (SELECT sk_movie_id, CAST(receita_brl - orcamento_brl AS REAL) / receita_brl"
        " AS margem FROM fact_movies_performance WHERE receita_brl > 0"
        " AND orcamento_brl IS NOT NULL)"
        " SELECT g.nome_genero AS genero, ROUND(AVG(mg.margem) * 100, 2) AS margem_pct"
        " FROM mg JOIN bridge_movie_genre AS b ON b.sk_movie_id = mg.sk_movie_id"
        " JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id"
        " GROUP BY g.sk_genre_id ORDER BY AVG(mg.margem) DESC LIMIT 3"
    ),
    "oficial_13_mais_avaliados": (  # todos com contagem >= a da 10ª linha: os empates entram
        "SELECT m.titulo, m.ano_lancamento, m.id_filme, r.qtd_avaliacoes_usuarios AS avaliacoes"
        " FROM dim_reviews AS r JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id"
        " WHERE r.qtd_avaliacoes_usuarios >= (SELECT qtd_avaliacoes_usuarios FROM dim_reviews"
        " ORDER BY qtd_avaliacoes_usuarios DESC LIMIT 1 OFFSET 9)"
        " ORDER BY r.qtd_avaliacoes_usuarios DESC, m.id_filme"
    ),
    "parafrase_05_diretor_no_singular": (
        "WITH d AS (SELECT b.sk_person_id FROM bridge_movie_person AS b"
        " JOIN dim_people AS p ON p.sk_person_id = b.sk_person_id WHERE p.tipo_pessoa = 'Diretor'"
        " GROUP BY b.sk_person_id HAVING COUNT(*) >= 5)"
        " SELECT p.nome_pessoa AS diretor, ROUND(AVG(f.nota_imdb), 2) AS media"
        " FROM d JOIN dim_people AS p ON p.sk_person_id = d.sk_person_id"
        " JOIN bridge_movie_person AS b ON b.sk_person_id = d.sk_person_id"
        " JOIN fact_movies_performance AS f ON f.sk_movie_id = b.sk_movie_id"
        " WHERE f.nota_imdb > 0 GROUP BY d.sk_person_id ORDER BY AVG(f.nota_imdb) DESC LIMIT 3"
    ),
    "livre_01_top5_atores_terror": (
        "SELECT p.nome_pessoa, COUNT(DISTINCT bm.sk_movie_id) AS n"
        " FROM dim_genres AS g JOIN bridge_movie_genre AS bm ON bm.sk_genre_id = g.sk_genre_id"
        " JOIN bridge_movie_person AS bp ON bp.sk_movie_id = bm.sk_movie_id"
        " JOIN dim_people AS p ON p.sk_person_id = bp.sk_person_id"
        " WHERE g.nome_genero = 'Horror' AND p.tipo_pessoa = 'Ator'"
        " GROUP BY p.sk_person_id ORDER BY n DESC, p.nome_pessoa LIMIT 5"
    ),
    "livre_02_filmes_dirigidos_nolan": (
        "SELECT m.titulo, m.ano_lancamento, f.nota_imdb FROM dim_people AS p"
        " JOIN bridge_movie_person AS b ON b.sk_person_id = p.sk_person_id"
        " JOIN dim_movies AS m ON m.sk_movie_id = b.sk_movie_id"
        " JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id"
        " WHERE p.nome_pessoa = 'Christopher Nolan' AND p.tipo_pessoa = 'Diretor'"
    ),
    "livre_03_receita_usd_animacao_2019": (  # sem ROUND
        "SELECT SUM(f.receita_usd) AS total FROM fact_movies_performance AS f"
        " JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id"
        " JOIN bridge_movie_genre AS b ON b.sk_movie_id = f.sk_movie_id"
        " JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id"
        " WHERE g.nome_genero = 'Animation' AND m.ano_lancamento = 2019"
    ),
    "livre_04_filmes_ultimos_3_anos": (
        "SELECT COUNT(*) AS n FROM dim_movies"
        " WHERE data_lancamento >= '2023-10-01' AND data_lancamento <= '2026-10-01'"
    ),
}

WRONG = [
    (
        "oficial_01_maior_receita",
        "em dólares",
        CORRECT["oficial_01_maior_receita"].replace("receita_brl", "receita_usd"),
    ),
    ("oficial_01_maior_receita", "top 9", CORRECT["oficial_01_maior_receita"][:-2] + "9"),
    (
        "oficial_01_maior_receita",
        "filme só pelo título",
        CORRECT["oficial_01_maior_receita"].replace(", m.ano_lancamento AS ano", ""),
    ),
    (
        "oficial_13_mais_avaliados",
        "LIMIT 10 no meio do empate da 8ª posição",
        "SELECT m.titulo, m.id_filme, r.qtd_avaliacoes_usuarios AS avaliacoes"
        " FROM dim_reviews AS r JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id"
        " ORDER BY r.qtd_avaliacoes_usuarios DESC, m.id_filme LIMIT 10",
    ),
    (
        "oficial_02_lucro_medio_por_genero",
        "sem exigir receita",
        "SELECT g.nome_genero, ROUND(AVG(f.lucro_brl), 2) FROM fact_movies_performance AS f"
        " JOIN bridge_movie_genre AS b ON b.sk_movie_id = f.sk_movie_id"
        " JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id GROUP BY g.sk_genre_id",
    ),
    (
        "oficial_06_nota_imdb_por_ano",
        "LIMIT 10 numa lista de 13 anos",
        CORRECT["oficial_06_nota_imdb_por_ano"] + " LIMIT 10",
    ),
    (
        "oficial_13_mais_avaliados",
        "ordem alfabética",
        "SELECT m.titulo, m.id_filme, r.qtd_avaliacoes_usuarios AS avaliacoes"
        " FROM dim_reviews AS r JOIN dim_movies AS m ON m.sk_movie_id = r.sk_movie_id"
        " ORDER BY m.titulo LIMIT 14",
    ),
    (
        "livre_02_filmes_dirigidos_nolan",
        "sem o papel (inclui Tenet, só roteiro)",
        CORRECT["livre_02_filmes_dirigidos_nolan"].replace(" AND p.tipo_pessoa = 'Diretor'", ""),
    ),
    (
        "livre_04_filmes_ultimos_3_anos",
        "início exclusivo",
        CORRECT["livre_04_filmes_ultimos_3_anos"].replace(">=", ">"),
    ),
    (
        "livre_04_filmes_ultimos_3_anos",
        "filtro de status não pedido",
        CORRECT["livre_04_filmes_ultimos_3_anos"] + " AND status_filme = 'Lançado'",
    ),
    (
        "livre_04_filmes_ultimos_3_anos",
        "sem o fim da janela (inclui datas futuras)",
        CORRECT["livre_04_filmes_ultimos_3_anos"].replace(
            " AND data_lancamento <= '2026-10-01'", ""
        ),
    ),
    (
        "livre_04_filmes_ultimos_3_anos",
        "outra data de referência",
        CORRECT["livre_04_filmes_ultimos_3_anos"]
        .replace("2023-10-01", "2022-10-03")
        .replace("2026-10-01", "2025-10-03"),
    ),
]


@pytest.mark.parametrize(("case_id", "sql"), list(CORRECT.items()), ids=list(CORRECT))
def test_differently_written_correct_sql_passes(
    real_db: SafeDatabase, case_id: str, sql: str
) -> None:
    score = _score_sql(real_db, case_id, sql)
    assert score.verdict is Verdict.PASS, (case_id, score.reason)


def test_rows_below_a_leaders_only_oracle_are_annotated(real_db: SafeDatabase) -> None:
    leader = _score_sql(
        real_db, "oficial_07_ator_mais_ativo_5_anos", CORRECT["oficial_07_ator_mais_ativo_5_anos"]
    )
    assert leader.verdict is Verdict.PASS
    assert any("não verificada" in note for note in leader.notes)


def test_q13_requires_its_complete_14_row_window(
    real_db: SafeDatabase, expected: dict[str, ReferenceResult]
) -> None:
    result = expected["oficial_13_mais_avaliados"]
    assert len(result.rows) == 14  # top 10 + os empatados do 8º lugar
    for size in (1, 2, 5, 10):
        score = _score(real_db, "oficial_13_mais_avaliados", result.columns, result.rows[:size])
        assert score.verdict is Verdict.FAIL and score.detail == "missing_rows", size
    # sem id_filme, título + ano + contagem coincidem nas 14 linhas, mas não provam 14 filmes
    drop = result.columns.index("id_filme")
    columns = result.columns[:drop] + result.columns[drop + 1 :]
    rows = [row[:drop] + row[drop + 1 :] for row in result.rows]
    score = _score(real_db, "oficial_13_mais_avaliados", columns, rows)
    assert score.verdict is Verdict.FAIL and score.detail == "ambiguous_identity"


def test_a_real_q13_numbered_list_cannot_borrow_list_ordinals(
    real_db: SafeDatabase, expected: dict[str, ReferenceResult]
) -> None:
    result = expected["oficial_13_mais_avaliados"]
    records = result.records()
    lines = [
        f"{n}. {r['titulo']} ({r['ano_lancamento']}, id {r['id_filme']}): "
        f"{r['qtd_avaliacoes_usuarios']} avaliações"
        for n, r in enumerate(records, 1)
    ]
    full = "\n".join(lines)
    good = _score(real_db, "oficial_13_mais_avaliados", result.columns, result.rows, text=full)
    assert good.verdict is Verdict.PASS, good.reason
    nines = [n for n, r in enumerate(records, 1) if r["qtd_avaliacoes_usuarios"] == n]
    assert nines == [9]  # o 9º item tem 9 avaliações: o número da lista coincide com a métrica
    lines[8] = lines[8].removesuffix(": 9 avaliações")
    text = "\n".join(lines)
    score = _score(real_db, "oficial_13_mais_avaliados", result.columns, result.rows, text=text)
    assert score.verdict is Verdict.FAIL and score.category is FailureCategory.ANSWER_TEXT
    assert len(score.answer.missing) == 1 and records[8]["id_filme"] in score.answer.missing[0]


def test_answers_on_real_oracles_need_every_required_row(
    expected: dict[str, ReferenceResult],
) -> None:
    for case in DATA_CASES:
        result = expected[case.case_id]
        check = case.check
        assert check_answer(check, result.columns, result.rows, _render(case, result)).verdict == (
            "ok"
        ), case.case_id
        assert check_answer(check, result.columns, result.rows, "Feito.").verdict == "missing"
    q13 = expected["oficial_13_mais_avaliados"]
    without_ids = _render(CASES_BY_ID["oficial_13_mais_avaliados"], q13)
    without_ids = "\n".join(line.split(", id ")[0] + ")" + line.split(")", 1)[1]
                            for line in without_ids.splitlines())  # fmt: skip
    verdict = check_answer(
        CASES_BY_ID["oficial_13_mais_avaliados"].check, q13.columns, q13.rows, without_ids
    )
    assert verdict.verdict == "missing" and len(verdict.missing) == 13  # título + ano se repetem


def _formats(case: EvalCase, result: ReferenceResult) -> dict[str, str]:
    """A mesma resposta certa em formatos que um modelo usa: lista numerada com negrito, tabela
    com "#" e bordas, tabela sem bordas com "Posição" e alinhamento."""
    metric = case.check.metrics[0]
    rows = required_rows(case.check, list(result.records()))
    shown = display_columns(case.check, rows)
    names = {"titulo": "Filme", "ano_lancamento": "Ano", "id_filme": "ID"}
    head = [*(names.get(column, column.title()) for column in shown), "Valor"]
    cells = [[*(str(row[column]) for column in shown)] for row in rows]
    values = [_render_value(metric, record[metric.column]) for record in rows]
    written = [  # fora de uma coluna de id, o id só vale escrito como id
        [f"id {row[column]}" if column == "id_filme" else str(row[column]) for column in shown]
        for row in rows
    ]
    numbered = "\n".join(
        f"{n}. **{' — '.join(parts)}**: {value}"
        for n, (parts, value) in enumerate(zip(written, values, strict=True), 1)
    )
    rule = "|".join(["---"] * (len(head) + 1))
    table = "\n".join(
        ["| # | " + " | ".join(head) + " |", f"|{rule}|"]
        + [f"| {n} | " + " | ".join([*parts, value]) + " |"
           for n, (parts, value) in enumerate(zip(cells, values, strict=True), 1)]
    )  # fmt: skip
    bare = "\n".join(
        ["Posição | " + " | ".join(head), "|".join([":---:"] * (len(head) + 1))]
        + [f"{n} | " + " | ".join([*parts, value])
           for n, (parts, value) in enumerate(zip(cells, values, strict=True), 1)]
    )  # fmt: skip
    bullets = "\n".join(
        f"- **{' — '.join(parts)}**: {value}" for parts, value in zip(written, values, strict=True)
    )
    lazy = "\n".join(
        f"1. {' — '.join(parts)}: {value}" for parts, value in zip(written, values, strict=True)
    )
    return {
        "lista numerada": numbered,
        "tabela com #": table,
        "tabela sem bordas": bare,
        "bullets com nota": f"Resultado:\n{bullets}\n- Observação: valores do banco, sem ajuste.",
        "numeração preguiçosa": lazy,
        "lista e tabela repetidas": f"{numbered}\n\n{table}",
    }


def _render_value(metric: Metric, value: float) -> str:
    if metric.kind is Quantity.COUNT:
        return _ptbr(value, 0)
    if metric.kind is Quantity.RATIO:
        return _ptbr(value * 100, 2) + "%"
    shown = _ptbr(value, 2)
    return (
        shown if metric.currency is None else {"BRL": "R$ ", "USD": "US$ "}[metric.currency] + shown
    )


@pytest.mark.parametrize(
    "case", [case for case in DATA_CASES if case.check.label], ids=lambda case: case.case_id
)
def test_correct_answers_pass_in_every_common_format(
    expected: dict[str, ReferenceResult], case: EvalCase
) -> None:
    result = expected[case.case_id]
    for style, text in _formats(case, result).items():
        verdict = check_answer(case.check, result.columns, result.rows, text)
        assert verdict.verdict == "ok", (case.case_id, style, verdict.missing, verdict.order)
    if case.check.shape is Shape.RANKED:  # sem posição escrita, a ordem inversa não passa
        lines = _render(case, result).splitlines()
        flipped = check_answer(case.check, result.columns, result.rows, "\n".join(lines[::-1]))
        assert flipped.verdict == "wrong_order", case.case_id


@pytest.mark.parametrize(
    "case", [case for case in DATA_CASES if case.check.label], ids=lambda case: case.case_id
)
def test_invented_rows_and_contradicting_presentations_fail_on_real_oracles(
    expected: dict[str, ReferenceResult], case: EvalCase
) -> None:
    result = expected[case.case_id]
    metric = case.check.metrics[0]
    good = _formats(case, result)["bullets com nota"].splitlines()[1:-1]
    first = required_rows(case.check, list(result.records()))[0]
    invented = f"- **Inventado Qwerty**: {_render_value(metric, first[metric.column])}"
    text = "\n".join([good[0], invented, *good[1:]])
    verdict = check_answer(case.check, result.columns, result.rows, text)
    assert verdict.verdict == "unsupported", (case.case_id, verdict)
    # uma apresentação completa com um valor errado não é salva por outra certa
    shifted = _render_value(metric, (first[metric.column] or 0) + 1000)
    wrong = [good[0].rsplit(": ", 1)[0] + f": {shifted}", *good[1:]]
    both = "\n".join(wrong) + "\n\n" + "\n".join(good)
    verdict = check_answer(case.check, result.columns, result.rows, both)
    assert verdict.verdict == "contradiction", (case.case_id, verdict)


@pytest.mark.parametrize(("case_id", "why", "sql"), WRONG, ids=[w[1] for w in WRONG])
def test_sql_with_wrong_semantics_fails(
    real_db: SafeDatabase, case_id: str, why: str, sql: str
) -> None:
    score = _score_sql(real_db, case_id, sql)
    assert score.verdict is Verdict.FAIL, (case_id, why)
    assert score.category is FailureCategory.RESULT_MISMATCH, (case_id, why, score.reason)


def test_freeform_answers_are_not_official_answers_in_disguise(
    expected: dict[str, ReferenceResult],
) -> None:
    # além do texto e do SQL diferentes (test_evals_corpus), o resultado também é outro: nenhuma
    # pergunta livre devolve as mesmas linhas de um exemplo oficial
    official = {
        case_id: {row for row in result.rows}
        for case_id, result in expected.items()
        if CASES_BY_ID[case_id].category.value == "official"
    }
    for case in DATA_CASES:
        if case.category.value != "freeform":
            continue
        mine = set(expected[case.case_id].rows)
        for case_id, rows in official.items():
            assert mine != rows, (case.case_id, case_id)
            assert not mine <= rows, (case.case_id, case_id)  # nem um recorte de um oficial


# --- gabaritos livres contra cálculos independentes -----------------------------------------


def test_freeform_oracles_match_independent_python(
    real_db: SafeDatabase, expected: dict[str, ReferenceResult]
) -> None:
    genres = dict(_bulk(real_db, "SELECT nome_genero, sk_genre_id FROM dim_genres"))
    by_genre: dict[str, set[str]] = {}
    for movie, genre in _bulk(real_db, "SELECT sk_movie_id, sk_genre_id FROM bridge_movie_genre"):
        by_genre.setdefault(genre, set()).add(movie)
    horror, animation = by_genre[genres["Horror"]], by_genre[genres["Animation"]]

    # livre_01: atores com mais filmes de terror (contagem e ranking em Python)
    actors = dict(
        _bulk(
            real_db, "SELECT sk_person_id, nome_pessoa FROM dim_people WHERE tipo_pessoa = 'Ator'"
        )
    )
    films: Counter[str] = Counter()
    in_horror = (
        f"SELECT sk_movie_id FROM bridge_movie_genre WHERE sk_genre_id = '{genres['Horror']}'"  # noqa: E501, S608
    )
    cast = f"SELECT sk_movie_id, sk_person_id FROM bridge_movie_person WHERE sk_movie_id IN ({in_horror})"  # noqa: E501, S608
    for movie, person in _bulk(real_db, cast):
        if person in actors and movie in horror:
            films[person] += 1
    people = list(films)
    ranks = competition_ranks([films[p] for p in people])
    top = sorted(
        (rank, actors[p], films[p]) for p, rank in zip(people, ranks, strict=True) if rank <= 5
    )
    assert tuple(top) == expected["livre_01_top5_atores_terror"].rows

    # livre_02: filmes da linha "Christopher Nolan" com papel Diretor
    [(nolan,)] = _bulk(
        real_db,
        "SELECT sk_person_id FROM dim_people"
        " WHERE nome_pessoa = 'Christopher Nolan' AND tipo_pessoa = 'Diretor'",
    )
    links = (
        f"SELECT sk_movie_id, sk_person_id FROM bridge_movie_person WHERE sk_person_id = '{nolan}'"  # noqa: E501, S608
    )
    directed = {movie for movie, person in _bulk(real_db, links) if person == nolan}
    movies = {
        sk: (id_filme, titulo, ano)
        for sk, id_filme, titulo, ano in _bulk(
            real_db, "SELECT sk_movie_id, id_filme, titulo, ano_lancamento FROM dim_movies"
        )
    }
    perf = {
        sk: (imdb, usd)
        for sk, imdb, usd in _bulk(
            real_db, "SELECT sk_movie_id, nota_imdb, receita_usd FROM fact_movies_performance"
        )
    }
    nolan_rows = sorted((*movies[m], perf[m][0]) for m in directed)
    assert sorted(expected["livre_02_filmes_dirigidos_nolan"].rows) == nolan_rows

    # livre_03: soma exata em Decimal da receita em USD
    total = sum(
        (
            Decimal(repr(perf[m][1]))
            for m in animation
            if movies[m][2] == 2019 and perf[m][1] is not None
        ),
        Decimal(0),
    )
    [(oracle_total,)] = expected["livre_03_receita_usd_animacao_2019"].rows
    assert abs(Decimal(repr(oracle_total)) - total) <= Decimal("0.005")

    # livre_04: comparação de datas ISO em Python
    start, end = (d.isoformat() for d in rolling_window(REF, 3))
    dates = _bulk(real_db, "SELECT data_lancamento FROM dim_movies")
    in_window = sum(1 for (value,) in dates if value is not None and start <= value <= end)
    assert expected["livre_04_filmes_ultimos_3_anos"].rows == ((in_window,),)


# --- premissas da política e das perguntas livres -------------------------------------------


def test_policy_and_freeform_premises(real_db: SafeDatabase) -> None:
    index = EntityIndex(real_db)
    elemental = index.find("filme", "Elemental")
    assert elemental.state is MatchState.EXACT_MULTIPLE
    assert elemental.total_matches == len(elemental.candidates) == 2  # cobertura comprovável
    # o gabarito só da avaliação traz exatamente os homônimos que find_entities encontra
    oracle = _oracle(real_db, "politica_01_titulo_ambiguo")
    assert {r["id_filme"] for r in oracle.records()} == {c.movie_id for c in elemental.candidates}
    assert sorted((r["ano_lancamento"], r["nota_imdb"]) for r in oracle.records()) == [
        (2022, 6.7),
        (2023, 7.0),
    ]
    assert index.find("pessoa", "Christopher Nolan", role="Diretor").state is (
        MatchState.EXACT_UNIQUE
    )
    for text, name in (("Terror", "Horror"), ("Animação", "Animation")):
        match = index.find("genero", text)
        assert match.state is MatchState.EXACT_UNIQUE and match.resolved.label == name
    roles = _bulk(
        real_db,
        "SELECT DISTINCT p.tipo_pessoa FROM dim_people AS p"
        " JOIN bridge_movie_person AS b ON b.sk_person_id = p.sk_person_id"
        " JOIN dim_movies AS m ON m.sk_movie_id = b.sk_movie_id"
        " WHERE p.nome_pessoa = 'Christopher Nolan' AND m.titulo = 'Tenet'",
    )
    assert roles == (("Roteirista",),)  # a armadilha do livre_02: Tenet só como roteirista
    # livre_04: o primeiro dia da janela e as datas futuras mudam a contagem
    [(boundary,)] = _bulk(
        real_db, "SELECT COUNT(*) FROM dim_movies WHERE data_lancamento = '2023-10-01'"
    )
    [(future,)] = _bulk(
        real_db, "SELECT COUNT(*) FROM dim_movies WHERE data_lancamento > '2026-10-01'"
    )
    assert boundary > 0 and future > 0
