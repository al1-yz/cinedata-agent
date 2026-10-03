"""Modelo do OpenRouter, fallback seletivo e tradução das falhas do provedor.

- O cliente HTTP é montado aqui, com a chave passada explicitamente (nunca lida de outro lugar),
  prazo por requisição e SEM retentativas silenciosas do SDK (`max_retries=0`; o padrão do SDK é
  repetir até 2 vezes e esperar até 600 s). Assim cada chamada ao provedor ou é uma resposta
  contada pelo `UsageLimits` ou é uma falha que passa pela política abaixo.
- Fallback só em falha transitória: 408, 429 e 5xx (os códigos que o OpenRouter documenta como
  prazo, limite de uso e modelo/provedor fora do ar) e falhas sem status (conexão, prazo de
  leitura, resposta sem conteúdo). Chave inválida (401), falta de crédito (402), moderação (403),
  modelo inexistente (404) e requisição inválida (400/413/422) não trocam de modelo: o mesmo
  problema se repetiria ou é configuração. Erros de SQL, ambiguidade e violações de protocolo do
  agente nunca chegam aqui.
- O fallback é por requisição (`FallbackModel`): cada chamada tenta o principal primeiro. O
  rastro registra o modelo que de fato respondeu cada requisição.
- O SDK da OpenAI lê do ambiente OPENAI_ORG_ID, OPENAI_PROJECT_ID e OPENAI_CUSTOM_HEADERS (que
  pode trazer um `Authorization`) e os mandaria ao OpenRouter, inclusive no lugar da nossa chave.
  Os cabeçalhos explícitos têm prioridade sobre os do ambiente, e `Omit()` os remove: a requisição
  leva só a chave do OpenRouter.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

from openai import AsyncOpenAI, Omit
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from cinedata.config import ENV_FALLBACK_MODELS, ENV_MODEL, ENV_REQUEST_LIMIT, Settings
from cinedata.runtime import AgentFailure, FailureKind

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"  # o mesmo de OpenRouterProvider.base_url
REQUEST_TIMEOUT_S = 120.0  # por requisição ao modelo
TRANSPORT_RETRIES = 0
MODEL_SETTINGS = ModelSettings(temperature=0.0)
FALLBACK_STATUS = frozenset({408, 429})  # além de toda a faixa 5xx
_MAX_DETAIL_CHARS = 240
_SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def is_transient_provider_error(exc: Exception) -> bool:
    """True quando outro modelo configurado pode legitimamente ser tentado."""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in FALLBACK_STATUS or 500 <= exc.status_code < 600
    return isinstance(exc, ModelAPIError)  # sem status: conexão, prazo, corpo vazio ou ilegível


def build_model(model_ids: Sequence[str], provider: OpenRouterProvider) -> Model:
    """Modelo principal e, se houver, fallbacks na ordem dada, todos no mesmo provedor."""
    if not model_ids:
        raise ValueError("informe pelo menos um modelo")
    models = [
        OpenRouterModel(model, provider=provider, settings=MODEL_SETTINGS) for model in model_ids
    ]
    if len(models) == 1:
        return models[0]
    return FallbackModel(*models, fallback_on=is_transient_provider_error)


@asynccontextmanager
async def open_openrouter_model(settings: Settings) -> AsyncIterator[Model]:
    """Abre o cliente do OpenRouter para uma pergunta e o fecha ao sair.

    Levanta `ConfigError` se a chave ou o modelo não estiverem configurados.
    """
    api_key = settings.require_api_key()
    model_ids = (settings.require_model(), *settings.fallback_models)
    client = AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        max_retries=TRANSPORT_RETRIES,
        timeout=REQUEST_TIMEOUT_S,
        default_headers=_isolated_headers(api_key),  # type: ignore[arg-type]  # Omit remove
    )
    async with client:
        yield build_model(model_ids, OpenRouterProvider(openai_client=client))


def _isolated_headers(api_key: str) -> Mapping[str, str | Omit]:
    """Cabeçalhos que anulam os herdados do ambiente pelo SDK da OpenAI (ver o docstring)."""
    headers: dict[str, str | Omit] = {}
    for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines():
        name, separator, _ = line.partition(":")
        if separator and name.strip():
            headers[name.strip()] = Omit()
    headers["OpenAI-Organization"] = Omit()
    headers["OpenAI-Project"] = Omit()
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


# --- falhas do provedor ----------------------------------------------------------------------


def redact(text: str) -> str:
    """Remove qualquer coisa com cara de chave de API e encurta o texto."""
    cleaned = _SECRET.sub("[removido]", " ".join(text.split()))
    if len(cleaned) > _MAX_DETAIL_CHARS:
        cleaned = cleaned[:_MAX_DETAIL_CHARS] + "..."
    return cleaned


def _provider_detail(exc: ModelAPIError) -> str:
    body = exc.body if isinstance(exc, ModelHTTPError) else exc.message
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            body = error.get("message", error)
        else:
            body = body.get("message", body)
    return redact(str(body)) if body else ""


_HTTP_FAILURES: dict[int, tuple[FailureKind, str]] = {
    401: (FailureKind.AUTH, "o OpenRouter recusou a chave da API. Confira OPENROUTER_API_KEY."),
    402: (FailureKind.PAYMENT, "a conta ou a chave do OpenRouter está sem créditos."),
    403: (
        FailureKind.FORBIDDEN,
        "o OpenRouter recusou a requisição (moderação, guardrail ou permissão).",
    ),
    404: (
        FailureKind.MODEL_UNAVAILABLE,
        f"modelo não encontrado ou sem endpoint compatível. Confira {ENV_MODEL}: o modelo "
        "precisa existir no OpenRouter e suportar tool calling.",
    ),
    408: (FailureKind.PROVIDER_UNAVAILABLE, "o provedor não respondeu a tempo."),
    429: (
        FailureKind.RATE_LIMITED,
        f"limite de uso do provedor atingido. Aguarde e tente de novo, ou configure "
        f"{ENV_FALLBACK_MODELS}.",
    ),
}


def _single_failure(exc: ModelAPIError) -> AgentFailure:
    detail = _provider_detail(exc)
    if isinstance(exc, ModelHTTPError):
        status = exc.status_code
        kind, text = _HTTP_FAILURES.get(status, (None, None))
        if kind is None:
            if status in (400, 413, 422):
                kind, text = FailureKind.BAD_REQUEST, "o provedor rejeitou a requisição."
            elif status >= 500:
                kind, text = (
                    FailureKind.PROVIDER_UNAVAILABLE,
                    "o modelo ou o provedor está fora do ar.",
                )
            else:
                kind, text = FailureKind.PROVIDER_ERROR, "o provedor devolveu um erro."
        prefix = f"{exc.model_name} (HTTP {status})"
    else:
        kind = FailureKind.PROVIDER_UNAVAILABLE
        text = "falha de conexão, prazo esgotado ou resposta vazia do provedor."
        prefix = exc.model_name
    message = f"{prefix}: {text}"
    return AgentFailure(kind, f"{message} Detalhe: {detail}" if detail else message)


def provider_failure(exc: BaseException) -> AgentFailure | None:
    """Traduz falhas do provedor em `AgentFailure`; None se `exc` não for uma delas."""
    if isinstance(exc, FallbackExceptionGroup):
        failures = [
            _single_failure(item) if isinstance(item, ModelAPIError) else None
            for item in exc.exceptions
        ]
        known = [failure for failure in failures if failure is not None]
        if not known:
            return AgentFailure(
                FailureKind.PROVIDER_ERROR, "todos os modelos configurados falharam."
            )
        kinds = {failure.kind for failure in known}
        kind = kinds.pop() if len(kinds) == 1 else FailureKind.PROVIDER_UNAVAILABLE
        details = " | ".join(failure.message for failure in known)
        return AgentFailure(kind, f"todos os modelos configurados falharam. {details}")
    if isinstance(exc, ModelAPIError):
        return _single_failure(exc)
    return None


def request_limit_message(limit: int, detail: str) -> str:
    if "tool_calls_limit" in detail:
        return "o agente passou do limite de chamadas de ferramenta desta pergunta."
    return (
        f"o agente não concluiu a resposta dentro do limite de {limit} requisições ao modelo "
        f"({ENV_REQUEST_LIMIT}). Reformule a pergunta de forma mais direta ou aumente o limite."
    )


__all__ = [
    "MODEL_SETTINGS",
    "OPENROUTER_BASE_URL",
    "FALLBACK_STATUS",
    "REQUEST_TIMEOUT_S",
    "TRANSPORT_RETRIES",
    "build_model",
    "is_transient_provider_error",
    "open_openrouter_model",
    "provider_failure",
    "redact",
    "request_limit_message",
]
