"""Contrato de seleção dos testes: sem intenção explícita, nenhum teste `llm` roda.

Cada caso roda uma sessão interna do pytest (pytester) com o `conftest.py` e as opções do pytest
do projeto sobre três testes falsos: um `llm`, um `realdb` sem banco e um comum. Nada aqui chama
um modelo nem usa rede. Comportamentos garantidos:

- `pytest -q`: o `-m 'not llm'` do addopts deixa o `llm` fora da seleção;
- `pytest -m "not realdb"`: o `-m` da linha de comando substitui o do addopts, e a trava do
  conftest pula o `llm`;
- sem `data/cinerocket.db`, o `realdb` é pulado, nunca falha;
- só uma expressão `-m` que cite `llm` libera esses testes.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[1]

FAKE_TESTS = """
from pathlib import Path

import pytest

REAL_DB = Path(__file__).parent / "data" / "cinerocket.db"


@pytest.fixture
def real_db():
    if not REAL_DB.is_file():
        pytest.skip("data/cinerocket.db não encontrado")
    return REAL_DB


@pytest.mark.llm
def test_falso_llm():
    pass


@pytest.mark.realdb
def test_falso_realdb(real_db):
    pass


def test_falso_offline():
    pass
"""

LLM_SKIPPED = "*SKIPPED*LLM real, consome cota*"
REALDB_SKIPPED = "*SKIPPED*cinerocket.db não encontrado*"


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    """Pasta com o conftest e as opções do pytest do projeto, mais os três testes falsos."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    pytester.makepyprojecttoml(
        "[tool.pytest.ini_options]\n"
        f"addopts = {json.dumps(options['addopts'])}\n"
        f"markers = {json.dumps(options['markers'])}\n"
    )
    pytester.makeconftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_falsos=FAKE_TESTS)
    return pytester


@pytest.mark.parametrize(
    ("args", "outcomes", "skip_reason"),
    [
        # pytest -q, como no CI: llm fora da seleção e realdb pulado sem o banco
        (("-q",), {"passed": 1, "skipped": 1, "deselected": 1}, REALDB_SKIPPED),
        # o -m substitui o addopts: o realdb sai da seleção e a trava do conftest pula o llm
        (("-m", "not realdb"), {"passed": 1, "skipped": 1, "deselected": 1}, LLM_SKIPPED),
        (("-m", "realdb"), {"skipped": 1, "deselected": 2}, REALDB_SKIPPED),
        # -k escolhe por nome e não substitui o -m do addopts
        (("-k", "llm"), {"deselected": 3}, None),
    ],
    ids=["pytest -q", "-m not realdb", "-m realdb", "-k llm"],
)
def test_llm_tests_never_run_without_an_explicit_llm_mark_expression(
    project: pytest.Pytester,
    args: tuple[str, ...],
    outcomes: dict[str, int],
    skip_reason: str | None,
) -> None:
    result = project.runpytest(*args)
    result.assert_outcomes(**outcomes)
    if skip_reason:
        result.stdout.fnmatch_lines([skip_reason])


@pytest.mark.parametrize(
    ("expression", "outcomes"),
    [
        ("llm", {"passed": 1, "deselected": 2}),
        ("llm or realdb", {"passed": 1, "skipped": 1, "deselected": 1}),
    ],
    ids=["-m llm", "-m llm or realdb"],
)
def test_only_an_explicit_llm_mark_expression_enables_them(
    project: pytest.Pytester, expression: str, outcomes: dict[str, int]
) -> None:
    result = project.runpytest("-m", expression, "-v")
    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines(["*test_falso_llm PASSED*"])
    result.stdout.no_fnmatch_line(LLM_SKIPPED)
