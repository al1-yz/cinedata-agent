"""Regras do CI offline (`.github/workflows/ci.yml`).

O CI nunca pode selecionar testes `llm`, usar segredos ou depender do banco real, e a matriz
cobre Ubuntu e Windows a partir do piso de Python do `pyproject.toml`. A coerência com o README
fica em `test_docs.py`.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def pytest_invocations(text: str) -> list[list[str]]:
    """Argumentos de cada linha que roda o pytest, sem comentários."""
    lines = re.findall(r"^[ \t]*(?:python -m )?pytest\b.*$", text, re.MULTILINE)
    invocations = []
    for line in lines:
        words = shlex.split(line, comments=True)
        invocations.append(words[words.index("pytest") + 1 :])
    return invocations


def matrix(text: str, key: str) -> list[str]:
    found = re.search(rf"^[ \t]*{key}:[ \t]*\[(.*)\][ \t]*$", text, re.MULTILINE)
    assert found, key
    return [item.strip().strip("\"'") for item in found.group(1).split(",")]


def test_ci_runs_pytest_without_a_mark_expression() -> None:
    # Sem -m na linha de comando vale o `-m 'not llm'` do addopts; qualquer -m o substituiria.
    invocations = pytest_invocations(workflow())
    assert invocations, "o CI precisa rodar o pytest"
    for args in invocations:
        assert not [arg for arg in args if arg.startswith("-m")], args
    assert "PYTEST_ADDOPTS" not in workflow()


def test_ci_has_no_secrets_live_calls_or_real_database() -> None:
    text = workflow()
    for forbidden in ("secrets.", "OPENROUTER_API_KEY", "--live", "cinerocket.db", ".env.example"):
        assert forbidden not in text, forbidden
    assert re.search(r"^permissions:\n[ \t]+contents: read$", text, re.MULTILINE)


def test_ci_matrix_covers_both_systems_from_the_python_floor() -> None:
    text = workflow()
    assert matrix(text, "os") == ["ubuntu-latest", "windows-latest"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    floor = project["requires-python"].replace(" ", "").removeprefix(">=")
    versions = matrix(text, "python-version")
    assert min(versions, key=lambda v: tuple(map(int, v.split(".")))) == floor
