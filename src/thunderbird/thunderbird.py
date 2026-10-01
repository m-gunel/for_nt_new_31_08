import os
import shutil
import sqlite3
from contextlib import ExitStack
from datetime import datetime
from email import policy
from email.header import decode_header
from email.parser import BytesHeaderParser
from functools import cached_property, partial
from pathlib import Path
from typing import Any, Generator, NamedTuple, Optional

import pyarrow as pa
from pydantic import BaseModel, Field
from tqdm import tqdm
from whoosh.analysis import NgramFilter, StandardAnalyzer
from whoosh.fields import ID, TEXT, Schema
from whoosh.index import create_in, exists_in, open_dir
from whoosh.qparser import MultifieldParser
from whoosh.query import FuzzyTerm
from whoosh.scoring import BM25F

from src.fio_parser import FIO, FioParser

_schema_messages = pa.schema(
    [
        pa.field("id", pa.int32()),
        pa.field("folderID", pa.int8()),
        pa.field("date", pa.timestamp(unit="ms")),
        pa.field("conversationID", pa.int32()),
        pa.field("headerMessageID", pa.string()),
        pa.field("from_", pa.int32()),
        pa.field("to", pa.list_(pa.int32())),
        pa.field("cc", pa.list_(pa.int32())),
        pa.field("bcc", pa.list_(pa.int32())),
    ]
)


class Message(NamedTuple):
    id: int
    folderID: int
    headerMessageID: str
    conversationID: str
    date: int
    jsonAttributes: str


class Attachment(NamedTuple):
    name: str
    doc_type: str
    size: int
    field_1: str
    field_2: Any
    field_3: bool


class JsonAttributes(BaseModel):
    from_: int = Field(alias="43")
    to: Optional[list[int]] = Field(alias="44", default_factory=list)
    cc: Optional[list[int]] = Field(alias="45", default_factory=list)
    bcc: Optional[list[int]] = Field(alias="46", default_factory=list)
    attachments: Optional[list[Attachment]] = Field(default_factory=list, alias="51")
    from_me: Optional[bool] = Field(False, alias="54")
    to_me: Optional[bool] = Field(False, alias="55")


def parse_header(filename):
    for f in decode_header(filename):
        body, enc = f
        if enc:
            yield body.decode(enc)
        else:
            yield body


def _lazy_mbox_reader(mbox_path: str):
    with open(mbox_path, "rb") as mbox_file:
        email_content = []
        for line in mbox_file:
            if line.startswith(b"From ") and email_content:
                # Yield the current email
                yield b"".join(email_content)
                email_content = [line]
            else:
                email_content.append(line)
        # Yield the last email
        if email_content:
            yield b"".join(email_content)


class IndexHeader(NamedTuple):
    index: str
    offset: int
    size: int


def _parse_id(content: bytes, offset: int, is_last: bool = False) -> IndexHeader:
    msg = BytesHeaderParser(policy=policy.default).parsebytes(content, headersonly=True)
    idx = msg["Message-ID"]
    return IndexHeader(idx[1:-1], offset, len(content))


def _lazy_mbox_index(mbox_path) -> Generator[IndexHeader, Any, None]:
    with open(mbox_path, "rb") as mbox_file:
        email_content = []
        offset = 0
        for line in mbox_file:
            if line.startswith(b"From ") and email_content:
                # Yield the current email
                yield _parse_id(b"".join(email_content), offset)
                offset = mbox_file.tell() - len(line)
                email_content = [line]
            else:
                email_content.append(line)
        # Yield the last email
        if email_content:
            yield _parse_id(b"".join(email_content), offset)


_query_contacts = """
select 
    c.id, 
    name, 
    i.value 
from contacts c 
join identities i 
    on c.id=i.contactId 
        and i.kind='email'
"""
_query_messages = """
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
where date is not null
"""

_sbrf_domains = ("sberbank.ru", "sbrf.ru")


async def _parse_fio(
    fio_parser: FioParser,
    email: str,
    value: str,
):
    if not email.lower().endswith(_sbrf_domains):
        return await fio_parser.parse_fio(value)
    splitted = value.split(" ")
    if len(splitted) >= 3:
        return FIO(
            last=splitted[0],
            first=splitted[1],
            middle=splitted[2],
        )
    elif len(splitted) == 2:
        return FIO(
            last=splitted[0],
            first=splitted[1],
        )
    elif len(splitted) == 1:
        return FIO(
            last=splitted[0],
        )
    else:
        raise ValueError(f"can not parse fio {value}")


class Index:
    def __init__(self, idx_path: os.PathLike):
        self.path = Path(idx_path)
        self._exit_stack = ExitStack()

    def _init_contacts(self):
        shutil.rmtree(self.path, ignore_errors=True)
        os.makedirs(self.path, exist_ok=True)
        ngram_start = StandardAnalyzer() | NgramFilter(2, 20, at="start")
        ngram = StandardAnalyzer() | NgramFilter(2, 5)
        schema = Schema(
            last=TEXT(stored=True, analyzer=ngram_start),
            first=TEXT(stored=True, analyzer=ngram_start),
            middle=TEXT(stored=True, analyzer=ngram_start),
            id=ID(stored=True, unique=True),
            email=TEXT(stored=True, analyzer=ngram),
        )
        idx = create_in(self.path, schema=schema)
        idx.close()

    @cached_property
    def _contacts_index(self):
        if exists_in(self.path):
            return open_dir(self.path)
        else:
            raise ValueError(f"index not found: {self.path}")

    # def __enter__(self):
    #     self._exit_stack.enter_context(self._index)
    #     return self

    # def __exit__(self, *args):
    #     self._exit_stack.close()

    async def index(
        self,
        db_path: str,
        fio_parser: FioParser,
        progress_enabled: bool = False,
        batch_size: int = 1024,
    ):
        db_path = f"file:{db_path}?mode=ro&immutable=1"
        with (
            tqdm(disable=not progress_enabled) as pbar,
            sqlite3.connect(db_path, uri=True) as conn,
            self._contacts_index.writer() as writer,
        ):
            cursor = conn.execute(_query_contacts)
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    row[1].split(" ")
                    writer.update_document(
                        id=str(row[0]),
                        **(
                            await _parse_fio(
                                fio_parser=fio_parser,
                                value=row[1],
                                email=row[2],
                            )
                        ).model_dump(exclude_none=True),
                        email=row[2],
                    )
                pbar.update(len(rows))

    def search(
        self,
        query: str,
        maxdist: int = 1,
        **kwargs,
    ):
        with self._contacts_index.searcher(weighting=BM25F(B=0.5)) as searcher:
            query = MultifieldParser(
                ["email", "last", "first", "middle"],
                self._contacts_index.schema,
                fieldboosts={"email": 3, "last": 3, "first": 2},
                termclass=partial(FuzzyTerm, maxdist=maxdist),
            ).parse(query)
            results = searcher.search(query, **kwargs)
            return [(x.score, x.fields()) for x in results]

    def _read_messages(db_path: str, batch_size: int = 1024):
        with sqlite3.connect(db_path, uri=True) as conn:
            cursor = conn.execute(_query_messages)
            while rows := cursor.fetchmany(batch_size):
                parsed = []
                for row in rows:
                    msg = Message(*row)
                    if not msg.jsonAttributes:
                        continue
                    json_parsed = JsonAttributes.model_validate_json(msg.jsonAttributes)
                    parsed.append(
                        (
                            msg.id,
                            msg.folderID,
                            datetime.fromtimestamp(msg.date / 1000000),
                            msg.conversationID,
                            msg.headerMessageID,
                            json_parsed.from_,
                            json_parsed.to,
                            json_parsed.cc,
                            json_parsed.bcc,
                        )
                    )
                yield parsed


class Thunderbird:
    def __init__(self, root: os.PathLike):
        self.root = Path(root)

    def _index_contacts(self) -> None:
        with sqlite3.connect(self.root / "global-messages-db.sqlite") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT max(date) FROM messages;")
