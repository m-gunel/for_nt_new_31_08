"""Письма из базы Gloda Thunderbird (global-messages-db.sqlite).

Демон модуль не использует — почту он читает из mbox (lazy_iter_mbox).
Модуль нужен скриптам experiments/ и блокноту notebooks/thunderbird.ipynb.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any, NamedTuple

import pyarrow as pa
from pydantic import BaseModel, Field
from rich.progress import Progress

from src.parsers import ChunkWithMeta, DocumentMetadata

# Отсюда extract_unique_body импортируют и скрипты experiments/.
from .body_cleanup import extract_unique_body


class _MessageRow(NamedTuple):
    id: int
    folderID: int
    headerMessageID: str
    conversationID: str
    date: int
    jsonAttributes: str
    c0body: str
    c1subject: str


class Attachment(NamedTuple):
    name: str
    doc_type: str
    size: int
    field_1: str
    field_2: str | None
    field_3: bool


class JsonAttributes(BaseModel):
    from_: int = Field(alias="43")
    to: list[int] | None = Field(alias="44", default_factory=list)
    cc: list[int] | None = Field(alias="45", default_factory=list)
    bcc: list[int] | None = Field(alias="46", default_factory=list)
    attachments: list[Attachment] | None = Field(default_factory=list, alias="51")
    from_me: bool | None = Field(False, alias="54")
    to_me: bool | None = Field(False, alias="55")


MESSAGE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("folderID", pa.int64()),
        pa.field("headerMessageID", pa.string()),
        pa.field("conversationID", pa.string()),
        pa.field("date", pa.timestamp("us")),
        pa.field("c0body", pa.string()),
        pa.field("c1subject", pa.string()),
        pa.field("from_", pa.int64()),
        pa.field("to", pa.list_(pa.int64())),
        pa.field("cc", pa.list_(pa.int64())),
        pa.field("bcc", pa.list_(pa.int64())),
        pa.field(
            "attachments",
            pa.list_(
                pa.struct(
                    [
                        pa.field("name", pa.string()),
                        pa.field("doc_type", pa.string()),
                        pa.field("size", pa.int64()),
                        pa.field("field_1", pa.string()),
                        pa.field("field_2", pa.string()),
                        pa.field("field_3", pa.bool_()),
                    ]
                )
            ),
        ),
        pa.field("from_me", pa.bool_()),
        pa.field("to_me", pa.bool_()),
    ]
)


_query_messages_full = """
select
	m.id,
    m.folderID, 
    m.headerMessageID, 
    m.conversationID, 
    m.date, 
    m.jsonAttributes,
    c.c0body,
    c.c1subject  
from messages as m
join messagesText_content as c
on m.id = c.docid
where date is not null and c.c0body is not null
    and (:min_date is null or date >= :min_date)
"""

_query_messages_full_conv = """
select
	m.id,
    m.folderID, 
    m.headerMessageID, 
    m.conversationID, 
    m.date, 
    m.jsonAttributes,
    c.c0body,
    c.c1subject  
from messages as m
join messagesText_content as c
on m.id = c.docid
where date is not null and c.c0body is not null
    and (:min_date is null or date >= :min_date)
    and m.conversationID == :conversation_id
"""

_query_count_messages = """
select count(1) from messages as m
join messagesText_content as c
on m.id = c.docid
where date is not null and c.c0body is not null
    and (:min_date is null or date >= :min_date)
"""


def _parse_row(
    row: Iterable[Any],
    split_threads: bool = True,
    min_msg_body_len: int = 10,
):
    msg = _MessageRow(*row)
    msg_dict = msg._asdict()
    json_attributes = msg_dict.pop("jsonAttributes")
    json_parsed = JsonAttributes.model_validate_json(json_attributes or "{}")
    msg_dict.update(json_parsed.model_dump())
    msg_dict["headerMessageID"] = str(msg_dict["headerMessageID"])
    msg_dict["conversationID"] = str(msg_dict["conversationID"])
    msg_dict["date"] = datetime.fromtimestamp(msg.date / 1000000)
    if "c0body" in msg_dict and split_threads:
        msg_dict["c0body"] = extract_unique_body(msg_dict["c0body"]).strip()
        if not msg_dict["c0body"] or len(msg_dict["c0body"]) < min_msg_body_len:
            return None
    return msg_dict


def _convert_to_chunk_with_meta(msg: dict[str, Any]) -> ChunkWithMeta:
    return ChunkWithMeta(
        idx=msg["id"],
        text=msg["c0body"],
        meta=DocumentMetadata(
            date_created=msg["date"],
            th_message_id=msg["conversationID"],
            content_type="email",
        ),
    )


def read_messages(
    db_path: str,
    batch_size: int = 1024,
    progress: Progress | None = None,
    min_date: datetime | None = None,
    split_threads: bool = True,
    query=_query_messages_full,
    min_msg_body_len: int = 10,
):
    with sqlite3.connect(db_path, uri=True) as conn:
        # date в Gloda — микросекунды Unix-времени.
        min_date_us = int(min_date.timestamp() * 1000000) if min_date else None
        total_cnt = (
            conn.execute(_query_count_messages, {"min_date": min_date_us}).fetchone()[0]
            if progress
            else 0
        )
        cursor = conn.execute(query, {"min_date": min_date_us})

        task = (
            progress.add_task(
                description="Reading messages...",
                total=total_cnt,
            )
            if progress
            else None
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                if row := _parse_row(
                    row,
                    split_threads=split_threads,
                    min_msg_body_len=min_msg_body_len,
                ):
                    yield row
                if progress and task is not None:
                    progress.update(task, advance=1)


def collect_batches(data: Iterable[dict[str, Any]], batch_size: int, schema: pa.Schema):
    batch = []
    for row in data:
        batch.append(row)

        if len(batch) == batch_size:
            yield pa.RecordBatch.from_pylist(batch, schema=schema)
            batch.clear()
    if batch:
        yield pa.RecordBatch.from_pylist(batch, schema=schema)
