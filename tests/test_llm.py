"""Modelo do OpenRouter, fallback seletivo e falhas do provedor (offline).

Os fallbacks rodam no `FallbackModel` real da biblioteca, com a política
`is_transient_provider_error` e sub-modelos `FunctionModel` que levantam os erros do OpenRouter.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from openai._models import FinalRequestOptions
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from cinedata.agent import answer_question, ask_async, run_blocking
from cinedata.config import ConfigError, load_settings
from cinedata.db import SafeDatabase
from cinedata.llm import (
    MODEL_SETTINGS,
    OPENROUTER_BASE_URL,
    REQUEST_TIMEOUT_S,
    TRANSPORT_RETRIES,
    build_model,
    is_transient_provider_error,
    open_openrouter_model,
    provider_failure,
    redact,
)
from cinedata.runtime import AgentOutcome, FailureKind
from gold_db import GoldScenario
from scripted import Script, final, sql

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef"
REFERENCE = date(2026, 10, 1)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    scenario = GoldScenario()
    scenario.movie("Avatar", date="2009-12-18", receita=2900.0)
    return scenario.write(tmp_path_factory.mktemp("llm") / "gold.db")


@pytest.fixture
def db(db_path: Path) -> Iterator[SafeDatabase]:
    with SafeDatabase(db_path, max_rows=10, timeout_s=5.0) as database:
        yield database


def failing(exc: Exception, name: str) -> tuple[FunctionModel, list[int]]:
    calls: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        raise exc

    return FunctionModel(respond, model_name=name), calls


def run_with(db: SafeDatabase, model: FunctionModel | FallbackModel) -> AgentOutcome:
    return run_blocking(
        answer_question("pergunta", db=db, model=model, reference_date=REFERENCE, request_limit=5)
    )


def http(
    status: int, message: str = "detalhe do provedor", model: str = "principal/modelo"
) -> ModelHTTPError:
    return ModelHTTPError(status, model, body={"error": {"message": message}})


# --- política ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (http(408), True),
        (http(429), True),
        (http(500), True),
        (http(501), True),
        (http(502), True),
        (http(503), True),
        (http(504), True),
        (http(524), True),
        (http(530), True),
        (http(599), True),
        (ModelAPIError("m", "Connection error."), True),
        (http(400), False),
        (http(401), False),
        (http(402), False),
        (http(403), False),
        (http(404), False),
        (http(413), False),
        (http(422), False),
        (ValueError("qualquer"), False),
        (RuntimeError("qualquer"), False),
    ],
)
def test_only_transient_provider_failures_trigger_fallback(exc: Exception, transient: bool) -> None:
    assert is_transient_provider_error(exc) is transient


def test_rate_limited_primary_falls_back_and_the_trace_names_the_model_used(
    db: SafeDatabase,
) -> None:
    primary, calls = failing(http(429, "free-models-per-day"), "principal/modelo")
    backup = Script([sql("SELECT 1")], [final("data_answer", "1.")], model_name="reserva/modelo")
    outcome = run_with(
        db, FallbackModel(primary, backup.model, fallback_on=is_transient_provider_error)
    )

    assert outcome.ok
    assert len(calls) == 2  # o fallback é por requisição: o principal é tentado a cada uma
    assert outcome.trace.models_used == ("reserva/modelo",)
    assert outcome.trace.model_responses == 2


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, FailureKind.AUTH),
        (402, FailureKind.PAYMENT),
        (403, FailureKind.FORBIDDEN),
        (404, FailureKind.MODEL_UNAVAILABLE),
        (400, FailureKind.BAD_REQUEST),
    ],
)
def test_non_transient_failures_do_not_fall_back(
    db: SafeDatabase, status: int, kind: FailureKind
) -> None:
    primary, _ = failing(http(status), "principal/modelo")
    backup = Script(model_name="reserva/modelo")
    outcome = run_with(
        db, FallbackModel(primary, backup.model, fallback_on=is_transient_provider_error)
    )

    assert outcome.failure is not None and outcome.failure.kind is kind
    assert f"HTTP {status}" in outcome.failure.message
    assert backup.requests == 0
    assert outcome.answer is None


def test_when_every_model_fails_the_categories_are_preserved(db: SafeDatabase) -> None:
    primary, _ = failing(http(429, "limite diário"), "principal/modelo")
    backup, _ = failing(http(503, "sem provedor", "reserva/modelo"), "reserva/modelo")
    outcome = run_with(db, FallbackModel(primary, backup, fallback_on=is_transient_provider_error))

    failure = outcome.failure
    assert failure is not None and failure.kind is FailureKind.PROVIDER_UNAVAILABLE
    assert "principal/modelo (HTTP 429)" in failure.message and "limite diário" in failure.message
    assert "reserva/modelo (HTTP 503)" in failure.message


def test_same_category_on_every_model_keeps_that_category(db: SafeDatabase) -> None:
    primary, _ = failing(http(429), "principal/modelo")
    backup, _ = failing(http(429, model="reserva/modelo"), "reserva/modelo")
    outcome = run_with(db, FallbackModel(primary, backup, fallback_on=is_transient_provider_error))
    assert outcome.failure is not None and outcome.failure.kind is FailureKind.RATE_LIMITED


def test_connection_failure_falls_back(db: SafeDatabase) -> None:
    primary, _ = failing(ModelAPIError("principal/modelo", "Connection error."), "principal/modelo")
    backup = Script([final("info", "Ok.")], model_name="reserva/modelo")
    outcome = run_with(
        db, FallbackModel(primary, backup.model, fallback_on=is_transient_provider_error)
    )
    assert outcome.ok and outcome.trace.models_used == ("reserva/modelo",)


def test_single_model_failure_without_fallback_is_controlled(db: SafeDatabase) -> None:
    primary, _ = failing(
        ModelAPIError("principal/modelo", "Request timed out."), "principal/modelo"
    )
    outcome = run_with(db, primary)
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.PROVIDER_UNAVAILABLE
    assert "Request timed out." in outcome.failure.message


def test_unknown_errors_are_not_swallowed(db: SafeDatabase) -> None:
    primary, _ = failing(ZeroDivisionError("bug"), "principal/modelo")
    with pytest.raises(ZeroDivisionError):
        run_with(db, primary)


def test_secrets_never_reach_failure_messages() -> None:
    exc = ModelHTTPError(401, "m/x", body={"error": {"message": f"bad key {FAKE_KEY} here"}})
    failure = provider_failure(exc)
    assert failure is not None and FAKE_KEY not in failure.message
    assert "[removido]" in failure.message
    assert len(redact("x" * 5_000)) < 300


# --- construção do modelo --------------------------------------------------------------------


def test_build_model_uses_fallback_only_when_configured() -> None:
    provider = OpenRouterProvider(api_key=FAKE_KEY)
    single = build_model(["a/um"], provider)
    assert isinstance(single, OpenRouterModel) and single.model_name == "a/um"
    assert single.settings == MODEL_SETTINGS

    chained = build_model(["a/um", "b/dois", "c/tres"], provider)
    assert isinstance(chained, FallbackModel)
    assert [model.model_name for model in chained.models] == ["a/um", "b/dois", "c/tres"]
    with pytest.raises(ValueError):
        build_model([], provider)


def test_openrouter_client_is_explicit_and_bounded() -> None:
    settings = replace(
        load_settings(env={}), api_key=FAKE_KEY, model="a/um", fallback_models=("b/dois",)
    )

    async def inspect() -> tuple[object, ...]:
        async with open_openrouter_model(settings) as model:
            assert isinstance(model, FallbackModel)
            first = model.models[0]
            assert isinstance(first, OpenRouterModel)
            client = first.client
            assert all(m.client is client for m in model.models)  # type: ignore[attr-defined]
            return client.max_retries, client.timeout, str(client.base_url), client.api_key

    retries, timeout, base_url, key = run_blocking(inspect())
    assert (retries, timeout) == (TRANSPORT_RETRIES, REQUEST_TIMEOUT_S) == (0, 120.0)
    assert base_url.rstrip("/") == OPENROUTER_BASE_URL == OpenRouterProvider(api_key="x").base_url
    assert key == FAKE_KEY  # a chave vem das Settings, nunca de os.environ


def test_openai_environment_never_leaks_into_openrouter_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "org-vazado")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-vazado")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://outro-host.example/v1")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS", "Authorization: Bearer sk-openai-vazada\nX-Segredo: vazado"
    )
    settings = replace(load_settings(env={}), api_key=FAKE_KEY, model="a/um")

    async def wire_request() -> tuple[dict[str, str], str]:
        async with open_openrouter_model(settings) as model:
            assert isinstance(model, OpenRouterModel)
            options = FinalRequestOptions.construct(
                method="post", url="/chat/completions", json_data={}
            )
            request = model.client._build_request(options)  # o que iria pela rede
            return dict(request.headers), str(request.url)

    headers, url = run_blocking(wire_request())
    assert url == OPENROUTER_BASE_URL + "/chat/completions"
    assert headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "vazad" not in repr(headers)
    assert not {"openai-organization", "openai-project", "x-segredo"} & set(headers)


@pytest.mark.parametrize("missing", ["api_key", "model"])
def test_missing_key_or_model_fails_before_opening_anything(db_path: Path, missing: str) -> None:
    settings = replace(load_settings(env={}), api_key=FAKE_KEY, model="a/um", db_path=db_path)
    settings = replace(settings, **{missing: None})
    with pytest.raises(ConfigError):
        run_blocking(ask_async("pergunta", settings))


def test_the_production_model_path_is_blocked_offline(db_path: Path) -> None:
    # O caminho real (cliente do OpenRouter) é montado, mas ALLOW_MODEL_REQUESTS=False o barra
    # antes de qualquer requisição de rede.
    settings = replace(load_settings(env={}), api_key=FAKE_KEY, model="a/um", db_path=db_path)
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        run_blocking(ask_async("pergunta", settings))
