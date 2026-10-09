"""Модули src/thunderbird импортируются, SQL read_messages разбирается.

read_messages и read_contacts демон не использует: их импортируют скрипты
experiments/ и блокнот notebooks/thunderbird.ipynb. Читают они базу Gloda
Thunderbird (global-messages-db.sqlite); в тесте — её минимальная схема.
"""

import importlib
import json
import pkgutil
import sqlite3
from datetime import datetime

import pytest

import src.thunderbird
from src.thunderbird import read_messages
from src.thunderbird.read_contacts import read_contacts

_GLODA_SCHEMA = """
create table messages (
    id integer primary key, folderID integer, conversationID integer,
    date integer, headerMessageID text, jsonAttributes text
);
create table messagesText_content (
    docid integer primary key, c0body text, c1subject text
);
create table contacts (id integer primary key, name text);
create table identities (
    id integer primary key, contactID integer, kind text, value text
);
"""


@pytest.mark.parametrize(
    "name", sorted(m.name for m in pkgutil.iter_modules(src.thunderbird.__path__))
)
def test_every_thunderbird_module_imports(name):
    importlib.import_module(f"src.thunderbird.{name}")


@pytest.fixture
def gloda(tmp_path):
    db = tmp_path / "global-messages-db.sqlite"
    conn = sqlite3.connect(db)
    with conn:
        conn.executescript(_GLODA_SCHEMA)
        conn.execute(
            "insert into messages values (1, 5, 10, 1700000000000000, 'h1', ?)",
            (json.dumps({"43": 1, "44": [2]}),),
        )
        conn.execute(
            "insert into messagesText_content values (1, 'Текст письма.', 'Тема')"
        )
        conn.execute("insert into contacts values (1, 'Иванов Иван')")
        conn.execute("insert into identities values (1, 1, 'email', 'i@example.com')")
    conn.close()
    return str(db)


@pytest.mark.parametrize(
    "name", sorted(k for k in vars(read_messages) if k.startswith("_query_"))
)
def test_read_messages_query_compiles(gloda, name):
    sql = getattr(read_messages, name)
    conn = sqlite3.connect(gloda)
    try:
        # EXPLAIN готовит запрос, не выполняя его: ловит и синтаксис, и
        # столбцы, которых нет в схеме.
        conn.execute(f"explain {sql}", {"min_date": None, "conversation_id": None})
    finally:
        conn.close()


def test_read_messages_and_contacts_read_gloda(gloda):
    (msg,) = read_messages.read_messages(gloda)
    assert (msg["id"], msg["c0body"], msg["c1subject"]) == (1, "Текст письма.", "Тема")
    assert (msg["from_"], msg["to"], msg["conversationID"]) == (1, [2], "10")
    assert msg["date"] == datetime.fromtimestamp(1_700_000_000)
    assert list(read_contacts(gloda)) == [
        {"id": 1, "name": "Иванов Иван", "email": "i@example.com"}
    ]
