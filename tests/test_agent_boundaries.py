"""Fronteiras de arquitetura do M2, verificadas no código-fonte e em processo limpo.

- Só `db.py` importa `sqlite3`: todo acesso ao banco passa pelo `SafeDatabase`.
- O agente nunca usa os overrides por chamada do `SafeDatabase` (teto de linhas e prazo).
- O agente não depende dos casos de referência do M1c: não os importa, não os carrega em tempo de
  execução e o prompt não traz nem as perguntas oficiais nem trechos dos SQLs de gabarito.
- O prompt descreve exatamente a allowlist do `SafeDatabase`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

import pytest

from cinedata.db import GOLD_TABLES, HIDDEN_COLUMNS
from cinedata.prompt import COLUMN_NOTES, TABLE_NOTES, build_instructions, schema_section
from cinedata.reference import OFFICIAL_CASES

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "cinedata"
M2_MODULES = ("agent.py", "llm.py", "prompt.py", "runtime.py", "cli.py")
REFERENCE_NAMES = {
    "OFFICIAL_CASES",
    "ReferenceCase",
    "ReferenceRegistry",
    "official_registry",
    "run_case",
    "run_cases",
}


def parse(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"), filename=name)


def imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_only_the_safe_database_module_imports_sqlite3() -> None:
    offenders = [
        path.name
        for path in SRC.glob("*.py")
        if path.name != "db.py"
        and any(m.split(".")[0] == "sqlite3" for m in imported_modules(parse(path.name)))
    ]
    assert offenders == []


def test_agent_never_overrides_database_limits() -> None:
    calls = [
        node
        for node in ast.walk(parse("agent.py"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]
    assert calls, "o agente deveria executar SQL"
    assert all(not call.keywords and len(call.args) == 1 for call in calls)


@pytest.mark.parametrize("module", M2_MODULES)
def test_m2_modules_do_not_use_the_reference_cases(module: str) -> None:
    tree = parse(module)
    assert not any(name.startswith("cinedata.reference") for name in imported_modules(tree))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not names & REFERENCE_NAMES


def test_running_the_agent_does_not_load_the_reference_module() -> None:
    code = (
        "import sys, cinedata.agent, cinedata.cli, cinedata.llm, cinedata.prompt;"
        "cinedata.cli._build_parser();"
        "print(sorted(m for m in sys.modules if m.startswith('cinedata')))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    loaded = result.stdout
    assert "cinedata.agent" in loaded and "cinedata.reference" not in loaded


def test_prompt_contains_no_official_question_or_reference_sql() -> None:
    prompt = " ".join(
        build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30).split()
    ).lower()
    for case in OFFICIAL_CASES:
        for text in (case.question, *case.paraphrases):
            assert text.lower() not in prompt, case.case_id
        assert case.case_id not in prompt
        for line in case.sql.splitlines():
            fragment = " ".join(line.split()).lower()
            if len(fragment) >= 30:
                assert fragment not in prompt, (case.case_id, fragment)


def test_prompt_schema_is_exactly_the_safe_database_allowlist() -> None:
    assert set(TABLE_NOTES) == set(GOLD_TABLES)
    assert all(column in GOLD_TABLES[table] for table, column in COLUMN_NOTES)
    lines = schema_section().splitlines()
    listed: dict[str, list[str]] = {}
    for table in GOLD_TABLES:
        header = next(i for i, line in enumerate(lines) if line.startswith(f"- {table}:"))
        without_notes = re.sub(r" \([^)]*\)", "", lines[header + 1])
        listed[table] = without_notes.strip().split(", ")
    assert listed == {table: list(columns) for table, columns in GOLD_TABLES.items()}
    for table, column in HIDDEN_COLUMNS:
        assert column not in listed[table]
    prompt = build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30)
    assert "alembic_version" not in prompt and "sqlite_master" not in prompt


def test_prompt_row_rules_separate_open_rankings_from_exhaustive_breakdowns() -> None:
    prompt = " ".join(
        build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30).split()
    )
    assert (
        "Sem N na pergunta, use 10" not in prompt
    )  # a regra antiga cortava "por ano"/"por gênero"
    start = prompt.index("Quantas linhas devolver")
    rules = prompt[start : prompt.index("## ", start)]
    open_ranking, breakdown, superlative, top_n = rules.split(" - ")[1:5]
    assert "ranking ou lista aberta sem N na pergunta" in open_ranking
    assert "padrão de exibição de 10" in open_ranking and "assumptions" in open_ranking
    assert "detalhamento por grupo" in breakdown
    assert all(f'"{group}"' in breakdown for group in ("por ano", "por gênero", "por status"))
    assert "TODOS os grupos, até o teto de 50 linhas, sem LIMIT 10" in breakdown
    assert "10" not in breakdown.replace("LIMIT 10", "")  # nenhum corte em 10 no detalhamento
    assert "superlativo no singular" in superlative and "todos os empatados no topo" in superlative
    assert "todos os empatados na N-ésima posição, mesmo passando de N linhas" in top_n


def test_prompt_movie_ids_are_shown_only_when_title_and_year_collide() -> None:
    prompt = " ".join(
        build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30).split()
    )
    sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", prompt)]
    # mesmo título, anos diferentes: o ano sempre aparece, e ele basta
    assert any("mostre sempre o ano de lançamento" in s for s in sentences)
    showing_ids = [s for s in sentences if "id_filme" in s and "mostre" in s]
    assert len(showing_ids) == 2, showing_ids
    collision, unique = showing_ids
    # mesmo título E mesmo ano: o id_filme dessas linhas é obrigatório
    assert collision.startswith(
        "Se dois ou mais filmes devolvidos tiverem o mesmo título e o mesmo ano"
    )
    assert "mostre também o id_filme dessas linhas" in collision
    # título + ano únicos: nenhuma exigência geral de expor ids
    assert unique.startswith("Quando título e ano já distinguem os filmes")
    assert "não mostre o id_filme" in unique
    assert not any("sempre" in s and "id_filme" in s for s in sentences)


def test_prompt_final_answer_keeps_the_ranking_order_and_comes_alone() -> None:
    prompt = " ".join(
        build_instructions(reference_date=date(2026, 10, 1), max_rows=50, timeout_s=30).split()
    )
    assert "ORDER BY determinístico" in prompt  # a ordem do SQL...
    final = prompt[prompt.index("## Resposta final") :]
    ranking = next(s for s in final.split(" - ") if s.startswith("Ranking:"))
    # ...é a ordem que o texto apresenta (a mesma regra que a avaliação confere)
    assert "mesma ordem do resultado do SQL" in ranking
    assert "empatados podem vir em qualquer ordem entre si" in ranking
    assert "Não reordene o ranking no texto" in ranking
    assert any(s.startswith("Zero linhas: diga explicitamente") for s in final.split(" - "))
    steps = prompt[prompt.index("## Como trabalhar") : prompt.index("## Esquema")]
    assert "final_answer, sozinho, numa resposta própria" in steps
    assert "nunca na mesma resposta de um run_sql ou find_entities" in steps
    # o texto das respostas sem dados, que a avaliação confere, também está dito ao agente
    assert "fora do seu escopo (o catálogo de filmes), sem atender ao pedido" in steps
    assert "que tipos de pergunta sobre o catálogo você responde, sem citar números" in steps


def test_real_llm_tests_are_opt_in() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "-m 'not llm'" in config["tool"]["pytest"]["ini_options"]["addopts"]
    tree = ast.parse((ROOT / "tests" / "test_agent_llm.py").read_text(encoding="utf-8"))
    marks = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "pytestmark" for t in node.targets)
    ]
    assert marks and "llm" in ast.unparse(marks[0].value)
