"""Компакция удаляет старые версии таблицы, а не держит их неделю.

Без `cleanup_older_than` LanceDB держит версии 7 дней: переписанные компакцией
фрагменты всё это время лежат на диске, и порог «свободно меньше 1 ГиБ —
индексацию не начинаю» срабатывает раньше.
"""

from datetime import timedelta

import numpy as np
import pyarrow as pa

from src.common.doc_meta import doc_meta_schema
from src.utils import lance_wrapper
from src.utils.lance_wrapper import LanceWrapper

DIM = 8


def _batch(n: int, offset: int) -> pa.Table:
    rng = np.random.default_rng(offset)
    vectors = rng.standard_normal((n, DIM)).astype(np.float32)
    return pa.table(
        {
            "idx": pa.array(range(n), pa.int64()),
            "text": pa.array([f"отчёт {offset + i}" for i in range(n)]),
            "meta": pa.array([None] * n, pa.struct(doc_meta_schema)),
            "path": pa.array([f"/home/u/f{offset + i}.txt" for i in range(n)]),
            "vector": pa.FixedSizeListArray.from_arrays(vectors.reshape(-1), DIM),
        }
    )


def test_compact_removes_versions_older_than_retention(tmp_path, monkeypatch):
    # Состарить версии в тесте нельзя, поэтому срок хранения — ноль.
    monkeypatch.setattr(lance_wrapper, "COMPACT_KEEP_VERSIONS", timedelta(0))
    store = LanceWrapper(str(tmp_path / "lance"), DIM, "files")
    for offset in range(0, 1000, 100):
        store.add_sync(_batch(100, offset))
    before = len(store.table.list_versions())

    store.compact()

    assert len(store.table.list_versions()) < before, (
        "версии старше срока хранения компакция обязана удалять"
    )
    assert store.table.count_rows() == 1000, "последняя версия читается целиком"


def test_compact_keeps_recent_versions_for_running_searches():
    """Срок хранения — не ноль (поиск дочитывает прежнюю версию) и не неделя."""
    keep = lance_wrapper.COMPACT_KEEP_VERSIONS
    assert timedelta(minutes=10) <= keep < timedelta(days=7)
