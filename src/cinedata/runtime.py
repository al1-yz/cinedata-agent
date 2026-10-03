"""Tipos de uma pergunta ao agente: a resposta do modelo, o rastro da execução e o desfecho.

Quem escreve o quê:

- `AgentAnswer` é o ÚNICO conteúdo escrito pelo modelo: status, texto, premissas e ressalvas.
- `RunTrace` é escrito só pelo código (ferramentas, validador e orquestração): SQL executado,
  colunas, linhas, truncamento, tempos, modelos que responderam e uso. O modelo não o altera.
- `AgentOutcome` junta os dois e acrescenta avisos determinísticos calculados a partir do rastro
  (resultado truncado, zero linhas, candidato escolhido entre homônimos), qualquer que seja o
  texto do modelo.
- `candidate_uses` aplica a política de entidades não resolvidas; o validador do agente recusa
  uma resposta baseada em dados que dependa de um uso não permitido.

O rastro fica só em memória: nada aqui grava em disco. As linhas guardadas já vêm limitadas pelo
`SafeDatabase` (teto de linhas e de tamanho de célula).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from cinedata.entities import Candidate, MatchState, normalize

# --- conteúdo escrito pelo modelo ------------------------------------------------------------


class AnswerStatus(StrEnum):
    DATA_ANSWER = "data_answer"  # resposta baseada em resultado de consulta ao banco
    CLARIFICATION = "clarification"  # falta informação ou há ambiguidade que muda a resposta
    INFO = "info"  # o que o agente faz e como usá-lo, sem dados do banco
    OUT_OF_SCOPE = "out_of_scope"  # pedido fora do catálogo de filmes


_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]


class AgentAnswer(BaseModel):
    """Resposta final ao usuário. Só conteúdo; SQL, linhas e uso são anexados pelo sistema."""

    status: AnswerStatus = Field(
        description=(
            "data_answer: baseada no resultado de pelo menos uma consulta run_sql já lida; "
            "clarification: pergunta de volta ao usuário (ambiguidade ou falta de informação); "
            "info: explica o que você sabe fazer, sem dados; "
            "out_of_scope: o pedido não é sobre o catálogo de filmes."
        )
    )
    answer: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ] = Field(description="Texto para o usuário, em português, sem SQL.")
    assumptions: list[_Text] = Field(
        default_factory=list,
        max_length=10,
        description="Interpretações adotadas (moeda, critérios, N do ranking, empates...).",
    )
    caveats: list[_Text] = Field(
        default_factory=list,
        max_length=10,
        description="Limitações dos dados ou da resposta.",
    )


# --- rastro escrito pelo código --------------------------------------------------------------


class SqlStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"  # violou a política do SafeDatabase (escrita, tabela ou função proibida)
    FAILED = "failed"  # SQL inválido (sintaxe, coluna inexistente...)
    TIMEOUT = "timeout"
    SKIPPED = "skipped"  # não executado: orçamento esgotado ou SQL idêntico a um que já falhou
    UNAVAILABLE = "unavailable"  # o banco ficou inutilizável; encerra a pergunta


@dataclass(frozen=True)
class SqlExecution:
    index: int  # ordem na pergunta, a partir de 1
    step: int  # número da requisição ao modelo que pediu a consulta
    sql: str
    status: SqlStatus
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    truncated: bool = False  # havia mais linhas que o teto do SafeDatabase
    truncated_cells: int = 0
    rows_shown: int = 0  # linhas que couberam no limite de tamanho enviado ao modelo
    elapsed_s: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is SqlStatus.OK

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class EntityLookup:
    step: int
    kind: str
    text: str
    role: str | None
    state: MatchState | None  # None = a busca falhou (`error`)
    total_matches: int = 0
    candidates: tuple[Candidate, ...] = ()
    resolved_key: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class OutputRejection:
    step: int
    status: AnswerStatus
    reason: str


@dataclass(frozen=True)
class ModelCall:
    model_name: str
    provider_name: str | None
    finish_reason: str | None
    input_tokens: int
    output_tokens: int


@dataclass
class RunTrace:
    """Rastro de uma pergunta. Preenchido pelo código durante a execução."""

    reference_date: date
    max_rows: int
    request_limit: int
    configured_models: tuple[str, ...] = ()
    entity_lookups: list[EntityLookup] = field(default_factory=list)
    sql_executions: list[SqlExecution] = field(default_factory=list)
    output_rejections: list[OutputRejection] = field(default_factory=list)
    model_calls: list[ModelCall] = field(default_factory=list)
    # Respostas recebidas do modelo (uma por rodada do agente). NÃO é a contagem de chamadas HTTP
    # ao provedor: com fallback, uma rodada pode ter custado várias tentativas.
    model_responses: int = 0
    tool_calls: int = 0
    elapsed_s: float = 0.0

    def successful_sql(self) -> list[SqlExecution]:
        return [execution for execution in self.sql_executions if execution.ok]

    def has_result_before(self, step: int) -> bool:
        """Houve consulta bem-sucedida cujo resultado o modelo já recebeu antes de `step`?"""
        return any(execution.ok and execution.step < step for execution in self.sql_executions)

    @property
    def models_used(self) -> tuple[str, ...]:
        """Modelos que de fato responderam, na ordem da primeira resposta de cada um."""
        return tuple(dict.fromkeys(call.model_name for call in self.model_calls))

    @property
    def input_tokens(self) -> int:
        return sum(call.input_tokens for call in self.model_calls)

    @property
    def output_tokens(self) -> int:
        return sum(call.output_tokens for call in self.model_calls)


# --- falhas e desfecho -----------------------------------------------------------------------


class FailureKind(StrEnum):
    AUTH = "auth"
    PAYMENT = "payment"
    FORBIDDEN = "forbidden"
    MODEL_UNAVAILABLE = "model_unavailable"  # 404: modelo inexistente ou sem endpoint compatível
    BAD_REQUEST = "bad_request"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # 408/5xx, conexão, prazo, resposta vazia
    PROVIDER_ERROR = "provider_error"
    REQUEST_LIMIT = "request_limit"
    AGENT_PROTOCOL = "agent_protocol"  # o modelo não seguiu o protocolo dentro dos retries
    DATABASE = "database"


@dataclass(frozen=True)
class AgentFailure:
    kind: FailureKind
    message: str  # para o usuário; nunca contém a chave da API


@dataclass(frozen=True)
class AgentOutcome:
    question: str
    answer: AgentAnswer | None
    failure: AgentFailure | None
    trace: RunTrace
    notices: tuple[str, ...] = ()  # avisos do sistema, calculados do rastro

    @property
    def ok(self) -> bool:
        return self.failure is None and self.answer is not None

    def to_dict(self) -> dict[str, object]:
        """Forma JSON: `answer` é do modelo; `runtime` e `notices`, da aplicação."""
        trace = self.trace
        return {
            "question": self.question,
            "answer": None if self.answer is None else self.answer.model_dump(mode="json"),
            "failure": None
            if self.failure is None
            else {"kind": self.failure.kind.value, "message": self.failure.message},
            "notices": list(self.notices),
            "runtime": {
                "reference_date": trace.reference_date.isoformat(),
                "max_rows": trace.max_rows,
                "request_limit": trace.request_limit,
                "configured_models": list(trace.configured_models),
                "models_used": list(trace.models_used),
                "model_responses": trace.model_responses,
                "tool_calls": trace.tool_calls,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "elapsed_s": round(trace.elapsed_s, 3),
                "entity_lookups": [_lookup_dict(lookup) for lookup in trace.entity_lookups],
                "sql": [_sql_dict(execution) for execution in trace.sql_executions],
                "output_rejections": [
                    {"step": item.step, "status": item.status.value, "reason": item.reason}
                    for item in trace.output_rejections
                ],
            },
        }


def _jsonable(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSON não tem infinito
    return value


def _sql_dict(execution: SqlExecution) -> dict[str, object]:
    data: dict[str, object] = {
        "index": execution.index,
        "step": execution.step,
        "status": execution.status.value,
        "sql": execution.sql,
    }
    if execution.ok:
        data |= {
            "columns": list(execution.columns),
            "row_count": execution.row_count,
            "truncated": execution.truncated,
            "truncated_cells": execution.truncated_cells,
            "rows_shown": execution.rows_shown,
            "rows": [[_jsonable(cell) for cell in row] for row in execution.rows],
        }
    if execution.elapsed_s is not None:
        data["elapsed_s"] = round(execution.elapsed_s, 4)
    if execution.error is not None:
        data["error"] = execution.error
    return data


def _lookup_dict(lookup: EntityLookup) -> dict[str, object]:
    return {
        "step": lookup.step,
        "kind": lookup.kind,
        "text": lookup.text,
        "role": lookup.role,
        "state": None if lookup.state is None else lookup.state.value,
        "total_matches": lookup.total_matches,
        "resolved_key": lookup.resolved_key,
        "candidate_keys": [candidate.key for candidate in lookup.candidates],
        "error": lookup.error,
    }


# --- avisos determinísticos ------------------------------------------------------------------


def describe_candidate(candidate: Candidate) -> str:
    details = [
        str(candidate.year) if candidate.year is not None else None,
        f"id_filme {candidate.movie_id}" if candidate.movie_id is not None else None,
        candidate.role,
    ]
    extra = ", ".join(item for item in details if item)
    return f"{candidate.label} ({extra})" if extra else candidate.label


# --- política de candidatos não resolvidos ---------------------------------------------------
#
# Só se observa o que o SQL mostra: a chave `sk_*` de um candidato entre aspas simples. Um modelo
# que ignore `find_entities` e filtre pelo texto do nome escapa desta verificação (limitação
# documentada). Não há NER: os qualificadores aceitos são um ano de 4 dígitos ou "id <número>"
# escritos na pergunta original.

_YEAR = re.compile(r"\b(1[89]\d{2}|2\d{3})\b")
_MOVIE_ID = re.compile(r"\bid(?:[ _-]?(?:do[ _-]?)?filme)?\s*[:=#]?\s*(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CandidateUse:
    """Candidatos de UMA busca não resolvida que aparecem em consultas bem-sucedidas."""

    lookup: EntityLookup
    used: tuple[Candidate, ...]
    queries: tuple[int, ...]  # índices das consultas que usaram as chaves
    allowed: bool
    basis: str


def explicit_choice(question: str, lookup: EntityLookup) -> Candidate | None:
    """O candidato de filme que um ano ou `id <número>` da pergunta identifica sozinho.

    None quando a pergunta não traz qualificador, quando ele casa com mais de um candidato ou
    quando a busca não mostrou todos os candidatos (a unicidade não pode ser provada).
    """
    if lookup.kind != "filme" or lookup.total_matches != len(lookup.candidates):
        return None
    years = set(_YEAR.findall(question))
    ids = set(_MOVIE_ID.findall(question))
    chosen = [
        candidate
        for candidate in lookup.candidates
        if str(candidate.year) in years
        or (candidate.movie_id is not None and candidate.movie_id in ids)
    ]
    return chosen[0] if len(chosen) == 1 else None


def _judge(lookup: EntityLookup, used: tuple[Candidate, ...], question: str) -> tuple[bool, str]:
    if lookup.state is MatchState.FUZZY_SUGGESTIONS:
        return False, "sugestão por semelhança sempre exige confirmação do usuário"
    complete = lookup.total_matches == len(lookup.candidates)
    if (
        lookup.state is MatchState.EXACT_MULTIPLE
        and complete
        and len(used) == len(lookup.candidates)
    ):
        return True, "a resposta cobre todos os homônimos"
    choice = explicit_choice(question, lookup)
    if choice is not None and used == (choice,):
        return True, "o ano ou o id_filme escrito na pergunta identifica este candidato"
    return False, "a pergunta não identifica este candidato sozinha"


def candidate_uses(trace: RunTrace, question: str) -> list[CandidateUse]:
    """Usos de candidatos de buscas sem `exact_unique`, julgados pela política (falha fechada).

    Uma chave que a MESMA busca, refeita com papel, resolveu como `exact_unique` (por exemplo,
    "Christopher Nolan" e depois "Christopher Nolan" com role=Diretor) conta como resolvida.
    """
    successful = trace.successful_sql()
    refined = {
        (normalize(lookup.text), lookup.resolved_key)
        for lookup in trace.entity_lookups
        if lookup.state is MatchState.EXACT_UNIQUE
    }
    uses: list[CandidateUse] = []
    for lookup in trace.entity_lookups:
        if lookup.state in (None, MatchState.EXACT_UNIQUE, MatchState.NONE):
            continue
        name = normalize(lookup.text)
        used: list[Candidate] = []
        queries: set[int] = set()
        for candidate in lookup.candidates:
            if (name, candidate.key) in refined:
                continue
            hits = [e.index for e in successful if f"'{candidate.key}'" in e.sql]
            if hits:
                used.append(candidate)
                queries.update(hits)
        if used:
            allowed, basis = _judge(lookup, tuple(used), question)
            uses.append(CandidateUse(lookup, tuple(used), tuple(sorted(queries)), allowed, basis))
    return uses


def compute_notices(answer: AgentAnswer | None, trace: RunTrace, question: str) -> tuple[str, ...]:
    """Fatos do rastro que valem para uma resposta baseada em dados, digam o que disser o modelo."""
    if answer is None or answer.status is not AnswerStatus.DATA_ANSWER:
        return ()
    successful = trace.successful_sql()
    notices: list[str] = []
    for execution in successful:
        if execution.truncated:
            notices.append(
                f"A consulta #{execution.index} tinha mais de {trace.max_rows} linhas; o modelo "
                f"viu só as {trace.max_rows} primeiras."
            )
        if execution.rows_shown < execution.row_count:
            notices.append(
                f"Da consulta #{execution.index}, só {execution.rows_shown} de "
                f"{execution.row_count} linhas couberam no limite de tamanho enviado ao modelo."
            )
        if execution.truncated_cells:
            notices.append(
                f"A consulta #{execution.index} teve {execution.truncated_cells} texto(s) "
                "cortado(s) por tamanho."
            )
    if successful and successful[-1].row_count == 0:
        notices.append(
            f"A última consulta bem-sucedida (#{successful[-1].index}) não devolveu nenhuma linha."
        )
    for use in candidate_uses(trace, question):
        if use.allowed:  # um uso não permitido nunca chega a uma resposta aceita
            queries = ", ".join(f"#{index}" for index in use.queries)
            chosen = "; ".join(describe_candidate(candidate) for candidate in use.used)
            notices.append(
                f"'{use.lookup.text}' não tinha resolução única ({use.lookup.state.value}); "
                f"{use.basis}. A(s) consulta(s) {queries} usaram: {chosen}."
            )
    return tuple(notices)


__all__ = [
    "AgentAnswer",
    "AgentFailure",
    "AgentOutcome",
    "AnswerStatus",
    "CandidateUse",
    "EntityLookup",
    "FailureKind",
    "ModelCall",
    "OutputRejection",
    "RunTrace",
    "SqlExecution",
    "SqlStatus",
    "candidate_uses",
    "compute_notices",
    "describe_candidate",
    "explicit_choice",
]
