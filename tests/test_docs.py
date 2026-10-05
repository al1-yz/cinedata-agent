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
        assert lines[2:] == [INSTALL], lines  # criar a .venv, ativá-la e instalar
    troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    for _, _, activation in INSTALLATIONS:
        assert f"`{activation}`" in troubleshooting, activation


# --- primeiro uso: banco, chave do OpenRouter, .env, doctor e primeira pergunta ----------------


def section(path: Path, heading: str) -> str:
    """Texto de um título até o próximo título de nível igual ou maior."""
    text = path.read_text(encoding="utf-8")
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    following = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE)
    found = following.search(text, start + len(heading) + 1)
    return text[start : found.start() if found else len(text)]


def prose(text: str) -> str:
    """Texto corrido: sem as quebras de linha do Markdown nem o `> ` das citações."""
    return " ".join(re.sub(r"^> ?", "", line) for line in text.splitlines()).replace("  ", " ")


def table_rows(text: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("|---")
    ]


README = ROOT / "README.md"
TROUBLESHOOTING = ROOT / "docs" / "TROUBLESHOOTING.md"


def test_the_first_run_path_covers_every_step_in_order() -> None:
    install = section(README, "## Instalação")
    steps = [
        "python.org",  # 1. Python oficial
        "git clone https://github.com/",  # 2. repositório
        "py -m venv .venv",  # 3. ambiente virtual e dependências
        INSTALL,
        "`data/cinerocket.db`",  # 4. banco
        "openrouter.ai/keys",  # 5. a chave do próprio usuário
        "Copy-Item .env.example .env",  # 6. .env a partir do modelo
        "OPENROUTER_API_KEY=",
        "CINEDATA_MODEL=openrouter/free",
        "cinedata doctor",  # 7. conferência offline
        'cinedata ask "',  # 8. primeira pergunta real
    ]
    positions = [install.index(step) for step in steps]
    assert positions == sorted(positions), list(zip(steps, positions, strict=True))


def test_the_readme_says_the_api_key_is_the_users_own_and_lives_only_in_dotenv() -> None:
    key = prose(section(README, "### 5. Crie a sua chave do OpenRouter")).lower()
    for claim in (
        "sua própria chave",
        "não contém nem fornece a chave do autor",
        "mesmo com modelos gratuitos",
        "nunca a coloque no `.env.example`",
        "não é preciso comprar créditos",
        "não há garantia de disponibilidade",
    ):
        assert claim in key, claim
    rows = {
        row[0]: row for row in table_rows(section(README, "### 6. Crie o `.env` e coloque a chave"))
    }
    assert (
        "versionado" in rows["`.env.example`"][1]
        and "sem nenhum segredo" in rows["`.env.example`"][2]
    )
    assert "ignora" in rows["`.env`"][1] and "sua chave" in rows["`.env`"][2]


def test_dotenv_creation_never_overwrites_and_the_readme_excerpt_matches_the_template() -> None:
    # Um bloco por terminal, só com o comando que copia sem sobrescrever um `.env` existente.
    copies = {
        lang: lines
        for lang, lines in blocks(README)
        if any(".env.example .env" in line for line in lines)
    }
    assert copies == {
        "powershell": ["if (-not (Test-Path .env)) { Copy-Item .env.example .env }"],
        "bat": ["if not exist .env copy .env.example .env"],
        "bash": ["[ -f .env ] || cp .env.example .env"],
    }
    template = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    shown = [line for _, lines in blocks(README) for line in lines if re.match(r"[A-Z_]+=", line)]
    assert shown and set(shown) <= set(template), shown
    assert "CINEDATA_MODEL=openrouter/free" in shown and "OPENROUTER_API_KEY=" in shown


def test_the_readme_separates_offline_from_online_commands() -> None:
    rows = table_rows(section(README, "### Offline ou online"))[1:]
    online = [row[0] for row in rows if row[1].startswith("**sim")]
    offline = [row[0] for row in rows if row[1].startswith("não")]
    assert len(online) + len(offline) == len(rows)
    assert len(online) == 3  # só estes falam com o OpenRouter
    for marker, cell in zip(("cinedata ask", "--live", "-m llm"), online, strict=True):
        assert marker in cell, (marker, cell)
    for command in (
        "cinedata doctor",
        "pytest -q",
        "ruff check",
        "--tier smoke",
        "cinedata --help",
    ):
        assert any(command in cell for cell in offline), command


def test_the_readme_says_doctor_is_offline_and_cannot_authenticate_the_key() -> None:
    doctor = prose(section(README, "### 7. Confira com o `doctor`"))
    assert "**offline**" in doctor and "não fala com o OpenRouter" in doctor
    assert "presente" in doctor and "não que o OpenRouter a autenticou" in doctor
    rows = table_rows(section(README, "### 7. Confira com o `doctor`"))
    cannot = " ".join(row[1] for row in rows[1:]).lower()
    for limit in ("válida", "revogada", "aceita", "openrouter/free", "429"):
        assert limit in cannot, limit


def test_the_first_real_question_is_marked_as_online() -> None:
    first = prose(section(README, "### 8. Faça a primeira pergunta"))
    commands = [lines[0] for _, lines in blocks(README) if lines and lines[0] in first]
    assert commands[0].startswith('cinedata ask "'), commands
    assert "primeiro comando que fala com o OpenRouter" in first
    assert "401" in first and "429" in first


def test_the_readme_database_step_answers_the_first_run_questions() -> None:
    database = prose(section(README, "### 4. Coloque o banco de dados"))
    for fact in (
        "`cinerocket.db`",  # que arquivo
        "**`data/cinerocket.db`**",  # nome e lugar exatos
        "não está no repositório nem no git",  # fora do Git
        "`cinedata_db_path`",  # outro lugar
        "o `doctor` lista o banco como pendência",  # o que acontece sem ele
        "são pulados",  # o que ainda dá para testar
    ):
        assert fact in database.lower(), fact


def test_troubleshooting_covers_the_api_key_and_the_provider() -> None:
    targets = anchors(TROUBLESHOOTING)
    for anchor in (
        "chave-da-api-ausente",
        "chave-expirada-ou-inválida-http-401",
        "limite-de-uso-do-provedor-gratuito-http-429",
        "por-que-openrouterfree-usa-modelos-diferentes",
        "o-que-o-doctor-confere",
    ):
        assert anchor in targets, anchor
    unauthorized = prose(section(TROUBLESHOOTING, "## Chave expirada ou inválida (HTTP 401)"))
    assert "revogada" in unauthorized and "`doctor` é offline" in unauthorized
    limited = prose(section(TROUBLESHOOTING, "## Limite de uso do provedor gratuito (HTTP 429)"))
    assert "pode estar perfeitamente válida" in limited and "sem repetir" in limited
    router = prose(section(TROUBLESHOOTING, "## Por que `openrouter/free` usa modelos diferentes"))
    assert "`models_used`" in router and "modelo fixo" in router.lower()


def test_table_cells_never_hide_a_pipe_inside_code() -> None:
    # No GitHub, um | dentro de uma célula divide a coluna mesmo em `código`: o comando quebra.
    for path in DOCS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("|"):
                spans = re.findall(r"`[^`]*`", line)
                assert not [span for span in spans if "|" in span.replace("\\|", "")], line


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_no_example_fills_the_key_with_a_value_that_passes_doctor(doc: Path) -> None:
    # O doctor aceita qualquer valor com o prefixo sk-or-v1-; um exemplo assim, copiado ao pé da
    # letra, passaria no doctor e só falharia no primeiro ask (HTTP 401).
    text = doc.read_text(encoding="utf-8")
    assert not re.findall(r"OPENROUTER_API_KEY=\s*['\"]?sk-or-v1-", text)


SECRET = re.compile(r"sk-or-v1-[0-9a-f]{16,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{20,}")


@pytest.mark.parametrize(
    "path",
    [*DOCS, ROOT / ".env.example", ROOT / ".github" / "workflows" / "ci.yml"],
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_no_real_looking_secret_is_documented(path: Path) -> None:
    assert not SECRET.findall(path.read_text(encoding="utf-8"))


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
