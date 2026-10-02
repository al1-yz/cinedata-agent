"""Testes da CLI (offline: nada aqui acessa a rede nem o banco real)."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from cinedata import __version__
from cinedata.cli import SQLITE_HEADER, main

FAKE_KEY = "sk-or-v1-" + "test"  # prefixo certo e valor curto: não se parece com uma chave real
FAKE_MODEL = "provedor/modelo-de-teste"


@pytest.fixture
def valid_db(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "cinerocket.db"
    path.parent.mkdir()
    path.write_bytes(SQLITE_HEADER + b"\x00" * 100)
    return path


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CINEDATA_MODEL", FAKE_MODEL)


# --- uso básico ------------------------------------------------------------------------------


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"cinedata {__version__}"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "usage" in output.lower()
    assert "doctor" in output


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "doctor" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["nao-existe"]) == 2
    assert "nao-existe" in capsys.readouterr().err


def test_unexpected_extra_argument_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "sobrando"]) == 2
    assert "sobrando" in capsys.readouterr().err


def test_ctrl_c_exits_with_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("cinedata.cli._run_doctor", interrupt)
    assert main(["doctor"]) == 130
    assert "Interrompido" in capsys.readouterr().err


# --- doctor: configuração --------------------------------------------------------------------


def test_doctor_reports_everything_that_is_missing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "AUSENTE" in output
    assert "NÃO CONFIGURADO" in output
    assert "NÃO ENCONTRADO" in output
    assert "Pendências (3)" in output
    assert "Traceback" not in output


def test_doctor_is_clean_when_everything_is_configured(
    monkeypatch: pytest.MonkeyPatch, valid_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch)
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Nenhuma pendência de configuração." in output
    assert "aviso" not in output.lower()
    assert "cabeçalho SQLite válido" in output
    assert FAKE_MODEL in output


def test_doctor_never_prints_the_api_key(
    monkeypatch: pytest.MonkeyPatch, valid_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = FAKE_KEY + "-segredo-que-nao-pode-vazar"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    main(["doctor"])
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "presente" in captured.out


def test_doctor_reports_a_suspicious_key_as_a_warning_and_not_as_a_clean_bill(
    monkeypatch: pytest.MonkeyPatch, valid_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "abc123")
    monkeypatch.setenv("CINEDATA_MODEL", FAKE_MODEL)
    assert main(["doctor"]) == 0  # o aviso não bloqueia
    output = capsys.readouterr().out
    assert "Configuração carregada com 1 aviso:" in output
    assert "\n  - OPENROUTER_API_KEY não começa com 'sk-or-v1-'." in output  # item da lista final
    assert "Nenhuma pendência" not in output
    assert "abc123" not in output  # nem o aviso deixa a chave vazar


def test_doctor_lists_pending_items_and_warnings_separately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "abc123")  # suspeita; sem modelo e sem banco
    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "Pendências (2):" in output
    assert "Avisos (1):\n  - OPENROUTER_API_KEY não começa com 'sk-or-v1-'." in output
    assert "Configuração carregada" not in output
    assert "Nenhuma pendência" not in output


def test_doctor_with_invalid_configuration_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_MAX_ROWS", "abc")
    assert main(["doctor"]) == 2
    captured = capsys.readouterr()
    assert "CINEDATA_MAX_ROWS" in captured.err
    assert "Traceback" not in captured.err


def test_doctor_rejects_a_reference_date_that_breaks_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_REFERENCE_DATE", "0001-01-01")
    assert main(["doctor"]) == 2
    captured = capsys.readouterr()
    assert "CINEDATA_REFERENCE_DATE" in captured.err
    assert "Traceback" not in captured.err


def test_doctor_accepts_an_old_reference_date_whose_window_fits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_REFERENCE_DATE", "1850-06-15")
    main(["doctor"])
    assert "1845-06-15 a 1850-06-15" in capsys.readouterr().out


def test_doctor_shows_the_reference_date_and_the_five_year_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CINEDATA_REFERENCE_DATE", "2026-10-01")
    main(["doctor"])
    output = capsys.readouterr().out
    assert "2026-10-01 (definida em CINEDATA_REFERENCE_DATE)" in output
    assert "2021-10-01 a 2026-10-01" in output


def test_doctor_reports_the_dotenv_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["doctor"])
    assert "não encontrado (copie .env.example" in capsys.readouterr().out
    (tmp_path / ".env").write_text(f"CINEDATA_MODEL={FAKE_MODEL}\n", encoding="utf-8")
    main(["doctor"])
    output = capsys.readouterr().out
    assert "encontrado:" in output
    assert FAKE_MODEL in output


def test_doctor_has_no_state_between_runs(
    monkeypatch: pytest.MonkeyPatch, valid_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor"]) == 1
    capsys.readouterr()
    configure(monkeypatch)
    assert main(["doctor"]) == 0


def test_doctor_output_has_no_terminal_escape_codes(capsys: pytest.CaptureFixture[str]) -> None:
    main(["doctor"])
    assert "\x1b" not in capsys.readouterr().out


# --- doctor: arquivo do banco ----------------------------------------------------------------


def test_doctor_suggests_renaming_a_download_with_a_suffix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cinerocket (1).db").write_bytes(SQLITE_HEADER)
    main(["doctor"])
    output = capsys.readouterr().out
    assert "cinerocket (1).db" in output
    assert "renomeie" in output


def test_doctor_uses_the_custom_database_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    custom = tmp_path / "outro.db"
    custom.write_bytes(SQLITE_HEADER + b"\x00" * 10)
    monkeypatch.setenv("CINEDATA_DB_PATH", str(custom))
    main(["doctor"])
    output = capsys.readouterr().out
    assert str(custom.resolve()) in output
    assert "cabeçalho SQLite válido" in output


def test_doctor_detects_an_empty_database_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cinerocket.db").write_bytes(b"")
    assert main(["doctor"]) == 1
    assert "arquivo vazio" in capsys.readouterr().out


def test_doctor_detects_a_file_that_is_not_sqlite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cinerocket.db").write_text("<html>erro de download</html>")
    assert main(["doctor"]) == 1
    assert "não parece um banco SQLite" in capsys.readouterr().out


def test_doctor_detects_a_directory_in_place_of_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data" / "cinerocket.db").mkdir(parents=True)
    assert main(["doctor"]) == 1
    assert "não é um arquivo" in capsys.readouterr().out


def test_doctor_reports_an_unreadable_database(
    monkeypatch: pytest.MonkeyPatch, valid_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def deny(self: Path, *args: object, **kwargs: object) -> io.BufferedReader:
        raise PermissionError("negado")

    monkeypatch.setattr(Path, "open", deny)
    assert main(["doctor"]) == 1
    assert "sem permissão de leitura" in capsys.readouterr().out


def test_doctor_does_not_touch_the_database_file(valid_db: Path) -> None:
    def snapshot() -> tuple[int, int, list[str]]:
        info = valid_db.stat()
        return info.st_size, info.st_mtime_ns, sorted(p.name for p in valid_db.parent.iterdir())

    before = snapshot()
    main(["doctor"])
    assert snapshot() == before  # nenhum arquivo novo (como -wal ou -shm) e nenhuma alteração


# --- terminal e execução como módulo ---------------------------------------------------------


class InteractiveStream(io.TextIOWrapper):
    """Fluxo de texto que se declara um terminal interativo (como um console do Windows)."""

    def isatty(self) -> bool:
        return True


def test_redirected_output_becomes_utf8_when_the_default_encoding_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252")  # padrão do Windows em PT-BR
    monkeypatch.setattr(sys, "stdout", stdout)
    assert main(["doctor"]) == 1
    stdout.flush()
    assert "Pendências".encode() in raw.getvalue()


def test_an_explicit_python_io_encoding_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert main(["doctor"]) == 1  # sem UnicodeEncodeError
    stdout.flush()
    assert b"Pend?ncias" in raw.getvalue()


def test_an_interactive_terminal_that_cannot_encode_accents_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    stdout = InteractiveStream(raw, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert main(["doctor"]) == 1
    stdout.flush()
    assert b"Pend?ncias" in raw.getvalue()


def test_module_entry_point_works() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cinedata", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"cinedata {__version__}"


def test_importing_the_cli_does_not_load_the_model_sdk() -> None:
    code = "import sys, cinedata.cli; print('pydantic_ai' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.stdout.strip() == "False"
