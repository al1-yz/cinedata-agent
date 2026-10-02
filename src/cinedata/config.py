"""Configuração do CineData Agent.

Prioridade das fontes: variáveis do ambiente real e, depois, o arquivo `.env` da pasta atual
(execute os comandos na raiz do repositório). Valores vazios ou só com espaços contam como
"não definido". Carregar a configuração nunca altera `os.environ`.

A chave da API nunca aparece em `repr`, `str`, mensagens de erro nem no `doctor`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import dotenv_values

ENV_API_KEY = "OPENROUTER_API_KEY"
ENV_MODEL = "CINEDATA_MODEL"
ENV_FALLBACK_MODELS = "CINEDATA_FALLBACK_MODELS"
ENV_DB_PATH = "CINEDATA_DB_PATH"
ENV_MAX_ROWS = "CINEDATA_MAX_ROWS"
ENV_SQL_TIMEOUT_S = "CINEDATA_SQL_TIMEOUT_S"
ENV_REQUEST_LIMIT = "CINEDATA_REQUEST_LIMIT"
ENV_REFERENCE_DATE = "CINEDATA_REFERENCE_DATE"

ALL_ENV_VARS = (
    ENV_API_KEY,
    ENV_MODEL,
    ENV_FALLBACK_MODELS,
    ENV_DB_PATH,
    ENV_MAX_ROWS,
    ENV_SQL_TIMEOUT_S,
    ENV_REQUEST_LIMIT,
    ENV_REFERENCE_DATE,
)

API_KEY_PREFIX = "sk-or-v1-"
DEFAULT_DB_PATH = Path("data") / "cinerocket.db"
DEFAULT_MAX_ROWS = 50
MAX_ROWS_LIMITS = (1, 200)
DEFAULT_SQL_TIMEOUT_S = 30.0
SQL_TIMEOUT_LIMITS = (1.0, 300.0)
DEFAULT_REQUEST_LIMIT = 5
REQUEST_LIMIT_LIMITS = (1, 20)
MAX_FALLBACK_MODELS = 2
DEFAULT_WINDOW_YEARS = 5

_INTEGER = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# provedor/modelo ou provedor/modelo:variante (todos os ids do catálogo do OpenRouter seguem isso).
_MODEL_ID = re.compile(r"[A-Za-z0-9._~-]+/[A-Za-z0-9._~+-]+(?::[A-Za-z0-9._~+-]+)?")


class ConfigError(ValueError):
    """Configuração inválida. A mensagem é para o usuário final e nunca contém a chave da API."""


@dataclass(frozen=True)
class Settings:
    """Configuração já validada. A chave da API fica fora de `repr` e `str`."""

    api_key: str | None = field(default=None, repr=False)
    model: str | None = None
    fallback_models: tuple[str, ...] = ()
    db_path: Path = DEFAULT_DB_PATH
    max_rows: int = DEFAULT_MAX_ROWS
    sql_timeout_s: float = DEFAULT_SQL_TIMEOUT_S
    request_limit: int = DEFAULT_REQUEST_LIMIT
    reference_date: date = field(default_factory=date.today)
    reference_date_from_env: bool = False
    dotenv_path: Path | None = None

    @property
    def api_key_format_ok(self) -> bool:
        """True quando a chave tem o prefixo esperado do OpenRouter."""
        return self.api_key is not None and self.api_key.startswith(API_KEY_PREFIX)

    @property
    def db_path_resolved(self) -> Path:
        """Caminho absoluto do banco; caminhos relativos valem a partir da pasta atual."""
        return self.db_path.resolve()

    def require_api_key(self) -> str:
        if self.api_key is None:
            raise ConfigError(
                f"{ENV_API_KEY} não configurada. Crie uma chave em https://openrouter.ai/keys "
                "e coloque-a no arquivo .env (veja .env.example)."
            )
        return self.api_key

    def require_model(self) -> str:
        if self.model is None:
            raise ConfigError(
                f"{ENV_MODEL} não configurado. Defina no .env o id de um modelo do OpenRouter "
                "com suporte a tool calling (veja o README)."
            )
        return self.model

    def rolling_window(self, years: int = DEFAULT_WINDOW_YEARS) -> tuple[date, date]:
        """Janela móvel de `years` anos até a data de referência, com extremos inclusivos."""
        return rolling_window(self.reference_date, years)


def rolling_window(reference: date, years: int = DEFAULT_WINDOW_YEARS) -> tuple[date, date]:
    """Devolve (início, fim) da janela móvel de `years` anos terminando em `reference`.

    Os dois extremos são inclusivos: `início <= data <= fim`. Em 29/fev, quando o ano de
    destino não é bissexto, o início vira 28/fev. Levanta ValueError se `years` for menor que 1
    ou se o início da janela cair antes do ano 1 (o menor ano que o calendário `date` representa).
    """
    if years < 1:
        raise ValueError("years deve ser pelo menos 1")
    start_year = reference.year - years
    if start_year < date.min.year:
        raise ValueError(
            f"a janela de {years} anos antes de {reference.isoformat()} começaria antes do ano "
            f"{date.min.year}, que o calendário não representa"
        )
    try:
        start = reference.replace(year=start_year)
    except ValueError:
        start = reference.replace(year=start_year, day=28)
    return start, reference


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] | None = None,
    today: date | None = None,
) -> Settings:
    """Lê e valida a configuração.

    `env` substitui `os.environ` (útil em testes), `dotenv_path` substitui o `.env` da pasta
    atual e `today` fixa a data usada quando CINEDATA_REFERENCE_DATE está vazio.
    """
    environment = os.environ if env is None else env
    path = Path(dotenv_path) if dotenv_path is not None else Path.cwd() / ".env"
    file_values = _read_dotenv(path)

    def value(name: str) -> str | None:
        return _first_set(name, environment, file_values)

    api_key = _parse_api_key(value(ENV_API_KEY))
    model = _parse_model(ENV_MODEL, value(ENV_MODEL))
    fallback_models = _parse_fallback_models(value(ENV_FALLBACK_MODELS), model)

    raw_db_path = value(ENV_DB_PATH)
    db_path = Path(raw_db_path).expanduser() if raw_db_path else DEFAULT_DB_PATH

    raw_max_rows = value(ENV_MAX_ROWS)
    max_rows = (
        _parse_int(ENV_MAX_ROWS, raw_max_rows, *MAX_ROWS_LIMITS)
        if raw_max_rows
        else DEFAULT_MAX_ROWS
    )

    raw_timeout = value(ENV_SQL_TIMEOUT_S)
    sql_timeout_s = (
        _parse_seconds(ENV_SQL_TIMEOUT_S, raw_timeout, *SQL_TIMEOUT_LIMITS)
        if raw_timeout
        else DEFAULT_SQL_TIMEOUT_S
    )

    raw_request_limit = value(ENV_REQUEST_LIMIT)
    request_limit = (
        _parse_int(ENV_REQUEST_LIMIT, raw_request_limit, *REQUEST_LIMIT_LIMITS)
        if raw_request_limit
        else DEFAULT_REQUEST_LIMIT
    )

    raw_reference_date = value(ENV_REFERENCE_DATE)
    explicit_reference_date = (
        _parse_date(ENV_REFERENCE_DATE, raw_reference_date) if raw_reference_date else None
    )

    return Settings(
        api_key=api_key,
        model=model,
        fallback_models=fallback_models,
        db_path=db_path,
        max_rows=max_rows,
        sql_timeout_s=sql_timeout_s,
        request_limit=request_limit,
        reference_date=explicit_reference_date or today or date.today(),
        reference_date_from_env=explicit_reference_date is not None,
        dotenv_path=path if path.is_file() else None,
    )


def _read_dotenv(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    try:
        # utf-8-sig descarta o BOM que o Bloco de Notas e alguns editores gravam;
        # interpolate=False mantém valores com `$` exatamente como foram escritos.
        return dict(dotenv_values(path, encoding="utf-8-sig", interpolate=False))
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path.name} não está em UTF-8. Salve o arquivo como UTF-8 (o redirecionamento `>` "
            "do PowerShell 5.1 grava UTF-16, e editores antigos gravam ANSI)."
        ) from None
    except OSError as exc:
        raise ConfigError(f"Não foi possível ler {path}: {exc.strerror or exc}") from None


def _first_set(name: str, *sources: Mapping[str, str | None]) -> str | None:
    """Primeiro valor não vazio entre as fontes (o ambiente real vem antes do `.env`)."""
    for source in sources:
        raw = source.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def _shown(raw: str) -> str:
    return repr(raw if len(raw) <= 40 else raw[:40] + "...")


def _fail(name: str, message: str) -> ConfigError:
    return ConfigError(f"{name} {message}")


def _parse_api_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    if re.search(r"\s", raw):
        raise _fail(ENV_API_KEY, "contém espaços ou quebras de linha; cole somente a chave.")
    if raw[0] in "\"'" or raw[-1] in "\"'":
        raise _fail(ENV_API_KEY, "contém aspas; cole somente a chave, sem aspas.")
    return raw


def _validate_model_id(name: str, raw: str) -> str:
    if raw.lower().startswith("openrouter:"):
        raise _fail(
            name,
            "deve ser o id do modelo no OpenRouter (provedor/modelo ou provedor/modelo:variante), "
            "sem o prefixo 'openrouter:'.",
        )
    if not _MODEL_ID.fullmatch(raw):
        raise _fail(
            name,
            "deve ter o formato provedor/modelo ou provedor/modelo:variante, sem espaços "
            f"(valor recebido: {_shown(raw)}).",
        )
    return raw


def _parse_model(name: str, raw: str | None) -> str | None:
    return None if raw is None else _validate_model_id(name, raw)


def _parse_fallback_models(raw: str | None, primary: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = [part.strip() for part in raw.split(",") if part.strip()]
    if not items:
        return ()
    if primary is None:
        raise _fail(ENV_FALLBACK_MODELS, f"só pode ser usado com {ENV_MODEL} definido.")
    if len(items) > MAX_FALLBACK_MODELS:
        raise _fail(ENV_FALLBACK_MODELS, f"aceita no máximo {MAX_FALLBACK_MODELS} modelos.")
    seen = {primary.lower()}
    for item in items:
        _validate_model_id(ENV_FALLBACK_MODELS, item)
        if item.lower() in seen:
            raise _fail(
                ENV_FALLBACK_MODELS,
                f"repete um modelo já usado (inclusive o principal): {_shown(item)}.",
            )
        seen.add(item.lower())
    return tuple(items)


def _parse_int(name: str, raw: str, low: int, high: int) -> int:
    if _INTEGER.fullmatch(raw) and low <= int(raw) <= high:
        return int(raw)
    raise _fail(
        name, f"deve ser um número inteiro entre {low} e {high} (valor recebido: {_shown(raw)})."
    )


def _parse_seconds(name: str, raw: str, low: float, high: float) -> float:
    if _DECIMAL.fullmatch(raw) and low <= float(raw) <= high:
        return float(raw)
    raise _fail(
        name,
        f"deve ser um número de segundos entre {low:g} e {high:g}, com ponto decimal "
        f"(valor recebido: {_shown(raw)}).",
    )


def _parse_date(name: str, raw: str) -> date:
    if not _ISO_DATE.fullmatch(raw):
        raise _fail(
            name,
            f"deve estar no formato AAAA-MM-DD, como 2026-10-01 (valor recebido: {_shown(raw)}).",
        )
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise _fail(name, f"não é uma data existente: {_shown(raw)}.") from None
    try:
        rolling_window(parsed)  # a única exigência real: a janela padrão precisa ser calculável
    except ValueError:
        raise _fail(
            name,
            f"está perto demais do início do calendário: a janela de {DEFAULT_WINDOW_YEARS} anos "
            f"antes de {raw} não pode ser calculada.",
        ) from None
    return parsed
