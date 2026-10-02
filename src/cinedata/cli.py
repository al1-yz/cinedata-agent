"""Interface de linha de comando do CineData Agent.

Neste marco (M0) existem apenas `--version` e `doctor`; ambos funcionam offline. Importações
pesadas (agente, SDKs de modelo) devem ficar dentro dos comandos que as usam, para que
`--version` e `doctor` continuem instantâneos.

Códigos de saída: 0 = ok (o `doctor` pode listar avisos); 1 = `doctor` encontrou pendências;
2 = uso ou configuração inválidos; 130 = interrompido (Ctrl+C).
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cinedata import __version__
from cinedata.config import (
    API_KEY_PREFIX,
    DEFAULT_WINDOW_YEARS,
    ENV_API_KEY,
    ENV_DB_PATH,
    ENV_FALLBACK_MODELS,
    ENV_MAX_ROWS,
    ENV_MODEL,
    ENV_REFERENCE_DATE,
    ENV_REQUEST_LIMIT,
    ENV_SQL_TIMEOUT_S,
    ConfigError,
    load_settings,
)

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class DbFileReport:
    """Resultado da inspeção do arquivo do banco (sem abri-lo como SQLite)."""

    ok: bool
    detail: str
    hint: str | None = None


def main(argv: Sequence[str] | None = None) -> int:
    """Ponto de entrada do comando `cinedata`; devolve o código de saída."""
    _prepare_output_streams()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:  # argparse encerra assim em --help e em erros de uso
        return exit_request.code if isinstance(exit_request.code, int) else EXIT_USAGE
    try:
        if args.version:
            print(f"cinedata {__version__}")
            return EXIT_OK
        if args.command == "doctor":
            return _run_doctor()
        parser.print_help()
        return EXIT_OK
    except KeyboardInterrupt:
        print("\nInterrompido.", file=sys.stderr)
        return EXIT_INTERRUPTED


def _prepare_output_streams() -> None:
    """Deixa a saída em UTF-8 quando é redirecionada e nunca falha por causa de acentos.

    Em console do Windows o Python já escreve Unicode, então nada muda. Já em pipe, arquivo ou
    mintty (Git Bash) a codificação padrão do Windows (cp1252) corromperia os acentos num terminal
    UTF-8; por isso esses casos passam a UTF-8, a menos que PYTHONIOENCODING ou PYTHONUTF8
    estejam definidos. Caracteres que a codificação não representa viram "?".
    """
    explicit_encoding = bool(os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8"))
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        options = {"errors": "replace"}
        if not explicit_encoding and not stream.isatty():
            options["encoding"] = "utf-8"
        try:
            reconfigure(**options)
        except (ValueError, OSError):
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cinedata",
        description=(
            "CineData Agent: consultas em linguagem natural, somente leitura, "
            "sobre a camada Gold da CineData Analytics."
        ),
    )
    parser.add_argument("--version", action="store_true", help="mostra a versão e sai")
    commands = parser.add_subparsers(dest="command", metavar="COMANDO")
    commands.add_parser(
        "doctor",
        help="diagnostica a configuração e o arquivo do banco (offline)",
        description=(
            "Mostra a configuração efetiva e confere o arquivo do banco, sem acessar a rede "
            "e sem imprimir a chave da API. Pendências impedem o uso (código de saída 1); "
            "avisos pedem conferência, mas não bloqueiam (código de saída 0)."
        ),
    )
    return parser


def _run_doctor() -> int:
    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"Erro de configuração: {error}", file=sys.stderr)
        return EXIT_USAGE

    pending: list[str] = []
    warnings: list[str] = []
    window_start, window_end = settings.rolling_window()

    print("CineData Agent - diagnóstico (offline: nenhuma chamada de rede é feita)\n")

    print("Ambiente")
    _row("cinedata", __version__)
    _row("Python", f"{platform.python_version()} ({platform.python_implementation()})")
    _row("Sistema", platform.platform())
    _row("Pasta atual", str(Path.cwd()))
    if settings.dotenv_path is not None:
        _row("Arquivo .env", f"encontrado: {settings.dotenv_path}")
    else:
        _row("Arquivo .env", "não encontrado (copie .env.example para .env)")

    print("\nConfiguração")
    if settings.api_key is None:
        _row(ENV_API_KEY, "AUSENTE")
        pending.append(
            f"{ENV_API_KEY} ausente. Crie uma chave em https://openrouter.ai/keys "
            "e coloque no .env."
        )
    elif settings.api_key_format_ok:
        _row(ENV_API_KEY, "presente")
    else:
        _row(ENV_API_KEY, f"presente (aviso: não começa com '{API_KEY_PREFIX}')")
        warnings.append(
            f"{ENV_API_KEY} não começa com '{API_KEY_PREFIX}'. Confira se a chave foi copiada "
            "inteira, sem aspas nem espaços."
        )
    if settings.model is None:
        _row(ENV_MODEL, "NÃO CONFIGURADO")
        pending.append(
            f"{ENV_MODEL} não configurado. Informe no .env o id de um modelo do OpenRouter "
            "com suporte a tool calling (veja o README)."
        )
    else:
        _row(ENV_MODEL, settings.model)
    _row(ENV_FALLBACK_MODELS, ", ".join(settings.fallback_models) or "nenhum")
    _row(ENV_MAX_ROWS, str(settings.max_rows))
    _row(ENV_SQL_TIMEOUT_S, f"{settings.sql_timeout_s:g} s")
    _row(ENV_REQUEST_LIMIT, str(settings.request_limit))
    if settings.reference_date_from_env:
        origin = f"definida em {ENV_REFERENCE_DATE}"
    else:
        origin = "padrão: data de hoje"
    _row(ENV_REFERENCE_DATE, f"{settings.reference_date.isoformat()} ({origin})")
    _row(
        f"Janela móvel de {DEFAULT_WINDOW_YEARS} anos",
        f"{window_start.isoformat()} a {window_end.isoformat()} (extremos inclusivos)",
    )

    print("\nBanco de dados")
    db_path = settings.db_path_resolved
    report = _inspect_db_file(db_path)
    _row("Caminho", str(db_path))
    _row("Situação", report.detail)
    if report.hint:
        _row("Dica", report.hint)
    if not report.ok:
        pending.append(f"Banco de dados: {report.detail}.")

    print()
    if pending:
        print(f"Pendências ({len(pending)}):")
        for item in pending:
            print(f"  - {item}")
        if warnings:
            print()
    if warnings:
        if pending:
            print(f"Avisos ({len(warnings)}):")
        else:
            noun = "aviso" if len(warnings) == 1 else "avisos"
            print(f"Configuração carregada com {len(warnings)} {noun}:")
        for item in warnings:
            print(f"  - {item}")
    if pending:
        return EXIT_PENDING
    if not warnings:
        print("Nenhuma pendência de configuração.")
    return EXIT_OK


def _row(label: str, value: str) -> None:
    print(f"  {label:<26}{value}")


def _inspect_db_file(path: Path) -> DbFileReport:
    """Confere existência, leitura e cabeçalho do arquivo; nunca o abre como SQLite."""
    if not path.exists():
        return DbFileReport(False, "NÃO ENCONTRADO", _missing_db_hint(path))
    if not path.is_file():
        return DbFileReport(False, "o caminho existe, mas não é um arquivo")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except PermissionError:
        return DbFileReport(False, "sem permissão de leitura")
    except OSError as error:
        return DbFileReport(False, f"não foi possível ler o arquivo ({error.strerror or error})")
    if size == 0:
        return DbFileReport(False, "arquivo vazio (0 bytes)", "Baixe o banco novamente.")
    if header != SQLITE_HEADER:
        return DbFileReport(
            False,
            "o arquivo não parece um banco SQLite (cabeçalho inválido)",
            "Confira se o download terminou e se o arquivo é o cinerocket.db da atividade.",
        )
    return DbFileReport(True, f"encontrado, {_format_mb(size)}, cabeçalho SQLite válido")


def _missing_db_hint(path: Path) -> str:
    parent = path.parent
    neighbours = sorted(parent.glob("cinerocket*.db")) if parent.is_dir() else []
    neighbours = [item for item in neighbours if item != path]
    if neighbours:
        names = ", ".join(f"'{item.name}'" for item in neighbours[:3])
        return (
            f"Há {names} nesta pasta. Se for o banco da atividade, renomeie para "
            f"'{path.name}' (ou aponte {ENV_DB_PATH} para ele)."
        )
    return f"Coloque o arquivo em {path} (veja data/README.md) ou defina {ENV_DB_PATH}."


def _format_mb(size_bytes: int) -> str:
    return f"{size_bytes / 1_000_000:.1f}".replace(".", ",") + " MB"
