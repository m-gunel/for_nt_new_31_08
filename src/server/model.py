import os
import re
from collections import defaultdict
from collections.abc import Collection
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.common import CARD_KIND, strip_doc_prefix

# Реэкспорт: роутер и тесты берут модели настроек отсюда.
from src.common.user_config import SberchatConfig, UserConfig  # noqa: F401
from src.utils.lance_wrapper import sql_quote


def _row_score(row: dict[str, Any]) -> float:
    """Оценка строки выдачи — под именем той колонки, что вернул LanceDB.

    Не через `dict.get(key, default)`: ключ может присутствовать со значением
    None, и тогда default не сработает.
    """
    if (relevance := row.get("relevance")) is not None:
        return relevance
    if (relevance := row.get("_relevance_score")) is not None:
        return relevance
    if (distance := row.get("_distance")) is not None:
        return -distance
    return row.get("_score") or 0.0


_SCORE_COLUMNS = ("relevance", "_relevance_score", "_distance", "_score")


def _has_score(row: dict[str, Any]) -> bool:
    """Строка из пула поиска, а не дочитанная без оценки (код из имени)."""
    return any(row.get(k) is not None for k in _SCORE_COLUMNS)


def _row_rank(row: dict[str, Any]) -> tuple[bool, float]:
    """Ключ сортировки строк: строки пула — по оценке, дочитанные — после всех.

    У дочитанных по коду из имени строк (src/indexer/name_codes.py) оценки нет,
    и их 0.0 обгонял бы векторный пул (оценка — минус расстояние), отбирая у
    копии из пула место «первого» пути в `duplicates`.
    """
    return (_has_score(row), _row_score(row))


class HealthCheckResponse(BaseModel):
    status: Literal["OK"] = "OK"


class Chunk(BaseModel):
    content: str
    score: float | None = Field(
        description=(
            "Оценка поиска; null — фрагмент дочитан вне пула у документа, "
            "найденного по коду из имени файла"
        )
    )
    # Место фрагмента в документе. Необязательные: у строк, записанных до
    # появления полей, их нет.
    kind: str | None = Field(
        None, description="text / table / table_row / notes / ocr / card"
    )
    page_start: int | None = Field(
        None, description="Страница PDF, слайд или лист — с 1"
    )
    page_end: int | None = None
    line_start: int | None = Field(None, description="Строка txt — с 1")
    line_end: int | None = None
    section: str | None = Field(None, description="Путь заголовков: «Глава 2 › 2.1»")
    table_id: str | None = None
    row_start: int | None = None
    row_end: int | None = None


_CHUNK_PLACE = tuple(k for k in Chunk.model_fields if k not in ("content", "score"))


class DocumentInfo(BaseModel):
    """Свойства документа из индекса (src/common/doc_meta.py); всё необязательное."""

    title: str | None = Field(
        None, description="Название из свойств или текста документа"
    )
    author: str | None = None
    last_modified_by: str | None = None
    subject: str | None = None
    keywords: str | None = None
    ext: str | None = None
    mime: str | None = None
    size: int | None = None
    page_count: int | None = Field(None, description="Страниц, слайдов или листов")
    language: str | None = None
    date_created: datetime | None = Field(
        None, description="Рождение файла; нет — пусто"
    )
    date_modified: datetime | None = None
    doc_created: datetime | None = Field(None, description="Дата из свойств документа")
    doc_modified: datetime | None = None
    sha256: str | None = None
    source_type: str | None = None
    source_uri: str | None = None
    origin_url: str | None = None


# Фрагментов на документ: у длинного документа в глубоком пуле десятки чанков,
# агенту хватает лучших, остальное — через /read_file. Столько же показывает
# MCP-мост.
MAX_FRAGMENTS_PER_DOC = 3


class _Hit:
    """Документ выдачи, собранный из строк пула (по убыванию оценки)."""

    __slots__ = (
        "path",
        "score",
        "pooled",
        "pinned",
        "rows",
        "card",
        "title",
        "document",
    )

    def __init__(self, path: str, row: dict[str, Any], pinned: bool) -> None:
        self.path = path
        # Лучшая строка документа, карточка тоже считается. pooled — была ли
        # у документа строка из пула поиска, а не только дочитанные.
        self.pooled, self.score = _row_rank(row)
        # Найден по коду из имени файла: в выдаче идёт первым.
        self.pinned = pinned
        self.rows: list[dict[str, Any]] = []  # фрагменты, не больше MAX_FRAGMENTS
        self.card = False
        # Имя и свойства — из любой строки документа, карточки тоже: документ,
        # найденный одной карточкой, иначе остался бы без заголовка.
        self.title: str | None = None
        self.document: DocumentInfo | None = None

    @property
    def rank(self) -> tuple[bool, bool, float]:
        """Ключ порядка выдачи: найденные по коду из имени — первыми."""
        return (self.pinned, self.pooled, self.score)

    def take_meta(self, row: dict[str, Any]) -> None:
        # meta nullable (src/common/doc_meta.py): у старых и мигрированных строк
        # meta=None, и одна такая строка уронила бы весь /search в 500.
        meta = row.get("meta") or {}
        self.title = self.title or meta.get("original_name")
        if self.document is None and any(
            meta.get(k) is not None for k in DocumentInfo.model_fields
        ):
            self.document = DocumentInfo.model_validate(
                {k: meta.get(k) for k in DocumentInfo.model_fields}
            )


def _distinct_docs(
    frame: pl.DataFrame,
    limit: int | None,
    duplicates: dict[str, set[str]],
    pinned: Collection[str] = (),
) -> list[_Hit]:
    """Документы выдачи: не больше limit, в порядке лучшей строки.

    Копии файла (тот же sha256 под другим путём) мест не занимают: их пути
    уходят в `duplicates` первого встреченного пути. Карточка (kind="card")
    фрагментом не становится, а лишь отмечает совпадение с именем или
    свойствами файла — и только найденная полнотекстовым каналом (есть
    `_score`): в векторный пул карточки попадают просто ближайшими соседями,
    а дочитанная по коду из имени с запросом не сверялась.

    Документы из `pinned` идут первыми и занимают места из того же limit;
    копия закреплённого файла закрепляет оставшийся в выдаче путь.
    """
    rows = sorted(frame.iter_rows(named=True), key=_row_rank, reverse=True)
    first: dict[str, str] = {}  # sha256 → путь, который остаётся в выдаче
    by_path: dict[str, list[dict[str, Any]]] = {}  # в порядке лучшей строки
    pins: set[str] = set()
    for row in rows:
        path = row["path"]
        sha = (row.get("meta") or {}).get("sha256")
        if sha:
            kept = first.setdefault(sha, path)
            if kept != path:
                duplicates[kept].add(path)
                if path in pinned:
                    pins.add(kept)
                continue
        by_path.setdefault(path, []).append(row)
        if path in pinned:
            pins.add(path)
    # Строки за пределами limit всё равно просмотрены: они добирают
    # фрагменты, карточки и копии отобранных документов.
    order = [p for p in by_path if p in pins] + [p for p in by_path if p not in pins]
    hits = []
    for path in order[:limit]:
        doc_rows = by_path[path]
        hit = _Hit(path, doc_rows[0], pinned=path in pins)
        for row in doc_rows:
            hit.take_meta(row)
            if row.get("kind") == CARD_KIND:
                hit.card = hit.card or row.get("_score") is not None
            elif len(hit.rows) < MAX_FRAGMENTS_PER_DOC:
                hit.rows.append(row)
        hits.append(hit)
    return hits


class BaseSearchResponseItem(BaseModel):
    id: str
    title: str
    chunks: list[Chunk]
    type: Literal["file", "application"]
    score: float | None = Field(
        description=(
            "Оценка лучшей строки документа. Порядок выдачи: сначала "
            "найденные по коду из имени (match name_code), затем по оценке. "
            "null — документа не было в пуле поиска, его нашёл только код "
            "из имени"
        )
    )
    document: DocumentInfo | None = None
    duplicates: list[str] = Field(
        default_factory=list,
        description="Другие пути того же содержимого (копии файла в других папках)",
    )
    match: list[str] = Field(
        default_factory=list,
        description=(
            "Чем ещё документ совпал с запросом, кроме фрагментов: "
            "name_code — код из запроса есть в имени файла (такие документы "
            "идут первыми), card — слова запроса есть в имени файла или его "
            "свойствах: название, автор, редактор, тема, ключевые слова, "
            "описание (полнотекстовое совпадение, в query_type=vector не "
            "ставится)"
        ),
    )


class SearchResponseItemFile(BaseSearchResponseItem):
    type: Literal["file"] = "file"  # type: ignore
    absolute_path: str


class SearchResponse(BaseModel):
    results: list[SearchResponseItemFile]
    total: int

    @classmethod
    def from_dataframe(
        cls,
        df: pl.DataFrame | None,
        limit: int | None = None,
        pinned: Collection[str] = (),
    ):
        """Собрать ответ из кадра выдачи (Index.search).

        None — поиск невозможен (например, FTS-индекс ещё не построен): ответ
        пустой, а не 500. limit — документов (None — все из пула), у документа
        до MAX_FRAGMENTS_PER_DOC лучших фрагментов. pinned — пути, найденные по
        коду из имени файла (Index.search): идут первыми и несут "name_code" в
        `match`. Порядок задан явно, а не подменой оценок: score остаётся
        настоящей оценкой поиска, у дочитанных вне пула — null.
        """
        if df is None:
            return cls(results=[], total=0)
        duplicates: dict[str, set[str]] = defaultdict(set)
        hits = _distinct_docs(df, limit, duplicates, set(pinned))
        results = [
            _result_item(hit, duplicates)
            for hit in sorted(hits, key=lambda h: h.rank, reverse=True)
        ]
        return cls(results=results, total=len(results))


def _result_item(hit: _Hit, duplicates: dict[str, set[str]]) -> SearchResponseItemFile:
    chunks = [
        Chunk(
            # Строка «Документ: <имя>» нужна индексу, а не читателю: имя файла
            # уже в заголовке результата.
            content=strip_doc_prefix(
                row["text"], (row.get("meta") or {}).get("original_name")
            ),
            score=_row_score(row) if _has_score(row) else None,
            **{k: row.get(k) for k in _CHUNK_PLACE},
        )
        for row in hit.rows
    ]
    return SearchResponseItemFile(
        id=hit.path,
        title=hit.title or Path(hit.path).name,
        chunks=chunks,
        absolute_path=hit.path,
        score=hit.score if hit.pooled else None,
        document=hit.document,
        duplicates=sorted(duplicates.get(hit.path, ())),
        match=[
            mark for mark, on in (("name_code", hit.pinned), ("card", hit.card)) if on
        ],
    )


# Расширение в фильтре: только латиница и цифры, как у разбираемых форматов.
_EXT = re.compile(r"[0-9a-z]+")


def _fold_yo(sql: str) -> str:
    """ё → е в SQL-выражении: «Ковалёв» и «Ковалев» пишут вперемешку."""
    return f"replace({sql}, 'ё', 'е')"


def _like_contains(value: str) -> str:
    """Шаблон LIKE «содержит value»: %, _ и \\ из ввода — буквально.

    Без экранирования «_» в «Иванов_И» совпадал бы с любым символом, а «%»
    в запросе снимал бы условие вовсе.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"'%{sql_quote(escaped)}%' ESCAPE '\\'"


# Даты в индексе — timestamp в нс (1677–2262 гг.): литерал за пределами LanceDB
# молча переполняет, поэтому вне диапазона — 422.
_FilterDate = Annotated[date, Field(ge=date(1900, 1, 1), le=date(2200, 12, 31))]


# Дата для фильтра — из свойств документа, а без них — файла на диске: у
# скачанного или скопированного файла дата на диске — день копирования.
_MODIFIED = "coalesce(meta.doc_modified, meta.date_modified)"
_CREATED = "coalesce(meta.doc_created, meta.date_created)"


def _sql_timestamp(day: date) -> str:
    # Даты в meta — локальное время без пояса (src/parsers/metadata.py),
    # поэтому и граница — без пояса: полночь этого дня по местному времени.
    return f"TIMESTAMP '{day.isoformat()} 00:00:00'"


class SearchFilters(BaseModel):
    """Фильтры поиска по метаданным документа (src/common/doc_meta.py).

    Переводятся в условие where LanceDB по строкам чанков и применяются ДО
    отбора пула (prefilter): иначе документы под фильтром вытеснялись бы из
    пула прочими, и выдача фильтра оказывалась бы пустой или короче limit.
    Условия складываются через AND. Даты — включительно с обеих сторон;
    у строки без даты условие по дате не выполняется. Пустое поле («», [])
    — то же, что его нет: фильтра по нему нет.
    """

    model_config = ConfigDict(extra="forbid")

    ext: list[str] | None = Field(
        None,
        description='Расширения файлов без точки, регистр не важен: ["docx", "pdf"]',
    )
    folder: str | None = Field(
        None, description="Папка: абсолютный путь, ищется во всём её поддереве"
    )
    modified_from: _FilterDate | None = Field(
        None,
        description=(
            "Изменён не раньше этой даты: дата из свойств документа "
            "(meta.doc_modified), без неё — файла (meta.date_modified)"
        ),
    )
    modified_to: _FilterDate | None = Field(
        None, description="Изменён не позже этой даты, включительно"
    )
    created_from: _FilterDate | None = Field(
        None,
        description=(
            "Создан не раньше этой даты: дата из свойств документа "
            "(meta.doc_created), без неё — файла (meta.date_created)"
        ),
    )
    created_to: _FilterDate | None = Field(
        None, description="Создан не позже этой даты, включительно"
    )
    author: str | None = Field(
        None,
        description="Подстрока автора или последнего редактора, регистр не важен",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_absent(cls, value):
        """Пустое поле формы — «не фильтровать»: пустой author молча отсёк бы
        документы без автора."""
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, list):
            value = [v for v in value if not (isinstance(v, str) and not v.strip())]
            return value or None
        return value

    @field_validator("ext")
    @classmethod
    def _normalize_ext(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        exts = [e.strip().lstrip(".").lower() for e in value]
        # Только буквы и цифры: «docx,pdf» одной строкой — ошибка клиента, а не
        # расширение, и молча вернуть по нему пустую выдачу было бы хуже.
        if bad := [e for e in exts if not _EXT.fullmatch(e)]:
            raise ValueError(f"недопустимое расширение: {bad[0]!r}")
        return list(dict.fromkeys(exts))

    @field_validator("folder")
    @classmethod
    def _absolute_folder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        folder = os.path.normpath(os.path.expanduser(value.strip()))
        # Относительный путь считался бы от каталога демона — не тот, что
        # имел в виду клиент.
        if not os.path.isabs(folder):
            raise ValueError("folder должен быть абсолютным путём")
        return folder

    @field_validator("author")
    @classmethod
    def _strip_author(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _ranges_in_order(self) -> "SearchFilters":
        for name, lo, hi in (
            ("modified", self.modified_from, self.modified_to),
            ("created", self.created_from, self.created_to),
        ):
            if lo and hi and lo > hi:
                raise ValueError(f"{name}_from позже {name}_to")
        return self

    def where(self) -> str | None:
        """Условие where LanceDB; None — фильтров нет."""
        parts = []
        if self.ext:
            # ext в индексе уже в нижнем регистре (file_metadata, вложения).
            parts.append(
                "meta.ext IN ({})".format(", ".join(f"'{e}'" for e in self.ext))
            )
        if self.folder:
            # starts_with, а не LIKE: в путях «_» и «%» — обычные символы.
            # Разделитель в конце — чтобы «/docs/a» не захватывал «/docs/ab».
            prefix = self.folder.rstrip(os.sep) + os.sep
            parts.append(f"starts_with(path, '{sql_quote(prefix)}')")
        for column, lo, hi in (
            (_MODIFIED, self.modified_from, self.modified_to),
            (_CREATED, self.created_from, self.created_to),
        ):
            if lo:
                parts.append(f"{column} >= {_sql_timestamp(lo)}")
            if hi:
                # «по 31.12.2024» — весь день: строго меньше полуночи следующего.
                parts.append(f"{column} < {_sql_timestamp(hi + timedelta(days=1))}")
        if self.author:
            pattern = _like_contains(self.author.lower().replace("ё", "е"))
            parts.append(
                f"({_fold_yo('lower(meta.author)')} LIKE {pattern}"
                f" OR {_fold_yo('lower(meta.last_modified_by)')} LIKE {pattern})"
            )
        return " AND ".join(parts) or None


class SearchRequest(BaseModel):
    query: str
    # Пул чанков растёт с limit (search_pool в src/indexer/indexer.py). Потолок
    # нужен: огромная выдача собиралась бы в pydantic прямо в event loop.
    limit: int = Field(10, ge=1, le=1000, description="Сколько документов вернуть")
    query_type: Literal["hybrid", "vector", "text"] = "hybrid"
    # Принимается ради старых клиентов: почты в индексе нет, "email" даёт пусто.
    sources: list[Literal["files", "email"]] = Field(
        default_factory=lambda: ["files"],
        description='Устарело: ищутся только файлы, ["email"] даёт пустой ответ',
        json_schema_extra={"deprecated": True},
    )
    filters: SearchFilters | None = Field(
        None,
        description=(
            "Фильтры по метаданным: расширение, папка, даты изменения и "
            "создания, автор. Действуют во всех query_type и на файлы, "
            "найденные по коду из имени"
        ),
    )

    @field_validator("query_type", "sources", mode="before")
    @classmethod
    def _lowercase(cls, value):
        """«Hybrid» = «hybrid», «Files» = «files»: регистр параметров не важен."""
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, list):
            return [v.strip().lower() if isinstance(v, str) else v for v in value]
        return value

    @field_validator("sources")
    @classmethod
    def _sources_are_unique_and_present(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("нужен хотя бы один источник")
        if len(set(value)) != len(value):
            raise ValueError("источники не должны повторяться")
        return value


# Тело POST /config строже базовых моделей из src/common/user_config.py: те же
# классы читают user_conf.yml, где лишний ключ не должен обнулять настройки, а в
# запросе опечатка давала бы 200 без изменений.
class SberchatConfigUpdate(SberchatConfig):
    model_config = ConfigDict(extra="forbid")


class UserConfigUpdate(UserConfig):
    model_config = ConfigDict(extra="forbid")

    # Принимается ради старых клиентов и скриптов НТ, которые шлют конфиг
    # целиком: почты больше нет, значение не сохраняется.
    email: dict[str, Any] | None = Field(
        None,
        exclude=True,
        description="Устарело: почты больше нет, поле игнорируется",
        json_schema_extra={"deprecated": True},
    )
    sberchat: SberchatConfigUpdate = Field(default_factory=SberchatConfigUpdate)


class IndexingControlResponse(BaseModel):
    """Ответ ручек управления индексацией — БТ Библиотеки §5.1-5.3."""

    active: bool = Field(description="Включена ли индексация ПОСЛЕ операции")
    changed: bool = Field(
        description=(
            "Изменила ли операция состояние. false означает «уже было так» — "
            "это успех, а не ошибка: ручки идемпотентны"
        )
    )
    detail: str = Field(description="Что произошло, человекочитаемо")


class ReadFileRequest(BaseModel):
    absolute_path: str = Field(description="Абсолютный путь файла из выдачи поиска")


class ReadFileResponse(BaseModel):
    """Полный текст файла в понимании парсеров индексации."""

    text: str = Field(
        description="Полный текст файла после разбора и нормализации, без нарезки"
    )
    total_chars: int = Field(description="Полная длина текста до обрезки")
    truncated: bool = Field(description="Обрезан ли текст лимитом ручки")
