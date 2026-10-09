"""Файл по коду из имени: «Найди файл ЦБ_21», «5348-У», «obs_286».

Код в имени файла — самый точный признак нужного файла, но поиск его почти
не видит: в векторе это пара редких токенов, а в BM25 он тонет среди слов
запроса («найди», «файл») и есть лишь в первой строке чанка. Поэтому
документы с кодом из запроса в имени выдача ставит первыми — явным порядком,
а не подделанной оценкой (src/server/model.py, параметр pinned).
"""

import re
import threading
from collections.abc import Iterable
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from src.common import norm_words
from src.parsers import get_parsers_registry
from src.utils.lance_wrapper import sql_quote

if TYPE_CHECKING:
    import polars as pl

# Знаки, которые срезаем с краёв слова запроса: «ЦБ_21», (5348-У), obs_286?
_EDGE_PUNCT = ".,;:!?«»\"'()[]"
_DIGIT = re.compile(r"\d")
# Буква любого алфавита: \w без цифр и «_».
_LETTER = re.compile(r"[^\W\d_]")

# Код учитывается, только если он есть в именах 1–3 документов: общий для
# многих файлов код («2024г», «v2») документ не выделяет. Считаются документы
# (sha256), а не пути: копии одного файла выдача схлопывает в один результат,
# а при подсчёте путей четвёртая копия отключала бы закрепление.
MAX_FILES_PER_CODE = 3

# Сколько первых чанков дочитывать у закреплённого документа, которого нет в
# пуле поиска: столько же фрагментов показывает выдача (MAX_FRAGMENTS_PER_DOC
# в src/server/model.py). Карточка (idx=-1) попадает в то же условие idx < N.
PINNED_CHUNKS = 3


def is_code(token: str) -> bool:
    """Код — цифры вместе с буквой или «_»: «ЦБ_21», «5348-У», «12-МР».

    Голые числа и годы («2024», «15») — не код: они есть в именах сотен
    файлов и ничего не выделяют.
    """
    return bool(_DIGIT.search(token)) and bool(_LETTER.search(token) or "_" in token)


def _without_extension(token: str) -> str:
    """«ЦБ_21.pdf» → «ЦБ_21»: файл часто называют полным именем.

    Имена в словаре — без расширения (doc_name), и «цб 21 pdf» в «цб 21» не
    входит. Срезаем только расширения разбираемых форматов: точка внутри
    кода («v1.2», «ф.0409») — часть кода, а не расширение.
    """
    stem, dot, ext = token.rpartition(".")
    return stem if dot and stem and ext.lower() in get_parsers_registry() else token


def query_codes(query: str) -> list[str]:
    """Коды из запроса, нормализованные как имена файлов, без повторов."""
    codes: list[str] = []
    for raw in query.split():
        token = _without_extension(raw.strip(_EDGE_PUNCT))
        if is_code(token) and (code := norm_words(token)) and code not in codes:
            codes.append(code)
    return codes


def doc_name(path: str, meta: dict[str, Any] | None) -> str:
    """Нормализованное имя документа без расширения.

    original_name — имя файла из метаданных. Срезается только расширение
    разбираемого формата, а не всё после последней точки: точки внутри имени
    («Протокол 1.2») — его часть.
    """
    name = (meta or {}).get("original_name")
    return norm_words(_without_extension(name) if name else PurePath(path).stem)


# Документ в словаре — по первому чанку: он есть у каждого документа, а
# карточки нет у файлов, проиндексированных до их появления и с тех пор не
# менявшихся.
_NAME_ROWS = "idx = 0"


def _load_names(table, known: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Путь → (нормализованное имя, ключ документа): одна строка на документ.

    Только три строковых поля, а не весь meta: словарь перестраивается после
    каждой записи в таблицу, то есть во время индексации — почти на каждый
    запрос с кодом, и разбор всего meta в Python занимал бы почти всё время.
    known — имя → нормализованное с прошлой сборки: имена меняются редко, и
    norm_words по всему корпусу заново не нужен. Ключ документа — sha256
    содержимого, без него — путь.
    """
    data = (
        table.search()
        .where(_NAME_ROWS)
        .select(
            {
                "path": "path",
                "name": "meta.original_name",
                "sha256": "meta.sha256",
            }
        )
        .to_arrow()
    )
    names: dict[str, tuple[str, str]] = {}
    for path, name, sha in zip(
        data.column("path").to_pylist(),
        data.column("name").to_pylist(),
        data.column("sha256").to_pylist(),
        strict=True,
    ):
        if not path:
            continue
        raw = name or path
        if (norm := known.get(raw)) is None:
            norm = known[raw] = doc_name(path, {"original_name": name})
        names[path] = (norm, sha or path)
    return names


class NameIndex:
    """Имена документов одной таблицы, перестраиваемые по версии таблицы.

    Полный проход по документам на каждый запрос дорог, а имена меняются
    только с записью в таблицу — и каждая запись даёт новую версию Lance.
    Поэтому словарь живёт, пока не сменились версия или сам хендл таблицы
    (после сброса индекса таблица создаётся заново, и номера версий
    начинаются сначала).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table = None
        self._version: int | None = None
        self._names: dict[str, tuple[str, str]] = {}
        # Исходное имя → нормализованное; переживает пересборки словаря.
        self._normalized: dict[str, str] = {}

    def names(self, table) -> dict[str, tuple[str, str]]:
        version = table.version
        # Под замком и сборка: два одновременных запроса не должны строить
        # один и тот же словарь дважды.
        with self._lock:
            if self._table is not table:
                self._normalized = {}
            if self._table is not table or self._version != version:
                self._names = _load_names(table, self._normalized)
                self._table, self._version = table, version
            return self._names

    def hits(self, table, codes: Iterable[str]) -> list[str]:
        """Пути файлов, в имени которых есть код из запроса.

        Код совпал, если стоит в имени отдельными словами: «21» не находит
        «ЦБ_210». Пути по всем кодам объединяются, по порядку кодов.
        """
        found: list[str] = []
        names = None
        for code in codes:
            if names is None:
                names = self.names(table)
            needle = f" {code} "
            paths = sorted(p for p, (n, _) in names.items() if needle in f" {n} ")
            # Порог — по документам, но вернуть нужно все пути: какой из
            # копий остаться в выдаче, решает SearchResponse.
            if 0 < len({names[p][1] for p in paths}) <= MAX_FILES_PER_CODE:
                found += [p for p in paths if p not in found]
        return found


def pinned_rows(table, paths: list[str], where: str | None = None) -> "pl.DataFrame":
    """Карточка и первые чанки документов, которых нет в пуле поиска.

    Без вектора: он выдаче не нужен, а весит больше всего остального.
    Строки по idx — чтобы фрагментами стали первые чанки документа, а не
    случайные из скана. where — фильтр поиска (SearchFilters.where): у
    документа, который ему не отвечает, строк не будет вовсе.
    """
    columns = [c for c in table.schema.names if c != "vector"]
    predicate = "path IN ({}) AND idx < {}".format(
        ", ".join(f"'{sql_quote(p)}'" for p in paths), PINNED_CHUNKS
    )
    if where:
        predicate += f" AND ({where})"
    return (
        table.search().where(predicate).select(columns).to_polars().sort("path", "idx")
    )
