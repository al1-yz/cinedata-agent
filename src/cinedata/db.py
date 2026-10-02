"""Acesso somente leitura e endurecido ao banco SQLite da camada Gold.

`SafeDatabase` é o único caminho do projeto até o banco. A segurança NÃO depende do prompt nem de
validar o texto do SQL: ela vem de camadas independentes, montadas nesta ordem e conferidas por
releitura (se a build do SQLite não oferecer uma proteção obrigatória, o construtor falha, sem
fallback silencioso):

1. Abertura `mode=ro` (protege só o arquivo principal) por URI montada com `Path.as_uri()`.
2. Limites do SQLite, com `SQLITE_LIMIT_ATTACHED=0` (fecha ATTACH e VACUUM INTO, que poderiam
   criar arquivos mesmo com `mode=ro`), e configuração: DQS desligado (aspas duplas nunca viram
   texto em silêncio), DEFENSIVE ligado, TRUSTED_SCHEMA desligado, `query_only` (apenas um
   extra) e o perfil conservador de cache (64 MB).
3. Conferência do esquema esperado da Gold.
4. Authorizer com negação por padrão: só leitura de tabelas/colunas da allowlist e só funções da
   allowlist. Nunca devolve IGNORE, que viraria NULL silencioso.
5. Prazo por progress handler e limite de linhas por `fetchmany(max_rows + 1)`, sem reescrever o
   SQL (nada de acrescentar LIMIT).

O pré-filtro de texto (primeira palavra SELECT/WITH) restringe a superfície sintática e melhora as
mensagens; mesmo sem ele, as demais camadas continuam garantindo o acesso somente leitura e
bloqueando operações fora da política de segurança.

Lacuna conhecida: o authorizer enxerga o NOME da função, não o argumento, então `date('now')`,
`date()` e formas semelhantes passam. A regra "janela móvel pela data de referência, nunca pelo
relógio" fica nas instruções do agente (M2) e na avaliação (M3). CURRENT_DATE, CURRENT_TIME e
CURRENT_TIMESTAMP já são negados porque chegam ao authorizer como funções fora da allowlist.

Escopo do tratamento de Ctrl+C: vale para `execute` chamado na thread que recebe o
`KeyboardInterrupt` (a principal). Cancelar uma consulta rodando em thread de trabalho do agente
será validado no M2.
"""

from __future__ import annotations

import difflib
import platform
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

from cinedata.config import DEFAULT_MAX_ROWS, DEFAULT_SQL_TIMEOUT_S

if TYPE_CHECKING:
    from cinedata.config import Settings

MIN_SQLITE_VERSION = (3, 31, 0)  # TRUSTED_SCHEMA (3.31), DQS_* (3.29) e DEFENSIVE (3.26)
DEFAULT_MAX_CELL_CHARS = 1_000
MAX_SQL_BYTES = 20_000
MAX_TIMEOUT_S = 3_600.0
CACHE_SIZE_KIB = 65_536
PROGRESS_INTERVAL = 1_000  # instruções da VM entre verificações do prazo (excesso medido: <= 0,3 s)
MAX_DENIALS = 5

# Tabelas e colunas que o agente pode ler. `alembic_version` e `sqlite_master` ficam de fora e
# `movie_reviews.name` (nome do autor da avaliação) também.
GOLD_TABLES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "bridge_movie_company": ("sk_movie_id", "sk_company_id"),
        "bridge_movie_genre": ("sk_movie_id", "sk_genre_id"),
        "bridge_movie_person": ("sk_movie_id", "sk_person_id"),
        "dim_companies": ("sk_company_id", "nome_produtora"),
        "dim_genres": ("sk_genre_id", "nome_genero"),
        "dim_movies": (
            "sk_movie_id",
            "id_filme",
            "titulo",
            "data_lancamento",
            "ano_lancamento",
            "duracao_minutos",
            "idioma_original",
            "status_filme",
            "sinopse",
            "url_poster",
            "url_backdrop",
        ),
        "dim_people": ("sk_person_id", "nome_pessoa", "tipo_pessoa"),
        "dim_reviews": (
            "sk_review_id",
            "sk_movie_id",
            "qtd_avaliacoes_usuarios",
            "nota_media_usuarios",
        ),
        "fact_movies_performance": (
            "sk_movie_id",
            "orcamento_usd",
            "receita_usd",
            "lucro_usd",
            "orcamento_brl",
            "receita_brl",
            "lucro_brl",
            "popularidade",
            "nota_tmdb",
            "qtd_tmdb",
            "nota_imdb",
            "qtd_imdb",
        ),
        "movie_reviews": (
            "id",
            "sk_movie_review_id",
            "sk_movie_id",
            "rating",
            "text",
            "created_at",
        ),
    }
)

HIDDEN_COLUMNS: Mapping[tuple[str, str], str] = MappingProxyType(
    {("movie_reviews", "name"): "é o nome de quem escreveu a avaliação (dado pessoal)."}
)

# Funções liberadas. O authorizer vê o nome (em minúsculas), nunca os argumentos. Ficam de fora,
# entre outras: load_extension, random, randomblob, zeroblob, hex, quote, printf, format, char,
# unicode, json*, sqlite_* e CURRENT_DATE/CURRENT_TIME/CURRENT_TIMESTAMP (current_*).
# fmt: off
ALLOWED_FUNCTIONS = frozenset(
    {
        # agregação
        "count", "sum", "avg", "total", "min", "max", "group_concat", "string_agg",
        # escalares
        "abs", "coalesce", "ifnull", "iif", "nullif", "length", "lower", "upper", "substr",
        "substring", "trim", "ltrim", "rtrim", "replace", "instr", "round", "typeof", "like",
        "glob", "concat", "concat_ws", "ceil", "ceiling", "floor", "trunc", "sqrt", "pow",
        "power", "ln", "log", "log10", "log2", "exp", "mod", "sign",
        # datas (sempre com datas explícitas; ver a lacuna conhecida no docstring do módulo)
        "date", "time", "datetime", "julianday", "strftime", "unixepoch",
        # janela
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
        "first_value", "last_value", "nth_value",
    }
)
# fmt: on

# Proteções obrigatórias. Cada uma é aplicada e RELIDA; qualquer falha impede a conexão.
_REQUIRED_DBCONFIG: tuple[tuple[str, bool], ...] = (
    ("SQLITE_DBCONFIG_DQS_DML", False),
    ("SQLITE_DBCONFIG_DQS_DDL", False),
    ("SQLITE_DBCONFIG_DEFENSIVE", True),
    ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
)
_LIMITS: tuple[tuple[str, int], ...] = (
    ("SQLITE_LIMIT_ATTACHED", 0),
    ("SQLITE_LIMIT_LENGTH", 1_000_000),
    ("SQLITE_LIMIT_SQL_LENGTH", MAX_SQL_BYTES),
    ("SQLITE_LIMIT_COLUMN", 100),
    ("SQLITE_LIMIT_EXPR_DEPTH", 200),
    ("SQLITE_LIMIT_COMPOUND_SELECT", 20),
    ("SQLITE_LIMIT_FUNCTION_ARG", 32),
    ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", 1_000),
)
_PRAGMAS: tuple[tuple[str, str, int], ...] = (
    ("PRAGMA query_only = ON", "PRAGMA query_only", 1),
    (f"PRAGMA cache_size = -{CACHE_SIZE_KIB}", "PRAGMA cache_size", -CACHE_SIZE_KIB),
)
_SETUP_ERRORS = (AttributeError, TypeError, ValueError, sqlite3.Error)

_ALLOWED_COLUMNS: Mapping[str, frozenset[str]] = MappingProxyType(
    {table: frozenset(columns) for table, columns in GOLD_TABLES.items()}
)
_ALL_COLUMNS = sorted({column for columns in GOLD_TABLES.values() for column in columns})
_SCHEMA_HINT = "Modelo de dados: " + "; ".join(
    f"{table}({', '.join(columns)})" for table, columns in GOLD_TABLES.items()
)
_READ_ONLY_HINT = "Só consultas de leitura (SELECT, ou WITH ... SELECT) são aceitas."
_FUNCTION_HINT = "Funções permitidas: " + ", ".join(sorted(ALLOWED_FUNCTIONS)) + "."
_TIMEOUT_HINT = (
    "Reduza o escopo: filtre antes de juntar tabelas de relação N:N, agregue em subconsultas "
    "e evite produtos cartesianos."
)


# --- erros -----------------------------------------------------------------------------------


class SafeDatabaseError(Exception):
    """Erro controlado. `message` e `hint` são seguros para o usuário e para o modelo."""

    recoverable = True  # reescrever a consulta pode resolver (vira ModelRetry no agente)

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} {self.hint}" if self.hint else self.message


class DatabaseUnavailableError(SafeDatabaseError):
    """Arquivo, esquema ou build do SQLite inadequados. Reescrever o SQL não resolve."""

    recoverable = False


class QueryRejectedError(SafeDatabaseError):
    """A consulta viola a política (não é leitura, usa algo fora da allowlist, vazia...)."""


class QueryFailedError(SafeDatabaseError):
    """A consulta é permitida, mas inválida ou inexecutável (sintaxe, coluna inexistente...)."""


class QueryTimeoutError(SafeDatabaseError):
    """A consulta passou do prazo e foi interrompida."""


@dataclass(frozen=True)
class QueryResult:
    """Resultado de uma consulta. As linhas são tuplas, então colunas repetidas não se perdem."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool  # havia mais de `max_rows` linhas; só as primeiras `max_rows` foram devolvidas
    max_rows: int
    elapsed_s: float
    truncated_cells: int  # células de texto cortadas por passarem de `max_cell_chars`


# --- authorizer ------------------------------------------------------------------------------


class Denial(NamedTuple):
    message: str
    hint: str | None


def _actions(*names: str) -> frozenset[int]:
    return frozenset(getattr(sqlite3, name) for name in names if hasattr(sqlite3, name))


_WRITE_ACTIONS = _actions("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
_STRUCTURE_ACTIONS = _actions(
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_VTABLE",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
)
_ATTACH_ACTIONS = _actions("SQLITE_ATTACH", "SQLITE_DETACH")
_TRANSACTION_ACTIONS = _actions("SQLITE_TRANSACTION", "SQLITE_SAVEPOINT")


def _read_denial(table: str | None, column: str | None, dbname: str | None) -> Denial | None:
    if dbname not in (None, "main"):
        return Denial("Só o banco principal pode ser lido.", _READ_ONLY_HINT)
    table_name = (table or "").lower()
    allowed = _ALLOWED_COLUMNS.get(table_name)
    if allowed is None:
        return Denial(
            f"A tabela '{table}' não faz parte do modelo de dados disponível.",
            "Tabelas disponíveis: " + ", ".join(GOLD_TABLES) + ".",
        )
    column_name = (column or "").lower()
    if column_name == "" or column_name in allowed:  # '' = leitura sem coluna, como em count(*)
        return None
    reason = HIDDEN_COLUMNS.get((table_name, column_name))
    if reason is not None:
        return Denial(
            f"A coluna '{table_name}.{column_name}' não está disponível: {reason}",
            "Liste explicitamente as colunas desejadas em vez de usar SELECT * nessa tabela.",
        )
    if column_name == "rowid":
        return Denial("O rowid não está disponível.", "Use as colunas de chave (sk_*) das tabelas.")
    return Denial(
        f"A coluna '{table_name}.{column_name}' não existe.",
        f"Colunas de {table_name}: {', '.join(GOLD_TABLES[table_name])}.",
    )


def _denial_for(
    action: int, arg1: str | None, arg2: str | None, dbname: str | None
) -> Denial | None:
    """Devolve o motivo da negação, ou None quando a operação é permitida (negação por padrão)."""
    if action == sqlite3.SQLITE_SELECT:
        return None
    if action == sqlite3.SQLITE_READ:
        return _read_denial(arg1, arg2, dbname)
    if action == sqlite3.SQLITE_FUNCTION:
        name = arg2 or ""
        if name.lower() in ALLOWED_FUNCTIONS:
            return None
        if name.lower().startswith("current_"):
            return Denial(
                f"A função '{name.lower()}' não é permitida: ela depende do relógio.",
                "Use a data de referência informada nas instruções, não datas relativas a hoje.",
            )
        return Denial(f"A função '{name}' não é permitida.", _FUNCTION_HINT)
    if action == getattr(sqlite3, "SQLITE_RECURSIVE", None):
        return Denial(
            "Consultas recursivas (WITH RECURSIVE) não são permitidas.",
            "Reescreva com junções, agregações ou subconsultas comuns.",
        )
    if action in _WRITE_ACTIONS:
        return Denial("Escrever dados (INSERT/UPDATE/DELETE) não é permitido.", _READ_ONLY_HINT)
    if action in _STRUCTURE_ACTIONS:
        return Denial("Alterar a estrutura do banco não é permitido.", _READ_ONLY_HINT)
    if action in _ATTACH_ACTIONS:
        return Denial("ATTACH e DETACH não são permitidos.", _READ_ONLY_HINT)
    if action == sqlite3.SQLITE_PRAGMA:
        return Denial("PRAGMA não é permitido.", _READ_ONLY_HINT)
    if action in _TRANSACTION_ACTIONS:
        return Denial("Controle de transação não é permitido.", _READ_ONLY_HINT)
    return Denial("Esta operação não é permitida.", _READ_ONLY_HINT)


class _Authorizer:
    """Callback do authorizer: nega tudo o que não for explicitamente permitido e guarda os motivos.

    Os motivos ficam aqui porque o SQLite só devolve "not authorized" (ou uma mensagem que varia
    com o tipo de negação). A classificação do erro usa esta lista, nunca a classe da exceção.
    """

    def __init__(self) -> None:
        self.denials: list[Denial] = []

    def reset(self) -> None:
        self.denials.clear()

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        try:
            denial = _denial_for(action, arg1, arg2, dbname)
        except Exception:  # uma falha interna nunca pode virar permissão
            denial = Denial("Falha interna ao avaliar a permissão.", None)
        if denial is None:
            return sqlite3.SQLITE_OK
        if len(self.denials) < MAX_DENIALS:
            self.denials.append(denial)
        return sqlite3.SQLITE_DENY


# --- abertura, endurecimento e esquema ---------------------------------------------------------


def _db_label(path: Path) -> str:
    """Nome neutro do arquivo para mensagens: nunca o caminho, que pode ir parar no modelo."""
    return f"'{path.name}'" if path.name else "o caminho informado"


def _failure_reason(exc: BaseException) -> str:
    """Motivo curto e sem caminho de um erro (o texto do erro pode trazer o nome do arquivo)."""
    return getattr(exc, "sqlite_errorname", None) or type(exc).__name__


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Abre o arquivo em `mode=ro` por URI (`as_uri` escapa `#`, `%`, espaços e acentos).

    O caminho real só é usado aqui dentro; as mensagens citam apenas o nome do arquivo.
    """
    label = _db_label(path)

    try:
        resolved = path.resolve()

        if not resolved.exists():
            raise DatabaseUnavailableError(
                f"Banco de dados não encontrado: {label}.",
                hint="Coloque o cinerocket.db em data/ (veja data/README.md) ou defina "
                "CINEDATA_DB_PATH. O comando `cinedata doctor` mostra o caminho completo.",
            )

        if not resolved.is_file():
            raise DatabaseUnavailableError(f"O caminho {label} existe, mas não é um arquivo.")

        if resolved.stat().st_size == 0:
            raise DatabaseUnavailableError(
                f"O arquivo do banco está vazio (0 bytes): {label}.",
                hint="Baixe o banco novamente.",
            )

    except DatabaseUnavailableError:
        raise
    except OSError as exc:
        raise DatabaseUnavailableError(
            f"Não foi possível acessar o banco {label}: {exc.strerror or type(exc).__name__}."
        ) from None

    try:
        return sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(
            f"Não foi possível abrir o banco {label} ({_failure_reason(exc)}).",
            hint="Confira as permissões do arquivo e se outro programa o está usando.",
        ) from None


def _version_text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _check_config(con: Any, driver: Any, name: str, wanted: bool) -> str | None:
    operation = getattr(driver, name, None)
    if operation is None:
        return f"{name} indisponível"
    try:
        con.setconfig(operation, wanted)
        actual = con.getconfig(operation)
    except _SETUP_ERRORS as exc:
        return f"{name} falhou ({type(exc).__name__})"
    if bool(actual) != wanted:
        return f"{name} não foi aplicado (a build ignorou a configuração)"
    return None


def _check_limit(con: Any, driver: Any, name: str, wanted: int) -> str | None:
    category = getattr(driver, name, None)
    if category is None:
        return f"{name} indisponível"
    try:
        con.setlimit(category, wanted)
        actual = con.getlimit(category)
    except _SETUP_ERRORS as exc:
        return f"{name} falhou ({type(exc).__name__})"
    if actual != wanted:
        return f"{name} não foi aplicado (valor lido: {actual})"
    return None


def _check_pragma(con: Any, setter: str, getter: str, expected: int) -> str | None:
    try:
        con.execute(setter).fetchall()
        row = con.execute(getter).fetchone()
    except _SETUP_ERRORS as exc:
        return f"{setter} falhou ({type(exc).__name__})"
    if row is None or row[0] != expected:
        return f"{setter} não foi aplicado"
    return None


def _apply_hardening(con: Any, driver: Any = sqlite3) -> None:
    """Aplica as proteções obrigatórias e confere cada uma relendo o valor (falha fechada).

    Se a build do Python/SQLite não oferecer alguma delas, levanta `DatabaseUnavailableError`
    listando tudo o que faltou. Não há fallback: nenhuma proteção fica silenciosamente desligada.
    """
    results: list[str | None] = []
    if tuple(driver.sqlite_version_info) < MIN_SQLITE_VERSION:
        floor = _version_text(MIN_SQLITE_VERSION)
        results.append(f"SQLite {driver.sqlite_version} é anterior ao mínimo {floor}")
    results += [_check_config(con, driver, name, wanted) for name, wanted in _REQUIRED_DBCONFIG]
    results += [_check_limit(con, driver, name, wanted) for name, wanted in _LIMITS]
    results += [_check_pragma(con, *pragma) for pragma in _PRAGMAS]
    failures = [result for result in results if result]
    if failures:
        raise DatabaseUnavailableError(
            "Esta build do Python/SQLite não oferece o endurecimento obrigatório de segurança: "
            + "; ".join(failures)
            + f" (Python {platform.python_version()}, SQLite {driver.sqlite_version}).",
            hint=f"Use Python 3.12 ou superior com SQLite {_version_text(MIN_SQLITE_VERSION)} "
            "ou superior.",
        )


def _validate_schema(con: sqlite3.Connection) -> None:
    """Confere que as tabelas e colunas da allowlist existem. Extras no arquivo são ignorados."""
    present = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing_tables = [table for table in GOLD_TABLES if table not in present]
    missing_columns: list[str] = []
    for table, columns in GOLD_TABLES.items():
        if table not in present:
            continue
        actual = {row[0] for row in con.execute("SELECT name FROM pragma_table_info(?)", (table,))}
        missing_columns.extend(f"{table}.{column}" for column in columns if column not in actual)
    if not missing_tables and not missing_columns:
        return
    details = []
    if missing_tables:
        details.append("tabelas ausentes: " + ", ".join(missing_tables))
    if missing_columns:
        shown = ", ".join(missing_columns[:10])
        extra = len(missing_columns) - 10
        details.append("colunas ausentes: " + shown + (f" (+{extra})" if extra > 0 else ""))
    raise DatabaseUnavailableError(
        "O banco não tem o esquema esperado da camada Gold (" + "; ".join(details) + ").",
        hint="Confira se o arquivo é o cinerocket.db da atividade.",
    )


def _unavailable_from_sqlite(exc: sqlite3.Error) -> DatabaseUnavailableError:
    text = str(exc)
    if "not a database" in text:
        return DatabaseUnavailableError(
            "O arquivo não é um banco SQLite válido (cabeçalho inválido).",
            hint="Confira se o download terminou e se o arquivo é o cinerocket.db da atividade.",
        )
    if "malformed" in text:
        return DatabaseUnavailableError(
            "O banco parece corrompido.", hint="Baixe o arquivo novamente."
        )
    return DatabaseUnavailableError(f"Não foi possível ler o banco ({_failure_reason(exc)}).")


# --- pré-filtro de texto (só UX) ---------------------------------------------------------------


def _check_text(sql: object) -> str:
    """Valida o que o próprio SQLite/Python rejeitaria de forma pouco clara. Sempre executado."""
    if not isinstance(sql, str):
        raise QueryRejectedError("A consulta deve ser um texto SQL.")
    if "\x00" in sql:
        raise QueryRejectedError("A consulta contém um caractere nulo (NUL).")
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeEncodeError:
        raise QueryRejectedError("A consulta contém caracteres Unicode inválidos.") from None
    if size > MAX_SQL_BYTES:
        raise QueryRejectedError(
            f"A consulta é grande demais ({size} bytes; o máximo é {MAX_SQL_BYTES}).",
            hint="Simplifique a consulta.",
        )
    return sql


def _first_keyword(sql: str) -> str | None:
    """Primeira palavra após espaços e comentários (None se nada sobra; '' se não é letra)."""
    index, length = 0, len(sql)
    while index < length:
        if sql[index] in " \t\r\n\f;":  # ';' sozinho conta como instrução vazia
            index += 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end + 1
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
        else:
            break
    if index >= length:
        return None
    match = re.match(r"[A-Za-z]+", sql[index:])
    return match.group(0).lower() if match else ""


def _check_statement_kind(sql: str) -> None:
    word = _first_keyword(sql)
    if word is None:
        raise QueryRejectedError(
            "A consulta está vazia (ou só tem comentários).", hint=_READ_ONLY_HINT
        )
    if word not in ("select", "with"):
        shown = word.upper() if word else "um símbolo"
        raise QueryRejectedError(
            f"A consulta começa com {shown}, mas só SELECT ou WITH são aceitos.",
            hint=_READ_ONLY_HINT,
        )


# --- células -----------------------------------------------------------------------------------


def _normalize_cell(value: object, max_chars: int) -> tuple[object, bool]:
    if isinstance(value, str) and len(value) > max_chars:
        cut = len(value) - max_chars
        return f"{value[:max_chars]}… [+{cut} caracteres]", True
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<blob de {len(value)} bytes>", False
    return value, False


# --- SafeDatabase ------------------------------------------------------------------------------

_NO_SUCH_TABLE = re.compile(r"no such table: (?P<name>.+)")
_NO_SUCH_COLUMN = re.compile(r"no such column: (?P<name>.+?)(?: - should this be.*)?$", re.DOTALL)
_AMBIGUOUS = re.compile(r"ambiguous column name: (?P<name>.+)")


class SafeDatabase:
    """Conexão somente leitura, endurecida e serializada com o banco Gold."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_s: float = DEFAULT_SQL_TIMEOUT_S,
        max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
    ) -> None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows deve ser um inteiro maior ou igual a 1")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise ValueError("timeout_s deve ser um número (int ou float), em segundos")
        if not 0 < timeout_s <= MAX_TIMEOUT_S:  # também rejeita nan, inf e -inf
            raise ValueError(
                f"timeout_s deve ser finito e estar entre 0 (exclusivo) e {MAX_TIMEOUT_S:g}"
            )
        if isinstance(max_cell_chars, bool) or not isinstance(max_cell_chars, int):
            raise ValueError("max_cell_chars deve ser um inteiro")
        if max_cell_chars < 10:
            raise ValueError("max_cell_chars deve ser pelo menos 10")
        self._path = Path(path)
        self._max_rows = max_rows
        self._timeout_s = float(timeout_s)
        self._max_cell_chars = max_cell_chars
        self._lock = threading.Lock()
        self._authorizer = _Authorizer()
        self._deadline = 0.0
        self._timed_out = False
        self._pregate_enabled = True  # gancho de teste: desliga só o pré-filtro de texto
        self._con: sqlite3.Connection | None = None

        con = _open_readonly(self._path)
        try:
            _apply_hardening(con)
            _validate_schema(con)
            con.set_authorizer(self._authorizer)  # por último, depois de tudo verificado
        except sqlite3.Error as exc:
            con.close()
            raise _unavailable_from_sqlite(exc) from exc
        except BaseException:
            con.close()
            raise
        self._con = con  # só um SafeDatabase completo chega a ter conexão

    @classmethod
    def from_settings(cls, settings: Settings) -> SafeDatabase:
        return cls(
            settings.db_path_resolved,
            max_rows=settings.max_rows,
            timeout_s=settings.sql_timeout_s,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_rows(self) -> int:
        return self._max_rows

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def __enter__(self) -> SafeDatabase:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Fecha a conexão. Pode ser chamado mais de uma vez."""
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def execute(self, sql: str) -> QueryResult:
        """Executa uma consulta de leitura e devolve até `max_rows` linhas.

        Levanta `QueryRejectedError` (política), `QueryFailedError` (SQL inválido),
        `QueryTimeoutError` (prazo) ou `DatabaseUnavailableError` (conexão fechada ou banco
        inutilizável). Um Ctrl+C durante a consulta relança `KeyboardInterrupt`.
        """
        statement = _check_text(sql)
        if self._pregate_enabled:
            _check_statement_kind(statement)
        with self._lock:
            if self._con is None:
                raise DatabaseUnavailableError("A conexão com o banco já foi fechada.")
            return self._run(self._con, statement)

    # --- execução ---

    def _progress(self) -> int:
        # Em CPython, um KeyboardInterrupt levantado aqui dentro é engolido e vira "interrupted";
        # por isso _translate trata "interrupted" sem a flag de prazo como Ctrl+C.
        if time.monotonic() >= self._deadline:
            self._timed_out = True
            return 1
        return 0

    def _run(self, con: sqlite3.Connection, sql: str) -> QueryResult:
        self._authorizer.reset()
        self._timed_out = False
        started = time.monotonic()
        self._deadline = started + self._timeout_s  # armado só depois de obter o lock
        con.set_progress_handler(self._progress, PROGRESS_INTERVAL)
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = con.execute(sql)
            if cursor.description is None:
                raise QueryRejectedError(
                    "A instrução não devolve linhas (vazia ou que não é uma consulta).",
                    hint=_READ_ONLY_HINT,
                )
            columns = tuple(column[0] for column in cursor.description)
            fetched = cursor.fetchmany(self._max_rows + 1)
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc
        finally:
            if cursor is not None:
                cursor.close()
            con.set_progress_handler(None, 0)

        truncated = len(fetched) > self._max_rows
        cut_cells = 0
        rows: list[tuple[object, ...]] = []
        for raw in fetched[: self._max_rows]:
            cells = []
            for value in raw:
                cell, was_cut = _normalize_cell(value, self._max_cell_chars)
                cut_cells += was_cut
                cells.append(cell)
            rows.append(tuple(cells))
        return QueryResult(
            columns=columns,
            rows=tuple(rows),
            truncated=truncated,
            max_rows=self._max_rows,
            elapsed_s=time.monotonic() - started,
            truncated_cells=cut_cells,
        )

    # --- tradução de erros ---

    def _translate(self, exc: sqlite3.Error) -> SafeDatabaseError:
        """Converte um erro do sqlite3 em erro controlado. Pode relançar KeyboardInterrupt."""
        if self._authorizer.denials:  # a lista do authorizer manda; a classe da exceção não
            first = self._authorizer.denials[0]
            return QueryRejectedError(first.message, hint=first.hint)
        text = str(exc)
        if self._timed_out:
            return QueryTimeoutError(
                f"A consulta passou de {self._timeout_s:g} s e foi interrompida.",
                hint=_TIMEOUT_HINT,
            )
        if text == "interrupted":
            raise KeyboardInterrupt  # sem prazo estourado, só pode ser o Ctrl+C do usuário
        if "not a database" in text or "malformed" in text:
            return _unavailable_from_sqlite(exc)
        if "only execute one statement" in text:
            return QueryRejectedError(
                "Só uma instrução SQL por vez é aceita.", hint=_READ_ONLY_HINT
            )
        return self._query_failed(text)

    def _query_failed(self, text: str) -> QueryFailedError:
        if match := _NO_SUCH_TABLE.match(text):
            name = match["name"].strip().strip("\"'`[]")
            close = difflib.get_close_matches(name.lower(), list(GOLD_TABLES), n=3, cutoff=0.6)
            hint = (f"Você quis dizer: {', '.join(close)}? " if close else "") + (
                "Tabelas disponíveis: " + ", ".join(GOLD_TABLES) + "."
            )
            return QueryFailedError(f"A tabela '{name}' não existe.", hint=hint)
        if match := _NO_SUCH_COLUMN.match(text):
            name = match["name"].strip()
            literal = name.startswith('"') and "should this be a string literal" in text
            bare = name.strip("\"'`[]").rsplit(".", 1)[-1]
            close = difflib.get_close_matches(bare.lower(), _ALL_COLUMNS, n=3, cutoff=0.6)
            if literal:
                return QueryFailedError(
                    f'A coluna {name} não existe. Se "{bare}" era um valor de texto, escreva-o '
                    f"entre aspas simples, como '{bare}': aspas duplas servem só para nomes.",
                    hint=_SCHEMA_HINT,
                )
            hint = (f"Você quis dizer: {', '.join(close)}? " if close else "") + _SCHEMA_HINT
            return QueryFailedError(f"A coluna '{bare}' não existe.", hint=hint)
        if match := _AMBIGUOUS.match(text):
            return QueryFailedError(
                f"A coluna '{match['name'].strip()}' é ambígua.",
                hint="Qualifique a coluna com o alias da tabela, por exemplo m.sk_movie_id.",
            )
        if "string or blob too big" in text:
            return QueryFailedError(
                "Um valor intermediário passou do tamanho máximo permitido.",
                hint="Evite concatenar muitos valores ou repetir textos; prefira agregações.",
            )
        return QueryFailedError(f"O SQLite não conseguiu executar a consulta: {text}.")
