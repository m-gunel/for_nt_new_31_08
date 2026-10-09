"""Сборка без модуля lance (pylance).

`LanceTable.to_lance()` начинается с `import lance`, а main.spec долго
исключал пакет из бандла. Без модуля дообновление индексов должно
пропускаться с предупреждением в журнале, а перенос писем из таблицы файлов —
идти запросом lancedb и не терять уже записанное в почтовую таблицу.
"""

import logging
import re
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from src.common.doc_meta import doc_meta_schema
from src.indexer.indexer import EMAIL_ROWS_PREDICATE, Index
from src.utils.lance_wrapper import LanceWrapper, new_lance_session

# Index с почтой на синтетическом профиле и индекс «до разделения таблиц».
from tests.test_source_separation import (  # noqa: F401
    _mixed_rows,
    docs_dir,
    email_config,
    index,
    mail_root,
)

ROOT = Path(__file__).resolve().parents[1]
DIM = 8
EMAIL_ROWS = "starts_with(path, 'email_')"


class _NoLance:
    """Как в бандле без пакета: `import lance` даёт ModuleNotFoundError."""

    def find_spec(self, name, path=None, target=None):
        if name == "lance" or name.startswith("lance."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


@pytest.fixture
def without_lance(monkeypatch):
    # Уже импортированный lance убираем на время теста, а teardown вернёт те же
    # объекты: нативное расширение повторно не инициализируется. Подмена
    # sys.modules['lance'] = None не годится: lancedb сам проверяет
    # `"lance" in sys.modules` и с ней не работает.
    for name in [m for m in sys.modules if m == "lance" or m.startswith("lance.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoLance(), *sys.meta_path])


def _rows(paths: list[str], seed: int = 1, dim: int = DIM) -> pa.Table:
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((len(paths), dim)).astype(np.float32)
    return pa.table(
        {
            "idx": pa.array(range(len(paths)), pa.int64()),
            "text": pa.array([f"текст {p}" for p in paths]),
            "meta": pa.array([None] * len(paths), pa.struct(doc_meta_schema)),
            "path": pa.array(paths),
            "vector": pa.FixedSizeListArray.from_arrays(vectors.reshape(-1), dim),
        }
    )


def _stores(tmp_path) -> tuple[LanceWrapper, LanceWrapper]:
    session = new_lance_session()
    db = str(tmp_path / "lance")
    return (
        LanceWrapper(db, DIM, "files", session=session),
        LanceWrapper(db, DIM, "files_email", session=session),
    )


def _paths(store: LanceWrapper) -> list[str]:
    rows = store.table.search().select(["path"]).to_arrow()
    return sorted(rows.column("path").to_pylist())


def test_optimize_indices_without_lance_warns_once(tmp_path, without_lance, caplog):
    files, _ = _stores(tmp_path)
    files.add_sync(_rows(["/home/u/отчёт.txt"]))

    with caplog.at_level(logging.WARNING):
        files.optimize_indices()
        files.optimize_indices()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "lance" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        "без модуля lance индексы не дообновляются — об этом нужна одна строка "
        f"WARNING, а не DEBUG на каждый цикл: {caplog.text!r}"
    )


def test_move_rows_works_without_lance(tmp_path, without_lance):
    files, email = _stores(tmp_path)
    mails = [f"email_INBOX:{i}:{i + 1}:body" for i in range(1500)]
    files.add_sync(_rows(["/home/u/отчёт.txt", *mails]))

    moved = files.move_rows_to(email, EMAIL_ROWS, batch_size=1024)

    assert moved == len(mails)
    assert _paths(email) == sorted(mails)
    assert _paths(files) == ["/home/u/отчёт.txt"]


def test_move_rows_keeps_rows_already_in_target(tmp_path, without_lance):
    """Повтор после обрыва: остаток прошлой копии заменяется, чужие письма целы.

    Письмо, записанное в почтовую таблицу после неудачной миграции, LSM уже
    отметил: удалив его, индексатор его больше не вернёт.
    """
    files, email = _stores(tmp_path)
    files.add_sync(
        _rows(["/home/u/отчёт.txt", "email_INBOX:0:1:body", "email_INBOX:1:2:body"])
    )
    # Остаток прерванного переноса и письмо, проиндексированное уже в email.
    email.add_sync(_rows(["email_INBOX:0:1:body", "email_INBOX:5:6:body"], seed=2))

    files.move_rows_to(email, EMAIL_ROWS)

    assert _paths(email) == [
        "email_INBOX:0:1:body",
        "email_INBOX:1:2:body",
        "email_INBOX:5:6:body",
    ], "без дублей и без потери письма, которого в files не было"
    assert _paths(files) == ["/home/u/отчёт.txt"]


@pytest.mark.asyncio
async def test_migration_without_lance_keeps_email_table(index: Index, without_lance):  # noqa: F811
    """Старый общий индекс, почтовая таблица уже не пуста: перенос без потерь."""
    dim = index.cfg.embed_dim
    await index.files_store.add_batch(_mixed_rows(dim))
    # Письмо, проиндексированное в почтовую таблицу после неудачной миграции.
    await index.email_store.add_batch(_rows(["email_INBOX:900:950:body"], dim=dim))

    moved = await index.migrate_emails_out_of_files_table()

    assert moved == 2
    assert index.files_store.count_matching(EMAIL_ROWS_PREDICATE) == 0
    assert sorted(index.email_store.paths_matching(EMAIL_ROWS_PREDICATE)) == [
        "email_INBOX:0:100:body",
        "email_INBOX:100:200:body",
        "email_INBOX:900:950:body",
    ]


def test_spec_does_not_exclude_lance():
    """optimize_indices работает только с пакетом lance в бандле."""
    spec = (ROOT / "main.spec").read_text(encoding="utf-8")
    excludes = re.search(r"excludes=\[(.*?)\]", spec, re.S)
    assert excludes is not None
    assert "'lance'" not in excludes.group(1) and '"lance"' not in excludes.group(1)
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert not re.search(r"--exclude-module[= ]lance\b", makefile)
