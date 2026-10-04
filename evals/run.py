"""Executor da avaliação M3: plano (dry-run), execução controlada (--live), JSON e resumo.

    python -m evals.run --tier smoke                    # dry-run: plano e orçamento
    python -m evals.run --tier smoke --check-oracles    # dry-run + calcula os gabaritos no banco
    python -m evals.run --tier smoke --live             # executa no OpenRouter (CONSOME COTA)
    python -m evals.run --tier smoke --live --resume    # continua o mesmo arquivo

Regras:

- Sem `--live`, nada fala com o provedor: `ALLOW_MODEL_REQUESTS` é desligado e o agente nem é
  chamado. O plano mostra os casos e o teto de requisições antes de qualquer execução.
- Cada pergunta usa o agente de produção (`cinedata.agent.ask`) com a configuração normal:
  CINEDATA_REQUEST_LIMIT, fallback seletivo do M2 (ou `--primary-only`) e a data de referência,
  que precisa estar fixada em CINEDATA_REFERENCE_DATE para uma execução real.
- Sem retentativa automática: a primeira falha de provedor ou de banco (não avaliada), a primeira
  requisição recusada pelo provedor (400/413/422, avaliada como falha) ou um defeito do pontuador
  (não avaliado, com o desfecho já pago gravado) interrompe a execução. Com `--resume`, os casos
  já avaliados (pass/fail) com a mesma definição são pulados e os não avaliados rodam de novo.
- `--resume` só continua um arquivo com a mesma configuração (modelo, fallbacks, data de
  referência, limites), o mesmo código de avaliação, gabarito e executor, o mesmo código do
  agente, as mesmas versões de Python, SQLite, PydanticAI e cliente OpenAI e o mesmo conteúdo do
  banco (SHA-256); qualquer diferença é recusada, nunca misturada.
- Cada caso é gravado assim que termina (JSON bruto + resumo Markdown), sem a chave da API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sqlite3  # só para registrar a versão do SQLite; o banco é aberto pelo SafeDatabase
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import openai
import pydantic_ai
from pydantic_ai import models
from pydantic_ai.models import Model

from cinedata.agent import ask
from cinedata.cli import _prepare_output_streams
from cinedata.config import ENV_MODEL, ENV_REFERENCE_DATE, ConfigError, Settings, load_settings
from cinedata.db import SafeDatabase, SafeDatabaseError
from cinedata.reference import ReferenceCaseError, ReferenceResult
from cinedata.runtime import AgentOutcome, FailureKind
from evals.cases import CORPUS, TIERS, EvalCase, expected_result, select_cases
from evals.scoring import CaseScore, FailureCategory, Verdict, score_case

SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "evals" / "results" / "raw"  # ignorado pelo Git
_SRC = ROOT / "src" / "cinedata"
# Avaliação, gabarito e o próprio executor (que calcula o gabarito, pula casos e classifica).
EVALUATION_FILES = (
    ROOT / "evals" / "cases.py",
    ROOT / "evals" / "scoring.py",
    ROOT / "evals" / "run.py",
    _SRC / "reference.py",
)
AGENT_FILES = tuple(
    _SRC / name
    for name in (
        "agent.py",
        "prompt.py",
        "runtime.py",
        "llm.py",
        "entities.py",
        "db.py",
        "config.py",
    )
)
ORACLE_TIMEOUT_S = 120.0  # prazo do gabarito (o 07 chegou a 14 s com o cache frio)
STOP_CATEGORIES = frozenset({FailureCategory.PROVIDER, FailureCategory.DATABASE})
SCORING_ERROR = "scoring_error"  # o pontuador quebrou num desfecho já pago: não avaliado, para

EXIT_OK = 0  # todos os casos desta seleção passaram (ou dry-run)
EXIT_INCOMPLETE = 1  # alguma falha, caso não avaliado ou pendente
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")

ModelFor = Callable[[EvalCase], Model]


class UsageError(ValueError):
    """Uso inválido do executor (arquivo existente, configuração diferente no --resume...)."""


# --- configuração ----------------------------------------------------------------------------


def load_eval_settings(model: str | None = None, *, primary_only: bool = False) -> Settings:
    """Configuração do projeto (ambiente + .env), com o modelo e o fallback da linha de comando."""
    environment = dict(os.environ)
    if model:
        environment[ENV_MODEL] = model
    settings = load_settings(env=environment)
    return replace(settings, fallback_models=()) if primary_only else settings


def files_fingerprint(paths: Sequence[Path]) -> str:
    """SHA-256 do conteúdo dos arquivos, na ordem dada (CRLF e LF valem o mesmo)."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


_DATABASE_FINGERPRINTS: dict[tuple[tuple[str, int, int], ...], str] = {}


def database_fingerprint(path: Path) -> str | None:
    """SHA-256 em fluxo do banco e do `-wal` (que pode ter dados), calculado uma vez por processo.

    O cache é chaveado por caminho, tamanho e mtime de cada arquivo: mudou um, calcula de novo.
    """
    parts = [part for part in (path, path.with_name(path.name + "-wal")) if part.is_file()]
    if not parts or parts[0] != path:
        return None
    key = tuple(
        (str(part.resolve()), part.stat().st_size, part.stat().st_mtime_ns) for part in parts
    )
    if key not in _DATABASE_FINGERPRINTS:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(b"\0" + part.name.encode("utf-8") + b"\0")
            with part.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        _DATABASE_FINGERPRINTS[key] = digest.hexdigest()
    return _DATABASE_FINGERPRINTS[key]


def run_config(settings: Settings) -> dict[str, object]:
    """O que precisa ser igual para dois resultados poderem conviver no mesmo arquivo.

    Além da configuração do agente: o código da avaliação e do gabarito, o código do agente que
    muda o comportamento dele, as versões que também mudam (o laço de ferramentas do PydanticAI,
    o cliente HTTP, o SQLite) e o conteúdo do banco (não só o tamanho).
    """
    return {
        "schema": SCHEMA_VERSION,
        "evaluation": files_fingerprint(EVALUATION_FILES),
        "agent": files_fingerprint(AGENT_FILES),
        "environment": environment(),
        "database_sha256": database_fingerprint(settings.db_path_resolved),
        "model": settings.model,
        "fallback_models": list(settings.fallback_models),
        "reference_date": settings.reference_date.isoformat(),
        "request_limit": settings.request_limit,
        "max_rows": settings.max_rows,
        "sql_timeout_s": settings.sql_timeout_s,
    }


def environment() -> dict[str, str]:
    """Versões que mudam o comportamento da execução (o PydanticAI 2, por exemplo, mudou a
    estratégia padrão de ferramentas): condição do --resume e registradas em cada caso."""
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "pydantic_ai": pydantic_ai.__version__,
        "openai": openai.__version__,
    }


def default_output(label: str, settings: Settings) -> Path:
    slug = re.sub(r"[^A-Za-z0-9.-]+", "_", settings.model or "sem-modelo")
    return RESULTS_DIR / f"{label}-{slug}-{settings.reference_date.isoformat()}.json"


def budget(cases: int, settings: Settings) -> tuple[int, int]:
    """(requisições do agente, chamadas HTTP no pior caso com fallback) para `cases` perguntas."""
    requests = cases * settings.request_limit
    return requests, requests * (1 + len(settings.fallback_models))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- arquivo de resultados -------------------------------------------------------------------


def scrub(text: str, secret: str | None) -> str:
    """Tira a chave da API (e qualquer coisa com cara de chave) de um texto a gravar."""
    if secret:
        text = text.replace(secret, "[removido]")
    return _SECRET.sub("[removido]", text)


def _jsonable(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def is_final(record: dict[str, object] | None, case: EvalCase) -> bool:
    """Um registro avaliado (pass/fail) da MESMA definição do caso não precisa rodar de novo."""
    return (
        record is not None
        and record.get("fingerprint") == case.fingerprint()
        and record.get("verdict") in (Verdict.PASS.value, Verdict.FAIL.value)
    )


class ResultStore:
    """O JSON bruto de uma execução (e o resumo .md ao lado), gravado a cada caso."""

    def __init__(
        self,
        path: Path,
        config: dict[str, object],
        *,
        resume: bool = False,
        secret: str | None = None,
    ) -> None:
        self.path = path
        self.summary_path = path.with_suffix(".md")
        self._secret = secret
        if path.exists():
            if not resume:
                raise UsageError(
                    f"{path} já existe. Use --resume para continuar essa execução ou --out para "
                    "gravar em outro arquivo."
                )
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("config") != config:
                raise UsageError(_config_difference(data.get("config"), config, path))
            self.data = data
        else:
            self.data = {
                "schema": SCHEMA_VERSION,
                "config": config,
                "created_at": _now(),
                "updated_at": _now(),
                "planned": [],
                "results": {},
            }

    @property
    def results(self) -> dict[str, dict[str, object]]:
        return self.data["results"]

    def plan(self, case_ids: Sequence[str]) -> None:
        self.data["planned"] = list(dict.fromkeys([*self.data["planned"], *case_ids]))

    def save(self, record: dict[str, object] | None = None) -> None:
        if record is not None:
            self.results[str(record["case_id"])] = record
        self.data["updated_at"] = _now()
        text = json.dumps(self.data, ensure_ascii=False, indent=2, default=str)
        _write(self.path, scrub(text, self._secret))
        _write(self.summary_path, scrub(render_markdown(self.data, self.path.name), self._secret))


def _config_difference(old: object, new: dict[str, object], path: Path) -> str:
    old = old if isinstance(old, dict) else {}
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    details = ", ".join(f"{k}: {old.get(k)!r} -> {new.get(k)!r}" for k in changed)
    return (
        f"--resume recusado: {path.name} foi gerado com outra configuração ({details}). "
        "Resultados de configurações diferentes nunca são misturados; use --out para outro arquivo."
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _expected_dict(result: ReferenceResult) -> dict[str, object]:
    return {
        "reference_id": result.case_id,
        "parameters": dict(result.parameters),
        "columns": list(result.columns),
        "rows": [[_jsonable(cell) for cell in row] for row in result.rows],
    }


def build_record(
    case: EvalCase,
    score: CaseScore,
    *,
    started_at: str,
    outcome: AgentOutcome | None = None,
    expected: ReferenceResult | None = None,
    oracle_s: float = 0.0,
) -> dict[str, object]:
    record: dict[str, object] = {
        "case_id": case.case_id,
        "category": case.category.value,
        "purpose": case.purpose,
        "question": case.question,
        "expected_status": sorted(status.value for status in case.expected_status),
        "reference_id": None if case.reference is None else case.reference.case_id,
        "fingerprint": case.fingerprint(),
        "started_at": started_at,
        "environment": environment(),
        "oracle_s": round(oracle_s, 3),
        **score.to_dict(),
        "status": None,
        "elapsed_s": None,
        "usage": None,
        "grounding": None,
        "expected": None if expected is None else _expected_dict(expected),
        "outcome": None,
    }
    if outcome is not None:
        trace = outcome.trace
        record["status"] = None if outcome.answer is None else outcome.answer.status.value
        record["elapsed_s"] = round(trace.elapsed_s, 3)
        record["usage"] = {
            "configured_models": list(trace.configured_models),
            "models_used": list(trace.models_used),
            "model_responses": trace.model_responses,
            "tool_calls": trace.tool_calls,
            "input_tokens": trace.input_tokens,
            "output_tokens": trace.output_tokens,
        }
        record["grounding"] = {
            "successful_sql": len(trace.successful_sql()),
            "unsuccessful_sql": len(trace.sql_executions) - len(trace.successful_sql()),
            "entity_lookups": len(trace.entity_lookups),
            "output_rejections": len(trace.output_rejections),
        }
        record["outcome"] = outcome.to_dict()
    return record


# --- execução --------------------------------------------------------------------------------


@dataclass
class RunReport:
    executed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    stopped: str | None = None  # motivo da interrupção antecipada


def run_cases(
    cases: Sequence[EvalCase],
    settings: Settings,
    store: ResultStore,
    *,
    model_for: ModelFor | None = None,
    log: Callable[[str], None] = print,
) -> RunReport:
    """Executa os casos em ordem, gravando cada um. `model_for=None` usa o OpenRouter (cota!)."""
    report = RunReport()
    store.plan([case.case_id for case in cases])
    store.save()
    total = len(cases)
    with SafeDatabase(
        settings.db_path_resolved, max_rows=settings.max_rows, timeout_s=ORACLE_TIMEOUT_S
    ) as oracle:
        for position, case in enumerate(cases, 1):
            prefix = f"[{position}/{total}] {case.case_id}"
            if is_final(store.results.get(case.case_id), case):
                report.skipped.append(case.case_id)
                log(f"{prefix}: já avaliado neste arquivo, pulado")
                continue
            record = _evaluate(case, settings, oracle, model_for)
            store.save(record)
            report.executed.append(case.case_id)
            log(f"{prefix}: {_progress(record)}")
            if _stops_the_run(record):
                report.stopped = f"{case.case_id}: {record['reason']}"
                log(
                    "Execução interrompida (sem retentativa automática). Os casos restantes "
                    "ficam pendentes; use --resume para continuar."
                )
                break
    return report


def _stops_the_run(record: dict[str, object]) -> bool:
    """Provedor ou banco fora do ar (não avaliado), requisição recusada (avaliado como falha) ou
    pontuador quebrado: os próximos casos falhariam igual, então param aqui e ficam pendentes."""
    if record["verdict"] == Verdict.ERROR.value and record["failure_category"] in STOP_CATEGORIES:
        return True
    if record.get("detail") == SCORING_ERROR:
        return True
    failure = (record.get("outcome") or {}).get("failure") or {}
    return failure.get("kind") == FailureKind.BAD_REQUEST.value


def _evaluate(
    case: EvalCase, settings: Settings, oracle: SafeDatabase, model_for: ModelFor | None
) -> dict[str, object]:
    started_at = _now()
    begin = time.perf_counter()
    expected: ReferenceResult | None = None
    if case.is_data:
        try:  # gabarito antes do modelo: se ele falhar, nenhuma cota é gasta
            expected = expected_result(oracle, case, settings.reference_date)
        except (ReferenceCaseError, SafeDatabaseError) as exc:
            score = CaseScore(Verdict.ERROR, FailureCategory.HARNESS, f"gabarito: {exc}")
            return build_record(case, score, started_at=started_at)
    oracle_s = time.perf_counter() - begin
    try:
        outcome = ask(case.question, settings, model=None if model_for is None else model_for(case))
    except SafeDatabaseError as exc:
        score = CaseScore(Verdict.ERROR, FailureCategory.DATABASE, f"banco do agente: {exc}")
        return build_record(case, score, started_at=started_at, expected=expected)
    try:
        score = score_case(case, outcome, expected)
    except Exception as exc:  # defeito do pontuador: o desfecho já custou cota e fica gravado
        score = CaseScore(
            Verdict.ERROR,
            FailureCategory.HARNESS,
            f"pontuação: {type(exc).__name__}: {exc}",
            detail=SCORING_ERROR,
        )
    return build_record(
        case,
        score,
        started_at=started_at,
        outcome=outcome,
        expected=expected,
        oracle_s=oracle_s,
    )


def _progress(record: dict[str, object]) -> str:
    verdict = str(record["verdict"]).upper()
    category = record["failure_category"]
    text = verdict if category is None else f"{verdict} ({category})"
    usage = record.get("usage") or {}
    parts = [text, str(record["reason"])]
    if usage:
        parts.append(f"{usage['model_responses']} resposta(s) do modelo")
    if record.get("elapsed_s") is not None:
        parts.append(f"{record['elapsed_s']:.1f} s")
    return " · ".join(parts)


# --- resumo Markdown -------------------------------------------------------------------------


def _cell(value: object) -> str:
    text = "—" if value is None or value == "" else str(value)
    return " ".join(text.replace("|", "\\|").split())


def _short(text: str, size: int = 90) -> str:
    return text if len(text) <= size else text[: size - 3] + "..."


def render_markdown(data: dict[str, object], json_name: str = "") -> str:
    """Resumo conciso do arquivo bruto. Não avaliados nunca entram na taxa de acerto."""
    config = data["config"]
    results: dict[str, dict[str, object]] = data["results"]
    planned = list(data["planned"])
    order = planned + [case_id for case_id in results if case_id not in planned]
    records = [results[case_id] for case_id in order if case_id in results]
    pending = [case_id for case_id in planned if case_id not in results]
    verdicts = Counter(str(record["verdict"]) for record in records)
    evaluated = verdicts["pass"] + verdicts["fail"]
    used = sorted({m for r in records for m in ((r.get("usage") or {}).get("models_used") or [])})
    fallbacks = ", ".join(config["fallback_models"]) or "nenhum"
    lines = [
        "# Avaliação M3 — CineData Agent",
        "",
        f"- Atualizado em {data['updated_at']}" + (f" · bruto: `{json_name}`" if json_name else ""),
        f"- Modelo configurado: `{config['model']}` · fallbacks: {fallbacks} · modelos que "
        f"responderam: {', '.join(f'`{m}`' for m in used) or '—'}",
        f"- Data de referência: {config['reference_date']} · CINEDATA_REQUEST_LIMIT="
        f"{config['request_limit']} · CINEDATA_MAX_ROWS={config['max_rows']}",
        "",
        "## Placar",
        "",
        f"- Seleção: {len(planned)} de {len(CORPUS)} casos do corpus"
        + (" (a taxa vale só para estes casos)" if len(planned) < len(CORPUS) else ""),
        f"- Avaliados: {evaluated} (pass {verdicts['pass']}, fail {verdicts['fail']})",
        f"- Não avaliados (provedor, banco, gabarito ou pontuador): {verdicts['error']}, "
        "fora da taxa",
        f"- Pendentes (não executados): {len(pending)}",
        "- Taxa de acerto sobre os avaliados: "
        + (f"{verdicts['pass']}/{evaluated}" if evaluated else "— (nada avaliado)"),
        "",
        "| Categoria | pass | fail | não avaliado | pendente |",
        "|---|---|---|---|---|",
    ]
    categories = dict.fromkeys(
        [case.category.value for case in CORPUS] + [str(r["category"]) for r in records]
    )
    by_id = {case.case_id: case.category.value for case in CORPUS}
    for category in categories:
        mine = [r for r in records if r["category"] == category]
        waiting = [c for c in pending if by_id.get(c) == category]
        if not mine and not waiting:
            continue
        count = Counter(str(r["verdict"]) for r in mine)
        lines.append(
            f"| {category} | {count['pass']} | {count['fail']} | {count['error']} | "
            f"{len(waiting)} |"
        )
    lines += [
        "",
        "## Casos",
        "",
        "| Caso | Categoria | Esperado | Obtido | SQL | Texto | Veredito | Modelo(s) | Resp. | "
        "Tools | Tokens in/out | Tempo (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        usage = record.get("usage") or {}
        sql = record.get("sql_check")
        if sql and sql.get("chosen_query") is not None:
            sql_text = f"confere (#{sql['chosen_query']})"
        elif sql:
            sql_text = f"não confere ({record.get('detail')})"
        else:
            sql_text = None
        answer = record.get("answer_check") or {}
        verdict = str(record["verdict"])
        if record["failure_category"]:
            verdict += f" ({record['failure_category']})"
        tokens = f"{usage['input_tokens']}/{usage['output_tokens']}" if usage else None
        cells = [
            record["case_id"],
            record["category"],
            "/".join(record["expected_status"]),
            record.get("status"),
            sql_text,
            answer.get("verdict"),
            verdict,
            ", ".join(usage.get("models_used") or []) or None,
            usage.get("model_responses"),
            usage.get("tool_calls"),
            tokens,
            record.get("elapsed_s"),
        ]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    if pending:
        lines += ["", "Pendentes: " + ", ".join(f"`{case_id}`" for case_id in pending)]
    problems = [r for r in records if r["verdict"] != Verdict.PASS.value]
    if problems:
        lines += ["", "## Falhas e não avaliados", ""]
        for record in problems:
            lines.append(
                f"- `{record['case_id']}` ({record['failure_category']}): "
                f"{_cell(_short(str(record['reason']), 300))}"
            )
    noted = [r for r in records if r.get("notes")]
    if noted:
        lines += ["", "## Observações", ""]
        for record in noted:
            lines.append(f"- `{record['case_id']}`: {_cell('; '.join(record['notes']))}")
    lines += [
        "",
        "Critério: o status precisa ser o esperado; em perguntas de dados, ao menos uma consulta "
        "bem-sucedida, lida pelo modelo antes da resposta final, precisa reproduzir o gabarito "
        "(calculado na hora, por valor, com tolerâncias explícitas; rankings com todos os "
        "empatados no corte do top N) e o texto final precisa trazer cada linha exigida com a "
        "identidade e a métrica principal (rankings na ordem do gabarito), sem linhas inventadas "
        "nem listas ou tabelas que se contradigam; um resultado vazio precisa ser dito no texto. "
        "Fora do escopo e ajuda: sem ferramentas e com o texto mínimo da política. Falhas de "
        "provedor, banco, gabarito ou pontuador são 'não avaliado', nunca pass/fail.",
        "",
    ]
    return "\n".join(lines)


# --- linha de comando ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description=(
            "Avaliação M3 do CineData Agent. Sem --live é só um plano (dry-run): nenhuma "
            "chamada ao provedor é feita."
        ),
    )
    parser.add_argument("--tier", default="smoke", choices=list(TIERS), help="padrão: smoke")
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        default=[],
        metavar="CASO",
        help="roda só este caso (pode repetir; substitui o tier)",
    )
    parser.add_argument("--limit", type=int, help="só os N primeiros casos da seleção")
    parser.add_argument("--model", help="substitui CINEDATA_MODEL nesta execução")
    parser.add_argument(
        "--primary-only", action="store_true", help="descarta CINEDATA_FALLBACK_MODELS"
    )
    parser.add_argument("--out", type=Path, help="arquivo JSON de resultados (o .md vai ao lado)")
    parser.add_argument(
        "--resume", action="store_true", help="continua o arquivo, pulando os casos avaliados"
    )
    parser.add_argument(
        "--live", action="store_true", help="executa de verdade no provedor (CONSOME COTA)"
    )
    parser.add_argument(
        "--check-oracles",
        action="store_true",
        help="no dry-run, calcula os gabaritos no banco (sem provedor)",
    )
    return parser


def _existing(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def plan_text(
    cases: Sequence[EvalCase],
    settings: Settings,
    out: Path,
    *,
    label: str,
    live: bool,
    resume: bool,
) -> str:
    existing = _existing(out) if out.exists() else None
    skip: set[str] = set()
    lines = [
        f"Avaliação M3 · {label} · {len(cases)} caso(s) · "
        + ("EXECUÇÃO REAL (consome cota)" if live else "DRY-RUN (nada é enviado ao provedor)"),
        "",
    ]
    if existing is not None and resume:
        if existing.get("config") == run_config(settings):
            results = existing.get("results") or {}
            skip = {case.case_id for case in cases if is_final(results.get(case.case_id), case)}
    for number, case in enumerate(cases, 1):
        mark = "  (já avaliado, será pulado)" if case.case_id in skip else ""
        lines.append(f"  {number:>2}. {case.case_id} [{case.category.value}]{mark}")
        lines.append(f"      {_short(case.question, 96)}")
    to_run = len(cases) - len(skip)
    requests, http = budget(to_run, settings)
    fallbacks = ", ".join(settings.fallback_models) or "nenhum"
    origin = ENV_REFERENCE_DATE if settings.reference_date_from_env else "data de hoje (NÃO fixada)"
    lines += [
        "",
        f"  Modelo principal       {settings.model or '(não configurado)'}",
        f"  Fallbacks              {fallbacks}",
        f"  Chave da API           {'presente' if settings.api_key else 'ausente'}",
        f"  Data de referência     {settings.reference_date.isoformat()} ({origin})",
        f"  Requisições/pergunta   {settings.request_limit} (CINEDATA_REQUEST_LIMIT)",
        f"  Banco                  {settings.db_path_resolved}",
        f"  Resultados             {out} (+ .md)",
        "",
        f"Orçamento máximo: {to_run} pergunta(s) x {settings.request_limit} = {requests} "
        f"requisição(ões) do agente; com {len(settings.fallback_models)} fallback(s), até {http} "
        "chamada(s) HTTP ao provedor.",
    ]
    if existing is not None and not resume:
        lines.append(f"AVISO: {out.name} já existe; --live vai recusar sem --resume ou --out.")
    if existing is not None and resume and existing.get("config") != run_config(settings):
        lines.append(f"AVISO: {out.name} tem outra configuração; --resume será recusado.")
    if not settings.reference_date_from_env:
        lines.append(f"AVISO: defina {ENV_REFERENCE_DATE} (ex.: 2026-10-01); --live exige isso.")
    return "\n".join(lines)


def check_oracles(cases: Sequence[EvalCase], settings: Settings) -> int:
    """Calcula os gabaritos da seleção no banco, sem provedor. 1 se algum falhar."""
    failed = 0
    with SafeDatabase(
        settings.db_path_resolved, max_rows=settings.max_rows, timeout_s=ORACLE_TIMEOUT_S
    ) as db:
        for case in cases:
            if not case.is_data:
                print(f"  {case.case_id}: sem gabarito SQL (política)")
                continue
            try:
                result = expected_result(db, case, settings.reference_date)
            except (ReferenceCaseError, SafeDatabaseError) as exc:
                failed += 1
                print(f"  {case.case_id}: FALHOU: {exc}")
                continue
            print(f"  {case.case_id}: {len(result.rows)} linha(s), {result.elapsed_s:.2f} s")
    return EXIT_INCOMPLETE if failed else EXIT_OK


def main(argv: Sequence[str] | None = None, *, model_for: ModelFor | None = None) -> int:
    """Ponto de entrada. `model_for` substitui o OpenRouter (só testes)."""
    _prepare_output_streams()  # a mesma política de UTF-8 do `cinedata ask`
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_request:
        return exit_request.code if isinstance(exit_request.code, int) else EXIT_USAGE
    if not args.live:
        models.ALLOW_MODEL_REQUESTS = False  # o dry-run nunca fala com um provedor
    try:
        cases = select_cases(args.tier, args.ids, args.limit)
        settings = load_eval_settings(args.model, primary_only=args.primary_only)
    except (KeyError, ValueError, ConfigError) as exc:
        print(f"Erro: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return EXIT_USAGE
    label = "selecao" if args.ids else args.tier
    out = args.out or default_output(label, settings)
    print(plan_text(cases, settings, out, label=label, live=args.live, resume=args.resume))
    if not args.live:
        print("\nDry-run: nenhuma chamada ao provedor foi feita. Para executar, acrescente --live.")
        if args.check_oracles:
            print("\nGabaritos no banco (sem provedor):")
            try:
                return check_oracles(cases, settings)
            except SafeDatabaseError as exc:
                print(f"Banco de dados indisponível: {exc}", file=sys.stderr)
                return EXIT_INCOMPLETE
        return EXIT_OK

    try:
        if model_for is None:
            settings.require_api_key()
            settings.require_model()
        if not settings.reference_date_from_env:
            raise ConfigError(
                f"{ENV_REFERENCE_DATE} precisa estar definida para uma avaliação reproduzível."
            )
        store = ResultStore(out, run_config(settings), resume=args.resume, secret=settings.api_key)
    except (ConfigError, UsageError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return EXIT_USAGE

    pydantic_ai.BANNER_ENABLED = False
    print()
    try:
        report = run_cases(cases, settings, store, model_for=model_for)
    except KeyboardInterrupt:
        print(f"\nInterrompido. Casos já concluídos estão em {store.path}.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except SafeDatabaseError as exc:
        print(f"Banco de dados indisponível: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    print(f"\nResultados: {store.path}\nResumo:     {store.summary_path}")
    selected = [store.results.get(case.case_id) for case in cases]
    passed = all(r is not None and r["verdict"] == Verdict.PASS.value for r in selected)
    return EXIT_OK if passed and report.stopped is None else EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
