"""Configuração global dos testes.

- Chamadas reais a modelos ficam bloqueadas (`ALLOW_MODEL_REQUESTS=False`): um teste que tente
  falar com um provider falha em vez de gastar cota.
- Cada teste roda numa pasta temporária e sem as variáveis OPENROUTER_API_KEY/CINEDATA_*, para
  não depender (nem expor) o `.env` ou o ambiente de quem desenvolve.
- Rede bloqueada: resolver um nome fora do loopback falha. Toda conexão TCP do Python a um host
  por nome (sync ou asyncio, inclusive httpx) passa por `socket.getaddrinfo` antes de conectar.
  Só os testes marcados `llm` (fora do pytest padrão) ficam livres.
- Testes `llm` só rodam quando a expressão `-m` cita `llm`. Um `-m` na linha de comando substitui
  o `-m 'not llm'` do addopts; sem esta trava, `pytest -m "not realdb"` gastaria cota.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest
from pydantic_ai import models

from cinedata.config import ALL_ENV_VARS

_LLM_IN_EXPRESSION = re.compile(r"\bllm\b")


def llm_tests_requested(markexpr: str | None) -> bool:
    """True só quando a expressão `-m` cita o marcador `llm` (pedido explícito de gastar cota)."""
    return bool(markexpr and _LLM_IN_EXPRESSION.search(markexpr))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if llm_tests_requested(config.getoption("markexpr")):
        return
    skip = pytest.mark.skip(reason="LLM real, consome cota: rode só com -m llm")
    for item in items:
        if item.get_closest_marker("llm"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.chdir(tmp_path)


_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("llm"):
        return
    resolve = socket.getaddrinfo

    def guarded(host, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in _LOOPBACK:
            raise OSError(f"rede bloqueada nos testes: {name!r}")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
