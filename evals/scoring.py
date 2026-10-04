"""Pontuação determinística de um caso: o rastro do agente contra o gabarito, sem LLM-juiz.

Fontes de verdade, todas do código: o gabarito (calculado na hora pelo `run_case` do M1c) e o
rastro do agente (`RunTrace`: status final, consultas bem-sucedidas, colunas e linhas). O SQL do
agente nunca é comparado como texto e não precisa ser o do gabarito.

Uma consulta bem-sucedida só é evidência (`_evidence`) se:

- reproduz o gabarito (`match_table`, abaixo), inclusive o rótulo que a resposta mostra;
- foi lida antes da resposta final: o PydanticAI 2 executa um run_sql pedido na mesma resposta
  do final_answer, mas o modelo escreveu o texto sem o resultado dele (a produção já recusa essa
  resposta; aqui a regra é conferida de novo, de forma independente);
- não foi truncada: com linhas escondidas, nada prova que elas não mudariam a resposta;
- o modelo recebeu todas as linhas exigidas: o rastro guarda o resultado completo, mas a
  ferramenta só manda ao modelo as `rows_shown` primeiras linhas (limite de tamanho).

Correção do resultado de UMA consulta (`match_table`):

1. Mapeamento por valor: cada coluna obrigatória do gabarito (identidade, rótulo exibido e
   métricas) é ligada a uma coluna do resultado cujos valores casam. Aliases, colunas extras
   (inclusive de desempate) e a ordem das colunas não importam. Um filme se identifica pelo
   id_filme ou por título + ano, e título + ano só vale quando é único no gabarito (senão, só o
   id_filme prova filmes distintos). Identificar não basta: o resultado precisa trazer também o
   rótulo que a resposta mostra (`display_columns`: título + ano, e o id quando eles se repetem).
2. Linhas como multiconjunto: emparelhamento bipartido máximo, cada linha do agente com no máximo
   uma do gabarito e vice-versa, então duplicatas não se escondem. Identidade por texto igual (sem
   caixa nem espaços repetidos); métricas com a tolerância explícita de cada uma (`cases.py`).
3. Forma da resposta:
   - set: exatamente as linhas do gabarito; ordem livre.
   - ranked: exatamente a janela do top N do gabarito (RANK() <= N): TODOS os empatados no corte
     entram, mesmo passando de N linhas, e nada fora dela; ordem compatível com o ranking
     (empatados em qualquer ordem).
   - leaders: todos os empatados no topo, nas primeiras linhas. Linhas abaixo deles são contexto:
     as que estão no gabarito precisam estar certas; as de fora dele ficam anotadas como não
     verificadas e nunca podem empatar com o líder.

Fidelidade do texto final (`check_answer`), também determinística: cada linha exigida do gabarito
(todas no set e no ranked, os líderes no leaders) precisa aparecer no texto com a identidade legível
(título + ano, nome, ano...) e a métrica principal, lida em formato pt-BR ou en com a mesma
tolerância e escala da métrica; cada trecho do texto serve a uma linha só, salvo distribuição
explícita ("cada", "ambos", "todos"). Números de posição não valem como valor, o id só vale escrito
como id, um número rotulado como outra grandeza ("65 anos", "9%", "nota 9,8") ou um limite ("mais
de 3") não vale como a métrica, e dinheiro exige a moeda certa, inclusive na frase ou no título
que rotula o número. Tabelas markdown valem com ou sem as barras das bordas. Nos rankings, o texto
segue a ordem do gabarito, e toda posição escrita junto do nome de uma linha precisa ser a dela (a
numeração preguiçosa do markdown, "1." em todos os itens, não é posição). As apresentações
ESTRUTURADAS (tabelas, listas, blocos de uma linha por resultado) não podem trazer linhas
inventadas nem se contradizer: cada apresentação completa prova sozinha o resultado. Gabarito vazio
exige um texto que diga que nada foi encontrado. O texto não decide a correção do SQL, e
contradições em prosa solta (frases, números auxiliares) não são julgadas.

Casos de política sem dados (`text_policy_problem`): além do status e de nenhuma ferramenta, um
texto mínimo: a recusa recusa (ou limita o escopo ao catálogo) sem entregar o pedido; a ajuda diz
o que o agente responde sobre o catálogo, sem afirmar números dele.
"""

from __future__ import annotations

import bisect
import itertools
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from cinedata.entities import _GENRE_ALIASES, MatchState
from cinedata.reference import ReferenceResult
from cinedata.runtime import (
    AgentAnswer,
    AgentFailure,
    AgentOutcome,
    AnswerStatus,
    FailureKind,
    RunTrace,
    SqlExecution,
)
from evals.cases import Category, EvalCase, Metric, Quantity, ResultCheck, Shape, TextPolicy

MAX_MAPPINGS = 512  # atribuições coluna -> coluna tentadas por consulta
# Folga para o erro de representação binária, além da tolerância da métrica: poucos ulps do valor
# (2e-6 em R$ 12 bilhões, 1e-14 numa nota), nunca uma fração dele.
FLOAT_SLACK_ULPS = 16


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 (veredito, não senha)
    FAIL = "fail"  # avaliado: o agente errou
    ERROR = "error"  # NÃO avaliado: provedor, banco ou gabarito falharam


class FailureCategory(StrEnum):
    PROVIDER = "provider"  # provedor ou limite de uso: nada a dizer sobre a semântica
    DATABASE = "database"  # banco indisponível durante a pergunta
    HARNESS = "harness"  # o gabarito não pôde ser calculado
    AGENT_PROTOCOL = "agent_protocol"  # sem resposta válida no orçamento, ou requisição recusada
    SQL_ERROR = "sql_error"  # só consultas com erro, nenhuma bem-sucedida
    WRONG_STATUS = "wrong_status"  # pergunta de dados respondida sem dados
    POLICY = "policy"  # ambiguidade, escopo ou ajuda tratados errado
    UNGROUNDED = "ungrounded"  # data_answer sem consulta bem-sucedida no rastro
    RESULT_MISMATCH = "result_mismatch"  # nenhuma consulta reproduz o gabarito
    ANSWER_TEXT = "answer_text"  # SQL certo, mas o texto não traz as linhas exigidas


NOT_EVALUATED = frozenset(
    {FailureCategory.PROVIDER, FailureCategory.DATABASE, FailureCategory.HARNESS}
)
# Falhas do provedor que nada dizem sobre o agente: não avaliado. BAD_REQUEST (400/413/422) não
# está aqui: o provedor recusou a requisição montada pela aplicação, e isso é uma falha avaliada.
PROVIDER_KINDS = frozenset(
    {
        FailureKind.AUTH,
        FailureKind.PAYMENT,
        FailureKind.FORBIDDEN,
        FailureKind.MODEL_UNAVAILABLE,
        FailureKind.RATE_LIMITED,
        FailureKind.PROVIDER_UNAVAILABLE,
        FailureKind.PROVIDER_ERROR,
    }
)

# --- valores ---------------------------------------------------------------------------------


def _key(value: object) -> str | None:
    """Identidade comparável: texto sem caixa nem espaços repetidos; 2016.0 vale 2016."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split()).casefold()


def _number(value: object) -> float | None:
    """Só números de verdade: texto ("65") e booleanos não valem como métrica."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _close(agent: object, expected: object, metric: Metric, scale: float) -> bool:
    if expected is None:
        return agent is None
    want, got = _number(expected), _number(agent)
    if want is None or got is None:
        return False
    target = want * scale
    slack = FLOAT_SLACK_ULPS * math.ulp(max(1.0, abs(target), abs(got)))  # só o erro binário
    return abs(got - target) <= metric.tolerance * scale + slack


def competition_ranks(values: Sequence[object]) -> list[int]:
    """Classificação de competição, maior primeiro (1, 2, 2, 4); nulos ficam por último."""
    keyed = [-math.inf if (number := _number(value)) is None else number for value in values]
    ascending = sorted(keyed)
    return [1 + len(ascending) - bisect.bisect_right(ascending, value) for value in keyed]


def _max_matching(edges: list[list[int]], right: int) -> dict[int, int]:
    """Emparelhamento bipartido máximo (Kuhn): item da esquerda -> item da direita."""
    owner = [-1] * right

    def augment(row: int, seen: set[int]) -> bool:
        for target in edges[row]:
            if target in seen:
                continue
            seen.add(target)
            if owner[target] == -1 or augment(owner[target], seen):
                owner[target] = row
                return True
        return False

    for row in range(len(edges)):
        augment(row, set())
    return {row: target for target, row in enumerate(owner) if row != -1}


# --- uma consulta contra o gabarito ----------------------------------------------------------


@dataclass(frozen=True)
class Binding:
    expected: str  # coluna do gabarito
    agent: str  # coluna do resultado do agente
    position: int
    scale: float = 1.0


@dataclass(frozen=True)
class TableMatch:
    matched: bool
    detail: str | None  # código do primeiro problema; None quando confere
    message: str
    bindings: tuple[Binding, ...] = ()
    matched_rows: int = 0
    notes: tuple[str, ...] = ()
    problems: int = 0
    pairs: tuple[tuple[int, int], ...] = ()  # (linha do agente, linha do gabarito) casadas

    def to_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "detail": self.detail,
            "message": self.message,
            "mapping": {
                b.expected: b.agent if b.scale == 1 else f"{b.agent} (x{b.scale:g})"
                for b in self.bindings
            },
            "matched_rows": self.matched_rows,
            "notes": list(self.notes),
        }


def _ranked_options(scored: list[tuple[int, Binding]]) -> list[Binding]:
    """Colunas candidatas com pelo menos um acerto, as que mais acertam primeiro."""
    ordered = sorted((item for item in scored if item[0]), key=lambda item: -item[0])
    return [binding for _, binding in ordered]


def _identity_options(
    name: str, expected: Sequence[object], columns: Sequence[str], rows: Sequence[tuple]
) -> list[Binding]:
    keys = {_key(value) for value in expected} - {None}
    return _ranked_options(
        [
            (sum(_key(row[j]) in keys for row in rows), Binding(name, column, j))
            for j, column in enumerate(columns)
        ]
    )


def _near(value: object, targets: list[float], metric: Metric, scale: float) -> bool:
    """`value` está dentro da tolerância de algum alvo (busca binária nos alvos ordenados)."""
    got = _number(value)
    if got is None or not targets:
        return False
    position = bisect.bisect_left(targets, got / scale)
    return any(
        _close(got, targets[k], metric, scale)
        for k in (position - 1, position)
        if 0 <= k < len(targets)
    )


def _metric_options(
    metric: Metric, expected: Sequence[object], columns: Sequence[str], rows: Sequence[tuple]
) -> list[Binding]:
    targets = sorted(number for value in expected if (number := _number(value)) is not None)
    return _ranked_options(
        [
            (
                sum(_near(row[j], targets, metric, scale) for row in rows),
                Binding(metric.column, column, j, scale),
            )
            for j, column in enumerate(columns)
            for scale in metric.scales
        ]
    )


def _unique(records: Sequence[dict[str, object]], columns: Sequence[str]) -> bool:
    keys = [tuple(_key(record[column]) for column in columns) for record in records]
    return len(set(keys)) == len(keys)


def disambiguator(check: ResultCheck, records: Sequence[dict[str, object]]) -> tuple[str, ...]:
    """Outra identidade que distingue as linhas quando o rótulo se repete (o id_filme)."""
    for alternative in check.identity[:-1]:
        if _unique(records, alternative):
            return alternative
    return ()


def display_columns(check: ResultCheck, records: Sequence[dict[str, object]]) -> tuple[str, ...]:
    """O que a resposta mostra de cada linha: o rótulo e, se ele se repete, o desambiguador.

    É a mesma regra do prompt: título + ano sempre; id_filme só quando título + ano se repetem.
    A consulta escolhida precisa trazer essas colunas, e o texto final precisa citá-las.
    """
    label = check.label
    if not label or _unique(records, label):
        return label
    return tuple(dict.fromkeys((*label, *disambiguator(check, records))))


def required_indices(check: ResultCheck, records: Sequence[dict[str, object]]) -> list[int]:
    """Linhas do gabarito que a resposta precisa trazer: todas, ou os líderes em `leaders`."""
    if check.shape is not Shape.LEADERS:
        return list(range(len(records)))
    ranks = competition_ranks([record[check.metrics[0].column] for record in records])
    return [k for k, rank in enumerate(ranks) if rank == 1]


def match_table(
    check: ResultCheck,
    expected_columns: Sequence[str],
    expected_rows: Sequence[Sequence[object]],
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    truncated: bool = False,
) -> TableMatch:
    """Confere um resultado do agente contra o gabarito (ver o docstring do módulo)."""
    table = [tuple(row) for row in rows]
    expected = [dict(zip(expected_columns, row, strict=True)) for row in expected_rows]
    if not table:
        if not expected:
            return TableMatch(True, None, "gabarito e resultado vazios")
        return TableMatch(
            False, "empty", f"o resultado veio vazio; o gabarito tem {len(expected)} linha(s)"
        )
    if not expected:
        return TableMatch(
            False, "unexpected_rows", f"o gabarito é vazio e o resultado tem {len(table)} linha(s)"
        )
    ranks = competition_ranks([row[check.metrics[0].column] for row in expected])
    display = display_columns(check, expected)
    best: TableMatch | None = None
    absent_by_alternative: list[list[str]] = []
    ambiguous: list[tuple[str, ...]] = []
    for alternative in check.identity or ((),):
        keys = [tuple(_key(e[column]) for column in alternative) for e in expected]
        if alternative and len(set(keys)) < len(keys):
            # Falha fechada: se título + ano se repetem no gabarito, linhas iguais no resultado
            # não provam que são filmes diferentes; só outra identidade (id_filme) serve.
            ambiguous.append(alternative)
            continue
        # Identificar a linha não basta: o resultado também precisa trazer o rótulo que a resposta
        # mostra (título + ano, e o id quando eles se repetem), senão o texto não tem de onde vir.
        identity = tuple(dict.fromkeys((*alternative, *display)))
        names = (*identity, *(metric.column for metric in check.metrics))
        options = [_identity_options(n, [e[n] for e in expected], columns, table) for n in identity]
        options += [
            _metric_options(m, [e[m.column] for e in expected], columns, table)
            for m in check.metrics
        ]
        absent = [name for name, found in zip(names, options, strict=True) if not found]
        if absent:
            absent_by_alternative.append(absent)
            continue
        capped = math.prod(len(found) for found in options) > MAX_MAPPINGS
        for combo in itertools.islice(itertools.product(*options), MAX_MAPPINGS):
            if len({binding.position for binding in combo}) != len(combo):
                continue
            judgement = _judge(check, combo, len(identity), expected, ranks, table)
            result = _finish(judgement, check, truncated=truncated)
            if result.matched:
                return result
            if capped:
                note = f"só as {MAX_MAPPINGS} primeiras atribuições de colunas foram tentadas"
                result = replace(result, notes=(*result.notes, note))
            if best is None or (result.matched_rows, -result.problems) > (
                best.matched_rows,
                -best.problems,
            ):
                best = result
    if best is not None:
        return best
    parts = []
    if ambiguous:
        repeated = " / ".join(" + ".join(alternative) for alternative in ambiguous)
        parts.append(f"{repeated} se repete no gabarito e não identifica as linhas")
    if absent_by_alternative:
        fewest = min(absent_by_alternative, key=len)
        parts.append("nenhuma coluna do resultado reproduz " + ", ".join(fewest) + " do gabarito")
    return TableMatch(
        False, "ambiguous_identity" if ambiguous else "missing_columns", "; ".join(parts)
    )


@dataclass(frozen=True)
class _Judgement:
    bindings: tuple[Binding, ...]
    pairs: dict[int, int]  # linha do agente -> linha do gabarito
    unmatched: dict[int, str]  # linha do agente sem par -> duplicate | wrong_values | unexpected
    ranks: list[int]
    rows: int
    expected: list[dict[str, object]]
    lead: tuple[Metric, Binding]
    agent_rows: list[tuple]


def _judge(
    check: ResultCheck,
    combo: tuple[Binding, ...],
    identity_count: int,
    expected: list[dict[str, object]],
    ranks: list[int],
    table: list[tuple],
) -> _Judgement:
    identity = combo[:identity_count]
    metrics = list(zip(check.metrics, combo[identity_count:], strict=True))
    by_identity: dict[tuple[str | None, ...], list[int]] = {}
    for k, target in enumerate(expected):
        key = tuple(_key(target[b.expected]) for b in identity)
        by_identity.setdefault(key, []).append(k)
    same = [by_identity.get(tuple(_key(row[b.position]) for b in identity), []) for row in table]

    def fits(row: tuple, target: dict[str, object]) -> bool:
        return all(_close(row[b.position], target[m.column], m, b.scale) for m, b in metrics)

    edges = [[k for k in same[i] if fits(row, expected[k])] for i, row in enumerate(table)]
    pairs = _max_matching(edges, len(expected))
    unmatched: dict[int, str] = {}
    for i in range(len(table)):
        if i in pairs:
            continue
        if edges[i]:
            unmatched[i] = "duplicate"
        elif identity and same[i]:
            unmatched[i] = "wrong_values"
        else:
            unmatched[i] = "unexpected"
    return _Judgement(combo, pairs, unmatched, ranks, len(table), expected, metrics[0], table)


_UNMATCHED = {
    "duplicate": ("duplicate_rows", "linha(s) repetida(s)"),
    "wrong_values": ("wrong_values", "linha(s) com entidade do gabarito e valor diferente"),
    "unexpected": ("unexpected_rows", "linha(s) que não estão no gabarito"),
}


def _finish(j: _Judgement, check: ResultCheck, *, truncated: bool) -> TableMatch:
    problems: list[tuple[str, str]] = []
    notes: list[str] = []
    if truncated:
        # Falha fechada: havia mais linhas do que o rastro guardou; as escondidas poderiam mudar
        # a resposta (outro líder, uma linha a mais no conjunto), mesmo que as vistas confiram.
        problems.append(
            ("truncated", "o resultado foi truncado pelo teto de linhas e não prova a resposta")
        )
    used = set(j.pairs.values())
    unmatched = dict(j.unmatched)
    leaders = [k for k, rank in enumerate(j.ranks) if rank == 1]

    if check.shape is Shape.LEADERS:
        # Linhas fora do gabarito, abaixo dos líderes, são contexto não verificável; uma linha que
        # empate com o líder (ou passe dele) seria um líder que o gabarito não tem.
        metric, binding = j.lead
        top = max(_number(j.expected[k][metric.column]) or 0.0 for k in leaders)
        unverified, tied = [], []
        for i, reason in unmatched.items():
            if reason != "unexpected":
                continue
            value = _number(j.agent_rows[i][binding.position])
            below = value is not None and value < (top - metric.tolerance) * binding.scale
            if not below:
                tied.append(i)
            elif i >= len(leaders):
                unverified.append(i)
        for i in (*unverified, *tied):
            del unmatched[i]
        if unverified:
            notes.append(
                f"{len(unverified)} linha(s) abaixo do(s) líder(es) fora do gabarito, não "
                "verificada(s)"
            )
        if tied:
            problems.append(
                (
                    "unexpected_rows",
                    f"{len(tied)} linha(s) extra(s) com valor igual ou maior que o do líder",
                )
            )

    for reason, (code, text) in _UNMATCHED.items():
        hits = sorted(i for i, r in unmatched.items() if r == reason)
        if hits:
            problems.append((code, f"{len(hits)} {text} (primeira: linha {hits[0] + 1})"))

    missing = [k for k in range(len(j.expected)) if k not in used]
    order = [j.ranks[j.pairs[i]] for i in range(j.rows) if i in j.pairs]
    if check.shape is Shape.LEADERS:
        lost = [k for k in leaders if k not in used]
        if lost:
            problems.append(
                ("missing_rows", f"falta(m) {len(lost)} de {len(leaders)} líder(es) empatado(s)")
            )
        head = [j.pairs.get(i) for i in range(min(len(leaders), j.rows))]
        if any(k is None or j.ranks[k] != 1 for k in head):
            problems.append(("wrong_order", "os líderes não estão nas primeiras linhas"))
    elif missing:
        window = f" do top {check.top_n}, com os empatados no corte" if check.top_n else ""
        problems.append(
            ("missing_rows", f"faltam {len(missing)} de {len(j.expected)} linha(s){window}")
        )
    if check.shape is not Shape.SET and order != sorted(order):
        problems.append(("wrong_order", "a ordem das linhas não segue o ranking do gabarito"))

    pairs = tuple(sorted(j.pairs.items()))
    if problems:
        return TableMatch(
            False,
            problems[0][0],
            "; ".join(text for _, text in problems),
            j.bindings,
            len(j.pairs),
            tuple(notes),
            problems=len(problems),
            pairs=pairs,
        )
    return TableMatch(
        True,
        None,
        f"{len(j.pairs)} linha(s) conferem",
        j.bindings,
        len(j.pairs),
        tuple(notes),
        pairs=pairs,
    )


# --- fidelidade do texto final ---------------------------------------------------------------

# Um número escrito: 12.390.136.500,54 · 1,234.5 · 6,34 · 28086 · -534,88. Espaço comum não é
# separador de milhar (separaria "2009 2900"); espaço fino e não separável são. Colado a uma
# letra ou dígito ("9th", "Mp3", "12,39bi") não é um valor (o grupo atômico impede ler só o
# começo dele); o sublinhado da ênfase markdown ("_65_", "__65__") não cola.
_NUMBER = re.compile(r"(?<![^\W_])(?<![.,])[-−]?(?>\d(?:[\d.,  ]*\d)?)(?![^\W\d_])")
_NEGATIVE_CURRENCY = re.compile(r"[-−](?:R\$|US\$|U\$|\$)\s*$")  # -R$ 3.581.373,25
# Multiplicador escrito depois do número: "3 mil" é 3000 (nunca 3); "R$ 12,39 bilhões".
_MULTIPLIER = re.compile(
    r"[ \t]*(mil|milh[ãa]o|milh[õo]es|mi|bilh[ãa]o|bilh[õo]es|bi|trilh[ãa]o|trilh[õo]es|tri)"
    r"(?![^\W\d_])",
    re.IGNORECASE,
)
# Números de posição: "9. Die Hart", "2) Titanic", "**3.** Dune", "8º lugar", "#9" e as células
# das colunas de posição de tabelas. Nunca valem como métrica, ano ou id; nos rankings, são as
# posições escritas, conferidas contra o gabarito. "top 10" não é valor nem posição de uma linha.
_POSITIONS = (
    re.compile(r"(?m)^[ \t]*(?:[-*+>][ \t]+)?[*_]{0,2}(\d{1,3})[*_]{0,2}[.)](?=\s|[*_])"),
    re.compile(  # "o 1º filme a...", "1º de janeiro", "a 2ª vez", "2º semestre" não são posições
        r"(?<![^\W_])(?<![.,])(\d{1,3})[ \t]?[ºª°]"
        r"(?![ \t]+(?:filmes?|vez(?:es)?|semestre|trimestre|dia|de|do|da|ano|m[eê]s|epis[óo]dio"
        r"|temporada|edi[çc][ãa]o|parte)(?![^\W_]))",
        re.IGNORECASE,
    ),
    re.compile(r"#[ \t]?(\d{1,3})(?!\d)"),
)
_CELL_ITEM = re.compile(r"[*_]{0,2}(\d{1,3})[*_]{0,2}[.)](?=\s)")  # "| 3. Dune |"
# Posições por extenso, só em formas de ranking (no texto dobrado): "em segundo", "primeiro
# lugar", "terceiro colocado", "na quarta posição". "Segundo o IMDb" não é posição.
_ORDINAL_WORDS = {
    "primeir": 1, "segund": 2, "terceir": 3, "quart": 4, "quint": 5,
    "sext": 6, "setim": 7, "oitav": 8, "non": 9, "decim": 10,
}  # fmt: skip
_STEMS = "|".join(_ORDINAL_WORDS)
_WORD_POSITIONS = (
    re.compile(rf"(?<!\w)em\s+({_STEMS})[oa]s?(?!\w)"),
    re.compile(rf"(?<!\w)({_STEMS})[oa]s?\s+(?:lugar|colocad[oa]s?)(?!\w)"),
    re.compile(rf"(?<!\w)na\s+({_STEMS})as?\s+(?:posicao|colocacao)(?!\w)"),
)
_TOP_N = re.compile(r"(?<!\w)[Tt][Oo][Pp][ \t]+(\d{1,3})(?!\d)")
# Rótulo de grandeza escrito junto do número. Um número rotulado como outra grandeza não vale
# como a métrica ("65 anos" não conta filmes, "9%" só vale numa razão, "R$" só em dinheiro,
# "id 65" nunca); sem rótulo, ele vale pelo contexto da pergunta. "6,7/10" é uma nota, e o 10
# da escala não é um valor.
_OUT_OF = re.compile(r"[ \t]*/[ \t]*(10|5|100)(?![\d.,]*\d|/)")
_UNITS = (
    ("percent", re.compile(r"[ \t]?(?:%|por[ \t]+cento(?![^\W_]))", re.IGNORECASE)),
    ("rating", _OUT_OF),
    (
        "duration",
        re.compile(r"[ \t]+(?:anos?|m[eê]s|meses|dias?|horas?|minutos?)(?![^\W_])", re.IGNORECASE),
    ),
    (
        "count",
        re.compile(
            r"[ \t]+(?:filmes?|avalia[çc](?:[õo]es|[ãa]o)|votos?|participa[çc](?:[õo]es|[ãa]o))"
            r"(?![^\W_])",
            re.IGNORECASE,
        ),
    ),
)
_EMPHASIS = re.compile(r"[*_]*")
# Desigualdade escrita logo antes do número (no texto dobrado, sem a moeda nem a ênfase do fim).
_BOUND_WORDS = re.compile(
    r"(?<!\w)(?:(?:mais|menos)\s+(?:de|do\s+que|que)|(?:acima|abaixo|alem)\s+de|(?:pelo|ao)\s+menos"
    r"|no\s+(?:minimo|maximo)|(?:superior|inferior)\s+a|a\s+partir\s+de|ate|quase)\s*$"
)
_BOUND_SIGN = re.compile(r"[<>≤≥][\s*_]*(?:R\$|US\$|U\$|\$)?[\s*_]*[-−]?$")
_CURRENCY_TAIL = re.compile(r"(?:R\$|US\$|U\$|\$|[*_\s])+$")
# Rótulo logo antes do número, na mesma linha e célula: "nota 9", "nota IMDb de 6,7", "nota
# média: 6,34". Não atravessa barra de tabela nem quebra de linha (o cabeçalho "Nota" de uma
# coluna não rotula a célula de outra).
_RATING_BEFORE = re.compile(
    r"(?<!\w)nota(?:[ \t]+(?:m[ée]dia|imdb|tmdb|dos?|usu[áa]rios?))*[ \t]*(?:[:=]|de)?[ \t]*$",
    re.IGNORECASE,
)
# Tabelas markdown (GFM): cabeçalho seguido de uma linha separadora com o mesmo número de
# células, com ou sem as barras das bordas. Cabeçalhos dobrados: posição na lista ou id do
# filme ("#" dobra para ""). "N"/"Nº" também pode ser "número de": só é posição na 1ª coluna.
_PIPE = re.compile(r"(?<!\\)\|")
_RULE_CELL = re.compile(r":?-+:?")
_POSITION_CELL = re.compile(r"[*_]{0,2}#?[ \t]*(\d{1,3})[ \t]*[ºª°.]?[*_]{0,2}")
_RANK_HEADERS = frozenset(
    {"", "posicao", "pos", "rank", "ranking", "ordem", "colocacao", "lugar", "top"}
)
_FIRST_COLUMN_RANK_HEADERS = frozenset({"n", "no", "n o"})
_ID_HEADERS = frozenset({"id", "id filme", "id do filme"})
_YEAR_HEADERS = frozenset({"ano", "ano de lancamento", "ano lancamento", "lancamento", "year"})
# O id só vale escrito como id: "id 1391481", "id_filme: 1391481", "ID do filme 1391481" (no texto
# dobrado a pontuação vira espaço) ou numa coluna de id. Um número solto igual ao id não conta.
_ID_MARKER = r"(?<!\w)id(?:\s+(?:do\s+)?filme)?\s+"
_RESPECTIVELY = re.compile(r"(?<!\w)respec?tivamente(?!\w)")
_SEGMENT_BREAK = re.compile(r"[\n;]|[.!?]\s+")
_CLAUSE_BREAK = re.compile(r"[\n;]")
# Distribuição explícita a um grupo enumerado: "cada" para um valor, "ambos"/"ambas" (par) e
# "todos"/"todas" para um rótulo.
_EACH = re.compile(r"(?<!\w)cada(?!\w)")
_SHARED_LABEL = re.compile(r"(?<!\w)(ambos|ambas|todos|todas)(?!\w)")
_JOINERS = frozenset({"e", "de", "do", "id", "filme"})  # entre os itens de "A (2009) e B"
_NOT_UNITS = frozenset({"a", "de", "do", "da", "em", "para", "por"})  # "3 filmes para cada..."

# Moeda: basta indicá-la uma vez (cabeçalho, texto ou premissas); um número rotulado com a outra
# moeda ao lado ("US$ 2.900,00" numa métrica em reais) não vale como a métrica.
_CURRENCY_MENTION = {
    "BRL": re.compile(r"R\$|\bBRL\b|\breais\b|\breal\s+brasileiro\b", re.IGNORECASE),
    "USD": re.compile(r"US\$|U\$|\bUSD\b|\bd[óo]lar(?:es)?\b|(?<![A-Za-z])\$", re.IGNORECASE),
}
_CURRENCY_BEFORE = {
    "BRL": re.compile(r"(?:R\$|\bBRL)[\s*_]*[-−]?[\s*_]*$", re.IGNORECASE),
    "USD": re.compile(r"(?:US\$|U\$|\bUSD|(?<![A-Za-z])\$)[\s*_]*[-−]?[\s*_]*$", re.IGNORECASE),
}
_CURRENCY_AFTER = {
    "BRL": re.compile(r"[ \t]*(?:de[ \t]+)?(?:reais|real|BRL)(?![^\W_])", re.IGNORECASE),
    "USD": re.compile(r"[ \t]*(?:de[ \t]+)?(?:d[óo]lar(?:es)?|USD)(?![^\W_])", re.IGNORECASE),
}
_CURRENCY_NAMES = {"BRL": "reais (R$, BRL ou reais)", "USD": "dólares (US$, USD ou dólares)"}
# Nomes de gênero na resposta: os aliases do agente (Guerra = War) e os nomes usuais em português
# que ele não usa para buscar, mas que um texto para leigos usa.
_ANSWER_GENRES = {
    **_GENRE_ALIASES,
    "Policial": "Crime",
    "Musical": "Music",
    "Telefilme": "Tv Movie",
    "Filme para TV": "Tv Movie",
    "Filme para televisão": "Tv Movie",
    "Filme de televisão": "Tv Movie",
}

MAX_OPTIONS = 64  # combinações de trechos por âncora de uma linha
MAX_ASSIGN_STEPS = 200_000  # passos da busca que atribui trechos exclusivos às linhas

Span = tuple[int, int]
Token = tuple[int, ...]  # um trecho (início, fim) ou um trecho distribuído (início, fim, grupo)
Option = tuple[Token, ...]  # trechos que provam uma linha: âncora, rótulo e métrica
Shared = dict[tuple[int, Span], list[Token]]  # (linha, âncora) -> trechos distribuídos
SharedLabels = dict[tuple[int, Span], dict[str, list[Token]]]


@dataclass(frozen=True)
class _Written:
    span: Span
    readings: tuple[float, ...]
    unit: str | None  # None (sem rótulo), id, BRL, USD, percent, rating, duration ou count
    # Moeda que rotula o número sem rótulo próprio: a única citada na frase dele ou, se a frase não
    # cita nenhuma, no título do bloco ("Receita em USD:" sobre uma lista). None = nenhuma ou duas.
    scope: str | None = None
    # Limite, não o valor: "mais de 3", "pelo menos 3", "até 3", "quase 3", "> 3". Uma linha com
    # ele ainda é uma linha de dados, mas o número nunca prova a métrica (3 não é "mais de 3").
    bound: bool = False


def _grouped(text: str, separator: str) -> bool:
    head, *groups = text.split(separator)
    return 1 <= len(head) <= 3 and bool(groups) and all(len(group) == 3 for group in groups)


def _readings(body: str) -> tuple[float, ...]:
    """Leituras possíveis de um número sem sinal: pt-BR e en (12.390 = 12390 ou 12,39)."""
    body = body.replace(" ", "").replace(" ", "")
    found: set[float] = set()
    if "." in body and "," in body:
        decimal = "," if body.rfind(",") > body.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        integer, _, fraction = body.rpartition(decimal)
        if decimal not in integer and _grouped(integer, thousands):
            found.add(float(integer.replace(thousands, "") + "." + fraction))
    elif "." in body or "," in body:
        separator = "," if "," in body else "."
        if body.count(separator) == 1:
            found.add(float(body.replace(separator, ".")))
        if _grouped(body, separator):
            found.add(float(body.replace(separator, "")))
    else:
        found.add(float(body))
    return tuple(found)


def _factor(word: str) -> float:
    word = _fold(word).strip()
    return 1e3 if word == "mil" else {"mi": 1e6, "bi": 1e9, "tr": 1e12}[word[:2]]


def _fold(text: str) -> str:
    """Mesmo tamanho do texto: minúsculas sem acento; o que não é letra ou dígito vira espaço."""
    folded = []
    for char in text:
        base = unicodedata.normalize("NFKD", char)[:1].lower()[:1]
        folded.append(base if base.isalnum() else " ")
    return "".join(folded)


def _overlaps(a: Token, b: Token) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _conflict(a: Token, b: Token) -> bool:
    """Um trecho escrito serve a uma linha só; a exceção é o mesmo trecho distribuído
    explicitamente ("3 filmes cada", "ambos de 2022") entre os membros do grupo dele."""
    if len(a) == 3 and len(b) == 3 and a[2] == b[2]:
        return False
    return _overlaps(a, b)


def _cells(text: str, start: int, end: int) -> list[Span] | None:
    """Células de uma linha de tabela (o conteúdo, sem os espaços em volta), com ou sem as barras
    das bordas; None se a linha não tem barra."""
    bars = [match.start() for match in _PIPE.finditer(text, start, end)]
    if not bars:
        return None
    pieces = list(zip([start, *(bar + 1 for bar in bars)], [*bars, end], strict=True))
    if not text[pieces[0][0] : pieces[0][1]].strip():
        pieces = pieces[1:]  # barra na borda esquerda
    if pieces and not text[pieces[-1][0] : pieces[-1][1]].strip():
        pieces = pieces[:-1]  # barra na borda direita
    cells = []
    for low, high in pieces:
        content = text[low:high]
        left = len(content) - len(content.lstrip())
        cells.append((low + left, low + max(left, len(content.rstrip()))))
    return cells


_COUNT_HEADER = frozenset({"filmes", "avaliacoes", "votos", "participacoes", "quantidade", "qtd"})
_SIGNS = re.compile(r"R\$|US\$|U\$|\bBRL\b|\bUSD\b|%|[-−]", re.IGNORECASE)  # o resto de "R$ 9"


def _header_unit(raw: str) -> str | None:
    """A grandeza que o cabeçalho de uma coluna dá às células dela, como um rótulo escrito ao lado
    do número na prosa: "Receita (US$)", "Margem (%)", "Avaliações", "Nota IMDb", "Idade"."""
    for currency in ("BRL", "USD"):
        if _CURRENCY_MENTION[currency].search(raw):
            return currency
    if "%" in raw:
        return "percent"
    words = set(_fold(raw).split())
    if words & _COUNT_HEADER:
        return "count"  # "Filmes com nota" conta filmes
    if words & {"nota", "notas"} or ("media" in words and words & {"imdb", "tmdb", "usuarios"}):
        return "rating"
    if words & {"idade", "anos", "duracao", "minutos"}:
        return "duration"
    return None


def _lines(text: str) -> list[Span]:
    """Cada linha do texto, sem a quebra."""
    lines: list[Span] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((offset, offset + len(line.rstrip("\r\n"))))
        offset += len(line)
    return lines


@dataclass(frozen=True)
class _TableScan:
    positions: list[tuple[Span, int]]  # células de posição, com o número
    ids: list[Span]  # células de colunas de id
    rows: list[Span]  # linhas de dados
    units: list[tuple[Span, str]]  # células cuja coluna tem uma grandeza no cabeçalho
    frames: list[Span]  # cabeçalhos e linhas separadoras
    cells: list[tuple[Span, tuple[Span, ...], int]]  # (linha de dados, células, nº da tabela)
    years: list[Span]  # células de colunas de ano (rótulo, nunca o valor da linha)


def _tables(text: str) -> _TableScan:
    """Tabelas markdown: as células de posição (com o número), as de id, cada linha de dados e as
    células cuja coluna tem uma grandeza no cabeçalho (`_header_unit`).

    Uma tabela é um cabeçalho seguido de uma linha separadora (`---`, `:---`, `---:`, `:---:`)
    com o mesmo número de células; as linhas de dados seguem até uma linha vazia ou sem barra.
    Prosa com barras não é tabela.
    """
    lines = _lines(text)
    positions: list[tuple[Span, int]] = []
    ids: list[Span] = []
    rows: list[Span] = []
    units: list[tuple[Span, str]] = []
    frames: list[Span] = []
    row_cells: list[tuple[Span, tuple[Span, ...], int]] = []
    years: list[Span] = []
    k = 0
    while k + 1 < len(lines):
        header, rule = _cells(text, *lines[k]), _cells(text, *lines[k + 1])
        if (
            not header
            or not rule
            or len(header) != len(rule)
            or not all(_RULE_CELL.fullmatch(text[start:end]) for start, end in rule)
        ):
            k += 1
            continue
        names = [" ".join(_fold(text[start:end]).split()) for start, end in header]
        kinds = [_header_unit(text[start:end]) for start, end in header]
        frames += [lines[k], lines[k + 1]]
        table = len(frames) // 2 - 1
        k += 2
        while k < len(lines) and text[slice(*lines[k])].strip():
            cells = _cells(text, *lines[k])
            if cells is None:
                break
            rows.append(lines[k])
            row_cells.append((lines[k], tuple(cells), table))
            for column, (name, kind, cell) in enumerate(zip(names, kinds, cells, strict=False)):
                if name in _RANK_HEADERS or (column == 0 and name in _FIRST_COLUMN_RANK_HEADERS):
                    if found := _POSITION_CELL.fullmatch(text[slice(*cell)]):
                        positions.append((cell, int(found.group(1))))
                        continue
                elif name in _ID_HEADERS:
                    ids.append(cell)
                    continue
                elif name in _YEAR_HEADERS:
                    years.append(cell)
                if item := _CELL_ITEM.match(text, cell[0], cell[1]):  # posição no começo da célula
                    positions.append((item.span(1), int(item.group(1))))
                if kind is not None:
                    units.append((cell, kind))
            k += 1
    return _TableScan(positions, ids, rows, units, frames, row_cells, years)


def _clauses(text: str) -> list[Span]:
    """Trechos entre quebras de linha e pontos e vírgulas (uma linha de lista ou de tabela)."""
    spans, start = [], 0
    for match in _CLAUSE_BREAK.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _unit(text: str, span: Span, tail: int, ids: Sequence[Span]) -> str | None:
    """O rótulo de grandeza escrito junto do número (`tail`: depois do multiplicador). O rótulo
    colado depois ("9 avaliações") vale mais que o "nota" escrito antes."""
    if any(_overlaps(span, other) for other in ids):
        return "id"
    before = text[max(0, span[0] - 10) : span[0]]
    tail = _EMPHASIS.match(text, tail).end()
    for currency in ("BRL", "USD"):
        if _CURRENCY_BEFORE[currency].search(before) or _CURRENCY_AFTER[currency].match(text, tail):
            return currency
    after = next((unit for unit, pattern in _UNITS if pattern.match(text, tail)), None)
    if after is None and _RATING_BEFORE.search(text, max(0, span[0] - 40), span[0]):
        return "rating"
    return after


def _lazy_markers(text: str, items: Sequence[tuple[Span, int]]) -> set[Span]:
    """Os "1." de uma lista ordenada markdown em que TODOS os itens usam 1: numeração
    preguiçosa, que o markdown renderiza como 1, 2, 3. São marcadores de lista, não posições: vale
    a ordem do texto. Itens da mesma lista: entre um e outro, só linhas vazias ou recuadas (a
    continuação do item). "1., 2., 3." e "1º", "#1" ou colunas de posição continuam posições."""
    lines = _lines(text)
    starts = [start for start, _ in lines]
    lazy: set[Span] = set()
    run: list[tuple[Span, int]] = []
    previous = -1

    def close() -> None:
        if len(run) >= 2 and all(number == 1 for _, number in run):
            lazy.update(span for span, _ in run)

    for item in sorted(items):
        line = bisect.bisect_right(starts, item[0][0]) - 1
        between = range(previous + 1, line)
        if not run or not all(
            not text[slice(*lines[j])].strip() or text[lines[j][0]] in " \t" for j in between
        ):
            close()
            run = []
        run.append(item)
        previous = line
    close()
    return lazy


_SENTENCE_BREAK = re.compile(r"[\n;]|[.!?](?=\s)")


def _currencies(text: str, start: int, end: int) -> set[str]:
    return {c for c, pattern in _CURRENCY_MENTION.items() if pattern.search(text[start:end])}


def _only(found: set[str]) -> str | None:
    return next(iter(found)) if len(found) == 1 else None


def _stated_currency(text: str, sentence: Span, number: Span) -> set[str]:
    """As moedas citadas na frase do número, sem os apartes entre parênteses ou colchetes que não
    o contêm ("Valores em USD (a pergunta pediu reais): ..." rotula em dólares)."""
    start, end = sentence
    chunk = text[start:end]
    for match in reversed(list(_GROUP.finditer(chunk))):
        if not (start + match.start() <= number[0] < start + match.end()):
            chunk = chunk[: match.start()] + " " * len(match.group()) + chunk[match.end() :]
    return _currencies(chunk, 0, len(chunk))


def _currency_scopes(text: str) -> tuple[list[Span], list[tuple[Span, str]]]:
    """As frases do texto e a moeda do título de cada linha.

    Título: uma linha que termina em ":" ou um cabeçalho markdown ("## ..."). A moeda dele vale
    para as linhas seguintes do bloco, até uma linha vazia depois de algum conteúdo; um título sem
    moeda não apaga a de um título anterior do mesmo bloco.
    """
    sentences, start = [], 0
    for match in _SENTENCE_BREAK.finditer(text):
        sentences.append((start, match.start()))
        start = match.end()
    sentences.append((start, len(text)))
    headings: list[tuple[Span, str]] = []
    current: str | None = None
    content = False
    for line in _lines(text):
        body = text[slice(*line)].strip().strip("*_ ")
        if not body:
            if content:
                current, content = None, False
            continue
        if body.endswith(":") or body.startswith("#"):
            current = _only(_currencies(text, *line)) or current
            continue
        content = True
        if current is not None:
            headings.append((line, current))
    return sentences, headings


@dataclass(frozen=True)
class _Answer:
    """O texto final pronto para a conferência: dobrado, números lidos, posições e tabelas."""

    text: str
    folded: str
    numbers: tuple[_Written, ...]  # sem os números de posição e as escalas de nota
    id_cells: tuple[Span, ...]  # células de colunas de id em tabelas
    positions: tuple[tuple[Span, int], ...]  # posições escritas: "3.", "3º", "#3", célula "3"
    table_rows: tuple[Span, ...]  # linhas de dados de tabelas
    table_frames: tuple[Span, ...] = ()  # cabeçalhos e linhas separadoras de tabelas
    table_cells: tuple[tuple[Span, tuple[Span, ...], int], ...] = ()  # (linha, células, tabela)
    position_cells: tuple[Span, ...] = ()  # células de colunas de posição
    year_cells: tuple[Span, ...] = ()  # células de colunas de ano

    @classmethod
    def read(cls, text: str) -> _Answer:
        folded = _fold(text)
        scan = _tables(text)
        id_cells, table_rows, column_units = scan.ids, scan.rows, scan.units
        items = [(match.span(1), int(match.group(1))) for match in _POSITIONS[0].finditer(text)]
        lazy = _lazy_markers(text, items)
        positions = [item for item in items if item[0] not in lazy]
        positions += [
            (match.span(1), int(match.group(1)))
            for pattern in _POSITIONS[1:]
            for match in pattern.finditer(text)
        ]
        positions += scan.positions
        words = {  # "em primeiro lugar" casa com duas formas: uma posição só
            match.span(1): _ORDINAL_WORDS[match.group(1)]
            for pattern in _WORD_POSITIONS
            for match in pattern.finditer(folded)
        }
        labels_only = list(words.items())  # posição escrita, mas nenhum número a bloquear
        found = list(_NUMBER.finditer(text))
        scales = [  # o 10 de "6,7/10" (ou de "**6,7**/10")
            scale.span(1)
            for match in found
            if (scale := _OUT_OF.match(text, _EMPHASIS.match(text, match.end()).end()))
        ]
        blocked = [span for span, _ in positions] + sorted(lazy)  # um "1." preguiçoso não é valor
        blocked += [match.span(1) for match in _TOP_N.finditer(text)] + scales
        ids = [m.span(1) for m in re.finditer(_ID_MARKER + r"(\d+)(?!\w)", folded)] + id_cells
        sentences, headings = _currency_scopes(text)
        numbers = []
        for match in found:
            span = match.span()
            if any(_overlaps(span, other) for other in blocked):
                continue
            raw = match.group()
            before = text[max(0, span[0] - 8) : span[0]]
            negative = raw[0] in "-−" or bool(_NEGATIVE_CURRENCY.search(before))
            tail, factor = span[1], 1.0
            if multiplier := _MULTIPLIER.match(text, tail):
                tail, factor = multiplier.end(), _factor(multiplier.group(1))
            readings = tuple(
                (-value if negative else value) * factor for value in _readings(raw.lstrip("-−"))
            )
            unit = _unit(text, span, tail, ids)
            if unit is None:  # célula só com o número: vale a grandeza do cabeçalho da coluna
                unit = next(
                    (
                        kind
                        for cell, kind in column_units
                        if cell[0] <= span[0] and span[1] <= cell[1]
                        and not _SIGNS.sub("", text[cell[0] : span[0]] + text[span[1] : cell[1]])
                        .strip(" \t*_")
                    ),
                    None,
                )  # fmt: skip
            scope = None
            if unit is None:
                sentence = next(where for where in sentences if where[0] <= span[0] <= where[1])
                stated = _stated_currency(text, sentence, span)
                scope = _only(stated) if stated else next(
                    (c for line, c in headings if line[0] <= span[0] <= line[1]), None
                )  # fmt: skip
            lead = text[max(0, span[0] - 30) : span[0]]
            bound = bool(_BOUND_SIGN.search(lead)) or bool(
                _BOUND_WORDS.search(_fold(_CURRENCY_TAIL.sub("", lead)))
            )
            numbers.append(_Written(span, readings, unit, scope, bound))
        positions = sorted([*positions, *labels_only])
        return cls(
            text,
            folded,
            tuple(numbers),
            tuple(id_cells),
            tuple(positions),
            tuple(table_rows),
            tuple(scan.frames),
            tuple(scan.cells),
            tuple(span for span, _ in scan.positions),
            tuple(scan.years),
        )


def _name_spans(folded: str, name: str) -> list[Span]:
    tokens = _fold(name).split()
    if not tokens:
        return []
    pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, tokens)) + r"(?!\w)"
    return [(m.start(), m.end()) for m in re.finditer(pattern, folded)]


def _id_spans(answer: _Answer, value: object) -> list[Span]:
    tokens = _fold(str(value)).split()
    if not tokens:
        return []
    body = r"\s+".join(map(re.escape, tokens))
    inline = [m.span(1) for m in re.finditer(_ID_MARKER + rf"({body})(?!\w)", answer.folded)]
    cells = [
        span
        for span in _name_spans(answer.folded, str(value))
        if any(start <= span[0] and span[1] <= end for start, end in answer.id_cells)
    ]
    return sorted({*inline, *cells})


def _value_spans(column: str, value: object, answer: _Answer) -> list[Span]:
    """Onde o texto cita o valor de uma coluna do rótulo (nome, ano, id)."""
    if column == "id" or column.startswith("id_"):
        return _id_spans(answer, value)
    if isinstance(value, str):
        names = {value}
        if column == "genero":  # o modelo pode citar o gênero em português (Guerra = War)
            names |= {alias for alias, target in _ANSWER_GENRES.items() if target == value}
        return sorted({span for name in names for span in _name_spans(answer.folded, name)})
    number = _number(value)
    if number is None:
        return []
    # um ano é um número sem rótulo: "2022 filmes", "R$ 2022" ou "id 2022" não são o ano
    return [n.span for n in answer.numbers if n.unit is None and number in n.readings]


def _scales(metric: Metric, unit: str | None, scope: str | None = None) -> tuple[float, ...]:
    """Escalas em que um número com este rótulo pode valer a métrica; () se é outra grandeza.

    Um número sem rótulo próprio, numa frase ou sob um título que cita só a outra moeda ("Receita
    em USD: Avatar (2009): 2.900,00"), está rotulado com ela: não vale como dinheiro da métrica,
    diga o que disserem as premissas.
    """
    kind = metric.kind
    if unit is None:
        if metric.currency and scope is not None and scope != metric.currency:
            return ()
        return metric.scales
    if unit == "count":
        return metric.scales if kind is Quantity.COUNT else ()
    if unit in ("BRL", "USD"):
        return metric.scales if unit == metric.currency else ()
    if unit == "rating":
        return metric.scales if kind is Quantity.RATING else ()
    if unit == "percent":
        return (100.0,) if kind is Quantity.RATIO else ()  # "0,25%" é 0,0025, nunca 0,25
    return ()  # id e duração nunca são métrica


def _metric_spans(metric: Metric, value: object, answer: _Answer) -> list[Span]:
    return [
        n.span
        for n in answer.numbers
        if not n.bound
        and any(_close(reading, value, metric, scale) for reading in n.readings
                for scale in _scales(metric, n.unit, n.scope))
    ]  # fmt: skip


def _currency_problem(metric: Metric, text: str) -> str | None:
    if metric.currency and not _CURRENCY_MENTION[metric.currency].search(text):
        return f"moeda: a resposta não indica {_CURRENCY_NAMES[metric.currency]}"
    return None


def _combos(choices: list[list[Token]], taken: Option) -> Iterator[Option]:
    """Cada escolha de um trecho por lista, sem sobreposição entre eles nem com `taken`."""
    if not choices:
        yield taken
        return
    for span in choices[0]:
        if not any(_conflict(span, other) for other in taken):
            yield from _combos(choices[1:], (*taken, span))


def _segments(text: str, anchors: Sequence[Span]) -> list[Span]:
    """Frases do "respectivamente": cortes em quebras de linha, pontos e vírgulas e fins de frase
    (ponto seguido de espaço), menos dentro de uma citação ("The Super Mario Bros. Movie") ou
    antes de minúscula ("Robert Downey Jr. e Chris Evans")."""
    spans, start = [], 0
    for match in _SEGMENT_BREAK.finditer(text):
        if text[match.start()] in ".!?" and (
            text[match.end() : match.end() + 1].islower()
            or any(a[0] <= match.start() < a[1] for a in anchors)
        ):
            continue
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _without_nested(spans: Sequence[Span]) -> list[Span]:
    """Uma citação dentro de outra maior ("Avatar" em "Avatar: The Way Of Water") não conta."""
    unique = sorted(set(spans))
    return [s for s in unique if not any(o != s and o[0] <= s[0] and s[1] <= o[1] for o in unique)]


def _words(folded: str, gap: Span, blank: Sequence[Span] = ()) -> list[str]:
    """Palavras de um trecho do texto dobrado, apagados os trechos de `blank` contidos nele."""
    chars = list(folded[gap[0] : gap[1]])
    for start, end in blank:
        if gap[0] <= start and end <= gap[1]:
            chars[start - gap[0] : end - gap[0]] = " " * (end - start)
    return "".join(chars).split()


def _few_words(words: list[str], limit: int) -> bool:
    return len(words) <= limit and all(word.isalpha() for word in words)


def _each_value(
    answer: _Answer, key: re.Match[str], clause: Span, anchors: list[Span]
) -> tuple[Span, Span] | None:
    """O número de "3 filmes cada" ou de "cada um com 3", e o trecho da expressão inteira."""
    hits = []
    for written in answer.numbers:
        span = written.span
        if not (clause[0] <= span[0] and span[1] <= clause[1]) or any(
            _overlaps(span, anchor) for anchor in anchors
        ):
            continue
        if span[1] <= key.start():
            words = _words(answer.folded, (span[1], key.start()))
            if _few_words(words, 2) and not set(words) & _NOT_UNITS:
                hits.append((span, (span[0], key.end())))
        elif span[0] >= key.end():
            words = _words(answer.folded, (key.end(), span[0]))
            if words[:1] in (["um"], ["uma"]) and _few_words(words, 4):
                hits.append((span, (key.start(), span[1])))
    return hits[0] if len(hits) == 1 else None


_SHARED_VERBS = frozenset({"com", "tem", "possuem", "possui"})  # "todos com 3", "ambos têm 9"


def _shared_value(
    answer: _Answer, key: re.Match[str], clause: Span, anchors: list[Span]
) -> tuple[Span, Span] | None:
    """O número de "todos com 3 filmes" ou "ambos têm 9 avaliações" (logo depois da palavra, com
    "com", "tem" ou "possuem" na frente) e o trecho da expressão inteira."""
    for written in answer.numbers:
        span = written.span
        if span[0] < key.end() or span[1] > clause[1]:
            continue
        if any(_overlaps(span, anchor) for anchor in anchors):
            return None
        words = _words(answer.folded, (key.end(), span[0]))
        words = words[1:] if words[:1] in (["eles"], ["elas"]) else words
        if words[:1] and words[0] in _SHARED_VERBS and _few_words(words, 3):
            return span, (key.start(), span[1])
        return None
    return None


def _next_to(
    folded: str, runs: list[list[Span]], phrase: Span, labels: list[Span], limit: int
) -> list[Span] | None:
    """A enumeração (de 2 ou mais) colada a uma expressão: a de logo antes dela, que é o sujeito
    ("A e B têm 3 filmes cada"), ou, se não houver, a de logo depois ("Com 3 filmes cada, A e
    B..."). Entre as duas, só poucas palavras e nenhum número além dos rótulos das linhas."""
    before = [run for run in runs if run[-1][1] <= phrase[0]]
    if (
        before
        and len(before[-1]) >= 2
        and _few_words(_words(folded, (before[-1][-1][1], phrase[0]), labels), limit)
    ):
        return before[-1]
    after = [run for run in runs if run[0][0] >= phrase[1]]
    if (
        after
        and len(after[0]) >= 2
        and _few_words(_words(folded, (phrase[1], after[0][0][0]), labels), limit)
    ):
        return after[0]
    return None


def _shared(
    answer: _Answer,
    anchors: list[Span],
    owners: dict[Span, list[int]],
    values: list[list[Span]],
    found: list[dict[str, list[Span]]],
) -> tuple[Shared, SharedLabels]:
    """Trechos distribuídos explicitamente a um grupo enumerado de linhas, por (linha, âncora).

    - "cada": "Drama e Comédia têm 3 filmes cada" dá o 3 a cada uma, se for o valor dela;
    - "ambos"/"ambas" (só um par) e "todos"/"todas": "Avatar e Top Gun, ambos de 2022" dá o
      ano 2022 a cada um cujo ano é 2022, e só esse rótulo; "Drama e Comédia, todos com 3
      filmes" ou "A e B, ambos com 9 avaliações" dão o valor escrito logo depois a cada membro
      cujo valor é aquele (um membro com outro valor fica sem ele).
    O grupo é uma enumeração explícita ("A e B", "A, B e C"), colada à expressão e no mesmo
    trecho (linha ou ponto e vírgula). Sem essas palavras, nada é distribuído: "A e B têm 3
    filmes" não prova que cada um tem 3.
    """
    folded = answer.folded
    labels = sorted({span for row in found for spans in row.values() for span in spans})
    each: Shared = {}
    both: SharedLabels = {}
    group = 0
    for start, end in _clauses(answer.text):
        inner = [anchor for anchor in anchors if start <= anchor[0] and anchor[1] <= end]
        if len(inner) < 2:
            continue
        runs = [[inner[0]]]
        for left, right in itertools.pairwise(inner):
            if set(_words(folded, (left[1], right[0]), labels)) <= _JOINERS:
                runs[-1].append(right)
            else:
                runs.append([right])
        for key in _EACH.finditer(folded, start, end):
            hit = _each_value(answer, key, (start, end), inner)
            run = None if hit is None else _next_to(folded, runs, hit[1], labels, 4)
            if run is None:
                continue
            group += 1
            for anchor in run:
                for i in owners.get(anchor, ()):
                    if hit[0] in values[i]:
                        each.setdefault((i, anchor), []).append((*hit[0], group))
        for key in _SHARED_LABEL.finditer(folded, start, end):
            pair = key.group(1) in ("ambos", "ambas")
            value = next(
                (
                    span
                    for span in labels
                    if key.end() <= span[0]
                    and span[1] <= end
                    and not any(_overlaps(span, anchor) for anchor in inner)
                    and _few_words(_words(folded, (key.end(), span[0])), 3)
                ),
                None,
            )
            run = None if value is None else _next_to(
                folded, runs, (key.start(), value[1]), labels, 3
            )  # fmt: skip
            if value is not None and run is not None and not (pair and len(run) != 2):
                group += 1
                for anchor in run:
                    for i in owners.get(anchor, ()):
                        for column, spans in found[i].items():
                            if value in spans:
                                both.setdefault((i, anchor), {}).setdefault(column, []).append(
                                    (*value, group)
                                )
            hit = _shared_value(answer, key, (start, end), inner)
            run = None if hit is None else _next_to(folded, runs, hit[1], labels, 3)
            if run is None or (pair and len(run) != 2):
                continue
            group += 1
            for anchor in run:
                for i in owners.get(anchor, ()):
                    if hit[0] in values[i]:
                        each.setdefault((i, anchor), []).append((*hit[0], group))
    return each, both


@dataclass(frozen=True)
class _Rows:
    """O que o texto diz das linhas exigidas: as provas de cada uma, a posição escrita junto de
    cada âncora, as linhas de cada âncora e todos os trechos de rótulo (título, nome, ano, id)."""

    options: list[list[Option]]
    written: dict[Span, int]
    owners: dict[Span, list[int]]
    known: list[Span]
    named: frozenset[Span] = frozenset()  # âncoras que são o nome (título, pessoa, gênero...)


def _row_options(check: ResultCheck, rows: list[dict[str, object]], answer: _Answer) -> _Rows:
    """Para cada linha exigida, os conjuntos de trechos do texto que a provam; e a posição
    escrita junto de cada âncora (`_written_positions`).

    1. Normal: âncora = o primeiro valor do rótulo (título, nome, ano). Na região dela (do fim da
       âncora anterior ao início da seguinte; numa tabela, só a linha da própria célula) estão os
       outros valores do rótulo, o desambiguador quando o rótulo se repete (o id_filme) e a
       métrica.
    2. Compacta, só para linhas cujo primeiro valor do rótulo se repete entre as exigidas e
       aparece no texto ("Há dois filmes Elemental: o de 2022 tem 6,7 e o de 2023, 7,0"): a âncora
       passa a ser o que distingue a linha (o ano, ou o id quando o ano também se repete), com a
       métrica na região dela.
    3. "respectivamente": numa frase com essa palavra, a k-ésima âncora corresponde ao k-ésimo
       valor da lista ("A e B têm X e Y, respectivamente"). Sem a palavra, nada é pareado por
       posição.
    4. Distribuição explícita (`_shared`): "cada" dá um valor, e "ambos"/"todos" um rótulo, a
       todos os membros de um grupo enumerado; esses trechos valem em qualquer das formas acima.
    """
    metric = check.metrics[0]
    label = check.label
    extra = disambiguator(check, rows)
    keys = [tuple(_key(row[column]) for column in label) for row in rows]
    repeated = Counter(keys)
    local = [[*label[1:], *(extra if repeated[key] > 1 else ())] for key in keys]
    found = [{column: _value_spans(column, row[column], answer) for column in local[i]}
             for i, row in enumerate(rows)]  # fmt: skip
    values = [_metric_spans(metric, row[metric.column], answer) for row in rows]
    named_all = [_value_spans(label[0], row[label[0]], answer) for row in rows]
    names = _without_nested([s for spans in named_all for s in spans])
    kept_names = set(names)
    named = [[s for s in spans if s in kept_names] for spans in named_all]

    # Linhas que dividem o primeiro valor do rótulo (o mesmo título): o que distingue cada uma
    # (`marks`: o ano, ou o id quando o ano também se repete) e as âncoras compactas.
    groups: list[list[int]] = [[i] for i in range(len(rows))]
    marks: list[list[Span]] = [[] for _ in rows]
    compact: list[list[Span]] = [[] for _ in rows]
    if len(label) >= 2:
        by_first: dict[str | None, list[int]] = {}
        for i, row in enumerate(rows):
            by_first.setdefault(_key(row[label[0]]), []).append(i)
        for members in by_first.values():
            if len(members) < 2:
                continue
            seconds = Counter(_key(rows[i][label[1]]) for i in members)
            for i in members:
                groups[i] = members
                row = rows[i]
                if len(label) == 2 and seconds[_key(row[label[1]])] == 1:
                    marks[i] = _value_spans(label[1], row[label[1]], answer)
                    cited = True
                elif len(extra) == 1:
                    marks[i] = _value_spans(extra[0], row[extra[0]], answer)
                    cited = all(_value_spans(c, row[c], answer) for c in label[1:])
                else:
                    continue
                if named[i] and cited:  # só com o valor comum (o título) citado no texto
                    compact[i] = marks[i]

    anchors = _without_nested([*names, *(s for spans in compact for s in spans)])
    kept = set(anchors)
    owners: dict[Span, list[int]] = {}
    for i in range(len(rows)):
        for anchor in {*named[i], *compact[i]} & kept:
            owners.setdefault(anchor, []).append(i)
    each, both = _shared(answer, anchors, owners, values, found)

    def region(anchor: Span, bounds: Sequence[Span]) -> Span:
        low = max((end for start, end in bounds if end <= anchor[0]), default=0)
        high = min((start for start, end in bounds if start >= anchor[1]), default=len(answer.text))
        for start, end in answer.table_rows:  # numa tabela, a linha da própria célula
            if start <= anchor[0] and anchor[1] <= end:
                return max(low, start), min(high, end)
        return low, high

    def inside(spans: list[Span], area: Span, anchor: Span) -> list[Token]:
        return [
            s for s in spans if area[0] <= s[0] and s[1] <= area[1] and not _overlaps(s, anchor)
        ]

    def labels_in(i: int, anchor: Span, area: Span) -> list[list[Token]]:
        shared = both.get((i, anchor), {})
        return [[*inside(found[i][c], area, anchor), *shared.get(c, ())] for c in local[i]]

    options: list[list[Option]] = [[] for _ in rows]
    for i in range(len(rows)):
        foreign = [s for j in groups[i] if j != i for s in marks[j]]
        for anchor in named[i]:
            area = region(anchor, names)  # o caminho normal só vê as citações do rótulo
            if any(area[0] <= s[0] and s[1] <= area[1] for s in foreign):
                continue  # um título comum citado uma vez não é de uma linha só do grupo
            choices = labels_in(i, anchor, area)
            choices.append([*inside(values[i], area, anchor), *each.get((i, anchor), ())])
            options[i] += itertools.islice(_combos(choices, (anchor,)), MAX_OPTIONS)
        for anchor in (s for s in compact[i] if s in kept):
            area = region(anchor, anchors)
            spans = [*inside(values[i], area, anchor), *each.get((i, anchor), ())]
            options[i] += [(anchor, s) for s in spans][:MAX_OPTIONS]

    for start, end in _segments(answer.text, anchors):
        keyword = _RESPECTIVELY.search(answer.folded, start, end)
        if keyword is None:
            continue
        # a lista é a que vem antes da palavra: "A e B têm X e Y, respectivamente, e C tem Z"
        inner = [a for a in anchors if start <= a[0] and a[1] <= keyword.start()]
        if any(a in compact[i] for a in inner for i in range(len(rows))):
            # com as âncoras compactas na frase, o título comum não é um item da lista
            shared_title = {a for i in range(len(rows)) if compact[i] for a in named[i]}
            inner = [a for a in inner if a not in shared_title]
        size = len(inner)
        if size < 2:
            continue
        numbers = [
            n for n in answer.numbers
            if start <= n.span[0] and n.span[1] <= end
            and not any(_overlaps(n.span, a) for a in inner)
        ]  # fmt: skip
        before = [n for n in numbers if n.span[0] >= inner[-1][1] and n.span[1] <= keyword.start()]
        after = [n for n in numbers if n.span[0] >= keyword.end()]
        for listed in (before[-size:], after[:size]):
            if len(listed) != size:
                continue
            stop = min(listed[0].span[0], keyword.start())
            for position, anchor in enumerate(inner):
                segment = (anchor[1], inner[position + 1][0] if position + 1 < size else stop)
                value = listed[position].span
                for i in range(len(rows)):
                    if value not in values[i]:
                        continue
                    if anchor in named[i]:
                        choices = labels_in(i, anchor, segment)
                        options[i] += itertools.islice(
                            _combos(choices, (anchor, value)), MAX_OPTIONS
                        )
                    elif anchor in compact[i]:
                        options[i].append((anchor, value))
    known = {*names, *(s for spans in compact for s in spans), *(s for m in marks for s in m)}
    known |= {span for row in found for spans in row.values() for span in spans}
    return _Rows(
        options,
        _written_positions(answer, anchors, kept_names),
        owners,
        sorted(known),
        frozenset(kept_names & kept),
    )


def _written_positions(answer: _Answer, anchors: list[Span], names: set[Span]) -> dict[Span, int]:
    """A posição escrita junto de cada âncora. Num trecho (linha ou ponto e vírgula), as posições
    escritas casam, na ordem, com as citações de título/nome dele, ou com as âncoras compactas
    (ano, id), quando há tantas quantas posições ("Em 2º está B; em 1º está A", "9. Die Hart 2
    (2024, id 1408039): 9", uma linha de tabela com a coluna "#"). Fora disso, nenhuma posição é
    atribuída: o texto não diz de que linha ela é."""
    written = [(s, n) for s, n in answer.positions if not any(_overlaps(s, a) for a in anchors)]
    kinds = ([a for a in anchors if a in names], [a for a in anchors if a not in names])
    labels: dict[Span, int] = {}
    for start, end in _clauses(answer.text):
        seen = [n for span, n in written if start <= span[0] and span[1] <= end]
        if not seen:
            continue
        for kind in kinds:
            inner = [anchor for anchor in kind if start <= anchor[0] and anchor[1] <= end]
            if len(inner) == len(seen):
                labels.update(zip(inner, seen, strict=True))
    return labels


def _partial(options: list[list[Option]]) -> set[int]:
    """O máximo de linhas cobertas com trechos exclusivos: cada trecho escrito serve a uma linha
    só (um valor citado uma vez não vale para duas linhas, nem a âncora de uma para outra)."""
    order = sorted((i for i, found in enumerate(options) if found), key=lambda i: len(options[i]))
    best: set[int] = set()
    steps = 0

    def walk(position: int, used: Option, chosen: frozenset[int]) -> bool:
        nonlocal best, steps
        steps += 1
        if steps > MAX_ASSIGN_STEPS or len(chosen) + len(order) - position <= len(best):
            return False
        if position == len(order):
            best = set(chosen)
            return len(best) == len(order)
        row = order[position]
        for option in options[row]:
            if not any(_conflict(a, b) for a in option for b in used):
                if walk(position + 1, (*used, *option), chosen | {row}):
                    return True
        return walk(position + 1, used, chosen)

    walk(0, (), frozenset())
    return best


def _exclusive(options: list[list[Option]]) -> dict[int, Option] | None:
    """Uma cobertura completa (uma prova por linha) com trechos exclusivos, ou None."""
    order = sorted(range(len(options)), key=lambda i: len(options[i]))
    chosen: dict[int, Option] = {}
    steps = 0

    def walk(k: int, used: Option) -> bool:
        nonlocal steps
        if k == len(order):
            return True
        row = order[k]
        for option in options[row]:
            steps += 1
            if steps > MAX_ASSIGN_STEPS:
                return False
            if not any(_conflict(a, b) for a in option for b in used):
                chosen[row] = option
                if walk(k + 1, (*used, *option)):
                    return True
                del chosen[row]
        return False

    return dict(chosen) if all(options) and walk(0, ()) else None


def _in_order(options: list[list[Option]], ranks: list[int]) -> dict[int, Option] | None:
    """Uma cobertura completa na ordem do ranking: cada linha citada depois de todas as de posição
    menor (empatadas em qualquer ordem). A busca vai da 1ª posição à última e só aceita uma
    citação posterior às já escolhidas, então uma leitura invertida morre logo."""
    order = sorted(range(len(options)), key=lambda i: (ranks[i], len(options[i])))
    by_position = [sorted(found, key=lambda option: option[0]) for found in options]
    chosen: dict[int, Option] = {}
    steps = 0

    def walk(k: int, used: Option, floor: int, top: int) -> bool:
        # floor: a última âncora das linhas de posição menor; top: a do grupo de empate atual
        nonlocal steps
        if k == len(order):
            return True
        row = order[k]
        if k and ranks[row] != ranks[order[k - 1]]:
            floor, top = max(floor, top), -1
        for option in by_position[row]:
            steps += 1
            if steps > MAX_ASSIGN_STEPS:
                return False
            if option[0][0] > floor and not any(_conflict(a, b) for a in option for b in used):
                chosen[row] = option
                if walk(k + 1, (*used, *option), floor, max(top, option[0][0])):
                    return True
                del chosen[row]
        return False

    return dict(chosen) if all(options) and walk(0, (), -1, -1) else None


def _cover(
    options: list[list[Option]],
    shape: Shape,
    ranks: list[int] | None,
    written: dict[Span, int],
    name: Callable[[int], str],
) -> tuple[set[int], str]:
    """As linhas cobertas e, se todas foram, o problema de ordem (vazio quando não há).

    Conjunto: basta uma cobertura com trechos exclusivos. Ranking: uma cobertura em que cada
    linha tem a posição escrita certa (aí a ordem do texto é livre) ou uma em que as posições
    escritas estão certas e o texto segue a ordem do ranking. Líderes: as posições escritas
    certas. Se só existe cobertura fora dessas regras, devolve o problema dela; sem cobertura
    completa, a maior parcial.
    """
    if ranks is None:
        return (set(range(len(options))), "") if _exclusive(options) else (_partial(options), "")
    ties = Counter(ranks)  # as linhas exigidas de um ranking são a janela inteira

    def fits(i: int, option: Option) -> bool:  # sem posição escrita, ou com a dela
        n = written.get(option[0])
        return n is None or ranks[i] <= n < ranks[i] + ties[ranks[i]]

    valid = [[option for option in found if fits(i, option)] for i, found in enumerate(options)]
    if shape is Shape.LEADERS:
        done = _exclusive(valid) is not None
    else:
        labeled = [[option for option in found if option[0] in written] for found in valid]
        done = _exclusive(labeled) is not None or _in_order(valid, ranks) is not None
    if done:
        return set(range(len(options))), ""
    full = _exclusive(options)
    if full is None:
        return _partial(options), ""

    def where(i: int) -> str:
        rank, size = ranks[i], ties[ranks[i]]
        return f"{rank}º" if size == 1 else f"{rank}º a {rank + size - 1}º (empate)"

    for i, option in sorted(full.items()):
        if not fits(i, option):
            return set(
                full
            ), f"{name(i)} aparece como {written[option[0]]}º, mas está em {where(i)}"
    sequence = [i for _, i in sorted((option[0], i) for i, option in full.items())]
    for a, b in itertools.pairwise(sequence):
        if ranks[a] > ranks[b]:
            return set(full), f"{name(a)} ({where(a)}) aparece antes de {name(b)} ({where(b)})"
    return set(full), "nenhuma leitura do texto segue a ordem do ranking"


# --- apresentações estruturadas: linhas inventadas e listas que se contradizem ----------------
#
# A leitura acima (`_cover`) acha UMA leitura do texto que prova cada linha exigida. Uma resposta
# pode, porém, apresentar o resultado mais de uma vez ou acrescentar linhas. Sem interpretar
# prosa, confere-se o que as formas ESTRUTURADAS afirmam:
#
# - unidade estruturada: linha de dados de tabela markdown; item de lista ("- ", "* ", "1. ",
#   "1) "); e as linhas comuns de um bloco (linhas seguidas, sem linha vazia; os itens de uma
#   lista solta continuam o bloco) em que pelo menos duas são linhas de dados e uma prova uma
#   linha exigida ("Avatar (2009): R$ 2.900,00", uma por linha). Itens e linhas comuns também se
#   cortam em ";" fora de parênteses.
# - linha de dados: uma unidade com um número compatível com a métrica na posição de valor:
#   rótulo, separador (":", "—", "–", "=", " - ", ", com" ou o fim de um parêntese, como em
#   "Inventado (2020) faturou"), até 3 palavras ("receita de", "média") e o número; numa tabela,
#   uma célula que é só o número. Parênteses, datas, posições, ids e os rótulos das linhas
#   exigidas (título, ano...) não são a posição de valor. Notas, totais e baldes ("Observação:
#   ...", "Total (BRL): ...", "Sem gênero: ...") não são linhas do resultado. Uma frase sem essa
#   forma não é linha de dados.
#
# Regras: (1) toda posição escrita junto do nome de uma linha exigida é a dela; (2) uma linha de
# dados que cita uma linha exigida precisa prová-la (o valor certo), salvo uma comparação que cita
# duas ("A (2009) supera B (1997) em R$ 699,50"); (3) uma linha de dados de um bloco de resultado
# que não cita nenhuma é inventada, salvo, nos líderes, uma linha abaixo do líder (contexto, como
# no SQL); fora dos blocos de resultado (uma lista de observações à parte), só a que tem a forma
# de filme ("Inventado (2020): ..."); (4) cada apresentação COMPLETA (unidades seguidas de um
# bloco, cortadas quando uma linha exigida se repete) precisa provar sozinha todas as linhas
# exigidas, na ordem do ranking: uma lista errada não é apagada por outra certa.

_LIST_ITEM = re.compile(
    r"[ \t]*(?:>[ \t]*)?(?:[-*+•][ \t]+|[*_]{0,2}\d{1,3}[*_]{0,2}[.)][*_]{0,2}(?=[ \t]))[ \t]*"
)
_MD_HEADING = re.compile(r"[ \t]*#{1,6}[ \t]")
_GROUP = re.compile(r"\([^()\n]*\)|\[[^\[\]\n]*\]")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}")
_ROW_SEPARATOR = re.compile(r"[:—–=]|[ \t]-[ \t]|,(?=[ \t]*com(?![^\W_]))")
MAX_ROW_LABEL_WORDS = 12
MAX_ROW_LEAD_WORDS = 3
# Rótulos de nota, total ou balde: a primeira palavra (dobrada) de um rótulo curto, sem ano entre
# parênteses e sem nenhuma linha exigida citada.
_NOTE_LABELS = frozenset(
    {
        "total", "totais", "subtotal", "soma", "media", "medias", "geral", "obs", "observacao",
        "observacoes", "nota", "notas", "fonte", "fontes", "periodo", "janela", "data",
        "referencia", "criterio", "criterios", "premissa", "premissas", "ressalva", "ressalvas",
        "aviso", "atencao", "importante", "moeda", "consulta", "filtro", "base", "amostra",
        "sem", "outros", "outras", "demais", "empate", "empates",
    }
)  # fmt: skip
# Resultado vazio dito no texto: uma negação SOBRE resultados, com o substantivo deles ("nenhum
# filme", "não há registros", "não encontrei filmes", "0 resultados") ou com o verbo de busca
# ("nada foi encontrado", "nenhum ... foi encontrado"). "Não há dúvida" ou "nenhum problema" não é.
_RESULT_NOUNS = (
    r"(?:filmes?|resultados?|registros?|linhas?|itens?|titulos?|ocorrencias?|correspondencias?"
    r"|dados|informac\w+|valores?|atores|atrizes|ator|atriz|pessoas?|diretor\w*|roteirist\w*"
    r"|generos?|produtoras?|avaliac\w+|lancamentos?|nomes?|entradas?)"
)
_EMPTY = re.compile(
    rf"(?<!\w)(?:(?:nenhum|nenhuma)\s+(?:\w+\s+){{0,2}}?{_RESULT_NOUNS}"
    r"|(?:nenhum|nenhuma)\s+(?:\w+\s+){0,3}?(?:foi|foram)\s+(?:encontrad|localizad|retornad"
    r"|devolvid)\w*"
    r"|nada\s+(?:foi\s+)?(?:encontrad\w*|localizad\w*|retornad\w*|devolvid\w*|consta)"
    r"|nao\s+(?:\w+\s+){0,2}?(?:ha|houve|havia|existe|existem|existia|existiam|consta|constam"
    rf"|possui|possuem|tem|teve|tinha|aparec\w*|encontr\w*|localiz\w*|retorn\w*|devolv\w*)\s+"
    rf"(?:\w+\s+){{0,3}}?{_RESULT_NOUNS}"
    r"|sem\s+(?:resultados?|correspondencias?|registros?|ocorrencias?)"
    rf"|(?:zero|0)\s+{_RESULT_NOUNS})(?!\w)"
)
_CLAUSE_END = re.compile(r"[.;:!?](?=\s|$)|\n")


def _says_empty(text: str) -> bool:
    """O texto diz, numa mesma oração, que nada foi encontrado (`_EMPTY`)."""
    return any(_EMPTY.search(_fold(clause)) for clause in _CLAUSE_END.split(text))


@dataclass(frozen=True)
class _Unit:
    span: Span  # a unidade inteira
    start: int  # início do conteúdo, depois do marcador de lista
    block: int  # linhas seguidas, sem linha vazia entre elas
    kind: str  # table, item ou plain
    cells: tuple[Span, ...] = ()


@dataclass(frozen=True)
class _DataRow:
    values: tuple[_Written, ...]  # o número na posição de valor (ou as células numéricas)
    note: bool  # nota, total ou balde, não uma linha do resultado
    film: bool = False  # rótulo com a forma de um filme: "Título (2020)" ou uma coluna de ano


_YEAR_GROUP = re.compile(r"\(\s*(?:1[89]|2\d)\d\d\s*(?:,[^()\n]*)?\)")  # "(2009)", "(2024, id 1)"


def _within(token: Token, span: Span) -> bool:
    return span[0] <= token[0] and token[1] <= span[1]


def _pieces(text: str, start: int, end: int) -> list[Span]:
    """Os trechos de [start, end) entre pontos e vírgulas fora de parênteses ou colchetes ("Avatar
    (2009; id 5)" é um trecho só), sem os vazios."""
    spans, low, depth = [], start, 0
    for k in range(start, end + 1):
        char = text[k] if k < end else ";"
        depth = depth + 1 if char in "([" else max(0, depth - 1) if char in ")]" else depth
        if k == end or (char == ";" and depth == 0):
            if text[low:k].strip():
                spans.append((low, k))
            low = k + 1
    return spans


def _units(answer: _Answer) -> list[_Unit]:
    """As unidades do texto, em ordem. Um bloco são linhas seguidas; uma linha vazia o fecha,
    salvo entre dois itens de lista (a lista "solta" do markdown continua a mesma)."""
    text = answer.text
    tables = {row: cells for row, cells, _ in answer.table_cells}
    frames = set(answer.table_frames)
    units: list[_Unit] = []
    block, gap, previous_item = 0, False, False
    for line in _lines(text):
        if not text[slice(*line)].strip():
            gap = True
            continue
        marker = None if line in tables else _LIST_ITEM.match(text, *line)
        if gap and not (previous_item and marker):
            block += 1
        gap, previous_item = False, marker is not None
        if line in frames or _MD_HEADING.match(text, *line):
            continue
        if line in tables:
            units.append(_Unit(line, line[0], block, "table", tables[line]))
            continue
        kind = "item" if marker else "plain"
        for piece in _pieces(text, marker.end() if marker else line[0], line[1]):
            units.append(_Unit(piece, piece[0], block, kind))
    return units


def _note(words: list[str], raw: str) -> bool:
    # um rótulo com o ano entre parênteses ("Total Recall (1990)") é de filme, não de nota;
    # "Total (BRL)" continua nota
    return (
        bool(words) and words[0] in _NOTE_LABELS and len(words) <= 4 and not _YEAR_GROUP.search(raw)
    )


def _masked(text: str, span: Span, known: Sequence[Span]) -> tuple[str, list[int]]:
    """O trecho com cada rótulo conhecido trocado por "x" e parênteses, colchetes e datas
    apagados (sobram o rótulo desconhecido, o separador e o valor), e onde terminam os
    parênteses e colchetes apagados ("Inventado (2020) faturou": o fim do ano separa o rótulo)."""
    start, end = span
    chars = list(text[start:end])
    for a, b in known:
        if start <= a and b <= end:
            chars[a - start : b - start] = "x" + " " * (b - a - 1)
    masked = "".join(chars)
    ends = [match.end() for match in _GROUP.finditer(masked)]
    for pattern in (_GROUP, _DATE):
        masked = pattern.sub(lambda m: " " * len(m.group()), masked)
    return masked, ends


def _data_row(
    answer: _Answer, unit: _Unit, metric: Metric, known: Sequence[Span]
) -> _DataRow | None:
    """A unidade como linha de dados (ver o comentário do bloco), ou None."""
    text = answer.text
    numbers = [
        n for n in answer.numbers
        if _within(n.span, (unit.start, unit.span[1])) and _scales(metric, n.unit, n.scope)
    ]  # fmt: skip
    if unit.kind == "table":
        values: list[_Written] = []
        label: tuple[list[str], str] | None = None
        for cell in unit.cells:
            if any(_overlaps(cell, o) for o in (*answer.id_cells, *answer.position_cells)):
                continue
            inside = [n for n in answer.numbers if _within(n.span, cell)]
            rest = "".join(text[a:b] for a, b in _gaps(cell, [n.span for n in inside]))
            if len(inside) == 1 and not _SIGNS.sub("", rest).strip(" \t*_"):
                written = inside[0]
                if (
                    written in numbers
                    and cell not in answer.year_cells
                    and not any(_overlaps(written.span, k) for k in known)
                ):
                    values.append(written)
            elif label is None and text[slice(*cell)].strip():
                raw = text[slice(*cell)]
                named = any(_within(k, cell) for k in known)
                label = (["x"] if named else _fold(raw).split(), raw)
        if not values:
            return None
        film = any(
            re.fullmatch(r"[\s*_]*(?:1[89]|2\d)\d\d[\s*_]*", text[slice(*cell)])
            for cell in unit.cells
            if cell in answer.year_cells
        )
        return _DataRow(tuple(values), label is not None and _note(*label), film)
    masked, group_ends = _masked(text, (unit.start, unit.span[1]), known)
    for written in numbers:
        offset = written.span[0] - unit.start
        if masked[offset] in " x":  # dentro de parênteses, de uma data ou de um rótulo conhecido
            continue
        before = masked[:offset]
        cuts = [m.span() for m in _ROW_SEPARATOR.finditer(before)]
        cuts += [(end, end) for end in group_ends if end <= offset]
        if not cuts:
            continue
        cut = max(cuts, key=lambda span: span[1])
        label_words = _fold(before[: cut[0]]).split()
        lead = _fold(_SIGNS.sub(" ", before[cut[1] :])).split()
        if 0 < len(label_words) <= MAX_ROW_LABEL_WORDS and _few_words(lead, MAX_ROW_LEAD_WORDS):
            raw = text[unit.start : unit.start + cut[0]]
            note = "x" not in label_words and _note(label_words, raw)
            return _DataRow((written,), note, bool(_YEAR_GROUP.search(raw)))
    return None


def _gaps(span: Span, holes: Sequence[Span]) -> list[Span]:
    """Os pedaços de `span` fora dos `holes`."""
    pieces, low = [], span[0]
    for start, end in sorted(holes):
        pieces.append((low, start))
        low = end
    pieces.append((low, span[1]))
    return [(a, b) for a, b in pieces if b > a]


def _below(metric: Metric, written: _Written, top: float) -> bool:
    """O valor fica abaixo do líder em toda leitura e escala (não empata nem passa dele)."""
    return all(
        reading < (top - metric.tolerance) * scale
        for reading in written.readings
        for scale in _scales(metric, written.unit, written.scope)
    )


def _line_number(answer: _Answer, unit: _Unit) -> int:
    return answer.text.count("\n", 0, unit.span[0]) + 1


def _snippet(answer: _Answer, unit: _Unit) -> str:
    text = _short_text(answer.text[slice(*unit.span)].strip(), 70)
    return f"linha {_line_number(answer, unit)}: «{text}»"


def _short_text(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 3] + "..."


def _structure(
    check: ResultCheck,
    rows: list[dict[str, object]],
    answer: _Answer,
    info: _Rows | None,
    ranks: list[int] | None,
    name: Callable[[int], str],
) -> tuple[str, str] | None:
    """O primeiro problema das apresentações estruturadas (ver o comentário do bloco):
    ("contradiction", motivo) ou ("unsupported", motivo); None quando não há.

    Sem linhas exigidas (gabarito vazio), toda linha de dados é inventada.
    """
    metric = check.metrics[0]
    known = info.known if info is not None else []
    options = info.options if info is not None else [[] for _ in rows]
    units = _units(answer)
    data = [_data_row(answer, unit, metric, known) for unit in units]
    starts = [unit.span[0] for unit in units]

    def unit_of(token: Token) -> int | None:
        k = bisect.bisect_right(starts, token[0]) - 1
        return k if k >= 0 and _within(token, units[k].span) else None

    proofs: list[set[int]] = [set() for _ in units]
    placed: list[list[tuple[Option, int]]] = [[] for _ in rows]
    for i, found in enumerate(options):
        for option in found:
            k = unit_of(option[0])
            if k is not None and all(_within(token, units[k].span) for token in option[1:]):
                proofs[k].add(i)
                placed[i].append((option, k))
    mentions: list[set[int]] = [set() for _ in units]
    names: list[set[Span]] = [set() for _ in units]  # nomes de linhas exigidas citados
    for anchor, owners in (info.owners if info is not None else {}).items():
        if (k := unit_of(anchor)) is not None:
            mentions[k].update(owners)
            if anchor in info.named:
                names[k].add(anchor)
    # Uma unidade que cita o nome de duas ou mais linhas exigidas ("Avatar (2009) supera Titanic
    # (1997) em R$ 699,50") é uma comparação, não a linha de uma delas: não reclama nenhuma.
    compares = [len(found) >= 2 and not proofs[k] for k, found in enumerate(names)]

    if ranks is not None and info is not None:
        # Uma posição escrita junto do nome de uma linha (lista, "3º", "#3", coluna de posição,
        # "em segundo") é uma afirmação de ranking, em qualquer lugar do texto: precisa ser a dela.
        ties = Counter(ranks)
        for anchor, number in sorted(info.written.items()):
            owners = info.owners.get(anchor, [])
            if owners and not any(ranks[i] <= number < ranks[i] + ties[ranks[i]] for i in owners):
                i = owners[0]
                rank, size = ranks[i], ties[ranks[i]]
                where = f"{rank}º" if size == 1 else f"{rank}º a {rank + size - 1}º (empate)"
                return "contradiction", f"{name(i)} aparece como {number}º, mas está em {where}"

    rows_per_block: dict[int, list[int]] = {}
    for k, unit in enumerate(units):
        if unit.kind == "plain" and data[k] is not None:
            rows_per_block.setdefault(unit.block, []).append(k)
    tabular = {
        block
        for block, found in rows_per_block.items()
        if len(found) >= 2 and (not rows or any(proofs[k] for k in found))
    }
    structured = [
        k for k, unit in enumerate(units) if unit.kind != "plain" or unit.block in tabular
    ]

    # Uma linha de dados cita as linhas exigidas que nomeia (salvo numa comparação). Para saber se
    # uma apresentação é completa, conta também o que ela cita além do que prova ("Avatar (2009):
    # R$ 2.900,00, Titanic (1997): R$ 9.999,00" cita Titanic com outro valor).
    cited = [
        mentions[k] if data[k] is not None and not compares[k] else set() for k in range(len(units))
    ]
    claims = [proofs[k] or cited[k] for k in range(len(units))]
    # Blocos de resultado: os que provam ou citam alguma linha exigida (com gabarito vazio, todos).
    # Uma lista à parte, de observações, não é um bloco de resultado.
    results = {units[k].block for k in range(len(units)) if claims[k]}

    top = None
    if check.shape is Shape.LEADERS and rows:
        top = max(_number(row[metric.column]) or 0.0 for row in rows)
    for k in structured:
        row = data[k]
        if row is None or proofs[k] or compares[k]:
            continue
        if mentions[k]:
            who = ", ".join(name(i) for i in sorted(mentions[k]))
            return "contradiction", f"{who} aparece com outro valor ({_snippet(answer, units[k])})"
        if rows and units[k].block not in results and not row.film:
            continue  # fora do bloco de resultado, só uma linha com forma de filme é linha
        if row.note or (top is not None and all(_below(metric, v, top) for v in row.values)):
            continue
        return "unsupported", f"linha que não está no resultado ({_snippet(answer, units[k])})"

    # Apresentações: unidades seguidas do mesmo bloco, cortadas quando uma linha exigida se repete.
    # Uma unidade reclama as linhas que prova ou, se é linha de dados, as que cita.
    groups: list[list[int]] = []
    seen: set[int] = set()
    for k in structured:
        if not groups or units[k].block != units[groups[-1][-1]].block or claims[k] & seen:
            groups.append([])
            seen = set()
        groups[-1].append(k)
        seen |= claims[k]
    everyone = set(range(len(rows)))
    for group in groups:
        if not rows or not everyone <= set().union(*(proofs[k] | cited[k] for k in group)):
            continue  # só apresentações completas são julgadas sozinhas
        members = set(group)
        restricted = [[option for option, k in found if k in members] for found in placed]
        covered, order = _cover(restricted, check.shape, ranks, info.written, name)
        first, last = (_line_number(answer, units[k]) for k in (group[0], group[-1]))
        where = f"linha {first}" if first == last else f"linhas {first} a {last}"
        if len(covered) < len(rows):
            lost = ", ".join(name(i) for i in sorted(everyone - covered))
            return "contradiction", f"a apresentação completa ({where}) não prova {lost}"
        if order:
            return "contradiction", f"a apresentação completa ({where}) está fora de ordem: {order}"
    return None


@dataclass(frozen=True)
class AnswerCheck:
    verdict: str  # ok | missing | wrong_order | contradiction | unsupported
    required: int = 0  # linhas do gabarito que o texto precisa trazer
    missing: tuple[str, ...] = ()
    reason: str = ""
    order: str = ""  # ranking fora de ordem ou posição escrita errada

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "required_rows": self.required,
            "missing": list(self.missing),
            "reason": self.reason,
            "order": self.order,
        }


def required_rows(check: ResultCheck, records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Linhas que o texto precisa trazer: todas (set, ranked) ou os líderes (leaders)."""
    return [records[k] for k in required_indices(check, records)]


def check_answer(
    check: ResultCheck,
    expected_columns: Sequence[str],
    expected_rows: Sequence[Sequence[object]],
    text: str,
    *,
    context: str = "",
) -> AnswerCheck:
    """O texto final traz cada linha exigida, com o rótulo legível e a métrica principal?

    Ver `_row_options` para as formas aceitas. Cada trecho do texto (âncora, rótulo, valor) serve a
    uma linha só, salvo distribuição explícita ("cada", "ambos", "todos"). Números de posição
    (`9.`, `8º`, `#9`, colunas de posição) nunca contam como valor, o id só conta escrito como id
    e um número rotulado como outra grandeza ("65 anos", "9%", "R$", "id") não vale como a
    métrica. Em métricas de dinheiro, a moeda certa precisa ser indicada no texto ou em `context`
    (premissas e ressalvas), e um número cuja frase ou título cita só a outra moeda não vale.
    Escalares (sem rótulo) só exigem a métrica.

    Rankings: a linha de posição menor vem antes no texto (empatados em qualquer ordem). Uma
    posição escrita junto de uma linha ("3.", "3º", "#3", coluna de posição) precisa ser a dela
    (um empate no 4º lugar com 4 linhas aceita de 4 a 7); com a posição escrita em todas as
    linhas, ela manda e a ordem do texto pode ser outra ("em 2º, B; em 1º, A"). A numeração
    preguiçosa do markdown ("1." em todos os itens) não é posição. Em líderes só se confere a
    posição escrita; conjuntos não têm ordem.

    Apresentações estruturadas (`_structure`): linha de dados inventada ou com outro valor e
    lista, tabela ou bloco completo que não prova o resultado sozinho falham. Gabarito vazio: o
    texto precisa dizer que nada foi encontrado ("nenhum", "não há", "0 filmes"...) e não pode
    listar linhas de dados.
    """
    records = [dict(zip(expected_columns, row, strict=True)) for row in expected_rows]
    metric = check.metrics[0]
    answer = _Answer.read(text)
    if not records:
        if not _says_empty(text):
            return AnswerCheck(
                "missing",
                0,
                ("o resultado certo não tem nenhuma linha, e o texto não diz que nada foi "
                 "encontrado",),
            )  # fmt: skip
        if found := _structure(check, [], answer, None, None, str):
            return AnswerCheck(found[0], 0, reason=found[1])
        return AnswerCheck("ok", 0, reason="o texto diz que nada foi encontrado")
    indices = required_indices(check, records)
    rows = [records[k] for k in indices]
    shown = display_columns(check, rows)

    def name(i: int) -> str:
        return " / ".join(str(rows[i][column]) for column in shown)

    ranks = None
    info = None
    if check.label:
        info = _row_options(check, rows, answer)
        options, written = info.options, info.written
        if check.shape is not Shape.SET:
            everyone = competition_ranks([record[metric.column] for record in records])
            ranks = [everyone[k] for k in indices]
    else:
        options = [[(s,) for s in _metric_spans(metric, r[metric.column], answer)] for r in rows]
        written = {}
    covered, order = _cover(options, check.shape, ranks, written, name)
    problems = []
    if currency := _currency_problem(metric, f"{text}\n{context}"):
        problems.append(currency)
    for i, row in enumerate(rows):
        if i not in covered:
            value = f"{metric.column} = {row[metric.column]}"
            problems.append(f"{name(i)}: {value}" if shown else value)
    if problems or order:
        verdict = "missing" if problems else "wrong_order"
        return AnswerCheck(verdict, len(rows), tuple(problems), order=order)
    if info is not None and (found := _structure(check, rows, answer, info, ranks, name)):
        return AnswerCheck(found[0], len(rows), reason=found[1])
    return AnswerCheck("ok", len(rows))


# --- o caso inteiro --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryScore:
    index: int  # SqlExecution.index
    match: TableMatch

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, **self.match.to_dict()}


@dataclass(frozen=True)
class CaseScore:
    verdict: Verdict
    category: FailureCategory | None
    reason: str
    detail: str | None = None  # código de result_mismatch (missing_rows, wrong_order...)
    queries: tuple[QueryScore, ...] = ()
    chosen_query: int | None = None
    answer: AnswerCheck | None = None
    notes: tuple[str, ...] = ()

    @property
    def evaluated(self) -> bool:
        return self.verdict is not Verdict.ERROR

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "failure_category": None if self.category is None else self.category.value,
            "reason": self.reason,
            "detail": self.detail,
            "sql_check": {
                "chosen_query": self.chosen_query,
                "queries": [query.to_dict() for query in self.queries],
            }
            if self.queries
            else None,
            "answer_check": None if self.answer is None else self.answer.to_dict(),
            "notes": list(self.notes),
        }


def failure_score(failure: AgentFailure) -> CaseScore:
    kind = failure.kind
    if kind is FailureKind.BAD_REQUEST:
        # 400/413/422: o provedor recusou a requisição que a aplicação montou para este modelo. É
        # uma falha funcional da configuração modelo + agente e fica no denominador.
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.AGENT_PROTOCOL,
            f"{kind.value}: o provedor recusou a requisição do agente. {failure.message}",
        )
    if kind in PROVIDER_KINDS:
        return CaseScore(
            Verdict.ERROR, FailureCategory.PROVIDER, f"{kind.value}: {failure.message}"
        )
    if kind is FailureKind.DATABASE:
        return CaseScore(Verdict.ERROR, FailureCategory.DATABASE, failure.message)
    return CaseScore(
        Verdict.FAIL, FailureCategory.AGENT_PROTOCOL, f"{kind.value}: {failure.message}"
    )


def _tools_used(trace: RunTrace) -> str:
    """Ferramentas de dados chamadas na pergunta (inclusive as que falharam), ou ''."""
    parts = []
    if trace.sql_executions:
        parts.append(f"{len(trace.sql_executions)} run_sql")
    if trace.entity_lookups:
        parts.append(f"{len(trace.entity_lookups)} find_entities")
    if trace.tool_calls and not parts:
        parts.append(f"{trace.tool_calls} ferramenta(s)")
    return ", ".join(parts)


def _same_text(a: str, b: str) -> bool:
    return " ".join(_fold(a).split()) == " ".join(_fold(b).split())


def _undistinguished(homonyms: list[dict[str, object]], text: str) -> list[str]:
    """Homônimos que o texto não distingue: pelo ano quando ele é único, senão pelo id_filme."""
    years = Counter(row["ano_lancamento"] for row in homonyms)
    answer = _Answer.read(text)
    missing = []
    for row in homonyms:
        if years[row["ano_lancamento"]] == 1:
            found = _value_spans("ano_lancamento", row["ano_lancamento"], answer)
            wanted = f"ano {row['ano_lancamento']}"
        else:
            found = _value_spans("id_filme", row["id_filme"], answer)  # só escrito como id
            wanted = f"id_filme {row['id_filme']}"
        if not found:
            missing.append(f"{row['titulo']} ({wanted})")
    return missing


def _clarification_score(
    case: EvalCase, trace: RunTrace, expected: ReferenceResult, text: str
) -> CaseScore:
    """Esclarecimento comprovado: a busca achou todos os homônimos e o texto distingue cada um.

    A busca precisa ter sido lida antes da resposta final: uma busca pedida na mesma resposta do
    modelo que entregou o esclarecimento roda, mas o texto já estava escrito sem o resultado dela.
    """
    title = case.ambiguous_title or ""
    homonyms = list(expected.records())
    lookups = [
        lookup
        for lookup in trace.entity_lookups
        if lookup.kind == "filme"
        and lookup.state is MatchState.EXACT_MULTIPLE
        and _same_text(lookup.text, title)
    ]
    if not lookups:
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.POLICY,
            f"pediu esclarecimento sem uma busca find_entities que encontre os homônimos de "
            f"'{title}'",
        )
    read = [lookup for lookup in lookups if lookup.step < _answer_step(trace)]
    if not read:
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.POLICY,
            f"a busca dos homônimos de '{title}' rodou na mesma resposta do modelo que pediu o "
            "esclarecimento: ele não leu os candidatos antes de perguntar",
        )
    lookups = read
    ids = {str(row["id_filme"]) for row in homonyms}
    if not any(
        lookup.total_matches == len(lookup.candidates)
        and {candidate.movie_id for candidate in lookup.candidates} == ids
        for lookup in lookups
    ):
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.POLICY,
            f"a busca não trouxe o conjunto completo dos {len(ids)} homônimos do gabarito",
        )
    missing = _undistinguished(homonyms, text)
    if missing:
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.POLICY,
            "o pedido de esclarecimento não distingue: " + "; ".join(missing),
        )
    return CaseScore(
        Verdict.PASS,
        None,
        f"esclarecimento depois da busca com os {len(ids)} homônimos, com o desambiguador de "
        "cada um",
    )


# --- texto mínimo dos casos de política sem dados (no texto dobrado: sem acento nem pontuação) --

_REFUSAL = re.compile(
    r"(?<!\w)(?:nao\s+(?:\w+\s+){0,4}?(?:tenho|temos|consig\w*|poss\w*|sei|sabemos|disponho"
    r"|dispomos|disponive\w*|disponivel|fac\w*|ofere\w*|fornec\w*|respond\w*|trat\w*|lid\w*"
    r"|cubr\w*|cobr\w*|acesso|capaz|capazes|faz\s+parte|e\s+possivel|tem\s+como|ha\s+como)"
    r"|fora\s+d[oe]\s+(?:meu\s+|nosso\s+|seu\s+)?(?:escopo|ambito|alcance|dominio|foco)"
    r"|(?:so|apenas|somente|exclusivamente|unicamente)\s+(?:\w+\s+){0,5}?(?:filmes?|catalogo"
    r"|cinema))(?!\w)"
)
_CAPABILITY = re.compile(
    r"(?<!\w)(?<!nao )(?:posso|podemos|consigo|conseguimos|sou\s+capaz|somos\s+capazes"
    r"|sei\s+responder|respondo|respondemos|consulto|consultamos|analiso|analisamos|calculo"
    r"|calculamos|ajudo|ajudamos|pergunte|(?:voce|vc)\s+pode\s+(?:me\s+)?(?:perguntar|pedir"
    r"|consultar|saber)|e\s+possivel\s+(?:perguntar|consultar|saber|obter))(?!\w)"
)
_TOPICS = {
    "filmes": r"filmes?",
    "catálogo": r"catalogo",
    "receita": r"receitas?|faturamentos?|bilheterias?",
    "lucro": r"lucros?|margens?|orcamentos?",
    "gêneros": r"generos?",
    "notas": r"notas?|avaliac\w+|imdb|tmdb",
    "pessoas": r"atores|ator|atrizes|atriz|diretores|diretoras?|diretor|roteiristas?|elenco",
    "produtoras": r"produtoras?|estudios?",
    "rankings": r"rankings?",
    "popularidade": r"popularidade|populares",
    "lançamentos": r"lancamentos?",
}
_TOPIC = {name: re.compile(rf"(?<!\w)(?:{pattern})(?!\w)") for name, pattern in _TOPICS.items()}
# Um dado do catálogo afirmado sem consulta: "O catálogo tem 95.645 filmes", "há 19 gêneros".
_CATALOG_FACT = re.compile(
    r"(?<!\w)(?:tem|temos|possui|possuimos|contem|ha|existem|sao|reune|reunimos|inclui|incluimos"
    r"|soma|somam|totaliza|totalizam|abrange|abriga|traz|conta\s+com|contamos\s+com)\s+"
    r"(?:cerca\s+de\s+|mais\s+de\s+|aproximadamente\s+|quase\s+|exatamente\s+|ao\s+todo\s+)?"
    r"\d[\d ]*\s+(?:mil\s+|milhoes\s+de\s+|milhao\s+de\s+)?(?:filmes?|titulos?|avaliac\w+"
    r"|atores|atrizes|pessoas|diretores|produtoras|generos)(?!\w)"
)


def text_policy_problem(case: EvalCase, text: str) -> str | None:
    """O que falta (ou sobra) no texto de um caso de política sem dados; None quando confere.

    Deliberadamente estreito e determinístico, sem interpretar a frase:
    - recusa (fora do escopo): uma expressão de recusa ou de limite ("não tenho acesso", "não
      posso", "fora do escopo", "só respondo ... catálogo de filmes") e nenhum padrão que entregue
      o pedido (`forbidden_answers`, como uma temperatura na previsão do tempo);
    - capacidades (ajuda): um verbo de capacidade ("posso", "respondo", "consigo", não negado) e
      pelo menos dois assuntos do catálogo (filmes, catálogo, receita, gêneros, notas...), sem
      afirmar um número do catálogo ("o catálogo tem 95.645 filmes") que só uma consulta daria.
    """
    folded = " ".join(_fold(text).split())
    if case.text_policy is TextPolicy.REFUSAL:
        if not _REFUSAL.search(folded):
            return "o texto não recusa nem diz que o pedido está fora do catálogo de filmes"
        for pattern in case.forbidden_answers:
            if hit := re.search(pattern, text, re.IGNORECASE):
                return f"o texto entrega o pedido fora do escopo («{hit.group()}»)"
    elif case.text_policy is TextPolicy.CAPABILITIES:
        if not _CAPABILITY.search(folded):
            return "o texto não diz o que o agente consegue responder"
        topics = [name for name, pattern in _TOPIC.items() if pattern.search(folded)]
        if len(topics) < 2:
            return (
                "o texto não diz o que o agente responde sobre o catálogo de filmes (citou "
                f"{', '.join(topics) or 'nenhum assunto do catálogo'})"
            )
        if fact := _CATALOG_FACT.search(folded):
            return f"o texto afirma um dado do catálogo sem consultar o banco («{fact.group()}»)"
    return None


def _unexpected_status(case: EvalCase, trace: RunTrace, status: AnswerStatus) -> CaseScore:
    wanted = "/".join(sorted(s.value for s in case.expected_status))
    if case.category is Category.POLICY:
        return CaseScore(
            Verdict.FAIL, FailureCategory.POLICY, f"esperado {wanted}, obtido {status.value}"
        )
    failed = [e for e in trace.sql_executions if not e.ok]
    if not trace.successful_sql() and failed:
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.SQL_ERROR,
            f"respondeu {status.value} depois de {len(failed)} consulta(s) sem sucesso",
        )
    return CaseScore(
        Verdict.FAIL, FailureCategory.WRONG_STATUS, f"esperado {wanted}, obtido {status.value}"
    )


def _answer_step(trace: RunTrace) -> int:
    """A requisição em que veio a resposta final: a última resposta do modelo (`ctx.run_step`
    soma um a cada requisição, e as ferramentas de uma resposta rodam com o número dela)."""
    return trace.model_responses


UNREAD = frozenset({"rows_not_shown", "not_read_before_answer"})


def _evidence(
    check: ResultCheck,
    expected: ReferenceResult,
    run: SqlExecution,
    required: set[int],
    answered_at: int,
) -> TableMatch:
    """Uma consulta só é evidência se reproduz o gabarito com o rótulo que a resposta mostra, não
    foi truncada e o modelo leu, antes de responder, todas as linhas exigidas.

    - O PydanticAI 2 (end_strategy 'graceful') executa um run_sql pedido na MESMA resposta do
      final_answer: a consulta roda, mas o texto já estava escrito. Só vale uma consulta de uma
      requisição anterior à da resposta final. A produção (M2) já recusa essa resposta final;
      esta conferência é independente dela (defesa em profundidade).
    - O rastro guarda todas as linhas, mas o modelo só vê as `rows_shown` primeiras (limite de
      tamanho do que a ferramenta devolve); uma linha que ele não viu não fundamenta a resposta.
    """
    match = match_table(
        check, expected.columns, expected.rows, run.columns, run.rows, truncated=run.truncated
    )
    if not match.matched:
        return match
    if run.step >= answered_at:
        return replace(
            match,
            matched=False,
            detail="not_read_before_answer",
            message=(
                f"o resultado confere, mas a consulta foi pedida na mesma resposta do modelo que "
                f"entregou a resposta final (requisição {run.step}): ele não leu esse resultado "
                "antes de responder"
            ),
            problems=1,
        )
    hidden = [k for agent, k in match.pairs if k in required and agent >= run.rows_shown]
    if not hidden:
        return match
    return replace(
        match,
        matched=False,
        detail="rows_not_shown",
        message=(
            f"o resultado confere, mas o modelo recebeu só as {run.rows_shown} primeira(s) das "
            f"{run.row_count} linha(s) (limite de tamanho do que é enviado a ele); "
            f"{len(hidden)} linha(s) exigida(s) ficaram de fora do que ele viu"
        ),
        problems=1,
    )


def _data_score(
    case: EvalCase, trace: RunTrace, expected: ReferenceResult, answer: AgentAnswer
) -> CaseScore:
    successful = trace.successful_sql()
    if not successful:
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.UNGROUNDED,
            "data_answer sem nenhuma consulta bem-sucedida no rastro",
        )
    answered_at = _answer_step(trace)
    if not any(e.step < answered_at for e in successful):  # a regra do M2, conferida aqui também
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.UNGROUNDED,
            "data_answer sem nenhuma consulta bem-sucedida lida antes da resposta final",
        )
    required = set(required_indices(case.check, list(expected.records())))
    queries = tuple(
        QueryScore(e.index, _evidence(case.check, expected, e, required, answered_at))
        for e in successful
    )
    passing = [query for query in queries if query.match.matched]
    if not passing:
        unseen = [query for query in queries if query.match.detail in UNREAD]
        if unseen:  # o SQL estava certo, mas a resposta não pode ter vindo do que o modelo leu
            return CaseScore(
                Verdict.FAIL,
                FailureCategory.UNGROUNDED,
                f"consulta #{unseen[-1].index}: {unseen[-1].match.message}",
                detail=unseen[-1].match.detail,
                queries=queries,
            )
        best = max(queries, key=lambda query: (query.match.matched_rows, query.index))
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.RESULT_MISMATCH,
            f"consulta #{best.index}: {best.match.message}",
            detail=best.match.detail,
            queries=queries,
        )
    chosen = passing[-1]
    notes = list(chosen.match.notes)
    if chosen.index != successful[-1].index:
        notes.append(
            f"a consulta que confere é a #{chosen.index}, não a última bem-sucedida "
            f"(#{successful[-1].index})"
        )
    # O texto é conferido contra o gabarito, não contra a consulta escolhida. A moeda pode vir
    # das premissas e ressalvas; linhas e valores, só do texto da resposta.
    fidelity = check_answer(
        case.check,
        expected.columns,
        expected.rows,
        answer.answer,
        context="\n".join((*answer.assumptions, *answer.caveats)),
    )
    if fidelity.verdict != "ok":
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.ANSWER_TEXT,
            _text_reason(fidelity),
            queries=queries,
            chosen_query=chosen.index,
            answer=fidelity,
            notes=tuple(notes),
        )
    said = (
        f"o texto traz as {fidelity.required} linha(s) exigida(s)"
        if fidelity.required
        else fidelity.reason
    )
    return CaseScore(
        Verdict.PASS,
        None,
        f"consulta #{chosen.index}: {chosen.match.message}; {said}",
        queries=queries,
        chosen_query=chosen.index,
        answer=fidelity,
        notes=tuple(notes),
    )


def _text_reason(fidelity: AnswerCheck) -> str:
    """O motivo de um texto final que não confere (qualquer veredito diferente de ok falha)."""
    if fidelity.verdict == "missing" and not fidelity.required:
        return f"o SQL confere, mas {fidelity.missing[0]}"
    if fidelity.verdict == "missing":
        shown = ", ".join(fidelity.missing[:5])
        more = f" e mais {len(fidelity.missing) - 5}" if len(fidelity.missing) > 5 else ""
        return (
            f"o SQL confere, mas o texto não traz {len(fidelity.missing)} de {fidelity.required} "
            f"linha(s) exigida(s) com a métrica: {shown}{more}"
        )
    if fidelity.verdict == "wrong_order":
        return (
            f"o SQL confere e o texto traz as {fidelity.required} linha(s) exigida(s), mas fora "
            f"da ordem do ranking: {fidelity.order}"
        )
    if fidelity.verdict == "contradiction":
        return f"o SQL confere, mas o texto se contradiz: {fidelity.reason}"
    if fidelity.verdict == "unsupported":
        return (
            f"o SQL confere, mas o texto afirma dados que não estão no resultado: {fidelity.reason}"
        )
    return f"o texto final não confere ({fidelity.verdict}): {fidelity.reason}"


def score_case(
    case: EvalCase, outcome: AgentOutcome, expected: ReferenceResult | None = None
) -> CaseScore:
    """Veredito de um caso. `expected` é o gabarito (obrigatório quando o caso tem um).

    Status fora do esperado falha. `data_answer` é conferido contra o gabarito (SQL e texto),
    inclusive a resposta completa do título ambíguo; o esclarecimento desse caso precisa da busca
    dos homônimos e dos desambiguadores no texto; casos sem dados não podem usar ferramentas e,
    com `text_policy`, precisam do texto mínimo da política.
    """
    if outcome.failure is not None:
        return failure_score(outcome.failure)
    answer = outcome.answer
    if answer is None:
        return CaseScore(
            Verdict.FAIL, FailureCategory.AGENT_PROTOCOL, "o agente terminou sem resposta final"
        )
    status, trace = answer.status, outcome.trace
    if status not in case.expected_status:
        return _unexpected_status(case, trace, status)
    if case.is_data and expected is None:
        raise ValueError(f"{case.case_id}: caso com gabarito exige o gabarito calculado")
    if status is AnswerStatus.DATA_ANSWER:
        return _data_score(case, trace, expected, answer)
    if status is AnswerStatus.CLARIFICATION and case.ambiguous_title is not None:
        return _clarification_score(case, trace, expected, answer.answer)
    if case.require_no_tools and (used := _tools_used(trace)):
        return CaseScore(
            Verdict.FAIL,
            FailureCategory.POLICY,
            f"status {status.value} certo, mas usou ferramentas de dados ({used}); esta pergunta "
            "se responde sem consultar o banco",
        )
    if case.text_policy is not None:
        if problem := text_policy_problem(case, answer.answer):
            return CaseScore(
                Verdict.FAIL, FailureCategory.POLICY, f"status {status.value} certo, mas {problem}"
            )
        return CaseScore(
            Verdict.PASS, None, f"status {status.value}, como esperado, e o texto da política"
        )
    return CaseScore(Verdict.PASS, None, f"status {status.value}, como esperado")


__all__ = [
    "MAX_MAPPINGS",
    "NOT_EVALUATED",
    "PROVIDER_KINDS",
    "AnswerCheck",
    "CaseScore",
    "FailureCategory",
    "QueryScore",
    "TableMatch",
    "Verdict",
    "check_answer",
    "competition_ranks",
    "disambiguator",
    "display_columns",
    "failure_score",
    "match_table",
    "required_indices",
    "required_rows",
    "score_case",
    "text_policy_problem",
]
