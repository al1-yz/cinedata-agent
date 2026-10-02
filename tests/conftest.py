"""Configuração global dos testes.

- Chamadas reais a modelos ficam bloqueadas (`ALLOW_MODEL_REQUESTS=False`): um teste que tente
  falar com um provider falha em vez de gastar cota.
- Cada teste roda numa pasta temporária e sem as variáveis OPENROUTER_API_KEY/CINEDATA_*, para
  não depender (nem expor) o `.env` ou o ambiente de quem desenvolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import models

from cinedata.config import ALL_ENV_VARS


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.chdir(tmp_path)
