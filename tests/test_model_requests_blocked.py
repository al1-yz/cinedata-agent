"""Garante que nenhum teste consegue falar com um modelo real (nem com a rede) por acidente."""

from __future__ import annotations

import socket

import pytest
from pydantic_ai import Agent, models
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from conftest import llm_tests_requested


def test_real_model_requests_are_blocked() -> None:
    # Chave fictícia: com ALLOW_MODEL_REQUESTS=False a requisição é recusada antes de sair.
    provider = OpenRouterProvider(api_key="not-a-real-key")
    agent = Agent(OpenRouterModel("provedor/modelo-de-teste", provider=provider))
    assert models.ALLOW_MODEL_REQUESTS is False
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        agent.run_sync("pergunta de teste")


def test_network_name_resolution_is_blocked() -> None:
    with pytest.raises(OSError, match="rede bloqueada"):
        socket.getaddrinfo("openrouter.ai", 443)
    assert socket.getaddrinfo("127.0.0.1", 80)  # o loopback continua (laço de eventos, sqlite)


@pytest.mark.parametrize(
    ("markexpr", "requested"),
    [
        (None, False),  # sem -m: vale o addopts, que já exclui `llm`
        ("not realdb", False),  # o -m da linha de comando substitui o `not llm` do addopts
        ("realdb", False),
        ("not realdb and not slow", False),
        ("llmish", False),  # só a palavra inteira conta
        ("llm", True),
        ("llm or realdb", True),
        ("not realdb and not llm", True),  # cita `llm`; a própria expressão os exclui
    ],
)
def test_real_llm_tests_need_an_explicit_llm_mark_expression(
    markexpr: str | None, requested: bool
) -> None:
    assert llm_tests_requested(markexpr) is requested


def test_collected_llm_tests_run_only_when_requested(request: pytest.FixtureRequest) -> None:
    # A trava age na coleta: um teste `llm` coletado só fica sem skip quando a expressão -m da
    # sessão cita `llm` (com `pytest -m "not realdb"`, os testes llm são coletados e pulados).
    requested = llm_tests_requested(request.config.getoption("markexpr"))
    for item in request.session.items:
        if item.get_closest_marker("llm"):
            skipped = any(mark.name == "skip" for mark in item.iter_markers())
            assert skipped is not requested, item.nodeid
