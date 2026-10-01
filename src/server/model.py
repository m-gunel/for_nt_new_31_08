from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import BaseModel, Field, field_validator

from src.common import strip_doc_prefix


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


class HealthCheckResponse(BaseModel):
    status: Literal["OK"] = "OK"


class Chunk(BaseModel):
    content: str
    score: float
    # Место фрагмента в документе. Необязательные: у строк, записанных до
    # появления полей, и в выдаче почты их нет.
    kind: str | None = Field(None, description="text / table / table_row / notes / ocr")
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


class BaseSearchResponseItem(BaseModel):
    id: str
    title: str
    chunks: list[Chunk]
    type: Literal["file", "application", "email", "email_attachment"]
    score: float
    document: DocumentInfo | None = None


class SearchResponseItemFile(BaseSearchResponseItem):
    type: Literal["file"] = "file"  # type: ignore
    absolute_path: str


class SearchResponseItemEmail(BaseSearchResponseItem):
    type: Literal["email"] = "email"  # type: ignore
    absolute_path: str


class SearchResponseItemEmailAttachment(BaseSearchResponseItem):
    type: Literal["email_attachment"] = "email_attachment"  # type: ignore
    absolute_path: str


SearchResponseItem = Annotated[
    SearchResponseItemFile
    | SearchResponseItemEmail
    | SearchResponseItemEmailAttachment,
    Field(discriminator="type"),
]


class SearchResponse(BaseModel):
    results: list[SearchResponseItem]
    total: int

    @classmethod
    def from_dataframe(cls, df: pl.DataFrame | None):
        return cls.from_dataframes([df])

    @classmethod
    def from_dataframes(cls, frames: Sequence[pl.DataFrame | None]):
        """Собрать ответ из выдач нескольких источников.

        Построчно, а не через `pl.concat`: схемы расходятся, если по одной
        таблице гибридный поиск деградировал до векторного — в одной
        `_relevance_score`, в другой `_distance`.
        """
        res: dict[str, list[Chunk]] = defaultdict(list)
        titles: dict[str, str] = {}
        documents: dict[str, DocumentInfo] = {}
        # None приходит, когда поиск невозможен (например, FTS-индекс ещё не
        # построен) — отдаём пустой ответ, а не падаем.
        rows = chain.from_iterable(
            df.iter_rows(named=True) for df in frames if df is not None
        )
        for row in rows:
            # meta в схеме чанка nullable (src/common/doc_meta.py): строка,
            # записанная до появления метаданных, мигрированная из старой
            # таблицы или вставленная руками, приходит с meta=None. Индексация
            # по None — TypeError, то есть одна такая строка роняла весь
            # /search в 500. Титул ниже и так имеет фолбэк на имя файла.
            meta = row.get("meta") or {}
            if title := meta.get("original_name"):
                if row["path"] not in titles:
                    titles[row["path"]] = title
            if row["path"] not in documents and any(
                meta.get(k) is not None for k in DocumentInfo.model_fields
            ):
                documents[row["path"]] = DocumentInfo.model_validate(
                    {k: meta.get(k) for k in DocumentInfo.model_fields}
                )
            chunk = Chunk(
                # Строка «Документ: <имя>» нужна индексу, а не читателю: имя файла
                # уже в заголовке результата.
                content=strip_doc_prefix(row["text"], meta.get("original_name")),
                score=_row_score(row),
                **{k: row.get(k) for k in _CHUNK_PLACE},
            )
            res[row["path"]].append(chunk)

        return cls(
            results=sorted(
                [
                    (
                        SearchResponseItemFile
                        if not file_path.startswith("email_")
                        else SearchResponseItemEmail
                        if file_path.endswith(":body")
                        else SearchResponseItemEmailAttachment
                    )(
                        id=str(file_path),
                        title=titles.get(file_path, Path(file_path).name),
                        chunks=chunks,
                        absolute_path=file_path,
                        score=max(chunk.score for chunk in chunks),
                        document=documents.get(file_path),
                    )
                    for file_path, chunks in res.items()
                ],
                key=lambda x: x.score,
                reverse=True,
            ),
            total=len(res),
        )


class SearchRequest(BaseModel):
    query: str
    # Верхняя граница обязательна: значение уходит прямо в .limit() LanceDB,
    # и запрос с limit=1_000_000 соберёт сотни тысяч строк в pydantic-модели
    # прямо в event loop. 1000 — с запасом над рабочими сценариями.
    limit: int = Field(10, ge=1, le=1000)
    query_type: Literal["hybrid", "vector", "text"] = "hybrid"
    # Только файлы по умолчанию: письма ищутся своим top_k в своей таблице,
    # и смешанная выдача — явный выбор клиента.
    sources: list[Literal["files", "email"]] = Field(
        default_factory=lambda: ["files"],
        description="Источники поиска; каждый получает свой limit",
    )
    # offset: int

    @field_validator("sources")
    @classmethod
    def _sources_are_unique_and_present(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("нужен хотя бы один источник")
        if len(set(value)) != len(value):
            # Повтор дал бы двойной top_k из одной таблицы.
            raise ValueError("источники не должны повторяться")
        return value


class EmailConfig(BaseModel):
    enabled: bool = False
    path_to_email_folder: str = ""
    # Окно по умолчанию, а не None: None — это «весь ящик за все годы», то
    # есть на порядок больше чанков, чем весь файловый корпус. Совпадает с
    # cfg/example.yml; явный null в запросе снимает ограничение.
    index_last_n_days: int | None = 30
    offsets: dict[str, int] | None = None


class SberchatConfig(BaseModel):
    enabled: bool = False


class UserConfig(BaseModel):
    folders: list[str] | None = Field(
        None, description="Список директорий для сканирвоания"
    )
    # Глубина рекурсивного наблюдения. None — как раньше: рекурсивно по всему
    # дереву (на каждый подкаталог ставится inotify-наблюдение и на большом
    # домашнем каталоге это упирается в fs.inotify.max_user_watches). Число —
    # наблюдать только каталоги до этой вложенности включительно, создавая
    # по одному наблюдению на каждый, без рекурсии watchdog'а.
    max_depth: int | None = Field(
        None, description="Глубина рекурсивного наблюдения файлового вотчера"
    )
    email: EmailConfig = Field(default_factory=EmailConfig)
    sberchat: SberchatConfig = Field(default_factory=SberchatConfig)


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


class RequestFilePath(BaseModel):
    absolute_path: str = Field(description="Исходный путь файла")


class ResponseFilePath(BaseModel):
    absolute_temp_path: str = Field(description="Новый путь файла в tmp директории")


class ReadFileRequest(BaseModel):
    absolute_path: str = Field(description="Абсолютный путь файла из выдачи поиска")


class ReadFileResponse(BaseModel):
    """Полный текст файла в понимании парсеров индексации."""

    text: str = Field(
        description="Полный текст файла после разбора и нормализации, без нарезки"
    )
    total_chars: int = Field(description="Полная длина текста до обрезки")
    truncated: bool = Field(description="Обрезан ли текст лимитом ручки")
