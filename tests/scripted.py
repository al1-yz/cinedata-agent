"""Modelo roteirizado para testar a orquestração do agente sem rede.

Cada turno é a lista de partes que o "modelo" devolve naquela requisição, ou uma função que recebe
as mensagens até ali (para usar, por exemplo, a chave devolvida por `find_entities`). O roteiro
guarda o que o modelo viu em cada requisição (mensagens e `AgentInfo`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

Turn = Sequence[ModelResponsePart] | Callable[[list[ModelMessage]], Sequence[ModelResponsePart]]


class Script:
    def __init__(self, *turns: Turn, model_name: str = "roteiro") -> None:
        self.turns = list(turns)
        self.model_name = model_name
        self.seen: list[tuple[list[ModelMessage], AgentInfo]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append((list(messages), info))
        if not self.turns:
            raise AssertionError("o roteiro acabou: o agente pediu mais requisições que o previsto")
        turn = self.turns.pop(0)
        parts = turn(messages) if callable(turn) else turn
        return ModelResponse(parts=list(parts), model_name=self.model_name)

    @property
    def model(self) -> FunctionModel:
        return FunctionModel(self, model_name=self.model_name)

    @property
    def requests(self) -> int:
        return len(self.seen)


def sql(query: str) -> ToolCallPart:
    return ToolCallPart("run_sql", {"sql": query})


def find(kind: str, text: str, role: str | None = None) -> ToolCallPart:
    args: dict[str, Any] = {"kind": kind, "text": text}
    if role is not None:
        args["role"] = role
    return ToolCallPart("find_entities", args)


def final(status: str, answer: str = "Resposta.", **extra: Any) -> ToolCallPart:
    return ToolCallPart("final_answer", {"status": status, "answer": answer, **extra})


def last_request(messages: list[ModelMessage]) -> ModelRequest:
    request = messages[-1]
    assert isinstance(request, ModelRequest)
    return request


def returns(messages: list[ModelMessage], tool: str) -> list[Any]:
    """Conteúdo devolvido por `tool` na última requisição."""
    return [
        part.content
        for part in last_request(messages).parts
        if isinstance(part, ToolReturnPart) and part.tool_name == tool
    ]


def retries(messages: list[ModelMessage]) -> list[str]:
    """Mensagens de retry (texto) da última requisição."""
    return [
        part.content if isinstance(part.content, str) else str(part.content)
        for part in last_request(messages).parts
        if isinstance(part, RetryPromptPart)
    ]
