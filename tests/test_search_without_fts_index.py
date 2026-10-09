"""Поиск по таблице, где строки есть, а FTS-индекса нет, не отдаёт 500.

Так выглядит база, у которой FTS-индекс не построен или потерян. LanceDB 0.39
ищет без индекса перебором; версия, которая в этом состоянии бросает, опознаётся
по тексту исключения (`_is_missing_fts_index`): гибрид откатывается на вектор,
текстовый поиск — на пустую выдачу.
"""

import numpy as np
import pyarrow as pa
import pytest
from lancedb.query import MatchQuery

from src.common import chunk_with_meta_schema
from src.common.doc_meta import doc_meta_schema
from src.indexer.indexer import Index, _is_missing_fts_index

# Index на временном хранилище.
from tests.test_indexing_live import index, isolated_config  # noqa: F401

DOC = "/tmp/договор.txt"
# Формулировка LanceDB, на которую написан откат (src/indexer/indexer.py).
MISSING_FTS = (
    "Cannot perform full text search unless an INVERTED index has been created"
)


def _table_without_fts(idx: Index):
    """Таблица файлов со строкой и без FTS-индекса.

    Мимо LanceWrapper.create_table: тот строит FTS-индекс сразу при создании.
    Вектор ненулевой: нулевой при cosine из выдачи выпадает.
    """
    store = idx.files_store
    dim = idx.cfg.embed_dim
    schema = chunk_with_meta_schema.append(
        pa.field("vector", pa.list_(pa.float32(), dim))
    )
    table = store.lancedb.create_table(store.table_name, schema=schema)
    table.add(
        pa.table(
            {
                "idx": pa.array([0], pa.int64()),
                "text": pa.array(["договор поставки оборудования"]),
                "meta": pa.array([None], pa.struct(doc_meta_schema)),
                "path": pa.array([DOC]),
                "vector": pa.FixedSizeListArray.from_arrays(
                    np.ones(dim, dtype=np.float32), dim
                ),
            }
        )
    )
    assert store.has_table and not store.fts_created, "индекса быть не должно"
    return store


def _lancedb_searches_without_fts(table) -> bool:
    """True — LanceDB ищет без индекса перебором, False — бросает «нет индекса».

    Исключение берётся от настоящей LanceDB: новая формулировка при обновлении
    библиотеки упадёт здесь, а не у пользователя пятисоткой.
    """
    try:
        rows = (
            table.search(
                MatchQuery("договор поставки", "text"),
                query_type="fts",
                fts_columns="text",
            )
            .limit(5)
            .to_list()
        )
    except Exception as exc:
        assert _is_missing_fts_index(exc), f"незнакомая формулировка LanceDB: {exc}"
        return False
    assert [r["path"] for r in rows] == [DOC]
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["hybrid", "vector", "text"])
async def test_search_survives_missing_fts_index(index: Index, query_type):  # noqa: F811
    """Строки есть, FTS-индекса нет — поиск отвечает и находит документ."""
    store = _table_without_fts(index)
    flat = _lancedb_searches_without_fts(store.table)

    frame = await index.search("договор поставки", top_k=5, query_type=query_type)

    if query_type == "text" and not flat:
        assert frame is None, "текстовый поиск без индекса — пустая выдача"
    else:
        assert frame is not None
        assert DOC in frame["path"].to_list()


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["hybrid", "text"])
async def test_search_degrades_when_lancedb_reports_missing_fts(
    index: Index,  # noqa: F811
    monkeypatch,
    query_type,
):
    """LanceDB, которая без индекса бросает: гибрид — вектор, текст — пусто."""
    table = _table_without_fts(index).table
    real_search = table.search

    def search(*args, **kwargs):
        if kwargs.get("query_type") in ("fts", "hybrid"):
            raise RuntimeError(MISSING_FTS)
        return real_search(*args, **kwargs)

    monkeypatch.setattr(table, "search", search)

    frame = await index.search("договор поставки", top_k=5, query_type=query_type)

    if query_type == "hybrid":
        assert frame is not None and DOC in frame["path"].to_list()
    else:
        assert frame is None


@pytest.mark.asyncio
async def test_search_on_empty_storage_returns_nothing(index: Index):  # noqa: F811
    """Таблицы ещё нет вовсе: поиск не должен её создавать и не должен падать."""
    frame = await index.search("что угодно", top_k=5)
    assert frame is None
    assert not index.cfg.lance.files.has_table, "поиск не должен создавать таблицу"


def test_missing_fts_index_is_recognised_by_message():
    """Страховка на случай, если формулировку LanceDB изменят."""
    assert _is_missing_fts_index(Exception(MISSING_FTS))
    assert not _is_missing_fts_index(Exception("disk is full"))
