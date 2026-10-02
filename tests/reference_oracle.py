"""Oráculo dos casos oficiais: a semântica aprovada reimplementada em Python, com aritmética exata.

Nada aqui reaproveita o SQL de `cinedata.reference`. Dinheiro vira `Decimal` a partir do valor
guardado (`repr` do REAL devolve o decimal original), notas viram decimais de 3 casas (`rating`),
margens e médias viram `Fraction`, e o ranking é a classificação de competição (1, 2, 2, 4) sobre o
valor exato. Assim, um empate aqui é um empate de verdade, sem ruído de ponto flutuante, e a
comparação com o SQL também confere a política de arredondamento dele.

`load_gold` lê o banco inteiro por `SafeDatabase` (leituras em massa só de teste), em pedaços na
ponte de pessoas para caber na memória do banco real.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

from cinedata.config import rolling_window
from cinedata.db import MAX_BULK_ROWS, SafeDatabase
from cinedata.reference import ReferenceCase

DIRETOR, ATOR = "Diretor", "Ator"
LANCADO = "Lançado"
MONEY_COLUMNS = frozenset({"lucro_medio_brl", "lucro_total_brl"})
_CHUNKS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f", "g"]


@dataclass(frozen=True)
class Movie:
    id_filme: str
    titulo: str
    data: str | None
    ano: int | None
    status: str | None


@dataclass(frozen=True)
class Perf:
    receita: Any
    orcamento: Any
    lucro: Any
    popularidade: Any
    nota_tmdb: Any
    qtd_tmdb: Any
    nota_imdb: Any


@dataclass
class GoldData:
    movies: dict[str, Movie] = field(default_factory=dict)
    perf: dict[str, Perf] = field(default_factory=dict)
    people: dict[str, tuple[str, str]] = field(default_factory=dict)  # sk -> (nome, papel)
    genres: dict[str, str] = field(default_factory=dict)
    companies: dict[str, str] = field(default_factory=dict)
    reviews: dict[str, tuple[int, Any]] = field(default_factory=dict)  # sk do filme -> (qtd, média)
    movie_people: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    movie_genres: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    movie_companies: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def _bulk(db: SafeDatabase, sql: str) -> tuple[tuple[object, ...], ...]:
    result = db.execute(sql, max_rows=MAX_BULK_ROWS, timeout_s=600)
    assert not result.truncated and not result.truncated_cells, sql
    return result.rows


def load_gold(db: SafeDatabase) -> GoldData:
    data = GoldData()
    for sk, id_filme, titulo, data_lanc, ano, status in _bulk(
        db,
        "SELECT sk_movie_id, id_filme, titulo, data_lancamento, ano_lancamento, status_filme"
        " FROM dim_movies",
    ):
        data.movies[sk] = Movie(id_filme, titulo, data_lanc, ano, status)
    for sk, *values in _bulk(
        db,
        "SELECT sk_movie_id, receita_brl, orcamento_brl, lucro_brl, popularidade, nota_tmdb,"
        " qtd_tmdb, nota_imdb FROM fact_movies_performance",
    ):
        data.perf[sk] = Perf(*values)
    for sk, name, role in _bulk(
        db, "SELECT sk_person_id, nome_pessoa, tipo_pessoa FROM dim_people"
    ):
        data.people[sk] = (name, role)
    data.genres = dict(_bulk(db, "SELECT sk_genre_id, nome_genero FROM dim_genres"))
    data.companies = dict(_bulk(db, "SELECT sk_company_id, nome_produtora FROM dim_companies"))
    for movie, qtd, media in _bulk(
        db, "SELECT sk_movie_id, qtd_avaliacoes_usuarios, nota_media_usuarios FROM dim_reviews"
    ):
        data.reviews[movie] = (qtd, media)
    for movie, genre in _bulk(db, "SELECT sk_movie_id, sk_genre_id FROM bridge_movie_genre"):
        data.movie_genres[movie].append(genre)
    for movie, company in _bulk(db, "SELECT sk_movie_id, sk_company_id FROM bridge_movie_company"):
        data.movie_companies[movie].append(company)
    canonical = {sk: sk for sk in data.people}  # um só objeto str por pessoa
    bounds = [None, *_CHUNKS, None]
    for low, high in zip(bounds, bounds[1:], strict=False):
        where = " AND ".join(
            part
            for part in (
                f"sk_movie_id >= '{low}'" if low else "",
                f"sk_movie_id < '{high}'" if high else "",
            )
            if part
        )
        for movie, person in _bulk(
            db,
            f"SELECT sk_movie_id, sk_person_id FROM bridge_movie_person WHERE {where}",  # noqa: S608
        ):
            data.movie_people[movie].append(canonical.get(person, person))
    return data


# --- aritmética exata e ranking -------------------------------------------------------------------


def dec(value: Any) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def rating(value: Any) -> Decimal:
    """Uma nota como decimal de até 3 casas, a precisão real das notas da Gold.

    Alguns REAL de `nota_tmdb` vêm com ruído binário do pipeline (6.903999999999999 no lugar de
    6.904); o valor pretendido é o de 3 casas.
    """
    return dec(value).quantize(Decimal("0.001"))


def margin(perf: Perf) -> Fraction | None:
    """(receita - orçamento) / receita, só com receita > 0 e orçamento informado."""
    if perf.receita is None or perf.orcamento is None or dec(perf.receita) <= 0:
        return None
    receita = Fraction(dec(perf.receita))
    return (receita - Fraction(dec(perf.orcamento))) / receita


def valid_imdb(perf: Perf | None) -> bool:
    return perf is not None and perf.nota_imdb is not None and perf.nota_imdb > 0


def valid_tmdb(perf: Perf) -> bool:
    if perf.nota_tmdb is None:
        return False
    return perf.nota_tmdb != 0 or (perf.qtd_tmdb is not None and perf.qtd_tmdb > 0)


def mean(values: Sequence[Any]) -> Fraction:
    return sum((Fraction(dec(v)) for v in values), Fraction(0)) / len(values)


def ranks(metrics: Sequence[Any]) -> list[int]:
    """Classificação de competição, maior primeiro: 1 + quantos valores são estritamente maiores."""
    ascending = sorted(metrics)
    return [1 + len(ascending) - bisect.bisect_right(ascending, m) for m in metrics]


def _top_movies(
    data: GoldData, items: list[tuple[str, Any, tuple[object, ...]]], limit: int
) -> list[tuple[object, ...]]:
    rows = []
    for rank, (sk, _, extra) in zip(ranks([m for _, m, _ in items]), items, strict=True):
        if rank <= limit and sk in data.movies:  # ranqueia antes de juntar com dim_movies
            movie = data.movies[sk]
            rows.append((rank, movie.id_filme, movie.titulo, movie.ano, *extra))
    return sorted(rows, key=lambda row: (row[0], row[2], row[1]))


def _leaders(
    entries: list[tuple[Any, tuple[object, ...], tuple[object, ...]]],
) -> list[tuple[object, ...]]:
    """`entries` = (métrica, linha, ordem). Devolve as linhas com a maior métrica, ordenadas."""
    if not entries:
        return []
    best = max(metric for metric, _, _ in entries)
    winners = [(order, row) for metric, row, order in entries if metric == best]
    return [row for _, row in sorted(winners)]


# --- os 14 casos ----------------------------------------------------------------------------------


def q01(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = [
        (sk, dec(p.receita), (p.receita,)) for sk, p in data.perf.items() if p.receita is not None
    ]
    return _top_movies(data, items, case.limit or 0)


def q02(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    profits: dict[str, list[Any]] = defaultdict(list)
    for movie, genres in data.movie_genres.items():
        perf = data.perf.get(movie)
        if perf is not None and perf.receita is not None:
            for genre in genres:
                profits[genre].append(perf.lucro)
    rows = [(data.genres[g], len(v), mean(v)) for g, v in profits.items() if g in data.genres]
    return sorted(rows, key=lambda row: row[0])


def q03(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = []
    for sk, perf in data.perf.items():
        value = margin(perf)
        if value is not None:
            items.append((sk, value, (perf.receita, perf.orcamento, value)))
    return _top_movies(data, items, case.limit or 0)


def q04(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = [
        (sk, dec(p.popularidade), (p.popularidade,))
        for sk, p in data.perf.items()
        if p.popularidade is not None
    ]
    return _top_movies(data, items, case.limit or 0)


def q05(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = []
    for sk, p in data.perf.items():
        if valid_imdb(p) and valid_tmdb(p):
            value = abs(rating(p.nota_tmdb) - rating(p.nota_imdb))
            items.append((sk, value, (p.nota_tmdb, p.qtd_tmdb, p.nota_imdb, value)))
    return _top_movies(data, items, case.limit or 0)


def q06(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    by_year: dict[Any, list[Any]] = defaultdict(list)
    for sk, perf in data.perf.items():
        if sk in data.movies and valid_imdb(perf):
            by_year[data.movies[sk].ano].append(rating(perf.nota_imdb))
    rows = [(year, len(v), mean(v)) for year, v in by_year.items()]
    return sorted(rows, key=lambda row: (row[0] is not None, row[0] or 0))


def q07(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    assert ref is not None and case.window_years is not None
    start, end = (d.isoformat() for d in rolling_window(ref, case.window_years))
    films: dict[str, set[str]] = defaultdict(set)
    for movie, people in data.movie_people.items():
        info = data.movies.get(movie)
        if info is None or info.status != LANCADO or info.data is None:
            continue
        if start <= info.data <= end:
            for person in people:
                if data.people[person][1] == ATOR:
                    films[person].add(movie)
    return _leaders(
        [(len(m), (data.people[p][0], len(m)), (data.people[p][0], p)) for p, m in films.items()]
    )


def q08(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    directed: dict[str, set[str]] = defaultdict(set)
    for movie, people in data.movie_people.items():
        for person in people:
            if data.people[person][1] == DIRETOR:
                directed[person].add(movie)
    candidates = []
    for person, movies in directed.items():
        rated = [rating(data.perf[m].nota_imdb) for m in movies if valid_imdb(data.perf.get(m))]
        if len(movies) >= 5 and rated:
            candidates.append((person, mean(rated), len(rated), len(movies)))
    rows = []
    for rank, (person, avg, n_rated, n_total) in zip(
        ranks([c[1] for c in candidates]), candidates, strict=True
    ):
        if rank <= (case.limit or 0):
            rows.append((rank, data.people[person][0], avg, n_rated, n_total, person))
    rows.sort(key=lambda row: (row[0], row[1], row[5]))
    return [row[:5] for row in rows]


def q09(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    pairs: Counter[tuple[str, str]] = Counter()
    for people in data.movie_people.values():
        directors = [p for p in set(people) if data.people[p][1] == DIRETOR]
        if not directors:
            continue
        actors = [p for p in set(people) if data.people[p][1] == ATOR]
        for actor in actors:
            for director in directors:
                pairs[(actor, director)] += 1
    return _leaders(
        [
            (
                count,
                (data.people[a][0], data.people[d][0], count),
                (data.people[a][0], data.people[d][0], a, d),
            )
            for (a, d), count in pairs.items()
        ]
    )


def q10(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    films: dict[str, set[str]] = {genre: set() for genre in data.genres}
    for movie, genres in data.movie_genres.items():
        for genre in genres:
            films.setdefault(genre, set()).add(movie)
    rows = [(data.genres[g], len(m), g) for g, m in films.items() if g in data.genres]
    rows.sort(key=lambda row: (-row[1], row[0], row[2]))
    return [row[:2] for row in rows]


def q11(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    counts: Counter[str] = Counter()
    for movie, companies in data.movie_companies.items():
        perf = data.perf.get(movie)
        if perf is None:
            continue
        for company in companies:
            counts[company] += 1
            if perf.lucro is not None:
                totals[company] += dec(perf.lucro)
    return _leaders(
        [
            (totals[c], (data.companies[c], totals[c], counts[c]), (data.companies[c], c))
            for c in counts
        ]
    )


def q12(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    margins: dict[str, list[Fraction]] = defaultdict(list)
    for movie, genres in data.movie_genres.items():
        perf = data.perf.get(movie)
        value = margin(perf) if perf is not None else None
        if value is not None:
            for genre in genres:
                margins[genre].append(value)
    entries = []
    for genre, values in margins.items():
        avg = sum(values, Fraction(0)) / len(values)
        entries.append((avg, (data.genres[genre], avg, len(values)), (data.genres[genre], genre)))
    return _leaders(entries)


def q13(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = [(sk, qtd, (qtd,)) for sk, (qtd, _) in data.reviews.items() if qtd > 0]
    return _top_movies(data, items, case.limit or 0)


def q14(data: GoldData, case: ReferenceCase, ref: date | None) -> list[tuple[object, ...]]:
    items = []
    for sk, (qtd, media) in data.reviews.items():
        perf = data.perf.get(sk)
        if media is not None and valid_imdb(perf):
            assert perf is not None
            value = abs(rating(media) - rating(perf.nota_imdb))
            items.append((sk, value, (media, qtd, perf.nota_imdb, value)))
    return _top_movies(data, items, case.limit or 0)


ORACLES: dict[str, Callable[[GoldData, ReferenceCase, date | None], list[tuple[object, ...]]]] = {
    "oficial_01_maior_receita": q01,
    "oficial_02_lucro_medio_por_genero": q02,
    "oficial_03_maior_margem": q03,
    "oficial_04_mais_populares": q04,
    "oficial_05_divergencia_tmdb_imdb": q05,
    "oficial_06_nota_imdb_por_ano": q06,
    "oficial_07_ator_mais_ativo_5_anos": q07,
    "oficial_08_diretores_melhor_nota": q08,
    "oficial_09_par_ator_diretor": q09,
    "oficial_10_filmes_por_genero": q10,
    "oficial_11_produtora_maior_lucro": q11,
    "oficial_12_genero_maior_margem": q12,
    "oficial_13_mais_avaliados": q13,
    "oficial_14_divergencia_usuarios_imdb": q14,
}
UNORDERED = frozenset({"oficial_02_lucro_medio_por_genero"})  # a ordem por média é só exibição


def assert_matches_oracle(
    case: ReferenceCase,
    actual: Sequence[tuple[object, ...]],
    expected: Sequence[tuple[object, ...]],
) -> None:
    """Mesmas linhas, na mesma ordem; valores exatos do oráculo contra os arredondados do SQL."""
    actual, expected = list(actual), list(expected)
    if case.case_id in UNORDERED:  # a primeira coluna é a chave
        actual.sort(key=lambda row: str(row[0]))
        expected.sort(key=lambda row: str(row[0]))
    assert len(actual) == len(expected), (case.case_id, actual, expected)
    for got, want in zip(actual, expected, strict=True):
        assert len(got) == len(case.columns) == len(want), (case.case_id, got, want)
        for column, value, exact in zip(case.columns, got, want, strict=True):
            if isinstance(exact, Decimal | Fraction):
                tolerance = 0.006 if column in MONEY_COLUMNS else 1e-8
                assert isinstance(value, int | float), (case.case_id, column, got)
                assert abs(float(exact) - value) <= tolerance, (case.case_id, column, got, want)
            else:
                assert value == exact and type(value) is type(exact), (case.case_id, got, want)
