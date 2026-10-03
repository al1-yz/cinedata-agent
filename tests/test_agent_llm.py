"""Smoke test do agente com um LLM REAL (OpenRouter). CONSOME COTA: fica fora do pytest padrão.

Rodar só de propósito, na raiz do repositório e com o `.env` configurado:

    pytest -m llm tests/test_agent_llm.py -v -s
    pytest -m llm tests/test_agent_llm.py -v -s -k top_revenue     # uma pergunta só

Só o modelo principal (CINEDATA_MODEL) é usado: os fallbacks do `.env` são descartados aqui, para
que tentativas extras de fallback não multipliquem o gasto. Cada pergunta faz no máximo
CINEDATA_REQUEST_LIMIT requisições de agente. Os testes conferem comportamento observável
(ferramentas usadas, fundamentação, tratamento de ambiguidade e o resultado contra um oráculo
independente), nunca a redação da resposta. Com `-s`, cada teste imprime o desfecho em JSON para
auditoria manual.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai import models

from cinedata.agent import ask
from cinedata.config import Settings, load_settings
from cinedata.db import SafeDatabase
from cinedata.entities import MatchState
from cinedata.reference import OFFICIAL_CASES, run_case
from cinedata.runtime import AgentOutcome, AnswerStatus

pytestmark = pytest.mark.llm

ROOT = Path(__file__).resolve().parents[1]
_ENV_AT_IMPORT = dict(os.environ)  # o conftest limpa o ambiente de cada teste; a chave vem daqui


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    loaded = load_settings(env=_ENV_AT_IMPORT, dotenv_path=ROOT / ".env")
    if loaded.api_key is None or loaded.model is None:
        pytest.skip("configure OPENROUTER_API_KEY e CINEDATA_MODEL no .env")
    db_path = loaded.db_path if loaded.db_path.is_absolute() else ROOT / loaded.db_path
    if not db_path.is_file():
        pytest.skip("data/cinerocket.db não encontrado")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    return replace(loaded, db_path=db_path, fallback_models=())  # só o modelo principal


@pytest.fixture
def oracle_db(settings: Settings) -> Iterator[SafeDatabase]:
    with SafeDatabase(settings.db_path, max_rows=50, timeout_s=60.0) as database:
        yield database


def ask_and_report(question: str, settings: Settings) -> AgentOutcome:
    outcome = ask(question, settings)
    print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2, default=str))
    trace = outcome.trace
    assert outcome.failure is None, outcome.failure
    assert trace.model_responses <= settings.request_limit
    return outcome


def cells(outcome: AgentOutcome) -> set[str]:
    """Todos os valores das consultas bem-sucedidas, como texto."""
    return {str(cell) for e in outcome.trace.successful_sql() for row in e.rows for cell in row}


def test_top_revenue_with_a_synonym(settings: Settings, oracle_db: SafeDatabase) -> None:
    outcome = ask_and_report("Quais são os 5 filmes com maior faturamento?", settings)

    assert outcome.answer is not None and outcome.answer.status is AnswerStatus.DATA_ANSWER
    queries = outcome.trace.successful_sql()
    assert queries and any("receita_" in e.sql.lower() for e in queries)  # faturamento = receita
    case = next(c for c in OFFICIAL_CASES if c.case_id == "oficial_01_maior_receita")
    expected = run_case(oracle_db, replace(case, limit=5))
    titles = {row[expected.columns.index("titulo")] for row in expected.rows}
    assert titles <= cells(outcome)


def test_question_outside_the_official_examples(
    settings: Settings, oracle_db: SafeDatabase
) -> None:
    outcome = ask_and_report("Qual gênero tem mais filmes com nota IMDb acima de 8?", settings)

    assert outcome.answer is not None and outcome.answer.status is AnswerStatus.DATA_ANSWER
    oracle = oracle_db.execute(
        "WITH contagem AS ("
        " SELECT b.sk_genre_id, COUNT(DISTINCT b.sk_movie_id) AS filmes"
        " FROM bridge_movie_genre AS b"
        " WHERE b.sk_movie_id IN (SELECT sk_movie_id FROM fact_movies_performance"
        "                         WHERE nota_imdb > 8)"
        " GROUP BY b.sk_genre_id)"
        " SELECT g.nome_genero, c.filmes FROM contagem AS c"
        " JOIN dim_genres AS g ON g.sk_genre_id = c.sk_genre_id"
        " WHERE c.filmes = (SELECT MAX(filmes) FROM contagem)"
    )
    leaders = {row[0] for row in oracle.rows}
    assert leaders & cells(outcome), (leaders, outcome.trace.successful_sql())


def test_ambiguous_title_is_not_silently_resolved(settings: Settings) -> None:
    outcome = ask_and_report("Qual é a nota IMDb do filme Elemental?", settings)

    lookups = [
        lookup
        for lookup in outcome.trace.entity_lookups
        if lookup.kind == "filme" and lookup.state is MatchState.EXACT_MULTIPLE
    ]
    assert lookups, "o agente deveria ter procurado o filme e encontrado homônimos"
    keys = {candidate.key for candidate in lookups[0].candidates}
    used = {key for e in outcome.trace.successful_sql() for key in keys if f"'{key}'" in e.sql}
    assert outcome.answer is not None
    # pedir esclarecimento, ou responder cobrindo os candidatos; nunca escolher um só sozinho
    assert outcome.answer.status is AnswerStatus.CLARIFICATION or len(used) != 1


def test_explicit_year_resolves_the_homonym(settings: Settings, oracle_db: SafeDatabase) -> None:
    outcome = ask_and_report("Qual é a nota IMDb do filme Elemental de 2023?", settings)

    assert outcome.answer is not None and outcome.answer.status is AnswerStatus.DATA_ANSWER
    expected = oracle_db.execute(
        "SELECT f.nota_imdb FROM dim_movies AS m"
        " JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id"
        " WHERE m.titulo = 'Elemental' AND m.ano_lancamento = 2023"
    )
    [(nota,)] = expected.rows
    assert str(nota) in cells(outcome)
