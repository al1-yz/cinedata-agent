"""Corpus da avaliação M3: os casos, como o resultado de cada um é conferido e os tiers de execução.

Quatro categorias, com papéis diferentes:

- `official`: os 14 exemplos do enunciado ("Categorias de Perguntas e Exemplos (Não exaustivo)"),
  lidos do registro do M1c (`cinedata.reference.OFFICIAL_CASES`). Pergunta, semântica e SQL de
  gabarito vêm de lá, sem cópia. São o benchmark oficial MÍNIMO, não a lista do que o agente
  responde.
- `paraphrase`: variações de linguagem de alguns oficiais (sinônimo, registro informal, outra
  ordem, singular). Usam o gabarito do M1c, às vezes com outro N ou outra forma de resposta.
- `freeform`: perguntas analíticas novas, sem caso oficial correspondente. Provam que o agente
  gera SQL livre. O gabarito é um `ReferenceCase` deste módulo, executado pelo mesmo `run_case` do
  M1c e conferido contra cálculos independentes nos testes.
- `policy`: comportamento. Fora do escopo e ajuda: status certo, nenhuma ferramenta de dados e
  um texto mínimo (`TextPolicy`): a recusa diz que não responde ou que o escopo é o catálogo e
  não entrega o pedido; a ajuda diz o que o agente responde sobre o catálogo, sem afirmar dados.
  Título ambíguo: esclarecimento comprovado pela busca dos homônimos e pelos desambiguadores no
  texto, ou uma resposta completa conferida contra um gabarito só da avaliação.

Nada aqui é importado pelo agente: `src/cinedata` não depende de `evals` e o prompt não contém
nenhuma pergunta deste corpus (testes conferem as duas coisas).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from cinedata.db import SafeDatabase
from cinedata.reference import (
    OFFICIAL_CASES,
    ReferenceCase,
    ReferenceRegistry,
    ReferenceResult,
    official_registry,
    run_case,
)
from cinedata.runtime import AnswerStatus


class Category(StrEnum):
    OFFICIAL = "official"
    PARAPHRASE = "paraphrase"
    FREEFORM = "freeform"
    POLICY = "policy"


class Shape(StrEnum):
    SET = "set"  # todas as linhas, sem ordem (agregados por grupo e escalares)
    RANKED = "ranked"  # top-N ordenado pela primeira métrica, maior primeiro
    LEADERS = "leaders"  # todos os empatados no topo, primeiro


class TextPolicy(StrEnum):
    """Conferência mínima e determinística do texto de um caso de política sem dados."""

    REFUSAL = "refusal"  # fora do escopo: recusa ou limite de escopo, sem entregar o pedido
    CAPABILITIES = "capabilities"  # ajuda: o que o agente responde sobre o catálogo, sem dados


# --- métricas e tolerâncias ------------------------------------------------------------------
# Tolerância absoluta, na escala do gabarito. Ela cobre só o arredondamento de exibição que uma
# consulta correta pode fazer (ROUND(x, 2), percentuais), nunca uma semântica diferente. Contagens
# são exatas. A comparação soma só alguns ulps (`scoring.FLOAT_SLACK_ULPS`) para o erro de
# representação do ponto flutuante (100.01 - 100.0 > 0.01 em binário).

COUNT_TOLERANCE = 0.0
MONEY_TOLERANCE = 0.01  # um centavo: médias e somas de dinheiro com ou sem ROUND(..., 2)
DECIMAL_TOLERANCE = 0.005  # notas, divergências e popularidade exibidas com 2 casas
RATIO_TOLERANCE = 5e-5  # margens com 4 casas da razão (ou 2 casas em %)


class Quantity(StrEnum):
    """A grandeza de uma métrica: decide que rótulo escrito ao lado de um número no texto final
    é compatível com ela ("65 anos" não é uma contagem de filmes, "9%" não é uma nota)."""

    COUNT = "count"
    MONEY = "money"
    RATING = "rating"  # notas e médias de notas ("nota 6,7", "6,7/10")
    DECIMAL = "decimal"  # outras medidas decimais: divergência entre notas, popularidade
    RATIO = "ratio"  # margens: fração ou percentual


@dataclass(frozen=True)
class Metric:
    """Uma coluna numérica do gabarito que o resultado do agente precisa reproduzir."""

    column: str
    kind: Quantity
    tolerance: float
    scales: tuple[float, ...] = (1.0,)  # (1, 100): a razão vale como fração ou em %
    currency: str | None = None  # BRL ou USD: o texto final precisa indicar a moeda certa

    def __post_init__(self) -> None:
        if (self.kind is Quantity.MONEY) != (self.currency is not None):
            raise ValueError(f"{self.column}: só dinheiro tem moeda, e dinheiro sempre tem")
        if (self.kind is Quantity.RATIO) != (self.scales != (1.0,)):
            raise ValueError(f"{self.column}: só razões valem também em %")


_CURRENCY_SUFFIXES = (("_brl", "BRL"), ("_usd", "USD"))


def count(column: str) -> Metric:
    return Metric(column, Quantity.COUNT, COUNT_TOLERANCE)


def money(column: str) -> Metric:
    """Dinheiro: a moeda vem do sufixo da coluna (`_brl`, `_usd`); sem sufixo é erro."""
    for suffix, currency in _CURRENCY_SUFFIXES:
        if column.endswith(suffix):
            return Metric(column, Quantity.MONEY, MONEY_TOLERANCE, currency=currency)
    raise ValueError(f"coluna de dinheiro sem moeda no nome (_brl ou _usd): {column}")


def rating(column: str) -> Metric:
    """Nota ou média de notas: aceita no texto "nota 6,7" e "6,7/10"."""
    return Metric(column, Quantity.RATING, DECIMAL_TOLERANCE)


def decimal(column: str) -> Metric:
    """Divergência entre notas ou popularidade: uma nota escrita ("nota IMDb 9,8") não é uma
    divergência, mesmo quando o número coincide (média dos usuários 0)."""
    return Metric(column, Quantity.DECIMAL, DECIMAL_TOLERANCE)


def ratio(column: str) -> Metric:
    return Metric(column, Quantity.RATIO, RATIO_TOLERANCE, scales=(1.0, 100.0))


# Títulos se repetem: um filme se identifica pelo id_filme OU por título + ano, nunca pelo título.
# A última alternativa é a identidade legível que o texto da resposta precisa citar.
MOVIE = (("id_filme",), ("titulo", "ano_lancamento"))


@dataclass(frozen=True)
class ResultCheck:
    """Como comparar o resultado de uma consulta do agente com o gabarito.

    - `identity`: alternativas de colunas que identificam uma linha (basta uma); vazio = escalar.
      A última alternativa é o rótulo legível (`label`) conferido no texto da resposta.
    - `metrics`: colunas numéricas obrigatórias; a primeira define o ranking (maior primeiro) e é
      a métrica que o texto precisa citar.
    - `top_n`: obrigatório em `ranked`: o N avaliado, igual ao N do gabarito (o da pergunta, ou o
      padrão de exibição 10 do M1c quando ela não fixa N). Todos os empatados no corte contam.
    Colunas extras, aliases e a ordem das colunas no resultado do agente não importam.
    """

    shape: Shape
    identity: tuple[tuple[str, ...], ...]
    metrics: tuple[Metric, ...]
    top_n: int | None = None

    def __post_init__(self) -> None:
        if not self.metrics:
            raise ValueError("um ResultCheck precisa de pelo menos uma métrica")
        if (self.shape is Shape.RANKED) != (self.top_n is not None):
            raise ValueError("top_n é obrigatório em ranked e só vale para ranked")
        if self.top_n is not None and self.top_n < 1:
            raise ValueError("top_n precisa ser >= 1")
        if self.shape is not Shape.SET and not self.identity:
            raise ValueError("ranked e leaders precisam de colunas de identidade")
        if any("titulo" in alternative and "ano_lancamento" not in alternative
               for alternative in self.identity):  # fmt: skip
            raise ValueError("um filme não se identifica só pelo título: use título + ano")

    @property
    def label(self) -> tuple[str, ...]:
        """Identidade legível de uma linha no texto (título + ano, nome...); vazio = escalar."""
        return self.identity[-1] if self.identity else ()

    @property
    def columns(self) -> frozenset[str]:
        identity = {column for alternative in self.identity for column in alternative}
        return frozenset({*identity, *(m.column for m in self.metrics)})


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: Category
    question: str
    purpose: str  # o que o caso exercita
    expected_status: frozenset[AnswerStatus]
    reference: ReferenceCase | None = None  # gabarito do data_answer aceito (oráculo)
    check: ResultCheck | None = None
    # Título ambíguo: aceita clarification (com a busca dos homônimos e os desambiguadores de cada
    # um) ou um data_answer completo, conferido contra `reference` como qualquer caso de dados.
    ambiguous_title: str | None = None
    require_no_tools: bool = False  # política: responde sem find_entities nem run_sql
    text_policy: TextPolicy | None = None  # política: o que o texto precisa (e não pode) dizer
    # Recusa: padrões (regex, sem caixa) que entregariam o pedido fora do escopo ("30 °C").
    forbidden_answers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        data = AnswerStatus.DATA_ANSWER in self.expected_status
        if (self.reference is None) != (self.check is None):
            raise ValueError(f"{self.case_id}: reference e check andam juntos")
        if self.reference is not None:
            if not data:
                raise ValueError(f"{self.case_id}: caso com gabarito precisa aceitar data_answer")
            if len(self.expected_status) > 1 and self.ambiguous_title is None:
                raise ValueError(f"{self.case_id}: só o título ambíguo aceita outro status")
            missing = self.check.columns - set(self.reference.columns)
            if missing:
                raise ValueError(f"{self.case_id}: colunas fora do gabarito: {sorted(missing)}")
            if self.check.top_n is not None and self.check.top_n != self.reference.limit:
                raise ValueError(f"{self.case_id}: top_n difere do N do gabarito")
        elif data:
            raise ValueError(f"{self.case_id}: data_answer exige gabarito")
        if self.ambiguous_title is not None and (
            self.reference is None or AnswerStatus.CLARIFICATION not in self.expected_status
        ):
            raise ValueError(f"{self.case_id}: título ambíguo exige gabarito e clarification")
        if self.require_no_tools and data:
            raise ValueError(f"{self.case_id}: resposta sem ferramentas não pode ser data_answer")
        policy_status = {
            TextPolicy.REFUSAL: AnswerStatus.OUT_OF_SCOPE,
            TextPolicy.CAPABILITIES: AnswerStatus.INFO,
        }
        if self.text_policy is not None and (
            self.expected_status != {policy_status[self.text_policy]} or not self.require_no_tools
        ):
            raise ValueError(f"{self.case_id}: {self.text_policy} exige o status dela, sem dados")
        if self.forbidden_answers and self.text_policy is not TextPolicy.REFUSAL:
            raise ValueError(f"{self.case_id}: forbidden_answers só vale para a recusa")

    @property
    def is_data(self) -> bool:
        return self.reference is not None

    def fingerprint(self) -> str:
        """Muda quando a definição do caso muda (pergunta, gabarito ou regra de conferência)."""
        reference = self.reference
        data = {
            "category": self.category.value,
            "question": self.question,
            "expected_status": sorted(status.value for status in self.expected_status),
            "check": repr(self.check),
            "ambiguous_title": self.ambiguous_title,
            "require_no_tools": self.require_no_tools,
            "text_policy": None if self.text_policy is None else self.text_policy.value,
            "forbidden_answers": list(self.forbidden_answers),
            "reference": None
            if reference is None
            else {
                "case_id": reference.case_id,
                "sql": reference.sql,
                "columns": reference.columns,
                "limit": reference.limit,
                "window_years": reference.window_years,
            },
        }
        text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def expected_result(db: SafeDatabase, case: EvalCase, reference_date: date) -> ReferenceResult:
    """Gabarito do caso, calculado agora pelo `run_case` do M1c (nunca guardado no código)."""
    if case.reference is None:
        raise ValueError(f"{case.case_id} não tem gabarito")
    return run_case(db, case.reference, reference_date=reference_date)


# --- official: os 14 do M1c, só com a regra de conferência ----------------------------------
# Rankings sem N na pergunta (03, 05, 08, 13, 14) são avaliados no top 10: a convenção de exibição
# do M1c (ReferenceCase.limit), não uma exigência do enunciado.

OFFICIAL_CHECKS = MappingProxyType(
    {
        "oficial_01_maior_receita": ResultCheck(
            Shape.RANKED, MOVIE, (money("receita_brl"),), top_n=10
        ),
        "oficial_02_lucro_medio_por_genero": ResultCheck(
            Shape.SET, (("genero",),), (money("lucro_medio_brl"),)
        ),
        "oficial_03_maior_margem": ResultCheck(
            Shape.RANKED, MOVIE, (ratio("margem_lucro"),), top_n=10
        ),
        "oficial_04_mais_populares": ResultCheck(
            Shape.RANKED, MOVIE, (decimal("popularidade"),), top_n=5
        ),
        "oficial_05_divergencia_tmdb_imdb": ResultCheck(
            Shape.RANKED, MOVIE, (decimal("divergencia"),), top_n=10
        ),
        "oficial_06_nota_imdb_por_ano": ResultCheck(
            Shape.SET, (("ano_lancamento",),), (rating("nota_media_imdb"),)
        ),
        "oficial_07_ator_mais_ativo_5_anos": ResultCheck(
            Shape.LEADERS, (("ator",),), (count("filmes"),)
        ),
        "oficial_08_diretores_melhor_nota": ResultCheck(
            Shape.RANKED, (("diretor",),), (rating("media_imdb"),), top_n=10
        ),
        "oficial_09_par_ator_diretor": ResultCheck(
            Shape.LEADERS, (("ator", "diretor"),), (count("filmes_juntos"),)
        ),
        "oficial_10_filmes_por_genero": ResultCheck(Shape.SET, (("genero",),), (count("filmes"),)),
        "oficial_11_produtora_maior_lucro": ResultCheck(
            Shape.LEADERS, (("produtora",),), (money("lucro_total_brl"),)
        ),
        "oficial_12_genero_maior_margem": ResultCheck(
            Shape.LEADERS, (("genero",),), (ratio("margem_media"),)
        ),
        "oficial_13_mais_avaliados": ResultCheck(
            Shape.RANKED, MOVIE, (count("qtd_avaliacoes_usuarios"),), top_n=10
        ),
        "oficial_14_divergencia_usuarios_imdb": ResultCheck(
            Shape.RANKED, MOVIE, (decimal("divergencia"),), top_n=10
        ),
    }
)

_DATA = frozenset({AnswerStatus.DATA_ANSWER})


def _official(reference: ReferenceCase) -> EvalCase:
    return EvalCase(
        case_id=reference.case_id,
        category=Category.OFFICIAL,
        question=reference.question,
        purpose="exemplo oficial do enunciado (benchmark mínimo, não exaustivo)",
        expected_status=_DATA,
        reference=reference,
        check=OFFICIAL_CHECKS[reference.case_id],
    )


_OFFICIAL = MappingProxyType({case.case_id: case for case in OFFICIAL_CASES})

# --- paraphrase: mesma semântica do M1c, outra redação ---------------------------------------

PARAPHRASE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="parafrase_01_faturamento_em_reais",
        category=Category.PARAPHRASE,
        question="Quais os dez filmes que mais faturaram em reais?",
        purpose="sinônimo (faturar = receita), N por extenso, 'em reais' = BRL",
        expected_status=_DATA,
        reference=_OFFICIAL["oficial_01_maior_receita"],
        check=OFFICIAL_CHECKS["oficial_01_maior_receita"],
    ),
    EvalCase(
        case_id="parafrase_02_bilheteria_informal_top5",
        category=Category.PARAPHRASE,
        question="me mostra os 5 filmes com a maior bilheteria",
        purpose="sinônimo bilheteria, registro informal, sem moeda (BRL por padrão), N = 5",
        expected_status=_DATA,
        reference=replace(_OFFICIAL["oficial_01_maior_receita"], limit=5),
        check=replace(OFFICIAL_CHECKS["oficial_01_maior_receita"], top_n=5),
    ),
    EvalCase(
        case_id="parafrase_03_generos_informal",
        category=Category.PARAPHRASE,
        question="quantos filmes tem em cada genero?",
        purpose="registro informal, sem acentos",
        expected_status=_DATA,
        reference=_OFFICIAL["oficial_10_filmes_por_genero"],
        check=OFFICIAL_CHECKS["oficial_10_filmes_por_genero"],
    ),
    EvalCase(
        case_id="parafrase_04_lucro_ordem_invertida",
        category=Category.PARAPHRASE,
        question=(
            "Considerando só os filmes com receita informada, qual é o lucro médio de cada gênero?"
        ),
        purpose="ordem invertida: o filtro vem antes da métrica",
        expected_status=_DATA,
        reference=_OFFICIAL["oficial_02_lucro_medio_por_genero"],
        check=OFFICIAL_CHECKS["oficial_02_lucro_medio_por_genero"],
    ),
    EvalCase(
        case_id="parafrase_05_diretor_no_singular",
        category=Category.PARAPHRASE,
        question="Qual diretor, com no mínimo 5 filmes, tem a maior nota média?",
        purpose="singular: pede o líder (todos os empatados), não o ranking",
        expected_status=_DATA,
        reference=_OFFICIAL["oficial_08_diretores_melhor_nota"],
        check=ResultCheck(Shape.LEADERS, (("diretor",),), (rating("media_imdb"),)),
    ),
)

# --- freeform: perguntas novas, com gabarito próprio -----------------------------------------
# Entram no registro do M1c por `ReferenceRegistry.register`, sem mudar `cinedata.reference`.

FREEFORM_REFERENCES: tuple[ReferenceCase, ...] = (
    ReferenceCase(
        case_id="livre_01_top5_atores_terror",
        question="Quais são os 5 atores com mais filmes do gênero Terror?",
        semantics=(
            "Por linha de ator (tipo_pessoa = 'Ator'), filmes distintos ligados ao gênero Horror "
            "(Terror) pelas duas pontes; top 5 por RANK, empates no corte entram."
        ),
        sql="""
WITH terror AS (
    SELECT b.sk_movie_id
    FROM bridge_movie_genre AS b
    JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id
    WHERE g.nome_genero = 'Horror'
),
contagem AS (
    SELECT b.sk_person_id, COUNT(DISTINCT b.sk_movie_id) AS filmes
    FROM bridge_movie_person AS b
    WHERE b.sk_movie_id IN (SELECT sk_movie_id FROM terror)
    GROUP BY b.sk_person_id
),
ranking AS (
    SELECT c.sk_person_id, p.nome_pessoa AS ator, c.filmes,
           RANK() OVER (ORDER BY c.filmes DESC) AS posicao
    FROM contagem AS c
    JOIN dim_people AS p ON p.sk_person_id = c.sk_person_id
    WHERE p.tipo_pessoa = 'Ator'
)
SELECT posicao, ator, filmes
FROM ranking
WHERE posicao <= {limit}
ORDER BY posicao, ator, sk_person_id
""",
        columns=("posicao", "ator", "filmes"),
        key_columns=("ator",),
        limit=5,
    ),
    ReferenceCase(
        case_id="livre_02_filmes_dirigidos_nolan",
        question=(
            "Quais filmes dirigidos por Christopher Nolan estão no catálogo e qual é a nota IMDb "
            "de cada um?"
        ),
        semantics=(
            "Filmes ligados pela ponte à linha 'Christopher Nolan' com papel Diretor (filmes em "
            "que ele só aparece como Roteirista ficam de fora), com a nota_imdb literal."
        ),
        sql="""
SELECT m.id_filme, m.titulo, m.ano_lancamento, f.nota_imdb
FROM dim_movies AS m
JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id
WHERE m.sk_movie_id IN (
    SELECT b.sk_movie_id
    FROM bridge_movie_person AS b
    JOIN dim_people AS p ON p.sk_person_id = b.sk_person_id
    WHERE p.nome_pessoa = 'Christopher Nolan'
      AND p.tipo_pessoa = 'Diretor'
)
ORDER BY m.ano_lancamento, m.titulo, m.id_filme
""",
        columns=("id_filme", "titulo", "ano_lancamento", "nota_imdb"),
        key_columns=("id_filme",),
    ),
    ReferenceCase(
        case_id="livre_03_receita_usd_animacao_2019",
        question=(
            "Qual é a receita total, em dólares, dos filmes do gênero Animação com ano de "
            "lançamento 2019?"
        ),
        semantics=(
            "SUM(receita_usd) dos filmes com ano_lancamento = 2019 ligados ao gênero Animation "
            "pela ponte (cada filme uma vez); receitas nulas não somam."
        ),
        sql="""
SELECT ROUND(SUM(f.receita_usd), 2) AS receita_total_usd
FROM fact_movies_performance AS f
JOIN dim_movies AS m ON m.sk_movie_id = f.sk_movie_id
WHERE m.ano_lancamento = 2019
  AND f.sk_movie_id IN (
      SELECT b.sk_movie_id
      FROM bridge_movie_genre AS b
      JOIN dim_genres AS g ON g.sk_genre_id = b.sk_genre_id
      WHERE g.nome_genero = 'Animation'
  )
""",
        columns=("receita_total_usd",),
        key_columns=("receita_total_usd",),
    ),
    ReferenceCase(
        case_id="livre_04_filmes_ultimos_3_anos",
        question="Quantos filmes do catálogo têm data de lançamento nos últimos 3 anos?",
        semantics=(
            "Janela móvel de 3 anos pela data de referência, extremos inclusivos (2026-10-01 -> "
            "2023-10-01 a 2026-10-01), sobre data_lancamento; sem filtro de status."
        ),
        sql="""
SELECT COUNT(*) AS filmes
FROM dim_movies
WHERE data_lancamento BETWEEN {start_date} AND {end_date}
""",
        columns=("filmes",),
        key_columns=("filmes",),
        window_years=3,
    ),
)


def reference_registry() -> ReferenceRegistry:
    """Os 14 oficiais mais os gabaritos livres, num registro do M1c."""
    registry = official_registry()
    for reference in FREEFORM_REFERENCES:
        registry.register(reference)
    return registry


_FREE = MappingProxyType({reference.case_id: reference for reference in FREEFORM_REFERENCES})

FREEFORM_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="livre_01_top5_atores_terror",
        category=Category.FREEFORM,
        question=_FREE["livre_01_top5_atores_terror"].question,
        purpose="gênero em português, papel Ator, duas pontes N:N e ranking com empate",
        expected_status=_DATA,
        reference=_FREE["livre_01_top5_atores_terror"],
        check=ResultCheck(Shape.RANKED, (("ator",),), (count("filmes"),), top_n=5),
    ),
    EvalCase(
        case_id="livre_02_filmes_dirigidos_nolan",
        category=Category.FREEFORM,
        question=_FREE["livre_02_filmes_dirigidos_nolan"].question,
        purpose="pessoa por papel (find_entities com role): só os filmes que ele dirigiu",
        expected_status=_DATA,
        reference=_FREE["livre_02_filmes_dirigidos_nolan"],
        check=ResultCheck(Shape.SET, MOVIE, (rating("nota_imdb"),)),
    ),
    EvalCase(
        case_id="livre_03_receita_usd_animacao_2019",
        category=Category.FREEFORM,
        question=_FREE["livre_03_receita_usd_animacao_2019"].question,
        purpose="gênero em português, moeda USD e soma escalar",
        expected_status=_DATA,
        reference=_FREE["livre_03_receita_usd_animacao_2019"],
        check=ResultCheck(Shape.SET, (), (money("receita_total_usd"),)),
    ),
    EvalCase(
        case_id="livre_04_filmes_ultimos_3_anos",
        category=Category.FREEFORM,
        question=_FREE["livre_04_filmes_ultimos_3_anos"].question,
        purpose="janela móvel diferente do exemplo do prompt, pela data de referência",
        expected_status=_DATA,
        reference=_FREE["livre_04_filmes_ultimos_3_anos"],
        check=ResultCheck(Shape.SET, (), (count("filmes"),)),
    ),
)

# --- policy: comportamento esperado -----------------------------------------------------------
# O título ambíguo tem um gabarito só da avaliação para a resposta completa permitida (todos os
# homônimos com a nota); o agente nunca o vê.

_ELEMENTAL = ReferenceCase(
    case_id="politica_01_homonimos_elemental",
    question="Qual é a nota IMDb do filme Elemental?",
    semantics=(
        "Resposta completa aceita para o título ambíguo: todos os filmes com o título exato "
        "Elemental (os homônimos que find_entities encontra), com id_filme, título, ano e a "
        "nota_imdb literal."
    ),
    sql="""
SELECT m.id_filme, m.titulo, m.ano_lancamento, f.nota_imdb
FROM dim_movies AS m
JOIN fact_movies_performance AS f ON f.sk_movie_id = m.sk_movie_id
WHERE m.titulo = 'Elemental'
ORDER BY m.ano_lancamento, m.id_filme
""",
    columns=("id_filme", "titulo", "ano_lancamento", "nota_imdb"),
    key_columns=("id_filme",),
)

# A previsão do tempo entregue apesar da recusa: temperatura, chance de chuva ou previsão afirmada
# ("não sei se vai chover" não é uma previsão).
WEATHER_ANSWERS: tuple[str, ...] = (
    r"\d+(?:[.,]\d+)?\s*(?:°\s*[cf]?|º\s*[cf]\b|graus\b)",
    r"\d+(?:[.,]\d+)?\s*%\s*de\s+(?:chance|probabilidade)",
    r"(?:chance|probabilidade)\s+de\s+chuva\s+(?:é\s+)?(?:de\s+)?\d",
    r"\bm[áa]xima\s+(?:ser[áa]\s+|fica\s+)?(?:de\s+|em\s+)?\d|\bm[íi]nima\s+(?:ser[áa]\s+|fica\s+)?"
    r"(?:de\s+|em\s+)?\d",
    r"(?<!\bse )(?<!\bnão )(?<!\bnao )\b(?:vai|deve|irá|ira)\s+"
    r"(?:chover|garoar|nevar|esquentar|esfriar|fazer\s+(?:sol|calor|frio))",
    r"(?<!\bse )(?<!\bnão )(?<!\bnao )\b(?:far[áa]\s+(?:sol|calor|frio)|chover[áa]|garoar[áa]"
    r"|nevar[áa]|estar[áa]\s+(?:ensolarado|nublado|chuvoso|frio|quente))",
    r"\bprevis[ãa]o\s+(?:é|e|indica|aponta)\s+(?:de\s+)?(?:sol|chuva|tempo|c[ée]u|nublad|\d)",
)

POLICY_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="politica_01_titulo_ambiguo",
        category=Category.POLICY,
        question=_ELEMENTAL.question,
        purpose=(
            "homônimos: pedir esclarecimento com os desambiguadores de cada um, ou responder por "
            "completo cobrindo todos; nunca escolher um sozinho"
        ),
        expected_status=frozenset({AnswerStatus.CLARIFICATION, AnswerStatus.DATA_ANSWER}),
        reference=_ELEMENTAL,
        check=ResultCheck(Shape.SET, MOVIE, (rating("nota_imdb"),)),
        ambiguous_title="Elemental",
    ),
    EvalCase(
        case_id="politica_02_fora_do_escopo",
        category=Category.POLICY,
        question="Qual é a previsão do tempo para amanhã em São Paulo?",
        purpose=(
            "pedido fora do catálogo de filmes, sem consultar o banco: recusa ou limite de escopo, "
            "sem entregar a previsão"
        ),
        expected_status=frozenset({AnswerStatus.OUT_OF_SCOPE}),
        require_no_tools=True,
        text_policy=TextPolicy.REFUSAL,
        forbidden_answers=WEATHER_ANSWERS,
    ),
    EvalCase(
        case_id="politica_03_capacidades",
        category=Category.POLICY,
        question="O que você consegue responder sobre o catálogo de filmes?",
        purpose=(
            "pergunta sobre o próprio agente, sem consultar o banco: diz o que ele responde sobre "
            "o catálogo, sem afirmar dados"
        ),
        expected_status=frozenset({AnswerStatus.INFO}),
        require_no_tools=True,
        text_policy=TextPolicy.CAPABILITIES,
    ),
)

OFFICIAL_EVAL_CASES: tuple[EvalCase, ...] = tuple(_official(case) for case in OFFICIAL_CASES)
CORPUS: tuple[EvalCase, ...] = (
    *OFFICIAL_EVAL_CASES,
    *PARAPHRASE_CASES,
    *FREEFORM_CASES,
    *POLICY_CASES,
)
CASES_BY_ID = MappingProxyType({case.case_id: case for case in CORPUS})

# --- tiers -----------------------------------------------------------------------------------
# smoke: barato e variado — finanças com ranking e empate no corte, agregação por grupo, relação
# via ponte com entidade (pergunta livre) e ambiguidade.

SMOKE_IDS: tuple[str, ...] = (
    "oficial_03_maior_margem",
    "oficial_06_nota_imdb_por_ano",
    "livre_01_top5_atores_terror",
    "politica_01_titulo_ambiguo",
)

TIERS = MappingProxyType(
    {
        "smoke": SMOKE_IDS,
        "official": tuple(case.case_id for case in OFFICIAL_EVAL_CASES),
        "paraphrase": tuple(case.case_id for case in PARAPHRASE_CASES),
        "freeform": tuple(case.case_id for case in FREEFORM_CASES),
        "policy": tuple(case.case_id for case in POLICY_CASES),
        "full": tuple(case.case_id for case in CORPUS),
    }
)


def select_cases(
    tier: str = "smoke", ids: Sequence[str] = (), limit: int | None = None
) -> tuple[EvalCase, ...]:
    """Casos de um tier, ou os ids pedidos (na ordem dada), cortados em `limit`."""
    if ids:
        unknown = [case_id for case_id in ids if case_id not in CASES_BY_ID]
        if unknown:
            raise KeyError(f"caso(s) desconhecido(s): {', '.join(unknown)}")
        chosen = tuple(CASES_BY_ID[case_id] for case_id in dict.fromkeys(ids))
    else:
        if tier not in TIERS:
            raise KeyError(f"tier desconhecido: {tier!r} (use {', '.join(TIERS)})")
        chosen = tuple(CASES_BY_ID[case_id] for case_id in TIERS[tier])
    if limit is not None:
        if limit < 1:
            raise ValueError("limit deve ser >= 1")
        chosen = chosen[:limit]
    return chosen


__all__ = [
    "CASES_BY_ID",
    "CORPUS",
    "FREEFORM_CASES",
    "FREEFORM_REFERENCES",
    "MOVIE",
    "OFFICIAL_CHECKS",
    "OFFICIAL_EVAL_CASES",
    "PARAPHRASE_CASES",
    "POLICY_CASES",
    "SMOKE_IDS",
    "TIERS",
    "WEATHER_ANSWERS",
    "Category",
    "EvalCase",
    "Metric",
    "Quantity",
    "ResultCheck",
    "Shape",
    "TextPolicy",
    "count",
    "decimal",
    "expected_result",
    "money",
    "rating",
    "ratio",
    "reference_registry",
    "select_cases",
]
