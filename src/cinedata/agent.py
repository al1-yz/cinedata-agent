"""O agente CineData: um agente PydanticAI, duas ferramentas e a regra de fundamentação.

Text-to-SQL livre: o modelo recebe o esquema da Gold (`prompt.py`) e escreve o SQL de cada
pergunta. Não há roteamento para perguntas conhecidas nem uso dos casos de referência do M1c, que
existem só para a avaliação.

Ferramentas visíveis ao modelo:

- `find_entities`: envolve o `EntityIndex` (M1b). Só `exact_unique` resolve; os demais estados
  voltam com candidatos e orientação, nunca com uma escolha.
- `run_sql`: executa o SQL do modelo por `SafeDatabase.execute(sql)`, sem nenhum override: teto
  de linhas, prazo, authorizer e conexão são os da configuração e o modelo não os controla.

Garantias aplicadas em código, não no prompt:

- Resposta final sozinha: nenhuma resposta final (de qualquer status) é aceita se a MESMA
  resposta do modelo também pediu `run_sql` ou `find_entities`. O PydanticAI 2 ('graceful')
  executa essas ferramentas (antes do final_answer, se vieram antes dele; depois, se vieram
  depois), mas o texto já estava escrito sem o resultado delas. A recusa vira `ModelRetry` e o
  modelo recebe, na requisição seguinte, os resultados e o motivo.
- Fundamentação: status `data_answer` exige uma consulta bem-sucedida cujo resultado o modelo já
  tenha recebido numa requisição ANTERIOR à da resposta (validador de saída; a violação vira
  `ModelRetry`, dentro do orçamento de retries e de requisições).
  Isso prova que a resposta veio depois de um resultado real, não que a consulta era a certa: a
  relevância do SQL é avaliada no M3, não aqui.
- Entidades não resolvidas (falha fechada, no mesmo validador): `data_answer` é recusado se uma
  consulta usou a chave de um candidato sem resolução única, a menos que o código prove a escolha
  (`runtime.candidate_uses`): sugestão fuzzy nunca; parcial ou homônimo só com ano ou id_filme
  escrito na pergunta original que identifique um candidato sozinho; homônimos também quando a
  resposta cobre todos eles. Pessoa por papel se resolve chamando `find_entities` com `role`.
- Laços limitados: `UsageLimits` (requisições = CINEDATA_REQUEST_LIMIT), retries por ferramenta e
  de saída, orçamento de consultas e de buscas por pergunta, SQL idêntico a um que falhou não é
  executado de novo e, depois de `MAX_SQL_TIMEOUTS` prazos estourados, nenhuma consulta roda mais.
- Formas diretas e comuns de ler o relógio do SQLite (`date('now')`, `date()`, `'subsec'`) são
  recusadas como proteção de reprodutibilidade. É um filtro de texto incompleto (`date('n' ||
  'ow')` passa) e não uma fronteira de segurança; a semântica da data de referência é garantida
  pelas instruções e pela avaliação (M3).
- Rastro (`RunTrace`) e avisos (truncamento, zero linhas, candidato escolhido) vêm do código; o
  modelo só escreve `AgentAnswer` (campos extras que ele mande são descartados).

As ferramentas são `async` e chamam o banco na própria thread do laço de eventos, de propósito: o
`SafeDatabase` converte Ctrl+C em `KeyboardInterrupt` na thread que o recebe (a principal), e uma
pergunta da CLI não tem concorrência a proteger. Em thread de trabalho, a consulta seguiria até o
prazo depois do Ctrl+C.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic_ai import (
    Agent,
    AgentRetries,
    ModelRetry,
    RunContext,
    ToolOutput,
    UsageLimits,
    capture_run_messages,
)
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model

from cinedata.config import Settings
from cinedata.db import (
    DatabaseUnavailableError,
    QueryRejectedError,
    QueryResult,
    QueryTimeoutError,
    SafeDatabase,
    SafeDatabaseError,
)
from cinedata.entities import EntityIndex, EntityIndexError, EntityMatch, MatchState
from cinedata.llm import open_openrouter_model, provider_failure, redact, request_limit_message
from cinedata.prompt import FINAL_TOOL, build_instructions, key_column
from cinedata.runtime import (
    AgentAnswer,
    AgentFailure,
    AgentOutcome,
    AnswerStatus,
    EntityLookup,
    FailureKind,
    ModelCall,
    OutputRejection,
    RunTrace,
    SqlExecution,
    SqlStatus,
    candidate_uses,
    compute_notices,
    describe_candidate,
)

MAX_QUESTION_CHARS = 2_000
MAX_SQL_CALLS = 6  # consultas por pergunta, contando as que falham
MAX_SQL_TIMEOUTS = 2  # depois disso nenhuma consulta roda (cada uma custa o prazo inteiro)
MAX_ENTITY_LOOKUPS = 8
MAX_TOOL_RESULT_CHARS = 24_000  # tamanho máximo das linhas enviadas ao modelo por consulta
TOOL_RETRIES = 2  # falhas SEGUIDAS da mesma ferramenta antes de encerrar a pergunta
OUTPUT_RETRIES = 2  # respostas finais inválidas ou sem fundamentação antes de encerrar
DATA_TOOLS = frozenset({"find_entities", "run_sql"})  # ferramentas cujo resultado precisa ser lido

EntityKindName = Literal["filme", "pessoa", "genero", "produtora"]
RoleName = Literal["Ator", "Diretor", "Roteirista"]

# Formas diretas de ler o relógio que o authorizer não enxerga (ele vê o nome da função, não o
# argumento; ver db.py): 'now', 'subsec' ou 'subsecond' como valor de tempo (1º argumento; 2º no
# strftime) ou função de data sem valor de tempo. Proteção de reprodutibilidade, não de segurança,
# e incompleta: um valor montado em tempo de execução, como 'n' || 'ow', não é detectado.
_CLOCK_VALUE = r"'(?:now|subsec|subsecond)'"
_CLOCK_READ = re.compile(
    rf"\b(?:date|time|datetime|julianday|unixepoch)\s*\(\s*{_CLOCK_VALUE}"
    rf"|\bstrftime\s*\(\s*'[^']*'\s*,\s*{_CLOCK_VALUE}"
    r"|\b(?:date|time|datetime|julianday|unixepoch)\s*\(\s*\)"
    r"|\bstrftime\s*\(\s*'[^']*'\s*\)",
    re.IGNORECASE,
)


class QuestionError(ValueError):
    """A pergunta é vazia, longa demais ou inválida (erro de uso, não de execução)."""


def validate_question(question: object) -> str:
    if not isinstance(question, str):
        raise QuestionError("A pergunta deve ser um texto.")
    text = question.strip()
    if not text:
        raise QuestionError("A pergunta está vazia.")
    if len(text) > MAX_QUESTION_CHARS:
        raise QuestionError(f"A pergunta passa de {MAX_QUESTION_CHARS} caracteres.")
    if "\x00" in text:
        raise QuestionError("A pergunta contém um caractere nulo.")
    return text


@dataclass
class AgentDeps:
    """Dependências de UMA pergunta. Nada aqui é visível ou configurável pelo modelo."""

    db: SafeDatabase
    entities: EntityIndex
    reference_date: date
    trace: RunTrace
    question: str  # a pergunta original: fonte dos qualificadores explícitos (ano, id_filme)
    interrupted: bool = False  # Ctrl+C já passou por uma ferramenta: as próximas não rodam
    failed_sql: dict[str, str] = field(default_factory=dict)  # SQL normalizado -> erro


# --- ferramentas -----------------------------------------------------------------------------


def _check_interrupted(deps: AgentDeps) -> None:
    if deps.interrupted:
        raise KeyboardInterrupt


async def find_entities(
    ctx: RunContext[AgentDeps],
    kind: EntityKindName,
    text: str,
    role: RoleName | None = None,
) -> dict[str, object]:
    """Procura um filme, pessoa, gênero ou produtora pelo nome e devolve as chaves sk_* candidatas.

    Só o estado exact_unique é uma resolução. Os demais trazem candidatos e uma orientação
    (guidance) que deve ser seguida.

    Args:
        kind: Tipo da entidade: filme, pessoa, genero ou produtora.
        text: O nome como aparece na pergunta, sem o ano nem outros qualificadores.
        role: Só para pessoa: o papel citado na pergunta (Ator, Diretor ou Roteirista).
    """
    deps = ctx.deps
    _check_interrupted(deps)
    trace = deps.trace
    if len(trace.entity_lookups) >= MAX_ENTITY_LOOKUPS:
        raise ModelRetry(
            f"Limite de {MAX_ENTITY_LOOKUPS} buscas de entidade por pergunta atingido. Responda "
            "com o que já tem ou peça esclarecimento."
        )
    if role is not None and kind != "pessoa":
        raise ModelRetry("role só se aplica a kind='pessoa'.")
    try:
        match = deps.entities.find(kind, text, role=role)
    except EntityIndexError as exc:
        trace.entity_lookups.append(
            EntityLookup(ctx.run_step, kind, text[:200], role, None, error=str(exc))
        )
        raise
    except KeyboardInterrupt:
        deps.interrupted = True
        raise
    trace.entity_lookups.append(
        EntityLookup(
            step=ctx.run_step,
            kind=kind,
            text=match.query,
            role=role,
            state=match.state,
            total_matches=match.total_matches,
            candidates=match.candidates,
            resolved_key=None if match.resolved is None else match.resolved.key,
        )
    )
    trace.tool_calls += 1
    return _entity_payload(kind, match)


def _entity_payload(kind: str, match: EntityMatch) -> dict[str, object]:
    def candidate(c: Any) -> dict[str, object]:
        data = {
            "key": c.key,
            "label": c.label,
            "year": c.year,
            "id_filme": c.movie_id,
            "role": c.role,
            "matched_via": c.matched_via,
            "edits": c.edits,
        }
        return {name: value for name, value in data.items() if value is not None}

    column = key_column(kind)
    count = match.total_matches
    if match.state is MatchState.EXACT_UNIQUE:
        guidance = f"Resolvido: filtre por {column} = '{match.candidates[0].key}'."
    elif match.state is MatchState.EXACT_MULTIPLE:
        guidance = (
            f"{count} entidades têm exatamente este nome. Não escolha sozinho: use um candidato "
            "só se o ano ou o id_filme escrito na pergunta identificar exatamente um, ou cubra "
            "todos; para pessoa, busque de novo com role. Senão responda com status "
            "clarification listando-os."
        )
    elif match.state is MatchState.PARTIAL_CANDIDATES:
        guidance = (
            "Nenhum nome exato; estes candidatos só contêm o texto buscado. Não use nenhum, a "
            "menos que o ano ou o id_filme escrito na pergunta identifique exatamente um; senão "
            "peça confirmação (status clarification)."
        )
    elif match.state is MatchState.FUZZY_SUGGESTIONS:
        guidance = (
            "Nenhum nome exato nem parcial; são sugestões por semelhança (possível erro de "
            "digitação). Nunca use uma sugestão sem confirmação do usuário: responda com status "
            "clarification perguntando qual delas ele quis."
        )
    else:
        reason = match.reason.value if match.reason else "no_match"
        guidance = f"Nada encontrado ({reason}). Não invente: diga que não encontrou."
        if match.detail:
            guidance += f" Detalhe: {match.detail}."
    payload: dict[str, object] = {
        "state": match.state.value,
        "query": match.query,
        "resolved": None if match.resolved is None else candidate(match.resolved),
        "candidates": [candidate(c) for c in match.candidates],
        "total_matches": count,
        "guidance": guidance,
    }
    if match.total_matches > len(match.candidates):
        payload["more_candidates_not_shown"] = match.total_matches - len(match.candidates)
    return payload


async def run_sql(ctx: RunContext[AgentDeps], sql: str) -> dict[str, object]:
    """Executa UMA consulta SQLite somente leitura (SELECT ou WITH ... SELECT) na camada Gold.

    O teto de linhas, o prazo e as permissões são fixos do sistema. Erros voltam como mensagem
    para você corrigir a consulta; não repita uma consulta que já falhou.

    Args:
        sql: A consulta completa, uma única instrução.
    """
    deps = ctx.deps
    _check_interrupted(deps)
    trace = deps.trace
    index = len(trace.sql_executions) + 1
    normalized = " ".join(sql.split())

    def record(status: SqlStatus, *, result: QueryResult | None = None, **extra: Any) -> None:
        if result is not None:
            extra |= {
                "columns": result.columns,
                "rows": result.rows,
                "truncated": result.truncated,
                "truncated_cells": result.truncated_cells,
                "elapsed_s": result.elapsed_s,
            }
        trace.sql_executions.append(SqlExecution(index, ctx.run_step, sql, status, **extra))

    def skip(reason: str) -> ModelRetry:
        record(SqlStatus.SKIPPED, error=reason)
        return ModelRetry(reason)

    executed = [e for e in trace.sql_executions if e.status is not SqlStatus.SKIPPED]
    if len(executed) >= MAX_SQL_CALLS:
        raise skip(
            f"Limite de {MAX_SQL_CALLS} consultas por pergunta atingido; nenhuma outra será "
            "executada. Responda com os resultados que já tem ou explique por que não foi possível."
        )
    if sum(e.status is SqlStatus.TIMEOUT for e in executed) >= MAX_SQL_TIMEOUTS:
        raise skip(
            f"{MAX_SQL_TIMEOUTS} consultas já passaram do prazo; nenhuma outra será executada. "
            "Explique ao usuário que a pergunta é cara demais para responder agora."
        )
    if normalized in deps.failed_sql:
        raise skip(
            "Esta mesma consulta já falhou e não será executada de novo: "
            f"{deps.failed_sql[normalized]} Mude a abordagem."
        )
    if _CLOCK_READ.search(sql):
        message = (
            "A consulta lê o relógio (como date('now'), date() ou 'subsec'), o que não é "
            f"permitido. Use a data de referência {deps.reference_date.isoformat()} como literal."
        )
        record(SqlStatus.REJECTED, error=message)
        deps.failed_sql[normalized] = message
        raise ModelRetry(message)

    started = time.perf_counter()
    try:
        result = deps.db.execute(sql)  # limites da configuração; nenhum override
    except KeyboardInterrupt:
        deps.interrupted = True
        raise
    except DatabaseUnavailableError as exc:
        record(SqlStatus.UNAVAILABLE, error=str(exc), elapsed_s=time.perf_counter() - started)
        raise
    except SafeDatabaseError as exc:
        if isinstance(exc, QueryTimeoutError):
            status = SqlStatus.TIMEOUT
        elif isinstance(exc, QueryRejectedError):
            status = SqlStatus.REJECTED
        else:
            status = SqlStatus.FAILED
        message = str(exc)
        record(status, error=message, elapsed_s=time.perf_counter() - started)
        deps.failed_sql[normalized] = message
        raise ModelRetry(message) from None

    payload, shown = _result_payload(result)
    record(SqlStatus.OK, result=result, rows_shown=shown)
    trace.tool_calls += 1
    return payload


def _json_cell(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSON não representa infinito
    return value


def _result_payload(result: QueryResult) -> tuple[dict[str, object], int]:
    rows: list[list[object]] = []
    size = 0
    for row in result.rows:
        cells = [_json_cell(value) for value in row]
        size += len(json.dumps(cells, ensure_ascii=False, default=str))
        if size > MAX_TOOL_RESULT_CHARS:
            break
        rows.append(cells)
    payload: dict[str, object] = {
        "columns": list(result.columns),
        "rows": rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "max_rows": result.max_rows,
        "elapsed_ms": round(result.elapsed_s * 1000),
    }
    notes: list[str] = []
    if result.truncated:
        notes.append(
            f"Havia mais de {result.max_rows} linhas e você vê só as {result.max_rows} "
            "primeiras: não conclua sobre o conjunto inteiro; agregue ou ordene com LIMIT."
        )
    if len(rows) < len(result.rows):
        payload["rows_shown"] = len(rows)
        notes.append(
            f"Só as {len(rows)} primeiras linhas couberam no limite de tamanho; selecione menos "
            "colunas ou textos menores."
        )
    if result.truncated_cells:
        payload["truncated_cells"] = result.truncated_cells
        notes.append(f"{result.truncated_cells} texto(s) longo(s) chegaram cortados.")
    if not result.rows:
        notes.append("A consulta rodou sem erro e não devolveu nenhuma linha.")
    if notes:
        payload["notes"] = notes
    return payload, len(rows)


# --- validação da resposta final -------------------------------------------------------------


def tools_sent_with_the_answer(ctx: RunContext[AgentDeps]) -> list[str]:
    """Ferramentas de dados pedidas na MESMA resposta do modelo que trouxe esta resposta final.

    O validador recebe o histórico até essa resposta (a última mensagem). A regra olha a própria
    resposta, não o número da requisição, então vale nas duas ordens em que o PydanticAI 2
    executa as ferramentas dela: antes do final_answer (se vieram antes) ou depois dele.
    """
    response = ctx.messages[-1] if ctx.messages else None
    calls = (
        [part.tool_name for part in response.parts if isinstance(part, ToolCallPart)]
        if isinstance(response, ModelResponse)
        else []
    )
    if FINAL_TOOL not in calls:
        # Falha fechada: sem ver a resposta que trouxe o final_answer, a regra não é conferível.
        raise RuntimeError("o validador de saída não encontrou a resposta com o final_answer")
    return sorted(set(calls) & DATA_TOOLS)


async def require_grounding(ctx: RunContext[AgentDeps], answer: AgentAnswer) -> AgentAnswer:
    """Resposta final sozinha na resposta do modelo; `data_answer` só com resultado já lido e
    sem entidade não resolvida escolhida sozinha."""
    trace = ctx.deps.trace
    if same := tools_sent_with_the_answer(ctx):
        reason = (
            f"Você chamou {' e '.join(same)} na mesma resposta do {FINAL_TOOL}: o texto foi "
            "escrito antes de você ler o resultado. Leia os resultados que voltaram nesta "
            f"mensagem e só então chame {FINAL_TOOL}, sozinho, numa resposta seguinte."
        )
        trace.output_rejections.append(OutputRejection(ctx.run_step, answer.status, reason))
        raise ModelRetry(reason)
    if answer.status is not AnswerStatus.DATA_ANSWER:
        return answer
    if not trace.has_result_before(ctx.run_step):
        reason = (
            "status data_answer exige que você já tenha lido o resultado de uma consulta "
            "run_sql bem-sucedida, numa resposta anterior a esta. Consulte o banco, leia o "
            "resultado e só então responda; ou use clarification, info ou out_of_scope se a "
            "resposta não depende de dados."
        )
    elif blocked := [u for u in candidate_uses(trace, ctx.deps.question) if not u.allowed]:
        use = blocked[0]
        used = "; ".join(describe_candidate(candidate) for candidate in use.used)
        options = "; ".join(describe_candidate(candidate) for candidate in use.lookup.candidates)
        reason = (
            f"A consulta #{use.queries[0]} usou {used} para '{use.lookup.text}' "
            f"({use.lookup.state.value}), mas {use.basis}. Não responda com dados escolhendo "
            "por conta própria: responda com status clarification perguntando qual destes o "
            f"usuário quis: {options}."
        )
    else:
        return answer
    trace.output_rejections.append(OutputRejection(ctx.run_step, answer.status, reason))
    raise ModelRetry(reason)


def _instructions(ctx: RunContext[AgentDeps]) -> str:
    return build_instructions(
        reference_date=ctx.deps.reference_date,
        max_rows=ctx.deps.db.max_rows,
        timeout_s=ctx.deps.db.timeout_s,
    )


def build_agent() -> Agent[AgentDeps, AgentAnswer]:
    """Um agente novo, sem modelo fixo: o modelo é escolhido a cada execução."""
    agent = Agent(
        deps_type=AgentDeps,
        output_type=ToolOutput(
            AgentAnswer,
            name=FINAL_TOOL,
            description="Entrega a resposta final ao usuário e encerra a pergunta.",
        ),
        instructions=_instructions,
        retries=AgentRetries(tools=TOOL_RETRIES, output=OUTPUT_RETRIES),
        name="cinedata",
    )
    agent.tool(find_entities)
    agent.tool(run_sql)
    agent.output_validator(require_grounding)
    return agent


# --- orquestração ----------------------------------------------------------------------------


def _record_messages(trace: RunTrace, messages: list[ModelMessage]) -> None:
    for message in messages:
        if isinstance(message, ModelResponse):
            trace.model_calls.append(
                ModelCall(
                    model_name=message.model_name or "?",
                    provider_name=message.provider_name,
                    finish_reason=message.finish_reason,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                )
            )
    trace.model_responses = len(trace.model_calls)


def _agent_failure(exc: BaseException, request_limit: int) -> AgentFailure | None:
    if (failure := provider_failure(exc)) is not None:
        return failure
    if isinstance(exc, UsageLimitExceeded):
        return AgentFailure(
            FailureKind.REQUEST_LIMIT, request_limit_message(request_limit, str(exc))
        )
    if isinstance(exc, UnexpectedModelBehavior):
        detail = str(exc)
        tool = detail.split("'")[1] if "exceeded max retries" in detail and "'" in detail else None
        if tool == FINAL_TOOL or "output retries" in detail:
            text = (
                "o modelo não entregou uma resposta final válida e fundamentada dentro das "
                f"{OUTPUT_RETRIES + 1} tentativas."
            )
        elif tool is not None:
            text = f"a ferramenta {tool} falhou {TOOL_RETRIES + 1} vezes seguidas e o agente parou."
        else:
            text = f"o modelo não seguiu o protocolo do agente ({redact(detail)})."
        return AgentFailure(FailureKind.AGENT_PROTOCOL, text)
    if isinstance(exc, SafeDatabaseError | EntityIndexError):
        return AgentFailure(FailureKind.DATABASE, f"o banco ficou indisponível: {exc}")
    return None


async def answer_question(
    question: str,
    *,
    db: SafeDatabase,
    model: Model,
    reference_date: date,
    request_limit: int,
    configured_models: tuple[str, ...] = (),
) -> AgentOutcome:
    """Executa UMA pergunta. Falhas conhecidas viram `AgentOutcome.failure`; Ctrl+C propaga."""
    text = validate_question(question)
    trace = RunTrace(
        reference_date=reference_date,
        max_rows=db.max_rows,
        request_limit=request_limit,
        configured_models=configured_models or (model.model_name,),
    )
    deps = AgentDeps(
        db=db,
        entities=EntityIndex(db),
        reference_date=reference_date,
        trace=trace,
        question=text,
    )
    limits = UsageLimits(
        request_limit=request_limit,
        tool_calls_limit=MAX_SQL_CALLS + MAX_ENTITY_LOOKUPS,
    )
    answer: AgentAnswer | None = None
    failure: AgentFailure | None = None
    started = time.perf_counter()
    with capture_run_messages() as messages:
        try:
            result = await build_agent().run(text, model=model, deps=deps, usage_limits=limits)
        except Exception as exc:
            failure = _agent_failure(exc, request_limit)
            if failure is None:
                raise
        else:
            answer = result.output
        finally:
            trace.elapsed_s = time.perf_counter() - started
            _record_messages(trace, messages)
    return AgentOutcome(text, answer, failure, trace, compute_notices(answer, trace, text))


@asynccontextmanager
async def _given(model: Model) -> AsyncIterator[Model]:
    yield model


async def ask_async(
    question: str, settings: Settings, *, model: Model | None = None
) -> AgentOutcome:
    """Pergunta completa: valida, abre o banco e o modelo, executa e fecha tudo.

    `model` substitui o OpenRouter (testes e avaliação offline). Levanta `QuestionError`,
    `ConfigError` (chave ou modelo ausentes) e `SafeDatabaseError` (banco não abre) ANTES de
    qualquer chamada ao modelo.
    """
    text = validate_question(question)
    if model is None:
        settings.require_api_key()
        configured = (settings.require_model(), *settings.fallback_models)
        opened: AbstractAsyncContextManager[Model] = open_openrouter_model(settings)
    else:
        opened = _given(model)
        configured = (model.model_name,)
    with SafeDatabase.from_settings(settings) as db:
        async with opened as active:
            return await answer_question(
                text,
                db=db,
                model=active,
                reference_date=settings.reference_date,
                request_limit=settings.request_limit,
                configured_models=configured,
            )


def ask(question: str, settings: Settings, *, model: Model | None = None) -> AgentOutcome:
    """Versão síncrona de `ask_async` (CLI e scripts)."""
    return run_blocking(ask_async(question, settings, model=model))


def run_blocking[T](coro: Coroutine[Any, Any, T]) -> T:
    """Roda `coro` num laço novo, sem o tratador de SIGINT do `asyncio.run`.

    `asyncio.run` transforma o primeiro Ctrl+C em cancelamento, que só chega no próximo `await`:
    uma consulta SQL em andamento iria até o prazo. Aqui o Ctrl+C levanta `KeyboardInterrupt` na
    hora (o `SafeDatabase` o repassa mesmo no meio de uma consulta); a tarefa é então cancelada e
    a limpeza (`async with`/`finally`, inclusive o fechamento do banco e do cliente HTTP) roda até
    o fim antes de o `KeyboardInterrupt` subir.
    """
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(coro)
        try:
            return loop.run_until_complete(task)
        except BaseException:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    loop.run_until_complete(task)
            raise
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


__all__ = [
    "MAX_ENTITY_LOOKUPS",
    "MAX_QUESTION_CHARS",
    "MAX_SQL_CALLS",
    "MAX_SQL_TIMEOUTS",
    "MAX_TOOL_RESULT_CHARS",
    "AgentDeps",
    "QuestionError",
    "answer_question",
    "ask",
    "ask_async",
    "build_agent",
    "find_entities",
    "require_grounding",
    "run_blocking",
    "run_sql",
    "tools_sent_with_the_answer",
    "validate_question",
]
