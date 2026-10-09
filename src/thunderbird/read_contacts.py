"""Контакты из базы Gloda Thunderbird (global-messages-db.sqlite).

Демон модуль не использует — он нужен скриптам experiments/.
"""

import sqlite3
from typing import TypedDict

import pyarrow as pa
from rich.progress import Progress


class _ContactRow(TypedDict):
    id: int
    name: str
    email: str


CONTACTS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("name", pa.string()),
        pa.field("email", pa.string()),
    ]
)


_query_contacts = """
SELECT 
    c.id, 
    name, 
    i.value 
FROM contacts c 
JOIN identities i 
    ON c.id=i.contactId 
        AND i.kind='email'
"""


def read_contacts(
    db_path: str,
    progress: Progress | None = None,
):
    """Словари {id, name, email} (_ContactRow), по одному на адрес контакта.

    NULL в имени или адресе отдаётся пустой строкой.
    """
    task = progress.add_task("Reading contacts...", total=None) if progress else None

    with sqlite3.connect(db_path, uri=True) as conn:
        cursor = conn.cursor()

        if progress and task is not None:
            cursor.execute("SELECT COUNT(*) FROM contacts")
            total = cursor.fetchone()[0]
            progress.update(task, total=total)

        cursor.execute(_query_contacts)

        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break

            for row in rows:
                contact = _ContactRow(id=row[0], name=row[1] or "", email=row[2] or "")
                if progress and task is not None:
                    progress.advance(task)
                yield dict(contact)
