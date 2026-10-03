"""Comando `cinedata ask` de ponta a ponta, offline: o modelo do OpenRouter vira um roteiro.

Tudo o mais é real: configuração pelo ambiente, abertura do banco sintético, agente, ferramentas,
rastro e a saída da CLI.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pydantic_ai
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cinedata.cli import main
from gold_db import GoldScenario
from scripted import Script, final, sql

FAKE_KEY = "sk-or-v1-" + "fedcba9876543210"
EVIL_TITLE = "Evil\x1b]0;pwned\x07\x1b[31m Title\x9b2J"


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    scenario = GoldScenario()
    scenario.movie("Avatar", id_filme="200", date="2009-12-18", receita=2900.0)
    scenario.movie(EVIL_TITLE, id_filme="666", date="2020-01-01")
    return scenario.write(tmp_path_factory.mktemp("cli") / "gold.db")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CINEDATA_MODEL", "provedor/modelo")
    monkeypatch.setenv("CINEDATA_DB_PATH", str(db_path))
    monkeypatch.setenv("CINEDATA_REFERENCE_DATE", "2026-10-01")


def use_model(monkeypatch: pytest.MonkeyPatch, model: FunctionModel) -> None:
    @asynccontextmanager
    async def fake_openrouter(settings):  # noqa: ANN001, ANN202
        assert settings.api_key == FAKE_KEY
        yield model

    monkeypatch.setattr("cinedata.agent.open_openrouter_model", fake_openrouter)


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["ask", *argv])
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    return code, captured.out, captured.err


COUNT = "SELECT count(*) AS filmes FROM dim_movies"


@pytest.mark.usefixtures("configured")
def test_answer_with_assumptions_and_footer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answer = final(
        "data_answer",
        "Há 2 filmes no catálogo.",
        assumptions=["Contei todos os status."],
        caveats=["Catálogo de teste."],
    )
    use_model(monkeypatch, Script([sql(COUNT)], [answer]).model)
    code, out, err = run_cli(capsys, "Quantos filmes existem?")

    assert code == 0 and err == ""
    assert "Há 2 filmes no catálogo." in out
    assert "Premissas:\n  - Contei todos os status." in out
    assert "Ressalvas:\n  - Catálogo de teste." in out
    assert "resposta baseada em 1 consulta(s) ao banco" in out
    assert "modelo: roteiro" in out and "2 resposta(s) do modelo" in out
    assert "SELECT" not in out  # o SQL só aparece com --show-sql


@pytest.mark.usefixtures("configured")
def test_show_sql_comes_from_the_trace_not_from_the_model_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claim = "Rodei SELECT inventado FROM lugar_nenhum e deu 99."
    script = Script(
        [sql("SELECT nada FROM dim_movies")], [sql(COUNT)], [final("data_answer", claim)]
    )
    use_model(monkeypatch, script.model)
    code, out, _ = run_cli(capsys, "--show-sql", "Quantos filmes existem?")

    assert code == 0
    section = out.split("SQL executado (rastro da aplicação):", 1)[1]
    assert "[1] failed: A coluna 'nada' não existe." in section
    assert "SELECT nada FROM dim_movies" in section
    assert "[2] ok, 1 linha(s)" in section and COUNT in section
    assert "inventado" not in section


@pytest.mark.usefixtures("configured")
def test_json_separates_the_model_answer_from_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_model(monkeypatch, Script([sql(COUNT)], [final("data_answer", "2.")]).model)
    code, out, _ = run_cli(capsys, "--json", "Quantos", "filmes", "existem?")

    data = json.loads(out)
    assert code == 0
    assert data["question"] == "Quantos filmes existem?"  # palavras sem aspas viram uma pergunta
    assert set(data["answer"]) == {"status", "answer", "assumptions", "caveats"}
    assert data["failure"] is None and data["notices"] == []
    runtime = data["runtime"]
    assert runtime["reference_date"] == "2026-10-01" and runtime["models_used"] == ["roteiro"]
    assert runtime["configured_models"] == ["provedor/modelo"]
    assert runtime["sql"][0]["sql"] == COUNT and runtime["sql"][0]["rows"] == [[2]]
    assert runtime["model_responses"] == 2 and runtime["max_rows"] == 50


@pytest.mark.usefixtures("configured")
def test_operational_failure_exits_1_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_REQUEST_LIMIT", "2")
    use_model(monkeypatch, Script(*[[sql(f"SELECT {n}")] for n in range(5)]).model)
    code, out, err = run_cli(capsys, "Pergunta sem fim")

    assert code == 1 and out == ""
    assert "Não foi possível responder:" in err and "2 requisições" in err


def test_missing_key_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CINEDATA_MODEL", "provedor/modelo")
    monkeypatch.setenv("CINEDATA_DB_PATH", str(tmp_path / "nao-existe.db"))
    code, _, err = run_cli(capsys, "Pergunta")
    assert code == 2 and "OPENROUTER_API_KEY não configurada" in err  # antes de abrir o banco


def test_missing_model_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    code, _, err = run_cli(capsys, "Pergunta")
    assert code == 2 and "CINEDATA_MODEL não configurado" in err


def test_invalid_configuration_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_MAX_ROWS", "muitas")
    code, _, err = run_cli(capsys, "Pergunta")
    assert code == 2 and "CINEDATA_MAX_ROWS" in err


@pytest.mark.usefixtures("configured")
def test_missing_database_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CINEDATA_DB_PATH", str(tmp_path / "nao-existe.db"))
    code, _, err = run_cli(capsys, "Pergunta")
    assert code == 1 and "Banco de dados indisponível" in err
    assert str(tmp_path) not in err  # só o nome do arquivo, nunca o caminho


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize("question", ["   ", "x" * 2_001])
def test_invalid_question_exits_2(capsys: pytest.CaptureFixture[str], question: str) -> None:
    code, _, err = run_cli(capsys, question)
    assert code == 2 and "Pergunta inválida" in err


@pytest.mark.usefixtures("configured")
def test_ctrl_c_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise KeyboardInterrupt

    use_model(monkeypatch, FunctionModel(interrupt))
    code, _, err = run_cli(capsys, "Pergunta")
    assert code == 130 and "Interrompido" in err


@pytest.mark.usefixtures("configured")
def test_terminal_control_sequences_from_data_or_model_are_stripped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = "SELECT titulo FROM dim_movies WHERE id_filme = '666'"
    script = Script([sql(query)], [final("data_answer", f"O título é {EVIL_TITLE}.")])
    use_model(monkeypatch, script.model)
    code, out, _ = run_cli(capsys, "--show-sql", "Qual o título?")
    assert code == 0 and "Title" in out
    assert not any(char in out for char in ("\x1b", "\x07", "\x9b"))

    use_model(monkeypatch, Script([sql(query)], [final("data_answer", EVIL_TITLE)]).model)
    code, out, _ = run_cli(capsys, "--json", "Qual o título?")
    assert code == 0 and not any(char in out for char in ("\x1b", "\x07", "\x9b"))
    assert json.loads(out)["answer"]["answer"] == EVIL_TITLE  # o JSON preserva o valor, escapado


@pytest.mark.usefixtures("configured")
def test_library_banner_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PYDANTIC_AI_NO_BANNER", raising=False)
    monkeypatch.setattr(pydantic_ai, "BANNER_ENABLED", True)  # restaurado ao fim do teste
    use_model(monkeypatch, Script([final("info", "Respondo perguntas sobre filmes.")]).model)
    code, out, err = run_cli(capsys, "O que você faz?")
    assert code == 0 and "pydantic" not in (out + err).lower()
    assert "resposta informativa, sem dados do banco" in out


def test_show_sql_and_json_are_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ask", "--json", "--show-sql", "Pergunta"]) == 2
    assert "--show-sql" in capsys.readouterr().err


def test_ask_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ask", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--show-sql" in help_text and "--json" in help_text
