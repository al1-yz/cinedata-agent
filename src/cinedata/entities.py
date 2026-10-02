"""Resolução de entidades (filme, pessoa, gênero e produtora) sobre a camada Gold.

Dado um texto livre ("zoe saldana", "Terror", "star wars"), `EntityIndex.find` devolve em qual de
cinco estados a busca caiu e quais candidatos existem, sempre com desambiguadores úteis. Só a
igualdade exata e única resolve (`exact_unique`); busca parcial e fuzzy apenas sugerem.

Toda leitura do banco passa por `SafeDatabase.execute` (este módulo não importa `sqlite3`): a carga
de um tipo usa os parâmetros por chamada `max_rows` e `timeout_s`, com teto, na MESMA conexão, com o
mesmo authorizer, lock e limites. Cada tipo é carregado sob demanda, na primeira busca, e nunca
deixa um índice parcial: truncamento, célula cortada, tabela vazia ou erro do banco viram
`EntityIndexError` e uma nova tentativa recarrega do zero.

Estrutura de um tipo: linhas em ordem canônica (chave normalizada e desempates explícitos, nada
depende da ordem do banco nem de hash), busca exata e por prefixo por bisseção, e dois índices
preguiçosos (tokens e vizinhança de tokens) só construídos quando uma busca parcial ou fuzzy
precisa deles.
"""

from __future__ import annotations

import bisect
import heapq
import threading
import time
import unicodedata
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import chain
from types import MappingProxyType

from cinedata.db import MAX_BULK_ROWS, SafeDatabase, SafeDatabaseError

MAX_QUERY_CHARS = 200  # o maior título real tem 151 caracteres; nomes têm no máximo 50
MAX_NORMALIZED_CHARS = 400  # guarda contra expansão do NFKD (ligaduras viram vários caracteres)
MIN_PARTIAL_CHARS = 3
MIN_PARTIAL_CHARS_CJK = 2
MIN_FUZZY_CHARS = 4
MIN_PREFIX_TOKEN_CHARS = 3  # um token menor só casa por inteiro
MIN_FUZZY_TOKEN_CHARS = 3  # um token menor só casa por inteiro, sem tolerância a erro
FUZZY_SHORT_TOKEN_MAX = 5  # até aqui, 1 edição por token; acima, até 2
FUZZY_MAX_NEIGHBORS = 50
PREFIX_EXPANSION_CAP = 2_000  # acima disso, o token deixa de aceitar prefixo
SCAN_ROW_LIMIT = 100_000  # linhas candidatas examinadas por busca (em ordem canônica)
DEFAULT_MAX_CANDIDATES = 10
MAX_CANDIDATES_CAP = 25
BULK_TIMEOUT_S = 60.0  # prazo mínimo de uma carga (a leitura de pessoas leva de 0,4 a 1,2 s)
_SENTINEL = "\U0010ffff"

# Mapa PT -> EN dos gêneros. O alvo é o nome do banco (normalizado); um alvo ausente é ignorado.
_GENRE_ALIASES = {
    "Ação": "Action",
    "Aventura": "Adventure",
    "Animação": "Animation",
    "Comédia": "Comedy",
    "Documentário": "Documentary",
    "Família": "Family",
    "Fantasia": "Fantasy",
    "História": "History",
    "Terror": "Horror",
    "Música": "Music",
    "Mistério": "Mystery",
    "Ficção científica": "Science Fiction",
    "Sci-Fi": "Science Fiction",
    "SciFi": "Science Fiction",
    "Suspense": "Thriller",
    "Cinema TV": "Tv Movie",
    "Filme de TV": "Tv Movie",
    "Guerra": "War",
    "Faroeste": "Western",
}


class EntityKind(StrEnum):
    FILME = "filme"
    PESSOA = "pessoa"
    GENERO = "genero"
    PRODUTORA = "produtora"


class Role(StrEnum):
    """Papéis de `dim_people.tipo_pessoa`, com a grafia exata do banco."""

    DIRETOR = "Diretor"
    ATOR = "Ator"
    ROTEIRISTA = "Roteirista"


class MatchState(StrEnum):
    EXACT_UNIQUE = "exact_unique"
    EXACT_MULTIPLE = "exact_multiple"
    PARTIAL_CANDIDATES = "partial_candidates"
    FUZZY_SUGGESTIONS = "fuzzy_suggestions"
    NONE = "none"


class Reason(StrEnum):
    """Por que a busca terminou em `none`."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    INVALID_TEXT = "invalid_text"
    TOO_SHORT_FOR_PARTIAL = "too_short_for_partial"
    NO_MATCH = "no_match"
    EXISTS_IN_OTHER_ROLE = "exists_in_other_role"


class EntityIndexError(Exception):
    """O índice de um tipo não pôde ser carregado. Nada parcial fica guardado."""


@dataclass(frozen=True)
class Candidate:
    kind: EntityKind
    key: str  # chave `sk_*` da entidade, para montar o SQL
    label: str  # grafia exata do banco (nome ou título)
    year: int | None = None  # filmes
    movie_id: str | None = None  # filmes: `id_filme`, único
    role: str | None = None  # pessoas
    matched_via: str = "exact"  # exact | alias | prefix | tokens | fuzzy
    edits: int | None = None  # só no fuzzy: total de edições nos tokens


@dataclass(frozen=True)
class EntityMatch:
    state: MatchState
    kind: EntityKind
    query: str  # o texto recebido, cortado em MAX_QUERY_CHARS só para exibição
    normalized_query: str
    candidates: tuple[Candidate, ...] = ()
    total_matches: int = 0  # todos os resultados, mesmo os que `candidates` não traz
    total_is_lower_bound: bool = False  # a busca parou em SCAN_ROW_LIMIT linhas
    resolved: Candidate | None = None  # só em `exact_unique`
    reason: Reason | None = None  # só em `none`
    detail: str | None = None


@dataclass(frozen=True)
class LoadStats:
    rows: int  # linhas lidas do banco
    skipped: int  # linhas sem nome ou chave utilizável
    seconds: float


# --- normalização -------------------------------------------------------------------------------

_EXTRA_LETTERS = {
    "ø": "o",
    "đ": "d",
    "ł": "l",
    "æ": "ae",
    "œ": "oe",
    "ð": "d",
    "þ": "th",
    "ħ": "h",
    "ı": "i",
}
_ASCII_TABLE = str.maketrans({chr(code): " " for code in range(128) if not chr(code).isalnum()})
_NON_LATIN_LETTER: dict[str, bool] = {}


def _is_non_latin_letter(char: str) -> bool:
    cached = _NON_LATIN_LETTER.get(char)
    if cached is None:
        if not unicodedata.category(char).startswith("L"):
            cached = False
        else:
            try:
                cached = not unicodedata.name(char).startswith("LATIN")
            except ValueError:  # letra sem nome: na dúvida, preserva as marcas
                cached = True
        _NON_LATIN_LETTER[char] = cached
    return cached


def _symbol_key(text: str) -> str:
    """Chave de reserva de textos sem letras nem dígitos (`★`, `________`): os símbolos."""
    out: list[str] = []
    for char in unicodedata.normalize("NFKC", text):  # sem casefold: símbolo não tem caixa
        category = unicodedata.category(char)
        if category[0] == "Z" or char.isspace():
            out.append(" ")
        elif category in ("Cf", "Cc", "Cs", "Co", "Cn", "Mn"):
            continue
        else:
            out.append(char)
    return " ".join("".join(out).split())


def _normalize_unicode(text: str) -> str:
    # NFKD, casefold, NFKD: a forma do padrão Unicode para comparar sem caixa nem compatibilidade
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())
    out: list[str] = []
    keep_marks = False  # a última letra foi de uma escrita não latina
    for char in folded:
        category = unicodedata.category(char)
        if category == "Mn":
            if keep_marks:
                out.append(char)
            continue
        if category in ("Mc", "Me"):
            out.append(char)
            continue
        if category == "Cf":
            continue
        if category[0] in "ZCPS":
            out.append(" ")
            keep_marks = False
            continue
        out.append(_EXTRA_LETTERS.get(char, char))
        keep_marks = _is_non_latin_letter(char)
    key = " ".join(unicodedata.normalize("NFC", "".join(out)).split())
    return key or _symbol_key(text)


def normalize(text: str) -> str:
    """Chave de busca: sem maiúsculas, acentos latinos, pontuação nem espaços repetidos.

    Letras não latinas mantêm suas marcas (dakuten do kana, niqqud do hebraico, acentos do grego
    e do cirílico). Caracteres de formato (zero-width, BOM) somem. Um texto só de símbolos (`★`)
    vira a própria chave de reserva; só devolve "" quando não resta nenhum caractere útil.
    A mesma função serve ao índice e à consulta.
    """
    if text.isascii():
        key = " ".join(text.lower().translate(_ASCII_TABLE).split())
        return key or _symbol_key(text)
    return _normalize_unicode(text)


def _has_cjk(text: str) -> bool:
    return any(
        0x3040 <= ord(c) <= 0x30FF  # hiragana e katakana
        or 0x3400 <= ord(c) <= 0x4DBF  # ideogramas, extensão A
        or 0x4E00 <= ord(c) <= 0x9FFF  # ideogramas unificados
        or 0xAC00 <= ord(c) <= 0xD7AF  # sílabas hangul
        or 0x1100 <= ord(c) <= 0x11FF  # jamo
        or 0xFF66 <= ord(c) <= 0xFF9F  # katakana de meia largura
        for c in text
    )


def _edit_distance(a: str, b: str, limit: int) -> int | None:
    """Distância de edição com transposição adjacente (OSA), ou None se passar de `limit`."""
    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > limit:
        return None
    if a == b:
        return 0
    previous2: list[int] = []
    previous = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        current = [i] + [0] * len_b
        best = i
        char_a = a[i - 1]
        for j in range(1, len_b + 1):
            char_b = b[j - 1]
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_a != char_b),
            )
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                value = min(value, previous2[j - 2] + 1)
            current[j] = value
            best = min(best, value)
        if best > limit:
            return None
        previous2, previous = previous, current
    return previous[len_b] if previous[len_b] <= limit else None


# --- validação de argumentos ---------------------------------------------------------------------

_KINDS = {kind.value: kind for kind in EntityKind}
_ROLE_NAMES = {
    "diretor": Role.DIRETOR,
    "diretora": Role.DIRETOR,
    "director": Role.DIRETOR,
    "ator": Role.ATOR,
    "atriz": Role.ATOR,
    "actor": Role.ATOR,
    "actress": Role.ATOR,
    "roteirista": Role.ROTEIRISTA,
    "writer": Role.ROTEIRISTA,
    "screenwriter": Role.ROTEIRISTA,
}


def _parse_kind(kind: object) -> EntityKind:
    if isinstance(kind, EntityKind):
        return kind
    if isinstance(kind, str):
        found = _KINDS.get(normalize(kind))
        if found is not None:
            return found
    valid = ", ".join(_KINDS)
    raise ValueError(f"kind inválido: {str(kind)[:40]!r}. Valores aceitos: {valid}.")


def _parse_role(role: object) -> Role | None:
    if role is None:
        return None
    if isinstance(role, Role):
        return role
    if isinstance(role, str):
        found = _ROLE_NAMES.get(normalize(role))
        if found is not None:
            return found
    valid = ", ".join(item.value for item in Role)
    raise ValueError(f"role inválido: {str(role)[:40]!r}. Valores aceitos: {valid}.")


def _check_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CANDIDATES_CAP
    ):
        raise ValueError(f"max_candidates deve ser um inteiro entre 1 e {MAX_CANDIDATES_CAP}")
    return value


def _unusable_text(query: str) -> Reason | None:
    if len(query) > MAX_QUERY_CHARS:
        return Reason.TOO_LONG
    if "\x00" in query:
        return Reason.INVALID_TEXT
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        return Reason.INVALID_TEXT
    return None


# --- um tipo carregado ---------------------------------------------------------------------------

_SOURCES = {
    EntityKind.FILME: "SELECT sk_movie_id, id_filme, titulo, ano_lancamento FROM dim_movies",
    EntityKind.PESSOA: "SELECT sk_person_id, nome_pessoa, tipo_pessoa FROM dim_people",
    EntityKind.GENERO: "SELECT sk_genre_id, nome_genero FROM dim_genres",
    EntityKind.PRODUTORA: "SELECT sk_company_id, nome_produtora FROM dim_companies",
}
_LABEL_COLUMN = {
    EntityKind.FILME: 2,
    EntityKind.PESSOA: 1,
    EntityKind.GENERO: 1,
    EntityKind.PRODUTORA: 1,
}


class _Table:
    """Linhas de um tipo em ordem canônica (listas paralelas) e índices preguiçosos."""

    __slots__ = (
        "buckets",
        "ids",
        "keys",
        "kind",
        "labels",
        "movie_ids",
        "postings",
        "roles",
        "stats",
        "tokens_lock",
        "via_alias",
        "vocab",
        "years",
    )

    def __init__(self, kind: EntityKind) -> None:
        self.kind = kind
        self.keys: list[str] = []  # chave normalizada, ordenada
        self.ids: list[str] = []
        self.labels: list[str] = []
        self.years: list[int | None] | None = None
        self.movie_ids: list[str | None] | None = None
        self.roles: list[str | None] | None = None
        self.via_alias: list[bool] | None = None
        self.stats = LoadStats(0, 0, 0.0)
        self.tokens_lock = threading.Lock()
        self.vocab: list[str] | None = None
        self.postings: dict[str, array[int]] | None = None
        self.buckets: dict[tuple[int, str], list[str]] | None = None


def _movie_order(movie_id: str | None) -> tuple[int, int, str]:
    if movie_id is not None and movie_id.isdigit():
        return (0, int(movie_id), "")
    return (1, 0, movie_id or "")


def _build_table(kind: EntityKind, rows: Sequence[Sequence[object]]) -> tuple[_Table, int]:
    """Monta um tipo a partir das linhas do banco; linhas sem chave ou nome são puladas."""
    table = _Table(kind)
    keys: list[str] = []
    ids: list[str] = []
    labels: list[str] = []
    years: list[int | None] = []
    movie_ids: list[str | None] = []
    roles: list[str | None] = []
    skipped = 0
    label_column = _LABEL_COLUMN[kind]
    for row in rows:
        sk, label = row[0], row[label_column]
        if not isinstance(sk, str) or not sk or not isinstance(label, str):
            skipped += 1
            continue
        key = normalize(label)
        if not key:
            skipped += 1
            continue
        keys.append(key)
        ids.append(sk)
        labels.append(label)
        if kind is EntityKind.FILME:
            year = row[3]
            years.append(year if isinstance(year, int) and not isinstance(year, bool) else None)
            movie_id = row[1]
            movie_ids.append(None if movie_id is None else str(movie_id))
        elif kind is EntityKind.PESSOA:
            role = row[2]
            roles.append(role if isinstance(role, str) else None)

    via_alias: list[bool] | None = None
    if kind is EntityKind.GENERO:
        via_alias = [False] * len(keys)
        first_row = {key: index for index, key in enumerate(keys)}
        for alias, target in _GENRE_ALIASES.items():
            alias_key, target_key = normalize(alias), normalize(target)
            source = first_row.get(target_key)
            if source is None or alias_key in first_row:
                continue  # alvo ausente no banco, ou alias igual a um nome real
            keys.append(alias_key)
            ids.append(ids[source])
            labels.append(labels[source])
            via_alias.append(True)
            first_row[alias_key] = len(keys) - 1  # dois aliases com a mesma chave valem um só

    def tie_break(index: int) -> tuple[object, ...]:
        if kind is EntityKind.FILME:
            year = years[index]
            return (year is None, year or 0, _movie_order(movie_ids[index]), ids[index])
        if kind is EntityKind.PESSOA:
            return (roles[index] or "", labels[index], ids[index])
        return (labels[index], ids[index])  # um alias nunca empata com um nome real (ver acima)

    order = sorted(range(len(keys)), key=keys.__getitem__)
    start = 0
    while start < len(order):  # empates de chave: ordem explícita, nunca a do banco
        end = start + 1
        while end < len(order) and keys[order[end]] == keys[order[start]]:
            end += 1
        if end - start > 1:
            order[start:end] = sorted(order[start:end], key=tie_break)
        start = end

    table.keys = [keys[i] for i in order]
    table.ids = [ids[i] for i in order]
    table.labels = [labels[i] for i in order]
    if kind is EntityKind.FILME:
        table.years = [years[i] for i in order]
        table.movie_ids = [movie_ids[i] for i in order]
    elif kind is EntityKind.PESSOA:
        table.roles = [roles[i] for i in order]
    if via_alias is not None:
        table.via_alias = [via_alias[i] for i in order]
    return table, skipped


def _ensure_tokens(table: _Table) -> None:
    if table.postings is not None:
        return
    with table.tokens_lock:
        if table.postings is not None:
            return
        postings: dict[str, array[int]] = {}
        for row, key in enumerate(table.keys):
            for token in set(key.split()):
                rows = postings.get(token)
                if rows is None:
                    postings[token] = array("I", (row,))
                else:
                    rows.append(row)
        table.vocab = sorted(postings)
        table.postings = postings  # por último: quem vê `postings` vê também `vocab`


def _ensure_buckets(table: _Table) -> None:
    # Vizinhança por tamanho e por primeira ou última letra: com 1 edição isso é completo; com 2,
    # um token errado nas duas pontas ao mesmo tempo não é encontrado (o fuzzy só sugere).
    _ensure_tokens(table)
    if table.buckets is not None:
        return
    with table.tokens_lock:
        if table.buckets is not None:
            return
        buckets: dict[tuple[int, str], list[str]] = {}
        for token in table.vocab or ():
            if len(token) >= MIN_FUZZY_TOKEN_CHARS:
                buckets.setdefault((len(token), token[0]), []).append(token)
                buckets.setdefault((len(token), "$" + token[-1]), []).append(token)
        table.buckets = buckets


def _role_ok(table: _Table, row: int, role: str | None) -> bool:
    return role is None or (table.roles is not None and table.roles[row] == role)


def _candidate(table: _Table, row: int, matched_via: str, edits: int | None = None) -> Candidate:
    if table.via_alias is not None and table.via_alias[row]:
        matched_via = "alias"
    return Candidate(
        kind=table.kind,
        key=table.ids[row],
        label=table.labels[row],
        year=table.years[row] if table.years is not None else None,
        movie_id=table.movie_ids[row] if table.movie_ids is not None else None,
        role=table.roles[row] if table.roles is not None else None,
        matched_via=matched_via,
        edits=edits,
    )


@dataclass(frozen=True)
class _Found:
    ranked: list[tuple[int, str, int | None]]  # (linha, como casou, edições), melhor primeiro
    total: int
    lower_bound: bool = False


_NOTHING = _Found([], 0)


def _finish(
    table: _Table,
    items: list[tuple[tuple[object, ...], int, str, int | None]],
    limit: int,
    lower_bound: bool,
) -> _Found:
    """Tira duplicatas de entidade (alias e nome real do mesmo gênero) e pega os melhores."""
    if table.via_alias is not None:
        best: dict[str, tuple[tuple[object, ...], int, str, int | None]] = {}
        for item in items:
            current = best.get(table.ids[item[1]])
            if current is None or item[0] < current[0]:
                best[table.ids[item[1]]] = item
        items = list(best.values())
    top = heapq.nsmallest(limit, items)
    return _Found([(row, via, edits) for _, row, via, edits in top], len(items), lower_bound)


# --- busca parcial e fuzzy -----------------------------------------------------------------------


@dataclass(frozen=True)
class _TokenPlan:
    token: str
    prefix_ok: bool
    expansions: list[str]
    size: int


def _plan_tokens(table: _Table, tokens: list[str]) -> list[_TokenPlan] | None:
    postings, vocab = table.postings or {}, table.vocab or []
    plans: list[_TokenPlan] = []
    for token in tokens:
        prefix_ok = len(token) >= MIN_PREFIX_TOKEN_CHARS
        expansions = [token] if token in postings else []
        if prefix_ok:
            low = bisect.bisect_left(vocab, token)
            high = bisect.bisect_left(vocab, token + _SENTINEL)
            if high - low > PREFIX_EXPANSION_CAP:
                prefix_ok = False
            else:
                expansions = vocab[low:high]
        if not expansions:
            return None
        plans.append(
            _TokenPlan(token, prefix_ok, expansions, sum(len(postings[e]) for e in expansions))
        )
    return plans


def _candidate_rows(
    table: _Table, alternatives: list[list[str]], sizes: list[int]
) -> tuple[list[int], bool]:
    """Linhas do token mais seletivo, em ordem canônica, limitadas a SCAN_ROW_LIMIT."""
    postings = table.postings or {}
    chosen = alternatives[sizes.index(min(sizes))]
    rows = sorted(set(chain.from_iterable(postings[token] for token in chosen)))
    if len(rows) > SCAN_ROW_LIMIT:
        return rows[:SCAN_ROW_LIMIT], True
    return rows, False


def _subset_class(key: str, plans: list[_TokenPlan]) -> int | None:
    """1 = tokens inteiros; 2 = algum só como prefixo; None = não casa."""
    key_tokens = key.split()
    present = set(key_tokens)
    prefixed = False
    for plan in plans:
        if plan.token in present:
            continue
        if plan.prefix_ok and any(token.startswith(plan.token) for token in key_tokens):
            prefixed = True
            continue
        return None
    return 2 if prefixed else 1


def _partial(table: _Table, q: str, role: str | None, limit: int) -> _Found:
    _ensure_tokens(table)
    keys = table.keys
    tokens = q.split()
    # linha -> classe: 0 = prefixo do nome, 1 = tokens inteiros, 2 = tokens com algum prefixo
    matches: dict[int, int] = {}
    low = bisect.bisect_left(keys, q)
    high = bisect.bisect_left(keys, q + _SENTINEL)
    for row in range(low, high):
        if _role_ok(table, row, role):
            matches[row] = 0
    lower_bound = False
    plans = _plan_tokens(table, tokens)
    if plans is not None:
        rows, lower_bound = _candidate_rows(
            table, [p.expansions for p in plans], [p.size for p in plans]
        )
        for row in rows:
            if row not in matches and _role_ok(table, row, role):
                cls = _subset_class(keys[row], plans)
                if cls is not None:
                    matches[row] = cls
    if not matches:
        return _NOTHING
    items = []
    for row, cls in matches.items():
        key = keys[row]
        rank = (cls, len(key.split()) - len(tokens), len(key), key)  # a linha desempata depois
        items.append((rank, row, "prefix" if cls == 0 else "tokens", None))
    return _finish(table, items, limit, lower_bound)


def _neighbors(table: _Table, token: str) -> dict[str, int]:
    """Tokens do vocabulário a poucas edições de `token` (1 até 5 letras, 2 acima)."""
    allowed = 1 if len(token) <= FUZZY_SHORT_TOKEN_MAX else 2
    buckets = table.buckets or {}
    found: dict[str, int] = {}
    seen: set[str] = set()
    for delta in range(-allowed, allowed + 1):
        length = len(token) + delta
        for bucket in ((length, token[0]), (length, "$" + token[-1])):
            for candidate in buckets.get(bucket, ()):
                if candidate in seen:
                    continue
                seen.add(candidate)
                distance = _edit_distance(token, candidate, allowed)
                if distance is not None:
                    found[candidate] = distance
    best = sorted(found.items(), key=lambda item: (item[1], item[0]))[:FUZZY_MAX_NEIGHBORS]
    return dict(best)


def _fuzzy(table: _Table, q: str, role: str | None, limit: int) -> _Found:
    _ensure_buckets(table)
    postings = table.postings or {}
    keys = table.keys
    tokens = q.split()
    alternatives: list[dict[str, int]] = []
    for token in tokens:
        if len(token) < MIN_FUZZY_TOKEN_CHARS:
            alternative = {token: 0} if token in postings else {}
        else:
            alternative = _neighbors(table, token)
        if not alternative:
            return _NOTHING
        alternatives.append(alternative)
    sizes = [sum(len(postings[t]) for t in alternative) for alternative in alternatives]
    rows, lower_bound = _candidate_rows(table, [list(a) for a in alternatives], sizes)
    items = []
    for row in rows:
        if not _role_ok(table, row, role):
            continue
        key = keys[row]
        key_tokens = key.split()
        total = exact_tokens = 0
        for alternative in alternatives:
            edits = min((alternative[t] for t in key_tokens if t in alternative), default=None)
            if edits is None:
                break
            total += edits
            exact_tokens += edits == 0
        else:
            rank = (total, -exact_tokens, len(key_tokens) - len(tokens), len(key), key)
            items.append((rank, row, "fuzzy", total))
    if not items:
        return _NOTHING
    return _finish(table, items, limit, lower_bound)


# --- o índice ------------------------------------------------------------------------------------


def _none(
    kind: EntityKind, shown: str, q: str, reason: Reason, detail: str | None = None
) -> EntityMatch:
    return EntityMatch(MatchState.NONE, kind, shown, q, reason=reason, detail=detail)


class _Slot:
    __slots__ = ("lock", "table")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.table: _Table | None = None


class EntityIndex:
    """Resolve nomes a entidades da Gold. Cada tipo é carregado na primeira busca que o usa."""

    def __init__(self, db: SafeDatabase, *, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> None:
        self._db = db
        self._default_limit = _check_limit(max_candidates)
        self._slots = {kind: _Slot() for kind in EntityKind}

    def preload(self, *kinds: EntityKind | str) -> None:
        """Carrega os tipos já (todos, se nenhum for dado). Opcional: `find` carrega sozinho."""
        for kind in kinds or tuple(EntityKind):
            self._table(_parse_kind(kind))

    def stats(self) -> Mapping[EntityKind, LoadStats]:
        """Estatísticas dos tipos já carregados."""
        loaded = {
            kind: slot.table.stats for kind, slot in self._slots.items() if slot.table is not None
        }
        return MappingProxyType(loaded)

    def find(
        self,
        kind: EntityKind | str,
        query: str,
        *,
        role: Role | str | None = None,
        max_candidates: int | None = None,
    ) -> EntityMatch:
        """Procura `query` entre as entidades de `kind` e devolve o estado e os candidatos."""
        entity_kind = _parse_kind(kind)
        role_filter = _parse_role(role)
        if role_filter is not None and entity_kind is not EntityKind.PESSOA:
            raise ValueError("role só se aplica ao tipo pessoa")
        limit = self._default_limit if max_candidates is None else _check_limit(max_candidates)
        if not isinstance(query, str):
            raise TypeError("query deve ser um texto")
        shown = query[:MAX_QUERY_CHARS]
        unusable = _unusable_text(query)
        if unusable is not None:
            return _none(entity_kind, shown, "", unusable)
        q = normalize(query)
        if not q:
            return _none(entity_kind, shown, q, Reason.EMPTY)
        if len(q) > MAX_NORMALIZED_CHARS:
            return _none(entity_kind, shown, q, Reason.TOO_LONG)

        table = self._table(entity_kind)
        role_value = role_filter.value if role_filter is not None else None

        low = bisect.bisect_left(table.keys, q)
        high = bisect.bisect_right(table.keys, q)
        exact = [row for row in range(low, high) if _role_ok(table, row, role_value)]
        if exact:
            state = MatchState.EXACT_UNIQUE if len(exact) == 1 else MatchState.EXACT_MULTIPLE
            candidates = tuple(_candidate(table, row, "exact") for row in exact[:limit])
            return EntityMatch(
                state,
                entity_kind,
                shown,
                q,
                candidates,
                total_matches=len(exact),
                resolved=candidates[0] if state is MatchState.EXACT_UNIQUE else None,
            )
        if low < high:  # existe, mas não no papel pedido
            roles = sorted(
                {table.roles[row] or "?" for row in range(low, high)} if table.roles else ()
            )
            return _none(
                entity_kind,
                shown,
                q,
                Reason.EXISTS_IN_OTHER_ROLE,
                "existe como: " + ", ".join(roles),
            )

        partial_min = MIN_PARTIAL_CHARS_CJK if _has_cjk(q) else MIN_PARTIAL_CHARS
        if len(q) >= partial_min:
            found = _partial(table, q, role_value, limit)
            if found.total:
                return self._match(
                    MatchState.PARTIAL_CANDIDATES, entity_kind, shown, q, table, found
                )
        if len(q) >= MIN_FUZZY_CHARS:
            found = _fuzzy(table, q, role_value, limit)
            if found.total:
                return self._match(
                    MatchState.FUZZY_SUGGESTIONS, entity_kind, shown, q, table, found
                )
        reason = Reason.TOO_SHORT_FOR_PARTIAL if len(q) < partial_min else Reason.NO_MATCH
        return _none(entity_kind, shown, q, reason)

    @staticmethod
    def _match(
        state: MatchState, kind: EntityKind, shown: str, q: str, table: _Table, found: _Found
    ) -> EntityMatch:
        candidates = tuple(_candidate(table, row, via, edits) for row, via, edits in found.ranked)
        return EntityMatch(
            state,
            kind,
            shown,
            q,
            candidates,
            total_matches=found.total,
            total_is_lower_bound=found.lower_bound,
        )

    # --- carga sob demanda ---

    def _table(self, kind: EntityKind) -> _Table:
        slot = self._slots[kind]
        table = slot.table
        if table is not None:
            return table
        with slot.lock:
            if slot.table is None:  # outro thread pode ter carregado enquanto esperávamos
                slot.table = self._load(kind)
            return slot.table

    def _load(self, kind: EntityKind) -> _Table:
        started = time.perf_counter()
        try:
            result = self._db.execute(
                _SOURCES[kind],
                max_rows=MAX_BULK_ROWS,
                timeout_s=max(self._db.timeout_s, BULK_TIMEOUT_S),
            )
        except SafeDatabaseError as exc:
            raise EntityIndexError(
                f"Não foi possível carregar o índice de {kind.value}: {exc}"
            ) from exc
        if result.truncated:
            raise EntityIndexError(
                f"O índice de {kind.value} passou de {MAX_BULK_ROWS} linhas e ficaria incompleto."
            )
        if result.truncated_cells:
            raise EntityIndexError(
                f"O índice de {kind.value} teve {result.truncated_cells} valor(es) cortado(s) "
                "por tamanho e ficaria incorreto."
            )
        if not result.rows:
            raise EntityIndexError(f"A tabela de {kind.value} está vazia.")
        table, skipped = _build_table(kind, result.rows)
        if not table.keys:
            raise EntityIndexError(f"Nenhuma linha de {kind.value} tem nome utilizável.")
        table.stats = LoadStats(len(result.rows), skipped, time.perf_counter() - started)
        return table


__all__ = [
    "BULK_TIMEOUT_S",
    "Candidate",
    "EntityIndex",
    "EntityIndexError",
    "EntityKind",
    "EntityMatch",
    "LoadStats",
    "MatchState",
    "Reason",
    "Role",
    "normalize",
]
