"""Параметры полнотекстового индекса и их миграция на готовом хранилище.

В пресете `ru_stem` max_token_length=40 байт UTF-8, и русские слова от 20
букв выпадали бы и из индекса, и из запроса. Поэтому параметры явные
(`FtsConfig`), а индекс со старыми пересобирается при старте демона
(`Index.migrate_fts_config`).
"""

from types import SimpleNamespace

import pytest

from src.common.fts import FtsConfig
from src.indexer.indexer import Index
from src.utils.lance_wrapper import LanceWrapper

# Index на временном хранилище.
from tests.test_indexing_live import (  # noqa: F401
    EMBED_DIM,
    _chunk_table,
    index,
    isolated_config,
)

# 20 букв = 40 байт UTF-8: такие слова пресет ru_stem отбрасывает.
LONG = "сельскохозяйственный"


def _fts_details(store: LanceWrapper) -> dict:
    [idx] = [
        i
        for i in store.lancedb.open_table(store.table_name).list_indices()
        if i.index_type == "FTS"
    ]
    return idx.index_details


def _old_index(store: LanceWrapper) -> None:
    """Индекс, каким его строили до явных параметров."""
    store.lancedb.open_table(store.table_name).create_fts_index(
        "text", use_tantivy=False, tokenizer_name="ru_stem", replace=True
    )


def _found(store: LanceWrapper, query: str) -> list[str]:
    """Поиск, как в демоне: через закэшированный хендл store.table."""
    rows = store.table.search(query, query_type="fts").limit(10).to_list()
    return [r["path"] for r in rows]


@pytest.mark.asyncio
async def test_new_index_follows_config(tmp_path):
    store = LanceWrapper(str(tmp_path / "lance"), EMBED_DIM)
    await store.add_batch(
        _chunk_table("/tmp/счёт.txt", f"Выставлен СЧЁТ, {LONG} кредит")
    )
    store.create_fts(force=True)  # индекс по строкам, а не по пустой таблице

    assert store.fts_config_diff() == {}
    details = _fts_details(store)
    assert details["max_token_length"] == 100
    assert details["remove_stop_words"] is False
    assert details["ascii_folding"] is False

    table = store.table
    long_word = [t.text for t in table.tokenize(LONG.upper(), column="text")]
    assert len(long_word) == 1, "длинное слово должно остаться одним токеном"
    # Регистр и ё/е склеивает русский стеммер.
    forms = {
        tuple(t.text for t in table.tokenize(w, column="text"))
        for w in ("Счёт", "СЧЕТА", "счет")
    }
    assert len(forms) == 1, forms
    assert _found(store, "счет") == ["/tmp/счёт.txt"]
    assert _found(store, LONG) == ["/tmp/счёт.txt"]


@pytest.mark.asyncio
async def test_migration_rebuilds_old_index(index: Index):  # noqa: F811
    store = index.files_store
    await store.add_batch(
        _chunk_table("/tmp/кредит.txt", f"Выдан {LONG} кредит", index.cfg.embed_dim)
    )
    _old_index(store)
    assert store.fts_config_diff() == {"max_token_length": (40, 100)}
    store.table  # noqa: B018 — хендл закэширован на снимке со старым индексом

    assert await index.migrate_fts_config() == ["files"]

    assert store.fts_config_diff() == {}
    # Через тот же закэшированный хендл: без пометки «устарел» поиск остался
    # бы на старом индексе, где длинного слова нет.
    assert _found(store, LONG) == ["/tmp/кредит.txt"]
    assert not index.email_store.has_table, "миграция не должна создавать таблицу почты"


@pytest.mark.asyncio
async def test_migration_skips_matching_index(index: Index):  # noqa: F811
    store = index.files_store
    await store.add_batch(
        _chunk_table("/tmp/a.txt", "договор поставки", index.cfg.embed_dim)
    )
    store.create_fts(force=True)
    before = [i.index_uuid for i in store.table.list_indices() if i.index_type == "FTS"]

    assert await index.migrate_fts_config() == []

    after = [
        i.index_uuid
        for i in store.lancedb.open_table("files").list_indices()
        if i.index_type == "FTS"
    ]
    assert after == before


@pytest.mark.asyncio
async def test_migration_without_tables_does_nothing(index: Index):  # noqa: F811
    assert await index.migrate_fts_config() == []
    assert not index.files_store.has_table
    assert not index.email_store.has_table


@pytest.mark.asyncio
async def test_index_without_details_is_a_mismatch(tmp_path, monkeypatch):
    """Индекс старой версии lancedb мог не отдавать параметры — пересобираем."""
    store = LanceWrapper(str(tmp_path / "lance"), EMBED_DIM)
    store.table  # noqa: B018 — создать таблицу
    fake_index = SimpleNamespace(index_type="FTS", columns=["text"], index_details=None)
    fake_table = SimpleNamespace(list_indices=lambda: [fake_index])
    monkeypatch.setitem(
        store.__dict__, "lancedb", SimpleNamespace(open_table=lambda name: fake_table)
    )

    diff = store.fts_config_diff()
    assert set(diff) == set(FtsConfig.model_fields)
    assert diff["max_token_length"] == (None, 100)
