from datetime import datetime
from typing import NamedTuple, TypedDict

import pyarrow as pa

# Префикс ключа письма: "email_<папка>:<начало>:<конец>". После разбора путь —
# единственный носитель источника: `file_meta` в схему таблицы не входит.
EMAIL_PREFIX = "email_"


def is_email_path(path: str) -> bool:
    return path.startswith(EMAIL_PREFIX)


class FileMetadata(NamedTuple):
    path: str
    size: int
    mtime_seconds: int


class Chunk(TypedDict):
    idx: int | None
    text: str


class DocumentMetadata(TypedDict, total=False):
    date_created: datetime | None
    date_modified: datetime | None
    original_name: str | None
    ext: str | None
    th_message_id: str | None
    conversation_id: str | None
    offset_start: int | None
    offset_stop: int | None
    content_type: str | None


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
    ]
)

chunk_with_meta_schema = pa.schema(
    [
        pa.field("idx", pa.int64()),
        pa.field("text", pa.string()),
        pa.field("meta", pa.struct(doc_meta_schema), nullable=True),
        pa.field("path", pa.string()),
    ]
)
