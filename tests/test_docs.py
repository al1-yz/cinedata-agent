"""Os comandos e links da documentação.

Cada bloco de código marcado com um shell (`powershell`, `bat` ou `bash`) só pode ter a sintaxe
daquele shell e comandos de uma linha; os comandos `cinedata` e `python -m evals.run` precisam
existir; as quatro instalações do README (PowerShell, CMD, Git Bash e Linux/macOS) seguem a mesma
sequência; a matriz do CI anunciada em "Ambientes validados" é a do workflow; e todo link relativo
aponta para um arquivo e um título que existem.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from cinedata.cli import _build_parser
from evals.run import _parser as evals_parser

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "data" / "README.md",
    ROOT / "docs" / "TROUBLESHOOTING.md",
    ROOT / "evals" / "README.md",
    ROOT / "evals" / "RESULTS.md",
]
SHELLS = ("powershell", "bat", "bash")

_FENCE = re.compile(r"^[ ]*```(\w*)\n(.*?)^[ ]*```[ ]*$", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Sintaxe que denuncia um bloco no shell errado.
FOREIGN = {
    "powershell": ("source ", "export ", "/bin/", "activate.bat", "[ -f", "rm -rf"),
    "bat": ("source ", "export ", "$env:", "/bin/", "Activate.ps1", "Copy-Item", "Test-Path"),
    "bash": ("\\", "$env:", "Copy-Item", "Test-Path", "Activate.ps1", "activate.bat"),
}
CONTINUATION = {"powershell": "`", "bat": "^", "bash": "\\"}

INSTALL = 'python -m pip install -e ".[dev]" -c constraints.txt'
# (linguagem do bloco, criação da .venv, ativação), na ordem do README
INSTALLATIONS = [
    ("powershell", "py -m venv .venv", r".\.venv\Scripts\Activate.ps1"),
    ("bat", "py -m venv .venv", r".venv\Scripts\activate.bat"),
    ("bash", "py -m venv .venv", "source .venv/Scripts/activate"),
    ("bash", "python3 -m venv .venv", "source .venv/bin/activate"),
]


def blocks(path: Path) -> list[tuple[str, list[str]]]:
    text = path.read_text(encoding="utf-8")
    return [(lang, body.splitlines()) for lang, body in _FENCE.findall(text)]


def shell_lines() -> list[tuple[str, str, str]]:
    """(arquivo, shell, linha) de todos os blocos marcados com um shell."""
    return [
        (path.relative_to(ROOT).as_posix(), lang, line)
        for path in DOCS
        for lang, lines in blocks(path)
        if lang in SHELLS
        for line in lines
        if line.strip()
    ]


def slug(heading: str) -> str:
    """Âncora que o GitHub gera para um título."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    found = set()
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE):
        base = slug(heading)
        found.add(f"{base}-{seen[base]}" if base in seen else base)
        seen[base] = seen.get(base, 0) + 1
    return found


def test_every_doc_exists_and_has_shell_blocks() -> None:
    assert all(path.is_file() for path in DOCS)
    assert {lang for _, lang, _ in shell_lines()} == set(SHELLS)


def test_shell_blocks_use_only_their_own_syntax_and_single_lines() -> None:
    wrong = [
        (doc, lang, line)
        for doc, lang, line in shell_lines()
        if any(token in line for token in FOREIGN[lang])
        or line.rstrip().endswith(CONTINUATION[lang])
    ]
    assert wrong == []


def test_documented_commands_and_options_exist() -> None:
    cli = _build_parser()
    [commands] = [action for action in cli._actions if action.choices]
    known = {"--help", "--version", *commands.choices}
    ask_options = set(commands.choices["ask"]._option_string_actions)
    eval_options = set(evals_parser()._option_string_actions)
    checked = 0
    for doc, _, line in shell_lines():
        if line.startswith("cinedata "):
            words = shlex.split(line)
            assert words[1] in known, (doc, line)
            if words[1] == "ask":
                options = {word for word in words[2:] if word.startswith("-")}
                assert options <= ask_options, (doc, line)
            checked += 1
        elif line.startswith("python -m evals.run"):
            options = {word for word in shlex.split(line)[3:] if word.startswith("-")}
            assert options <= eval_options, (doc, line)
            checked += 1
    assert checked >= 10


def test_the_four_readme_installations_follow_the_same_steps() -> None:
    installs = [(lang, lines) for lang, lines in blocks(ROOT / "README.md") if INSTALL in lines]
    assert [(lang, lines[0], lines[1]) for lang, lines in installs] == INSTALLATIONS
    for _, lines in installs:
        # criar e ativar a .venv, instalar, criar o .env (só se faltar) e conferir
        assert len(lines) == 5 and lines[2] == INSTALL, lines
        assert ".env.example .env" in lines[3] and lines[4] == "cinedata doctor", lines
    troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    for _, _, activation in INSTALLATIONS:
        assert f"`{activation}`" in troubleshooting, activation


def test_readme_ci_row_matches_the_workflow_matrix() -> None:
    # "Ambientes validados" não pode anunciar uma versão ou um sistema que o CI não roda.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    [versions] = re.findall(r"^[ \t]*python-version:[ \t]*\[(.*)\]", workflow, re.MULTILINE)
    [systems] = re.findall(r"^[ \t]*os:[ \t]*\[(.*)\]", workflow, re.MULTILINE)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    [row] = [line for line in readme.splitlines() if line.startswith("| GitHub Actions")]
    assert re.findall(r"\b3\.\d+\b", row) == re.findall(r"3\.\d+", versions)
    assert re.findall(r"`([a-z]+-latest)`", row) == re.findall(r"[a-z]+-latest", systems)


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_relative_links_point_to_existing_files_and_headings(doc: Path) -> None:
    text = _FENCE.sub("", doc.read_text(encoding="utf-8"))
    for target in _LINK.findall(text):
        if re.match(r"[a-z]+:", target):
            continue
        path, _, anchor = target.partition("#")
        linked = (doc.parent / path).resolve() if path else doc
        assert linked.exists(), (doc.name, target)
        if anchor:
            assert anchor in anchors(linked), (doc.name, target)
