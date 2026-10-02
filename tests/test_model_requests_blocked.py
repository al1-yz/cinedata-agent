"""Garante que nenhum teste consegue falar com um modelo real por acidente."""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, models
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider


def test_real_model_requests_are_blocked() -> None:
    # Chave fictícia: com ALLOW_MODEL_REQUESTS=False a requisição é recusada antes de sair.
    provider = OpenRouterProvider(api_key="not-a-real-key")
    agent = Agent(OpenRouterModel("provedor/modelo-de-teste", provider=provider))
    assert models.ALLOW_MODEL_REQUESTS is False
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        agent.run_sync("pergunta de teste")
