"""Bancos sintéticos com a forma da Gold: o catálogo fixo do M1b e cenários livres do M1c."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterable
from pathlib import Path

GOLD_DDL = (
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
    "CREATE TABLE dim_genres (sk_genre_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_genero VARCHAR(50) NOT NULL UNIQUE)",
    "CREATE TABLE dim_companies (sk_company_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_produtora VARCHAR(255) NOT NULL)",
    "CREATE TABLE dim_people (sk_person_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " nome_pessoa VARCHAR(255), tipo_pessoa VARCHAR(20))",
    "CREATE TABLE dim_movies (sk_movie_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " id_filme VARCHAR(50), titulo VARCHAR(500), data_lancamento DATE, ano_lancamento INTEGER,"
    " duracao_minutos INTEGER, idioma_original VARCHAR(10), status_filme VARCHAR(50),"
    " sinopse VARCHAR(4000), url_poster VARCHAR(2048), url_backdrop VARCHAR(2048))",
    "CREATE TABLE fact_movies_performance (sk_movie_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " orcamento_usd NUMERIC(18, 2), receita_usd NUMERIC(18, 2), lucro_usd NUMERIC(18, 2),"
    " orcamento_brl NUMERIC(18, 2), receita_brl NUMERIC(18, 2), lucro_brl NUMERIC(18, 2),"
    " popularidade DOUBLE, nota_tmdb DOUBLE, qtd_tmdb INTEGER, nota_imdb DOUBLE,"
    " qtd_imdb INTEGER)",
    "CREATE TABLE dim_reviews (sk_review_id VARCHAR(64) NOT NULL PRIMARY KEY,"
    " sk_movie_id VARCHAR(64), qtd_avaliacoes_usuarios INTEGER, nota_media_usuarios DOUBLE)",
    "CREATE TABLE bridge_movie_company (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_company_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_company_id))",
    "CREATE TABLE bridge_movie_genre (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_genre_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_genre_id))",
    "CREATE TABLE bridge_movie_person (sk_movie_id VARCHAR(64) NOT NULL,"
    " sk_person_id VARCHAR(64) NOT NULL, PRIMARY KEY (sk_movie_id, sk_person_id))",
    "CREATE TABLE movie_reviews (id INTEGER NOT NULL PRIMARY KEY, sk_movie_review_id VARCHAR(64),"
    " sk_movie_id VARCHAR(64), name VARCHAR(120), rating DOUBLE, text VARCHAR(4000),"
    " created_at DATETIME)",
)

# Os 19 gêneros reais do banco (note o "Tv Movie").
REAL_GENRES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family",
    "Fantasy", "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction",
    "Thriller", "Tv Movie", "War", "Western",
]  # fmt: skip
DEFAULT_GENRES = [(f"g{number:02d}", name) for number, name in enumerate(REAL_GENRES, 1)]

DEFAULT_COMPANIES = [
    ("c01", "Studio Ghibli"),
    ("c02", "Café Filmes"),
    ("c03", "CAFE FILMES"),
    ("c04", "Émile 🎬 Productions"),
    ("c05", "Warner Bros."),
    ("c06", "Warner Bros"),
    ("c07", "Pixar"),
]

# (sk, nome, papel). Cada papel é uma linha própria, como no banco real.
DEFAULT_PEOPLE = [
    ("p01", "Christopher Nolan", "Diretor"),
    ("p02", "Christopher Nolan", "Roteirista"),
    ("p03", "Christopher Nolan", "Ator"),
    ("p04", "Jonathan Nolan", "Roteirista"),
    ("p05", "Nolan Gould", "Ator"),
    ("p06", "Zoë Saldaña", "Ator"),
    ("p07", "Zoe Saldana", "Ator"),
    ("p08", "Hayao Miyazaki", "Diretor"),
    ("p09", "宮崎 駿", "Diretor"),
    ("p10", "宮崎 駿", "Roteirista"),
    ("p11", "Alexander Chard​", "Ator"),
    ("p12", "  Padded   Name ", "Ator"),
    ("p13", "Bjørk", "Ator"),
    ("p14", "שָׁלוֹם", "Ator"),
    ("p15", "がぎぐ", "Ator"),
    ("p16", "かきく", "Ator"),
    ("p17", "Йосиф", "Ator"),
    ("p18", "Иосиф", "Ator"),
    ("p19", "Larry Rosen", "Diretor"),
    ("p20", "Larry Rosen", "Ator"),
    ("p21", "Larry Rosen", "Roteirista"),
    ("p22", "O'Brien", "Ator"),
    ("p23", "Jean-Luc Godard", "Diretor"),
    ("p24", "Wes Anderson", "Diretor"),
    ("p25", "Wes Craven", "Diretor"),
    ("p26", "Wesley Snipes", "Ator"),
]


def repeated_titles(title: str, count: int) -> list[tuple[str, str, str, int]]:
    """`count` filmes de MESMO título; os anos repetem (2000..2009), só o id desambigua."""
    return [(f"ms{n:03d}", f"s{n:03d}", title, 2000 + n % 10) for n in range(count)]


# (sk, id_filme, título, ano)
DEFAULT_MOVIES = [
    ("m01", "14564", "Avatar", 2009),
    ("m02", "2001", "Avatar: The Way of Water", 2022),
    ("m03", "100", "Dune", 1984),
    ("m04", "101", "Dune", 2021),
    ("m05", "102", "Dune", 2021),
    ("m06", "201", "Amélie", 2001),
    ("m07", "202", "Amelie", 2001),
    ("m08", "301", "★", 2020),
    ("m09", "302", "________", 2021),
    ("m10", "401", "Spider-Man", 2002),
    ("m11", "402", "Spider Man", 2017),
    ("m12", "501", "S.W.A.T.", 2003),
    ("m13", "601", "Star Wars: Episode IV - A New Hope", 1977),
    ("m14", "602", "Star Wars: Episode V - The Empire Strikes Back", 1980),
    ("m15", "701", "Her", 2013),
    ("m16", "702", "Her Smell", 2018),
    ("m17", "801", "千と千尋の神隠し", 2001),
    ("m18", "901", "Blade Runner 2049", 2017),
    ("m19", "902", "Blade Runner", 1982),
    ("m20", "9999", "9", 2009),
    ("m21", "8", "Heat", 1995),  # ids de comprimentos diferentes: a ordem é numérica, não textual
    ("m22", "70", "Heat", 1995),
    *repeated_titles("Silence", 40),
]


def build_gold_db(
    path: Path,
    *,
    genres: list[tuple[str, str]] | None = None,
    companies: list[tuple[str, str]] | None = None,
    people: list[tuple[str, str, str]] | None = None,
    movies: list[tuple[str, str, str, int]] | None = None,
    extra_movies: int = 0,
    shuffle_seed: int | None = None,
) -> Path:
    """Cria o banco com as 10 tabelas da Gold. `shuffle_seed` embaralha a ordem de inserção."""
    datasets = {
        "genres": list(DEFAULT_GENRES if genres is None else genres),
        "companies": list(DEFAULT_COMPANIES if companies is None else companies),
        "people": list(DEFAULT_PEOPLE if people is None else people),
        "movies": list(DEFAULT_MOVIES if movies is None else movies),
    }
    if shuffle_seed is not None:
        rng = random.Random(shuffle_seed)  # noqa: S311
        for rows in datasets.values():
            rng.shuffle(rows)
    con = sqlite3.connect(path)
    try:
        for ddl in GOLD_DDL:
            con.execute(ddl)
        con.execute("INSERT INTO alembic_version VALUES ('abc123')")
        con.executemany("INSERT INTO dim_genres VALUES (?, ?)", datasets["genres"])
        con.executemany("INSERT INTO dim_companies VALUES (?, ?)", datasets["companies"])
        con.executemany("INSERT INTO dim_people VALUES (?, ?, ?)", datasets["people"])
        con.executemany(
            "INSERT INTO dim_movies VALUES (?, ?, ?, ?, ?, 100, 'en', 'Lançado', 'Sinopse.',"
            " NULL, NULL)",
            [
                (sk, mid, title, f"{year}-01-01", year)
                for sk, mid, title, year in datasets["movies"]
            ],
        )
        con.executemany(
            "INSERT INTO dim_movies (sk_movie_id, titulo, ano_lancamento, duracao_minutos)"
            " VALUES (?, ?, ?, ?)",
            [(f"x{i}", f"Extra {i}", 1990 + i % 35, 60 + i % 100) for i in range(extra_movies)],
        )
        con.commit()
    finally:
        con.close()
    return path


# --- cenários do M1c ------------------------------------------------------------------------------

_INSERTS = {
    "dim_genres": "INSERT INTO dim_genres VALUES (?, ?)",
    "dim_companies": "INSERT INTO dim_companies VALUES (?, ?)",
    "dim_people": "INSERT INTO dim_people VALUES (?, ?, ?)",
    "dim_movies": (
        "INSERT INTO dim_movies VALUES (?, ?, ?, ?, ?, 100, 'en', ?, 'Sinopse.', NULL, NULL)"
    ),
    "fact_movies_performance": (
        "INSERT INTO fact_movies_performance VALUES (?, NULL, NULL, 0, ?, ?, ?, ?, ?, ?, ?, NULL)"
    ),
    "dim_reviews": "INSERT INTO dim_reviews VALUES (?, ?, ?, ?)",
    "bridge_movie_genre": "INSERT INTO bridge_movie_genre VALUES (?, ?)",
    "bridge_movie_company": "INSERT INTO bridge_movie_company VALUES (?, ?)",
    "bridge_movie_person": "INSERT INTO bridge_movie_person VALUES (?, ?)",
}
AUTO = object()  # lucro calculado como na Gold real


class GoldScenario:
    """Cenário montado em código: cada filme já com fato, pontes e avaliações.

    Como na Gold real, cada papel de uma pessoa é uma linha própria de `dim_people`, e o lucro
    padrão trata receita e orçamento ausentes como 0 (há lucro mesmo com um dos lados faltando).
    """

    def __init__(self, genres: Iterable[str] = REAL_GENRES) -> None:
        self.rows: dict[str, list[tuple[object, ...]]] = {table: [] for table in _INSERTS}
        self._genres: dict[str, str] = {}
        self._companies: dict[str, str] = {}
        self._people: dict[tuple[str, str], str] = {}
        self._movies = 0
        for name in genres:
            self.genre(name)

    def genre(self, name: str) -> str:
        if name not in self._genres:
            self._genres[name] = f"g{len(self._genres) + 1:03d}"
            self.rows["dim_genres"].append((self._genres[name], name))
        return self._genres[name]

    def company(self, name: str) -> str:
        if name not in self._companies:
            self._companies[name] = f"c{len(self._companies) + 1:04d}"
            self.rows["dim_companies"].append((self._companies[name], name))
        return self._companies[name]

    def person(self, name: str, role: str) -> str:
        if (name, role) not in self._people:
            self._people[(name, role)] = f"p{len(self._people) + 1:05d}"
            self.rows["dim_people"].append((self._people[(name, role)], name, role))
        return self._people[(name, role)]

    def movie(
        self,
        title: str = "Filme",
        *,
        id_filme: str | None = None,
        date: str | None = "2020-06-15",
        year: int | None = None,
        status: str = "Lançado",
        receita: float | None = None,
        orcamento: float | None = None,
        lucro: object = AUTO,
        popularidade: float | None = None,
        nota_tmdb: float | None = 0.0,
        qtd_tmdb: int | None = None,
        nota_imdb: float | None = None,
        reviews: tuple[int, float | None] | None = None,
        genres: Iterable[str] = (),
        companies: Iterable[str] = (),
        actors: Iterable[str] = (),
        directors: Iterable[str] = (),
        writers: Iterable[str] = (),
        with_fact: bool = True,
    ) -> str:
        """Acrescenta um filme e devolve o `sk_movie_id`."""
        self._movies += 1
        sk = f"m{self._movies:05d}"
        if year is None and date is not None:
            year = int(date[:4])
        self.rows["dim_movies"].append(
            (sk, id_filme or str(1000 + self._movies), title, date, year, status)
        )
        if with_fact:
            if lucro is AUTO:  # em centavos, como na Gold
                lucro = round((receita or 0) - (orcamento or 0), 2)
            self.rows["fact_movies_performance"].append(
                (sk, orcamento, receita, lucro, popularidade, nota_tmdb, qtd_tmdb, nota_imdb)
            )
        if reviews is not None:
            self.rows["dim_reviews"].append((f"r{self._movies:05d}", sk, *reviews))
        for name in genres:
            self.rows["bridge_movie_genre"].append((sk, self.genre(name)))
        for name in companies:
            self.rows["bridge_movie_company"].append((sk, self.company(name)))
        for role, names in (("Ator", actors), ("Diretor", directors), ("Roteirista", writers)):
            for name in names:
                self.rows["bridge_movie_person"].append((sk, self.person(name, role)))
        return sk

    def write(self, path: Path, *, shuffle_seed: int | None = None) -> Path:
        """Grava o banco. `shuffle_seed` embaralha a ordem de inserção de cada tabela."""
        rng = random.Random(shuffle_seed)  # noqa: S311
        con = sqlite3.connect(path)
        try:
            for ddl in GOLD_DDL:
                con.execute(ddl)
            for table, sql in _INSERTS.items():
                rows = list(self.rows[table])
                if shuffle_seed is not None:
                    rng.shuffle(rows)
                con.executemany(sql, rows)
            con.commit()
        finally:
            con.close()
        return path
