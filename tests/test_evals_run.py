"""Executor da avaliação M3, offline: dry-run, execução com modelo roteirizado, resultados e resume.

Nenhum teste aqui fala com um provedor. O "modelo" é um `FunctionModel` por caso; o banco é um
cenário sintético com a forma da Gold; o gabarito é o SQL do M1c executado nele.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cinedata.agent import OUTPUT_RETRIES
from cinedata.config import load_settings
from cinedata.reference import ReferenceCase
from cinedata.runtime import AnswerStatus
from evals import run as runner
from evals.cases import (
    CASES_BY_ID,
    MOVIE,
    Category,
    EvalCase,
    ResultCheck,
    Shape,
    money,
    select_cases,
)
from evals.run import ResultStore, main, render_markdown, run_cases, run_config, scrub
from gold_db import REAL_GENRES, GoldScenario
from scripted import Script, final, sql

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
REF = "2026-10-01"

TOP_SQL = (
    "SELECT m.titulo AS filme, f.receita_brl AS faturamento, m.ano_lancamento AS ano"
    " FROM fact_movies_performance AS f JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id"
    " WHERE f.receita_brl IS NOT NULL ORDER BY f.receita_brl DESC LIMIT 10"
)
GENRES_SQL = (
    "SELECT g.nome_genero AS genero, COUNT(DISTINCT b.sk_movie_id) AS total"
    " FROM dim_genres AS g LEFT JOIN bridge_movie_genre AS b ON b.sk_genre_id = g.sk_genre_id"
    " GROUP BY g.sk_genre_id, g.nome_genero ORDER BY total DESC"
)
Q01, Q10 = "oficial_01_maior_receita", "oficial_10_filmes_por_genero"
OUT = "politica_02_fora_do_escopo"


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    scenario = GoldScenario()
    scenario.movie("Avatar", id_filme="10", date="2009-12-18", receita=2900.0, genres=["Action"])
    scenario.movie("Titanic", id_filme="11", date="1997-12-19", receita=2200.5, genres=["Drama"])
    scenario.movie("Dune", id_filme="12", date="2021-10-22", receita=400.25, genres=["Drama"])
    scenario.movie("Sem Receita", id_filme="13", date="2020-01-01", genres=["Horror"])
    return scenario.write(tmp_path_factory.mktemp("evals") / "gold.db")


@pytest.fixture
def settings(db_path: Path):  # noqa: ANN201
    base = load_settings(env={}, dotenv_path=db_path.parent / "sem.env")
    return replace(
        base,
        db_path=db_path,
        model="provedor/modelo",
        reference_date=date(2026, 10, 1),
        reference_date_from_env=True,
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("CINEDATA_MODEL", "provedor/modelo")
    monkeypatch.setenv("CINEDATA_DB_PATH", str(db_path))
    monkeypatch.setenv("CINEDATA_REFERENCE_DATE", REF)


def scripts(**by_case: Script):  # noqa: ANN201
    """`model_for` que entrega o roteiro de cada caso e falha se um caso inesperado rodar."""
    calls: list[str] = []

    def model_for(case: EvalCase):  # noqa: ANN202
        calls.append(case.case_id)
        if case.case_id not in by_case:
            raise AssertionError(f"o caso {case.case_id} não deveria chamar o modelo")
        return by_case[case.case_id].model

    model_for.calls = calls  # type: ignore[attr-defined]
    return model_for


TOP_ANSWER = "Avatar (2009): R$ 2.900,00\nTitanic (1997): R$ 2.200,50\nDune (2021): R$ 400,25"
GENRE_COUNTS = {"Action": 1, "Drama": 2, "Horror": 1}  # os 19 gêneros existem; os demais têm 0
GENRES_ANSWER = "\n".join(f"{name}: {GENRE_COUNTS.get(name, 0)} filme(s)" for name in REAL_GENRES)


def good_top() -> Script:
    return Script([sql(TOP_SQL)], [final("data_answer", TOP_ANSWER)])


def good_genres() -> Script:
    return Script([sql(GENRES_SQL)], [final("data_answer", GENRES_ANSWER)])


def rate_limited() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(429, "provedor/modelo", body={"error": {"message": "limite"}})

    return FunctionModel(respond, model_name="provedor/modelo")


def store_for(path: Path, settings, **kwargs) -> ResultStore:  # noqa: ANN001
    return ResultStore(path, run_config(settings), secret=FAKE_KEY, **kwargs)


# --- dry-run ----------------------------------------------------------------------------------


def test_dry_run_never_calls_the_agent_and_blocks_model_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], configured: None
) -> None:
    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("o dry-run chamou o agente")

    monkeypatch.setattr(runner, "ask", forbidden)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)  # mesmo que estivesse liberado
    assert main(["--tier", "full"]) == 0
    out = capsys.readouterr().out
    assert models.ALLOW_MODEL_REQUESTS is False
    assert "DRY-RUN" in out and "nenhuma chamada ao provedor" in out
    assert "26 pergunta(s) x 5 = 130 requisição(ões)" in out
    for case_id in (Q01, "livre_01_top5_atores_terror", OUT):
        assert case_id in out


def test_dry_run_needs_no_api_key_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], configured: None, tmp_path: Path
) -> None:
    target = tmp_path / "saida.json"
    assert main(["--tier", "smoke", "--out", str(target)]) == 0
    assert "Chave da API           ausente" in capsys.readouterr().out
    assert not target.exists() and not target.with_suffix(".md").exists()


def test_budget_shows_fallback_multiplication_and_primary_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], configured: None
) -> None:
    monkeypatch.setenv("CINEDATA_FALLBACK_MODELS", "reserva/um,reserva/dois")
    assert main(["--tier", "smoke"]) == 0
    assert "até 60 chamada(s) HTTP" in capsys.readouterr().out  # 4 x 5 x (1 + 2)
    assert main(["--tier", "smoke", "--primary-only"]) == 0
    out = capsys.readouterr().out
    assert "até 20 chamada(s) HTTP" in out and "Fallbacks              nenhum" in out


def test_selection_and_model_override(
    capsys: pytest.CaptureFixture[str], configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["--id", OUT, "--id", Q01, "--model", "outro/modelo:free"]) == 0
    out = capsys.readouterr().out
    assert "2 caso(s)" in out and "outro/modelo:free" in out
    assert out.index(OUT) < out.index(Q01)
    assert main(["--tier", "official", "--limit", "2"]) == 0
    assert "2 caso(s)" in capsys.readouterr().out
    monkeypatch.setenv("CINEDATA_FALLBACK_MODELS", "reserva/um")
    assert main(["--model", "reserva/um"]) == 2  # principal repetido nos fallbacks
    assert main(["--id", "nao_existe"]) == 2
    assert main(["--limit", "0"]) == 2
    assert main(["--tier", "tudo"]) == 2


def test_dry_run_can_check_the_oracles_without_a_provider(
    capsys: pytest.CaptureFixture[str], configured: None
) -> None:
    assert main(["--id", Q01, "--id", OUT, "--check-oracles"]) == 0
    out = capsys.readouterr().out
    assert f"{Q01}: 3 linha(s)" in out and f"{OUT}: sem gabarito" in out


# --- execução real exige opt-in e configuração fixa -----------------------------------------


def test_live_requires_a_fixed_reference_date_and_a_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: None,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runner, "ask", lambda *a, **k: pytest.fail("não deveria executar"))
    out = tmp_path / "r.json"
    assert main(["--id", Q01, "--live", "--out", str(out)]) == 2  # sem chave
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.delenv("CINEDATA_REFERENCE_DATE")
    assert main(["--id", Q01, "--live", "--out", str(out)]) == 2
    assert "CINEDATA_REFERENCE_DATE" in capsys.readouterr().err
    assert not out.exists()


def test_live_cli_end_to_end_with_scripted_models(
    capsys: pytest.CaptureFixture[str], configured: None, tmp_path: Path
) -> None:
    out = tmp_path / "r.json"
    model_for = scripts(**{Q01: good_top(), OUT: Script([final("out_of_scope", "Só filmes.")])})
    code = main(["--id", Q01, "--id", OUT, "--live", "--out", str(out)], model_for=model_for)
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "EXECUÇÃO REAL" in printed and "PASS" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["config"]["reference_date"] == REF and data["config"]["model"] == "provedor/modelo"
    record = data["results"][Q01]
    assert {"evaluation", "agent", "database_sha256"} <= set(data["config"])
    assert len(data["config"]["database_sha256"]) == 64
    for field in (
        "started_at",
        "environment",
        "elapsed_s",
        "verdict",
        "failure_category",
        "reason",
        "status",
        "usage",
        "grounding",
        "sql_check",
        "answer_check",
        "expected",
        "outcome",
        "fingerprint",
    ):
        assert field in record, field
    assert record["verdict"] == "pass" and record["status"] == "data_answer"
    assert record["usage"]["models_used"] == ["roteiro"]
    assert record["usage"]["model_responses"] == 2 and record["usage"]["tool_calls"] == 1
    assert record["grounding"]["successful_sql"] == 1
    assert record["expected"]["rows"][0][2] == "Avatar"
    assert (
        record["answer_check"]["verdict"] == "ok" and record["answer_check"]["required_rows"] == 3
    )
    assert set(record["environment"]) == {"python", "sqlite", "pydantic_ai", "openai"}
    assert data["config"]["environment"] == record["environment"]  # condição do --resume
    assert record["outcome"]["runtime"]["sql"][0]["sql"] == TOP_SQL
    summary = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "Taxa de acerto sobre os avaliados: 2/2" in summary
    # 2/2 é desta seleção, não do corpus: o resumo diz quantos casos ficaram de fora
    assert "Seleção: 2 de 26 casos do corpus" in summary


def test_existing_results_are_never_overwritten_without_resume(
    capsys: pytest.CaptureFixture[str], configured: None, tmp_path: Path
) -> None:
    out = tmp_path / "r.json"
    out.write_text("{}", encoding="utf-8")
    model_for = scripts()
    assert main(["--id", OUT, "--live", "--out", str(out)], model_for=model_for) == 2
    assert "--resume" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "{}" and model_for.calls == []


# --- execução, interrupção e resume ---------------------------------------------------------


def test_semantic_failure_is_recorded_with_the_detail(settings, tmp_path: Path) -> None:  # noqa: ANN001
    wrong = TOP_SQL.replace("DESC", "ASC")
    model_for = scripts(**{Q01: Script([sql(wrong)], [final("data_answer", "Dune lidera.")])})
    store = store_for(tmp_path / "r.json", settings)
    report = run_cases(select_cases(ids=[Q01]), settings, store, model_for=model_for, log=print)
    record = store.results[Q01]
    assert report.executed == [Q01] and report.stopped is None
    assert (record["verdict"], record["failure_category"], record["detail"]) == (
        "fail",
        "result_mismatch",
        "wrong_order",
    )


def test_provider_failure_stops_the_run_without_retry_and_is_not_evaluated(
    settings,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    calls: list[str] = []

    def model_for(case: EvalCase):  # noqa: ANN202
        calls.append(case.case_id)
        return good_top().model if case.case_id == Q01 else rate_limited()

    store = store_for(tmp_path / "r.json", settings)
    cases = select_cases(ids=[Q01, Q10, OUT])
    report = run_cases(cases, settings, store, model_for=model_for, log=print)
    assert calls == [Q01, Q10]  # parou no primeiro 429, sem repetir e sem seguir para o OUT
    assert report.stopped is not None and Q10 in report.stopped
    assert store.results[Q01]["verdict"] == "pass"
    failed = store.results[Q10]
    assert (failed["verdict"], failed["failure_category"]) == ("error", "provider")
    assert failed["outcome"]["failure"]["kind"] == "rate_limited"
    assert OUT not in store.results
    summary = render_markdown(store.data)
    assert "Avaliados: 1 (pass 1, fail 0)" in summary
    assert "Não avaliados (provedor, banco, gabarito ou pontuador): 1, fora da taxa" in summary
    assert "Pendentes (não executados): 1" in summary
    assert "Taxa de acerto sobre os avaliados: 1/1" in summary


def test_a_rejected_request_is_an_evaluated_failure_and_stops_the_run(
    settings, tmp_path: Path  # noqa: ANN001
) -> None:  # fmt: skip
    def bad_request(case: EvalCase):  # noqa: ANN202
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise ModelHTTPError(400, "provedor/modelo", body={"error": {"message": "invalid"}})

        return FunctionModel(respond, model_name="provedor/modelo")

    store = store_for(tmp_path / "r.json", settings)
    report = run_cases(select_cases(ids=[Q01, OUT]), settings, store, model_for=bad_request,
                       log=print)  # fmt: skip
    record = store.results[Q01]
    assert (record["verdict"], record["failure_category"]) == ("fail", "agent_protocol")
    assert report.stopped is not None and OUT not in store.results  # o próximo caso fica pendente
    summary = render_markdown(store.data)
    assert "Avaliados: 1 (pass 0, fail 1)" in summary and "Pendentes (não executados): 1" in summary
    resumed = store_for(tmp_path / "r.json", settings, resume=True)
    again = scripts(**{OUT: Script([final("out_of_scope", "x")])})
    run_cases(select_cases(ids=[Q01, OUT]), settings, resumed, model_for=again, log=print)
    assert again.calls == [OUT]  # a falha avaliada não roda de novo


def test_resume_skips_evaluated_cases_and_reruns_the_rest(settings, tmp_path: Path) -> None:  # noqa: ANN001
    path = tmp_path / "r.json"
    cases = select_cases(ids=[Q01, Q10, OUT])
    first = scripts(**{Q01: good_top(), Q10: Script()})  # Q10: o roteiro vazio nunca é usado

    def failing_q10(case: EvalCase):  # noqa: ANN202
        return rate_limited() if case.case_id == Q10 else first(case)

    run_cases(cases, settings, store_for(path, settings), model_for=failing_q10, log=print)
    second = scripts(**{Q10: good_genres(), OUT: Script([final("out_of_scope", "Só filmes.")])})
    store = store_for(path, settings, resume=True)
    report = run_cases(cases, settings, store, model_for=second, log=print)
    assert second.calls == [Q10, OUT]  # o Q01 já avaliado não gasta cota de novo
    assert report.skipped == [Q01]
    assert {case_id: r["verdict"] for case_id, r in store.results.items()} == {
        Q01: "pass",
        Q10: "pass",
        OUT: "pass",
    }


def test_resume_reruns_a_case_whose_definition_changed(settings, tmp_path: Path) -> None:  # noqa: ANN001
    path = tmp_path / "r.json"
    case = CASES_BY_ID[OUT]
    run_cases(
        [case],
        settings,
        store_for(path, settings),
        model_for=scripts(**{OUT: Script([final("out_of_scope", "x")])}),
        log=print,
    )
    changed = replace(case, question="Qual é a cotação do dólar hoje?")
    again = scripts(**{OUT: Script([final("out_of_scope", "x")])})
    run_cases(
        [changed], settings, store_for(path, settings, resume=True), model_for=again, log=print
    )
    assert again.calls == [OUT]


@pytest.mark.parametrize(
    "change",
    [
        {"model": "outro/modelo"},
        {"reference_date": date(2026, 10, 2)},
        {"fallback_models": ("reserva/um",)},
        {"request_limit": 6},
        {"max_rows": 20},
    ],
)
def test_resume_refuses_to_mix_configurations(settings, tmp_path: Path, change: dict) -> None:  # noqa: ANN001
    path = tmp_path / "r.json"
    run_cases(
        [CASES_BY_ID[OUT]],
        settings,
        store_for(path, settings),
        model_for=scripts(**{OUT: Script([final("out_of_scope", "x")])}),
        log=print,
    )
    with pytest.raises(runner.UsageError, match="--resume recusado"):
        store_for(path, replace(settings, **change), resume=True)


def _one_evaluated_case(path: Path, settings) -> None:  # noqa: ANN001
    run_cases(
        [CASES_BY_ID[OUT]],
        settings,
        store_for(path, settings),
        model_for=scripts(**{OUT: Script([final("out_of_scope", "x")])}),
        log=print,
    )


def test_resume_refuses_a_database_with_other_contents_and_the_same_size(
    settings, tmp_path: Path, db_path: Path  # noqa: ANN001
) -> None:  # fmt: skip
    path = tmp_path / "r.json"
    _one_evaluated_case(path, settings)
    copy = tmp_path / "copia.db"
    copy.write_bytes(db_path.read_bytes())
    con = sqlite3.connect(copy)
    with con:
        con.execute("UPDATE dim_movies SET titulo = 'Avatax' WHERE titulo = 'Avatar'")
    con.close()
    assert copy.stat().st_size == db_path.stat().st_size  # o tamanho sozinho não denunciaria
    with pytest.raises(runner.UsageError, match="database_sha256"):
        store_for(path, replace(settings, db_path=copy), resume=True)


def _changed_copy(source: Path, folder: Path) -> Path:
    copy = folder / source.name
    copy.write_bytes(source.read_bytes() + b"\n# mudanca\n")
    return copy


def test_resume_refuses_a_changed_agent(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:  # fmt: skip
    path = tmp_path / "r.json"
    _one_evaluated_case(path, settings)
    files = list(runner.AGENT_FILES)
    position = [f.name for f in files].index("prompt.py")
    files[position] = _changed_copy(files[position], tmp_path)
    monkeypatch.setattr(runner, "AGENT_FILES", tuple(files))
    with pytest.raises(runner.UsageError, match="agent"):
        store_for(path, settings, resume=True)


def test_resume_refuses_a_changed_evaluation_oracle_or_runner(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:  # fmt: skip
    path = tmp_path / "r.json"
    _one_evaluated_case(path, settings)
    for name in ("cases.py", "scoring.py", "run.py", "reference.py"):
        files = list(runner.EVALUATION_FILES)
        position = [f.name for f in files].index(name)
        folder = tmp_path / name.removesuffix(".py")
        folder.mkdir()
        files[position] = _changed_copy(files[position], folder)
        with monkeypatch.context() as patch:
            patch.setattr(runner, "EVALUATION_FILES", tuple(files))
            with pytest.raises(runner.UsageError, match="evaluation"):
                store_for(path, settings, resume=True)
    store_for(path, settings, resume=True)  # sem mudança, continua aceito


def test_fingerprints_cover_the_behavioural_files() -> None:
    assert {f.name for f in runner.AGENT_FILES} == {
        "agent.py", "prompt.py", "runtime.py", "llm.py", "entities.py", "db.py", "config.py",
    }  # fmt: skip
    assert [f.parent.name + "/" + f.name for f in runner.EVALUATION_FILES] == [
        "evals/cases.py",
        "evals/scoring.py",
        "evals/run.py",
        "cinedata/reference.py",
    ]
    assert all(f.is_file() for f in (*runner.AGENT_FILES, *runner.EVALUATION_FILES))
    assert all(f.suffix == ".py" for f in runner.EVALUATION_FILES)  # documentação não entra


def test_file_fingerprint_ignores_line_endings_only(tmp_path: Path) -> None:
    unix, windows = tmp_path / "u" / "a.py", tmp_path / "w" / "a.py"
    unix.parent.mkdir()
    windows.parent.mkdir()
    unix.write_bytes(b"x = 1\ny = 2\n")
    windows.write_bytes(b"x = 1\r\ny = 2\r\n")
    assert runner.files_fingerprint([unix]) == runner.files_fingerprint([windows])
    windows.write_bytes(b"x = 1\r\ny = 3\r\n")
    assert runner.files_fingerprint([unix]) != runner.files_fingerprint([windows])


def test_database_fingerprint_includes_the_wal_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "x.db"
    database.write_bytes(b"conteudo")
    alone = runner.database_fingerprint(database)
    wal = tmp_path / "x.db-wal"
    wal.write_bytes(b"paginas recentes")
    with_wal = runner.database_fingerprint(database)
    assert alone != with_wal and len(with_wal) == 64
    reads: list[Path] = []
    original = Path.open

    def counting(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    assert runner.database_fingerprint(database) == with_wal
    assert reads == []  # o mesmo banco não é lido de novo no mesmo processo
    assert runner.database_fingerprint(tmp_path / "nao_existe.db") is None


def test_the_agent_never_returns_an_answer_written_with_its_own_query(
    settings, tmp_path: Path
) -> None:  # noqa: ANN001, E501
    # Resposta 1: um SQL irrelevante, bem-sucedido. Resposta 2: o SQL certo e a resposta final
    # juntos. O PydanticAI 2 (end_strategy 'graceful') executa os dois; a PRODUÇÃO recusa a
    # resposta final (ela não leu o SQL da mesma resposta), o modelo lê o resultado e responde na
    # resposta 3. O texto escrito sem ler nunca sai do agente.
    warmup = "SELECT COUNT(*) AS n FROM dim_movies"
    stale = "Avatar (2009): R$ 1,00\nTitanic (1997): R$ 2,00\nDune (2021): R$ 3,00"
    script = Script(
        [sql(warmup)],
        [sql(TOP_SQL), final("data_answer", stale)],
        [final("data_answer", TOP_ANSWER)],
    )
    store = store_for(tmp_path / "r.json", settings)
    run_cases(select_cases(ids=[Q01]), settings, store, model_for=scripts(**{Q01: script}),
              log=print)  # fmt: skip
    record = store.results[Q01]
    runtime = record["outcome"]["runtime"]
    assert [(q["index"], q["step"], q["status"]) for q in runtime["sql"]] == [
        (1, 1, "ok"),
        (2, 2, "ok"),
    ]
    assert [r["step"] for r in runtime["output_rejections"]] == [2]
    assert "na mesma resposta do final_answer" in runtime["output_rejections"][0]["reason"]
    assert record["outcome"]["answer"]["answer"] == TOP_ANSWER  # nunca o texto prematuro
    assert record["usage"]["model_responses"] == 3
    assert record["verdict"] == "pass", record["reason"]
    # um modelo que insiste em responder junto com a consulta termina sem resposta: é uma falha
    # de protocolo avaliada, nunca um pass apoiado em consulta não lida
    insisting = Script(
        [sql(warmup)], *[[sql(TOP_SQL), final("data_answer", TOP_ANSWER)]] * (OUTPUT_RETRIES + 1)
    )
    store = store_for(tmp_path / "r2.json", settings)
    run_cases(select_cases(ids=[Q01]), settings, store, model_for=scripts(**{Q01: insisting}),
              log=print)  # fmt: skip
    record = store.results[Q01]
    assert record["outcome"]["answer"] is None
    assert (record["verdict"], record["failure_category"]) == ("fail", "agent_protocol")


EMPTY_REFERENCE = ReferenceCase(
    case_id="teste_sem_resultado",
    question="Quais filmes lançados em 2099 têm receita informada?",
    semantics="Nenhum filme do cenário é de 2099: o gabarito certo é vazio.",
    sql=(
        "SELECT m.id_filme, m.titulo, m.ano_lancamento, f.receita_brl FROM dim_movies AS m"
        " JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id"
        " WHERE m.ano_lancamento = 2099 AND f.receita_brl IS NOT NULL ORDER BY m.id_filme"
    ),
    columns=("id_filme", "titulo", "ano_lancamento", "receita_brl"),
    key_columns=("id_filme",),
)
EMPTY_CASE = EvalCase(
    case_id="teste_sem_resultado",
    category=Category.FREEFORM,
    question=EMPTY_REFERENCE.question,
    purpose="gabarito vazio: o SQL certo devolve zero linhas",
    expected_status=frozenset({AnswerStatus.DATA_ANSWER}),
    reference=EMPTY_REFERENCE,
    check=ResultCheck(Shape.SET, MOVIE, (money("receita_brl"),)),
)
EMPTY_SQL = (
    "SELECT m.titulo AS filme, m.ano_lancamento AS ano, m.id_filme, f.receita_brl AS receita"
    " FROM dim_movies AS m JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id"
    " WHERE m.ano_lancamento = 2099 AND f.receita_brl IS NOT NULL"
)


@pytest.mark.parametrize(
    ("text", "verdict", "category"),
    [
        ("Há 123 filmes chamados Inventado.", "fail", "answer_text"),
        ("Nenhum filme encontrado.\n- Inventado (2099): R$ 3.000,00", "fail", "answer_text"),
        ("Não há filmes de 2099 com receita informada no catálogo.", "pass", None),
    ],
    ids=["inventado", "linha_inventada", "honesto"],
)
def test_an_empty_correct_result_needs_an_answer_that_says_so(
    settings, tmp_path: Path, text: str, verdict: str, category: str | None  # noqa: ANN001
) -> None:  # fmt: skip
    script = Script([sql(EMPTY_SQL)], [final("data_answer", text)])
    store = store_for(tmp_path / "r.json", settings)
    run_cases([EMPTY_CASE], settings, store, model_for=scripts(teste_sem_resultado=script),
              log=print)  # fmt: skip
    record = store.results[EMPTY_CASE.case_id]
    assert record["expected"]["rows"] == []
    assert record["outcome"]["runtime"]["sql"][0]["row_count"] == 0
    assert record["sql_check"]["chosen_query"] == 1  # o SQL vazio confere com o gabarito vazio
    assert (record["verdict"], record["failure_category"]) == (verdict, category), record["reason"]


def test_policy_text_is_checked_end_to_end(settings, tmp_path: Path) -> None:  # noqa: ANN001
    forecast = Script([final("out_of_scope", "A previsão é 30 °C e vai chover.")])
    store = store_for(tmp_path / "r.json", settings)
    run_cases([CASES_BY_ID[OUT]], settings, store, model_for=scripts(**{OUT: forecast}),
              log=print)  # fmt: skip
    record = store.results[OUT]
    assert (record["verdict"], record["failure_category"]) == ("fail", "policy")


def test_a_scoring_crash_keeps_the_paid_outcome_and_stops_the_run(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:  # fmt: skip
    def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("defeito no pontuador")

    monkeypatch.setattr(runner, "score_case", broken)
    model_for = scripts(**{Q01: good_top(), OUT: Script([final("out_of_scope", "x")])})
    store = store_for(tmp_path / "r.json", settings)
    report = run_cases(select_cases(ids=[Q01, OUT]), settings, store, model_for=model_for,
                       log=print)  # fmt: skip
    record = store.results[Q01]
    assert (record["verdict"], record["failure_category"], record["detail"]) == (
        "error",
        "harness",
        "scoring_error",
    )
    assert "defeito no pontuador" in record["reason"]
    assert record["outcome"]["runtime"]["sql"][0]["sql"] == TOP_SQL  # o desfecho pago fica gravado
    assert report.stopped is not None and model_for.calls == [Q01] and OUT not in store.results


def test_resume_refuses_other_library_versions(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:  # fmt: skip
    path = tmp_path / "r.json"
    _one_evaluated_case(path, settings)
    store_for(path, settings, resume=True)  # o mesmo ambiente continua aceito
    for name in ("python", "sqlite", "pydantic_ai", "openai"):
        changed = {**runner.environment(), name: "0.0.0"}
        with monkeypatch.context() as patch:
            patch.setattr(runner, "environment", lambda changed=changed: changed)
            with pytest.raises(runner.UsageError, match="environment"):
                store_for(path, settings, resume=True)


def test_harness_failure_spends_no_quota_and_is_not_evaluated(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from cinedata.reference import ReferenceCaseError

    def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ReferenceCaseError("gabarito truncado")

    monkeypatch.setattr(runner, "expected_result", broken)
    model_for = scripts()
    store = store_for(tmp_path / "r.json", settings)
    report = run_cases(select_cases(ids=[Q01]), settings, store, model_for=model_for, log=print)
    assert model_for.calls == [] and report.stopped is None
    assert (store.results[Q01]["verdict"], store.results[Q01]["failure_category"]) == (
        "error",
        "harness",
    )


def test_results_never_contain_the_api_key(settings, tmp_path: Path) -> None:  # noqa: ANN001
    leaky = Script([final("out_of_scope", f"Minha chave é {FAKE_KEY}.")])
    store = store_for(tmp_path / "r.json", replace(settings, api_key=FAKE_KEY))
    run_cases([CASES_BY_ID[OUT]], settings, store, model_for=scripts(**{OUT: leaky}), log=print)
    for path in (store.path, store.summary_path):
        text = path.read_text(encoding="utf-8")
        assert FAKE_KEY not in text and "sk-or-v1-" not in text
    assert "[removido]" in store.path.read_text(encoding="utf-8")
    assert scrub(f"a {FAKE_KEY} b sk-or-v1-zzzzzzzzzzzzzzzzzzzz", None).count("[removido]") == 2
    assert scrub("Brisk-walking-in-the-park-forever", None) == "Brisk-walking-in-the-park-forever"


def test_summary_never_counts_unevaluated_cases_as_semantic_results() -> None:
    data = {
        "config": {
            "model": "m/x",
            "fallback_models": [],
            "reference_date": REF,
            "request_limit": 5,
            "max_rows": 50,
        },
        "updated_at": "agora",
        "planned": ["a", "b", "c"],
        "results": {
            "a": {
                "case_id": "a",
                "category": "official",
                "verdict": "error",
                "failure_category": "provider",
                "reason": "rate_limited",
                "expected_status": ["data_answer"],
            },
            "b": {
                "case_id": "b",
                "category": "official",
                "verdict": "error",
                "failure_category": "harness",
                "reason": "x",
                "expected_status": ["data_answer"],
            },
        },
    }
    summary = render_markdown(data)
    assert "Avaliados: 0 (pass 0, fail 0)" in summary
    assert "Taxa de acerto sobre os avaliados: — (nada avaliado)" in summary
    assert "Não avaliados (provedor, banco, gabarito ou pontuador): 2" in summary
    assert "Pendentes (não executados): 1" in summary
