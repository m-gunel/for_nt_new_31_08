from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import BaseModel, Field, field_validator

from src.common import CARD_KIND, strip_doc_prefix


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


def _row_rank(row: dict[str, Any]) -> tuple[bool, float]:
    """Ключ сортировки строк: строки пула — по оценке, дочитанные — после всех.

    Строки документа, найденного по коду из имени и дочитанного вне пула
    (src/indexer/name_codes.py), оценки не имеют. Без этого их 0.0 обгонял
    бы векторный пул, где оценка — минус расстояние, и дочитанный путь
    отбирал бы у копии из пула место «первого» пути в `duplicates`.
    """
    return (any(row.get(k) is not None for k in _SCORE_COLUMNS), _row_score(row))


class HealthCheckResponse(BaseModel):
    status: Literal["OK"] = "OK"


class Chunk(BaseModel):
    content: str
    score: float
    # Место фрагмента в документе. Необязательные: у строк, записанных до
    # появления полей, и в выдаче почты их нет.
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


# Фрагментов на документ в выдаче. Пул поиска глубокий (src/indexer/indexer.py,
# search_pool), и у длинного документа в нём бывают десятки чанков: без
# границы ответ раздувался бы ими, а агенту хватает лучших — остальное он
# дочитает через /read_file. 3 — столько показывает и MCP-мост.
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

    def merge(self, other: "_Hit") -> None:
        """Добавить строки того же пути из выдачи другого источника."""
        self.pooled, self.score = max(
            (self.pooled, self.score), (other.pooled, other.score)
        )
        self.pinned = self.pinned or other.pinned
        self.rows = sorted(self.rows + other.rows, key=_row_rank, reverse=True)[
            :MAX_FRAGMENTS_PER_DOC
        ]
        self.card = self.card or other.card
        self.title = self.title or other.title
        self.document = self.document or other.document

    def take_meta(self, row: dict[str, Any]) -> None:
        # meta в схеме чанка nullable (src/common/doc_meta.py): строка,
        # записанная до появления метаданных, мигрированная из старой таблицы
        # или вставленная руками, приходит с meta=None. Индексация по None —
        # TypeError, то есть одна такая строка роняла весь /search в 500.
        # Титул и так имеет фолбэк на имя файла.
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
    """Документы выдачи одного источника: не больше limit, в порядке лучшей строки.

    Строки идут по убыванию оценки, поэтому первая строка документа — его
    лучшая, и порядок первых вхождений — это порядок по оценке документа.
    Копии уже встреченного файла (тот же sha256 под другим путём) мест не
    занимают: их пути уходят в `duplicates` того пути, что встретился первым.
    Карточка (kind="card") фрагментом не становится — она лишь отмечает, что
    запрос совпал с именем или свойствами файла.

    Документы из `pinned` (найдены по коду из имени файла) идут первыми и
    занимают места из того же limit. Копия закреплённого файла закрепляет
    путь, под которым она осталась в выдаче.
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
                hit.card = True
            elif len(hit.rows) < MAX_FRAGMENTS_PER_DOC:
                hit.rows.append(row)
        hits.append(hit)
    return hits


class BaseSearchResponseItem(BaseModel):
    id: str
    title: str
    chunks: list[Chunk]
    type: Literal["file", "application", "email", "email_attachment"]
    score: float
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
            "идут первыми), card — именем файла или свойствами (название, "
            "автор, тема)"
        ),
    )


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
    def from_dataframes(
        cls,
        frames: Sequence[pl.DataFrame | None],
        limit: int | None = None,
        pinned: Collection[str] = (),
    ):
        """Собрать ответ из выдач нескольких источников.

        Построчно, а не через `pl.concat`: схемы расходятся, если по одной
        таблице гибридный поиск деградировал до векторного — в одной
        `_relevance_score`, в другой `_distance`.

        limit — ДОКУМЕНТОВ на источник (None — все из пула). Документ
        оценивается лучшей своей строкой и несёт до MAX_FRAGMENTS_PER_DOC
        лучших фрагментов. Копии одного файла (одинаковый sha256)
        схлопываются: остаётся путь с лучшей строкой, остальные — в
        `duplicates`, мест они не занимают. Карточка документа — не фрагмент,
        а отметка "card" в `match`.

        pinned — пути документов, найденных по коду из имени файла
        (Index.search, тот же параметр). Они идут первыми — в порядке своей
        лучшей строки пула, дочитанные вне пула — после них — и несут
        "name_code" в `match`. Порядок задан явно, а не подменой оценок:
        score остаётся настоящей оценкой поиска.
        """
        hits: dict[str, _Hit] = {}
        duplicates: dict[str, set[str]] = defaultdict(set)
        pinned = set(pinned)  # проверяется на каждой строке пула
        # None приходит, когда поиск невозможен (например, FTS-индекс ещё не
        # построен) — отдаём пустой ответ, а не падаем.
        for df in frames:
            if df is None:
                continue
            for hit in _distinct_docs(df, limit, duplicates, pinned):
                if (seen := hits.get(hit.path)) is None:
                    hits[hit.path] = hit
                else:
                    # Один путь в двух кадрах — один документ, а не два
                    # элемента выдачи.
                    seen.merge(hit)

        results = []
        for hit in sorted(hits.values(), key=lambda h: h.rank, reverse=True):
            file_path = hit.path
            chunks = [
                Chunk(
                    # Строка «Документ: <имя>» нужна индексу, а не читателю:
                    # имя файла уже в заголовке результата.
                    content=strip_doc_prefix(
                        row["text"], (row.get("meta") or {}).get("original_name")
                    ),
                    score=_row_score(row),
                    **{k: row.get(k) for k in _CHUNK_PLACE},
                )
                for row in hit.rows
            ]
            results.append(
                (
                    SearchResponseItemFile
                    if not file_path.startswith("email_")
                    else SearchResponseItemEmail
                    if file_path.endswith(":body")
                    else SearchResponseItemEmailAttachment
                )(
                    id=str(file_path),
                    title=hit.title or Path(file_path).name,
                    chunks=chunks,
                    absolute_path=file_path,
                    score=hit.score,
                    document=hit.document,
                    duplicates=sorted(duplicates.get(file_path, ())),
                    match=[
                        mark
                        for mark, on in (("name_code", hit.pinned), ("card", hit.card))
                        if on
                    ],
                )
            )
        return cls(results=results, total=len(results))


class SearchRequest(BaseModel):
    query: str
    # limit — документов на источник; пул чанков из LanceDB растёт с ним
    # (search_pool в src/indexer/indexer.py, потолок там же). Верхняя граница
    # всё равно обязательна: выдача из сотен тысяч документов собиралась бы в
    # pydantic-модели прямо в event loop. 1000 — с запасом над рабочими
    # сценариями.
    limit: int = Field(
        10, ge=1, le=1000, description="Сколько документов вернуть на источник"
    )
    query_type: Literal["hybrid", "vector", "text"] = "hybrid"
    # Только файлы по умолчанию: письма ищутся своим top_k в своей таблице,
    # и смешанная выдача — явный выбор клиента.
    sources: list[Literal["files", "email"]] = Field(
        default_factory=lambda: ["files"],
        description="Источники поиска; каждый получает свой limit документов",
    )
    # offset: int

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
