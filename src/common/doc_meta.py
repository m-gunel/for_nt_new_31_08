import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePath
from typing import NamedTuple, NotRequired, TypedDict

import pyarrow as pa

# Строка сверху каждого чанка документа: имя файла попадает и в BM25, и в вектор,
# и чанк «знает», из какого он документа (похожие отчёты разных банков и лет).
# Хранится в text; выдача её снимает — имя и так в заголовке результата.
DOC_PREFIX = "Документ: "


def doc_prefix(original_name: str | None) -> str:
    return f"{DOC_PREFIX}{PurePath(original_name).stem}\n" if original_name else ""


# Карточка документа — лишняя строка индекса после всех чанков документа: имя
# файла и свойства (название, автор, тема…), которых нет в самих чанках. Запрос
# «найди файл …» и код из имени («ЦБ_21», «5348-У») попадают в неё целиком.
# idx=-1: у чанков idx с нуля, а idx задаёт только порядок внутри документа.
CARD_KIND = "card"
CARD_IDX = -1
# Свойства документа в карточке — подпись и ключ в meta, в этом порядке.
CARD_FIELDS = (
    ("Название", "title"),
    ("Автор", "author"),
    ("Изменил", "last_modified_by"),
    ("Тема", "subject"),
    ("Ключевые слова", "keywords"),
    ("Описание", "description"),
)
# Длинное «Описание» или «Ключевые слова» не должны вытеснять из карточки
# остальное: вектор строится по началу текста.
CARD_VALUE_MAX = 300
_WORD = re.compile(r"[0-9a-zа-я]+")


def norm_words(text: str) -> str:
    """Слова текста для сравнения: нижний регистр, ё→е, без знаков.

    Одна нормализация на карточку и на поиск файла по коду из имени
    (src/indexer/name_codes.py): «ЦБ_21» и «цб 21» должны совпадать и там,
    и там.
    """
    return " ".join(_WORD.findall(text.lower().replace("ё", "е")))


def document_card_text(meta: Mapping | None) -> str:
    """Текст карточки документа: «Документ: <имя>» и непустые свойства.

    Свойство пропускаем, если его слова подряд и целиком уже есть в имени
    файла или в свойстве выше: название, совпадающее с именем файла, или
    автор, повторённый в «Изменил», только раздули бы её и сместили вектор
    к повтору. Подписи полей не сравниваются. Пустая строка — карточке
    не из чего состоять.
    """
    meta = meta or {}
    name = meta.get("original_name")
    lines = [p.rstrip("\n")] if (p := doc_prefix(name)) else []
    # Слова в пробелах — сравнение целыми словами: «Петров» не начало «Петрова».
    seen = [f" {norm_words(PurePath(name).stem)} "] if lines else []
    for label, key in CARD_FIELDS:
        value = " ".join(str(meta.get(key) or "").split())
        words = norm_words(value)
        if words and not any(f" {words} " in s for s in seen):
            lines.append(f"{label}: {value[:CARD_VALUE_MAX]}")
            seen.append(f" {norm_words(value[:CARD_VALUE_MAX])} ")
    return "\n".join(lines)


def strip_doc_prefix(text: str, original_name: str | None) -> str:
    """Текст чанка без строки «Документ: …» — только если это ровно префикс этого файла."""
    prefix = doc_prefix(original_name)
    return text[len(prefix) :] if prefix and text.startswith(prefix) else text


# Колонки дат в схеме — timestamp("ns"): примерно 1677-09-21 … 2262-04-11.
# Дата вне диапазона (1601 год с SMB, 2262+ на APFS) роняет запись всего
# батча с ArrowInvalid, и проход обрывается. Границы — с запасом от краёв.
_STORABLE_MIN = datetime(1678, 1, 1)
_STORABLE_MAX = datetime(2262, 1, 1)


def storable_datetime(dt: datetime | None) -> datetime | None:
    """Дата, если её примет колонка timestamp("ns"), иначе None."""
    if dt is None or not _STORABLE_MIN <= dt.replace(tzinfo=None) < _STORABLE_MAX:
        return None
    return dt


def datetime_from_timestamp(ts: float) -> datetime | None:
    """Локальная дата по времени Unix для колонок дат; не представима — None."""
    try:
        return storable_datetime(datetime.fromtimestamp(ts))
    except (OverflowError, OSError, ValueError):
        return None


class FileMetadata(NamedTuple):
    path: str
    size: int
    mtime_seconds: int


class Chunk(TypedDict):
    idx: int | None
    text: str
    # Место чанка в документе (src/parsers/doc_model.py); у старых строк — null.
    kind: NotRequired[str | None]  # text / table / table_row / notes / ocr / card
    page_start: NotRequired[int | None]  # страница PDF, слайд, лист — с 1
    page_end: NotRequired[int | None]
    line_start: NotRequired[int | None]  # строка txt — с 1
    line_end: NotRequired[int | None]
    section: NotRequired[str | None]  # путь заголовков: «Глава 2 › 2.1»
    table_id: NotRequired[str | None]
    row_start: NotRequired[int | None]
    row_end: NotRequired[int | None]
    char_start: NotRequired[int | None]  # позиция в полном тексте документа
    char_end: NotRequired[int | None]


class DocumentMetadata(TypedDict, total=False):
    # date_created — время рождения файла (не ctime: тот меняется от chmod и
    # копирования); ОС не знает его — None.
    date_created: datetime | None
    date_modified: datetime | None
    original_name: str | None
    ext: str | None
    # Поля писем: почты в индексе больше нет, но колонки остаются в схеме —
    # иначе таблицы прежних версий пришлось бы пересобирать. У файлов — null.
    th_message_id: str | None
    conversation_id: str | None
    offset_start: int | None
    offset_stop: int | None
    content_type: str | None
    # Файл и источник (src/parsers/metadata.py).
    size: int | None
    sha256: str | None  # одинаковые файлы в разных папках, кэш OCR
    mime: str | None  # по сигнатуре содержимого
    source_type: str | None  # file
    source_uri: str | None  # file:///…
    origin_url: str | None  # откуда скачан (user.xdg.origin.url)
    # Свойства самого документа.
    title: str | None
    author: str | None
    last_modified_by: str | None
    subject: str | None
    keywords: str | None
    description: str | None
    category: str | None
    doc_created: datetime | None  # даты из свойств файла, не ФС
    doc_modified: datetime | None
    page_count: int | None  # страниц PDF, ≈страниц docx, слайдов, листов
    language: str | None  # ru / en / ru,en
    encoding: str | None  # кодировка txt


class ChunkWithMeta(Chunk, total=False):
    meta: DocumentMetadata | None
    file_meta: "FileMetadata"
    path: str | None


doc_meta_schema = pa.schema(
    [
        pa.field("date_created", pa.timestamp("ns"), nullable=True),
        pa.field("date_modified", pa.timestamp("ns"), nullable=True),
        pa.field("original_name", pa.string(), nullable=True),
        pa.field("ext", pa.string(), nullable=True),
        pa.field("th_message_id", pa.string(), nullable=True),
        pa.field("conversation_id", pa.string(), nullable=True),
        pa.field("offset_start", pa.int64(), nullable=True),
        pa.field("offset_stop", pa.int64(), nullable=True),
        pa.field("content_type", pa.string(), nullable=True),
        pa.field("size", pa.int64(), nullable=True),
        pa.field("sha256", pa.string(), nullable=True),
        pa.field("mime", pa.string(), nullable=True),
        pa.field("source_type", pa.string(), nullable=True),
        pa.field("source_uri", pa.string(), nullable=True),
        pa.field("origin_url", pa.string(), nullable=True),
        pa.field("title", pa.string(), nullable=True),
        pa.field("author", pa.string(), nullable=True),
        pa.field("last_modified_by", pa.string(), nullable=True),
        pa.field("subject", pa.string(), nullable=True),
        pa.field("keywords", pa.string(), nullable=True),
        pa.field("description", pa.string(), nullable=True),
        pa.field("category", pa.string(), nullable=True),
        pa.field("doc_created", pa.timestamp("ns"), nullable=True),
        pa.field("doc_modified", pa.timestamp("ns"), nullable=True),
        pa.field("page_count", pa.int64(), nullable=True),
        pa.field("language", pa.string(), nullable=True),
        pa.field("encoding", pa.string(), nullable=True),
    ]
)

chunk_with_meta_schema = pa.schema(
    [
        pa.field("idx", pa.int64()),
        pa.field("text", pa.string()),
        pa.field("kind", pa.string(), nullable=True),
        pa.field("page_start", pa.int64(), nullable=True),
        pa.field("page_end", pa.int64(), nullable=True),
        pa.field("line_start", pa.int64(), nullable=True),
        pa.field("line_end", pa.int64(), nullable=True),
        pa.field("section", pa.string(), nullable=True),
        pa.field("table_id", pa.string(), nullable=True),
        pa.field("row_start", pa.int64(), nullable=True),
        pa.field("row_end", pa.int64(), nullable=True),
        pa.field("char_start", pa.int64(), nullable=True),
        pa.field("char_end", pa.int64(), nullable=True),
        pa.field("meta", pa.struct(doc_meta_schema), nullable=True),
        pa.field("path", pa.string()),
    ]
)
