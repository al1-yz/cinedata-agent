"""Testes da configuração (offline: sem rede e sem ler o ambiente real)."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from cinedata.config import ALL_ENV_VARS, ConfigError, Settings, load_settings, rolling_window

TODAY = date(2026, 10, 1)
FAKE_KEY = "sk-or-v1-" + "test"  # prefixo certo e valor curto: não se parece com uma chave real


def load(env: dict[str, str] | None = None, **kwargs) -> Settings:
    return load_settings(env if env is not None else {}, today=TODAY, **kwargs)


def write_env(directory: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = directory / ".env"
    path.write_text(text, encoding=encoding)
    return path


# --- padrões e sobrescrita -------------------------------------------------------------------


def test_the_test_environment_is_isolated() -> None:
    for name in ALL_ENV_VARS:
        assert name not in os.environ


def test_defaults_without_any_configuration() -> None:
    settings = load()
    assert settings.api_key is None
    assert settings.model is None
    assert settings.fallback_models == ()
    assert settings.db_path == Path("data") / "cinerocket.db"
    assert settings.max_rows == 50
    assert settings.sql_timeout_s == 30.0
    assert settings.request_limit == 5
    assert settings.reference_date == TODAY
    assert settings.reference_date_from_env is False
    assert settings.dotenv_path is None


def test_env_example_recommends_the_free_router_without_a_key() -> None:
    # o código não tem modelo padrão (teste acima); a recomendação vive só no .env.example
    settings = load(dotenv_path=Path(__file__).resolve().parents[1] / ".env.example")
    assert settings.api_key is None  # o exemplo nunca traz chave
    assert settings.model == "openrouter/free"
    assert settings.fallback_models == ()
    assert settings.reference_date_from_env is False
    defaults = load()
    for name in ("db_path", "max_rows", "sql_timeout_s", "request_limit"):
        assert getattr(settings, name) == getattr(defaults, name), name


def test_default_reference_date_is_today() -> None:
    before = date.today()
    settings = load_settings({})
    after = date.today()
    assert settings.reference_date in {before, after}


def test_every_variable_can_be_overridden() -> None:
    settings = load(
        {
            "OPENROUTER_API_KEY": FAKE_KEY,
            "CINEDATA_MODEL": "google/gemma-4-26b-a4b-it:free",
            "CINEDATA_FALLBACK_MODELS": "nvidia/nemotron-3.5-lightning:free, openrouter/free",
            "CINEDATA_DB_PATH": "outra/pasta/banco.db",
            "CINEDATA_MAX_ROWS": "120",
            "CINEDATA_SQL_TIMEOUT_S": "12.5",
            "CINEDATA_REQUEST_LIMIT": "8",
            "CINEDATA_REFERENCE_DATE": "2025-02-28",
        }
    )
    assert settings.api_key == FAKE_KEY
    assert settings.model == "google/gemma-4-26b-a4b-it:free"
    assert settings.fallback_models == ("nvidia/nemotron-3.5-lightning:free", "openrouter/free")
    assert settings.db_path == Path("outra/pasta/banco.db")
    assert settings.max_rows == 120
    assert settings.sql_timeout_s == 12.5
    assert settings.request_limit == 8
    assert settings.reference_date == date(2025, 2, 28)
    assert settings.reference_date_from_env is True


@pytest.mark.parametrize("blank", ["", " ", "\t", "  \t "])
def test_blank_values_count_as_unset(blank: str) -> None:
    assert load({name: blank for name in ALL_ENV_VARS}) == load()


def test_settings_are_immutable() -> None:
    settings = load()
    with pytest.raises(FrozenInstanceError):
        settings.max_rows = 10  # type: ignore[misc]


# --- valores numéricos -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "attribute", "value", "expected"),
    [
        ("CINEDATA_MAX_ROWS", "max_rows", "1", 1),
        ("CINEDATA_MAX_ROWS", "max_rows", "200", 200),
        ("CINEDATA_MAX_ROWS", "max_rows", " 75 ", 75),
        ("CINEDATA_REQUEST_LIMIT", "request_limit", "1", 1),
        ("CINEDATA_REQUEST_LIMIT", "request_limit", "2", 2),
        ("CINEDATA_REQUEST_LIMIT", "request_limit", "20", 20),
    ],
)
def test_integer_boundaries_are_accepted(
    name: str, attribute: str, value: str, expected: int
) -> None:
    assert getattr(load({name: value}), attribute) == expected


@pytest.mark.parametrize("value", ["1", "30", "300", "12.5", "1.0"])
def test_timeout_accepts_values_in_range(value: str) -> None:
    assert load({"CINEDATA_SQL_TIMEOUT_S": value}).sql_timeout_s == float(value)


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("CINEDATA_MAX_ROWS", "abc"),
        ("CINEDATA_MAX_ROWS", "0"),
        ("CINEDATA_MAX_ROWS", "201"),
        ("CINEDATA_MAX_ROWS", "-5"),
        ("CINEDATA_MAX_ROWS", "1.5"),
        ("CINEDATA_MAX_ROWS", "٣"),  # dígito arábico-índico: não é ASCII
        ("CINEDATA_REQUEST_LIMIT", "0"),
        ("CINEDATA_REQUEST_LIMIT", "-1"),
        ("CINEDATA_REQUEST_LIMIT", "21"),
        ("CINEDATA_REQUEST_LIMIT", "cinco"),
        ("CINEDATA_SQL_TIMEOUT_S", "abc"),
        ("CINEDATA_SQL_TIMEOUT_S", "0"),
        ("CINEDATA_SQL_TIMEOUT_S", "0.5"),
        ("CINEDATA_SQL_TIMEOUT_S", "301"),
        ("CINEDATA_SQL_TIMEOUT_S", "-1"),
        ("CINEDATA_SQL_TIMEOUT_S", "nan"),
        ("CINEDATA_SQL_TIMEOUT_S", "inf"),
        ("CINEDATA_SQL_TIMEOUT_S", "30,5"),
        ("CINEDATA_SQL_TIMEOUT_S", "1_0"),
    ],
)
def test_invalid_numeric_values_name_the_variable(name: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load({name: bad})


# --- data de referência ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-01",
        "2024-02-29",
        "2016-01-01",
        "1900-01-01",
        "1899-12-31",
        "1500-06-15",
        "9999-12-31",
    ],
)
def test_valid_reference_dates(value: str) -> None:
    assert load({"CINEDATA_REFERENCE_DATE": value}).reference_date == date.fromisoformat(value)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-13-01",
        "2026-02-30",
        "2025-02-29",
        "01/10/2026",
        "2026-1-1",
        "20261001",
        "2026-10-01T00:00",
        "hoje",
        "２０２６-１０-０１",  # algarismos de largura total
    ],
)
def test_malformed_reference_dates(bad: str) -> None:
    with pytest.raises(ConfigError, match="CINEDATA_REFERENCE_DATE"):
        load({"CINEDATA_REFERENCE_DATE": bad})


@pytest.mark.parametrize("bad", ["0001-01-01", "0003-05-05", "0004-02-29", "0005-12-31"])
def test_reference_dates_too_close_to_the_calendar_start_are_rejected(bad: str) -> None:
    with pytest.raises(ConfigError, match="CINEDATA_REFERENCE_DATE.*janela"):
        load({"CINEDATA_REFERENCE_DATE": bad})


def test_the_only_lower_bound_is_the_window_being_computable() -> None:
    first_valid = load({"CINEDATA_REFERENCE_DATE": "0006-01-01"})
    assert first_valid.rolling_window() == (date(1, 1, 1), date(6, 1, 1))


# --- modelos ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "openrouter/free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
        "meta-llama/llama-3.1-8b-instruct:free",
        "qwen/qwen3.8-27b:free",
        "~anthropic/claude-latest",
        "Provedor/Modelo-X",
    ],
)
def test_valid_model_ids(model: str) -> None:
    assert load({"CINEDATA_MODEL": model}).model == model


@pytest.mark.parametrize(
    "bad",
    [
        "sem-barra",
        "tem espaco/x",
        "openrouter:openrouter/free",
        "OpenRouter:google/gemma-4-26b-a4b-it:free",
        "/sem-provedor",
        "provedor/",
        ":free",
        "a/:free",
        "a/b:",
        "a/b/c",
        "x/y:z/w",
        '"a/b"',
        "'a/b'",
        "a/b?x",
        "a/b;rm -rf",
        "a/b\x00",
    ],
)
def test_invalid_model_ids(bad: str) -> None:
    with pytest.raises(ConfigError, match="CINEDATA_MODEL"):
        load({"CINEDATA_MODEL": bad})


def test_fallback_models_are_parsed() -> None:
    settings = load({"CINEDATA_MODEL": "a/b", "CINEDATA_FALLBACK_MODELS": " c/d ,, e/f "})
    assert settings.fallback_models == ("c/d", "e/f")


@pytest.mark.parametrize("fallbacks", ["c/d,e/f,g/h", "a/b", "c/d,C/D", "semBarra"])
def test_invalid_fallback_models(fallbacks: str) -> None:
    with pytest.raises(ConfigError, match="CINEDATA_FALLBACK_MODELS"):
        load({"CINEDATA_MODEL": "a/b", "CINEDATA_FALLBACK_MODELS": fallbacks})


def test_fallback_models_require_a_primary_model() -> None:
    with pytest.raises(ConfigError, match="CINEDATA_MODEL"):
        load({"CINEDATA_FALLBACK_MODELS": "c/d"})


def test_blank_fallback_list_needs_no_primary_model() -> None:
    assert load({"CINEDATA_FALLBACK_MODELS": " , ,"}).fallback_models == ()


# --- chave da API ----------------------------------------------------------------------------


def test_api_key_is_never_exposed() -> None:
    secret = FAKE_KEY + "-segredo"
    settings = load({"OPENROUTER_API_KEY": secret})
    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert settings.require_api_key() == secret


@pytest.mark.parametrize(
    "bad", ["sk-or-v1-abc def", "sk-or-v1-abc\tdef", '"sk-or-v1-abc"', "'sk-or-v1-abc'"]
)
def test_malformed_api_key_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ConfigError) as error:
        load({"OPENROUTER_API_KEY": bad})
    assert "OPENROUTER_API_KEY" in str(error.value)
    assert "abc" not in str(error.value)


def test_api_key_format_check() -> None:
    assert load({"OPENROUTER_API_KEY": FAKE_KEY}).api_key_format_ok
    assert not load({"OPENROUTER_API_KEY": "abc123"}).api_key_format_ok
    assert not load().api_key_format_ok


def test_require_helpers_explain_what_is_missing() -> None:
    settings = load()
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        settings.require_api_key()
    with pytest.raises(ConfigError, match="CINEDATA_MODEL"):
        settings.require_model()
    configured = load({"CINEDATA_MODEL": "provedor/modelo-de-teste"})
    assert configured.require_model() == "provedor/modelo-de-teste"


# --- arquivo .env ----------------------------------------------------------------------------


def test_dotenv_in_current_directory_is_read(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, "CINEDATA_MODEL=provedor/modelo\nCINEDATA_MAX_ROWS=77\n")
    settings = load()
    assert settings.model == "provedor/modelo"
    assert settings.max_rows == 77
    assert settings.dotenv_path is not None
    assert settings.dotenv_path.samefile(env_file)


def test_dotenv_path_can_be_given_explicitly(tmp_path: Path) -> None:
    other = tmp_path / "outro.env"
    other.write_text("CINEDATA_MAX_ROWS=33\n", encoding="utf-8")
    assert load(dotenv_path=other).max_rows == 33


def test_real_environment_wins_over_dotenv(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_MAX_ROWS=77\nCINEDATA_MODEL=do/arquivo\n")
    settings = load({"CINEDATA_MAX_ROWS": "99"})
    assert settings.max_rows == 99
    assert settings.model == "do/arquivo"


def test_blank_real_value_does_not_hide_the_dotenv_value(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_MAX_ROWS=77\n")
    assert load({"CINEDATA_MAX_ROWS": "  "}).max_rows == 77


def test_loading_never_changes_os_environ(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_MODEL=do/arquivo\n")
    before = dict(os.environ)
    settings = load_settings(today=TODAY)
    assert settings.model == "do/arquivo"
    assert dict(os.environ) == before


def test_dotenv_with_bom_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfCINEDATA_MODEL=provedor/modelo\n")
    assert load().model == "provedor/modelo"


def test_dotenv_in_utf16_gets_a_clear_error(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_MODEL=provedor/modelo\n", encoding="utf-16")
    with pytest.raises(ConfigError, match="UTF-8"):
        load()


def test_dotenv_in_ansi_gets_a_clear_error(tmp_path: Path) -> None:
    write_env(tmp_path, "# configuração\nCINEDATA_MODEL=provedor/modelo\n", encoding="cp1252")
    with pytest.raises(ConfigError, match="UTF-8"):
        load()


def test_dotenv_with_windows_line_endings(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(
        b"CINEDATA_MODEL=provedor/modelo\r\nCINEDATA_MAX_ROWS=12\r\nOPENROUTER_API_KEY=sk-or-v1-abc\r\n"
    )
    settings = load()
    assert settings.model == "provedor/modelo"
    assert settings.max_rows == 12
    assert settings.api_key == "sk-or-v1-abc"  # sem "\r" sobrando


def test_dotenv_ignores_lines_it_cannot_parse(tmp_path: Path) -> None:
    write_env(tmp_path, "isso não é uma linha válida\n=sem-chave\nCINEDATA_MODEL=provedor/modelo\n")
    assert load().model == "provedor/modelo"


def test_unreadable_dotenv_gets_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_env(tmp_path, "CINEDATA_MODEL=provedor/modelo\n")

    def deny(*args: object, **kwargs: object) -> dict[str, str]:
        raise PermissionError(13, "Acesso negado")

    monkeypatch.setattr("cinedata.config.dotenv_values", deny)
    with pytest.raises(ConfigError, match="Não foi possível ler"):
        load()


def test_dotenv_values_are_not_interpolated(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_DB_PATH=${HOME}/banco.db\n")
    assert "${HOME}" in str(load().db_path)


def test_dotenv_accepts_comments_quotes_and_export(tmp_path: Path) -> None:
    write_env(
        tmp_path,
        '# comentário\nexport CINEDATA_MODEL="provedor/modelo"  # inline\nCINEDATA_MAX_ROWS=10\n',
    )
    settings = load()
    assert settings.model == "provedor/modelo"
    assert settings.max_rows == 10


def test_invalid_value_in_dotenv_names_the_variable(tmp_path: Path) -> None:
    write_env(tmp_path, "CINEDATA_MAX_ROWS=muitas\n")
    with pytest.raises(ConfigError, match="CINEDATA_MAX_ROWS"):
        load()


def test_missing_dotenv_is_fine() -> None:
    assert load().dotenv_path is None


# --- caminho do banco ------------------------------------------------------------------------


def test_relative_db_path_resolves_from_the_current_directory() -> None:
    settings = load({"CINEDATA_DB_PATH": "dados/x.db"})
    assert settings.db_path_resolved == (Path.cwd() / "dados" / "x.db").resolve()


def test_absolute_db_path_is_kept(tmp_path: Path) -> None:
    absolute = tmp_path / "banco.db"
    assert load({"CINEDATA_DB_PATH": str(absolute)}).db_path_resolved == absolute.resolve()


def test_db_path_expands_the_home_directory() -> None:
    settings = load({"CINEDATA_DB_PATH": "~/banco.db"})
    assert settings.db_path.is_absolute()
    assert "~" not in str(settings.db_path)


# --- janela móvel ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected_start"),
    [
        (date(2026, 10, 1), date(2021, 10, 1)),
        (date(2026, 3, 1), date(2021, 3, 1)),
        (date(2026, 12, 31), date(2021, 12, 31)),
        (date(2026, 1, 1), date(2021, 1, 1)),
        (date(2025, 2, 28), date(2020, 2, 28)),
        (date(2024, 2, 29), date(2019, 2, 28)),  # 29/fev vira 28/fev
        (date(2028, 2, 29), date(2023, 2, 28)),
    ],
)
def test_rolling_window_of_five_years(reference: date, expected_start: date) -> None:
    assert rolling_window(reference) == (expected_start, reference)


def test_rolling_window_keeps_a_leap_day_when_the_target_year_has_one() -> None:
    assert rolling_window(date(2024, 2, 29), years=4) == (date(2020, 2, 29), date(2024, 2, 29))


def test_rolling_window_is_inclusive_on_both_ends() -> None:
    start, end = rolling_window(date(2026, 10, 1))
    assert (start, end) == (date(2021, 10, 1), date(2026, 10, 1))
    assert (end - start).days == 1826  # 5 anos com um 29/fev, contando os dois extremos


def test_rolling_window_rejects_non_positive_years() -> None:
    with pytest.raises(ValueError, match="years"):
        rolling_window(TODAY, years=0)


def test_rolling_window_reaches_the_first_representable_year() -> None:
    assert rolling_window(date(2026, 10, 1), years=2025) == (date(1, 10, 1), date(2026, 10, 1))


@pytest.mark.parametrize(
    ("reference", "years"),
    [(date(2026, 10, 1), 2026), (date(2026, 1, 1), 3000), (date(5, 12, 31), 5)],
)
def test_rolling_window_that_would_leave_the_calendar_raises(reference: date, years: int) -> None:
    with pytest.raises(ValueError, match="calendário"):
        rolling_window(reference, years)


def test_settings_window_follows_the_reference_date() -> None:
    settings = load({"CINEDATA_REFERENCE_DATE": "2026-10-01"})
    assert settings.rolling_window() == (date(2021, 10, 1), date(2026, 10, 1))
