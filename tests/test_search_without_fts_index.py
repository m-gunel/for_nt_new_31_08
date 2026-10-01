"""Поиск во время первой индексации не должен отдавать 500.

Пока идёт первый проход, строки в таблице уже есть, а полнотекстовый индекс
ещё не построен: LanceDB на такой запрос бросает исключение. Демон обязан
деградировать — гибридный поиск до векторного, текстовый до пустой выдачи —
и вернуть ответ, а не пятисотку.

Опознаётся эта ситуация по тексту исключения (`_is_missing_fts_index`), то
есть держится на формулировке LanceDB. Тест нужен именно поэтому: при
обновлении библиотеки формулировка может измениться, и защита отвалится молча.
"""

import pytest

from src.indexer.indexer import Index, _is_missing_fts_index

# Фикстуры поднимают Index на временном хранилище — переиспользуем их.
from tests.test_indexing_live import _chunk_table, index, isolated_config  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", ["hybrid", "vector", "text"])
async def test_search_survives_missing_fts_index(index: Index, query_type):  # noqa: F811
    """Строки есть, FTS-индекса нет — поиск обязан ответить."""
    await index.cfg.lance.files.add_batch(
        _chunk_table("/tmp/договор.txt", "договор поставки оборудования", index.cfg.embed_dim)
    )

    frames = await index.search("договор поставки", top_k=5, query_type=query_type)

    # Ответ может быть и пустым (текстовый поиск без индекса), но исключения
    # быть не должно — именно оно превращалось в 500 у клиента.
    assert isinstance(frames, list)


@pytest.mark.asyncio
async def test_search_on_empty_storage_returns_nothing(index: Index):  # noqa: F811
    """Таблицы ещё нет вовсе: поиск не должен её создавать и не должен падать."""
    frames = await index.search("что угодно", top_k=5)
    assert frames == []
    assert not index.cfg.lance.files.has_table, "поиск не должен создавать таблицу"


def test_missing_fts_index_is_recognised_by_message():
    """Страховка на случай, если формулировку LanceDB изменят."""
    assert _is_missing_fts_index(
        Exception("Cannot perform full text search unless an INVERTED index has been created")
    )
    assert not _is_missing_fts_index(Exception("disk is full"))
