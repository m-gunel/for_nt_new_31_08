"""Индекс, собранный до появления карточек документов: код из имени и карточки.

Карточку пишет разбор файла, а неизменённый файл проход заново не разбирает
(LSM сверяет только size и mtime). Поэтому словарь имён строится по первым
чанкам, а карточки старым документам дописывает миграция из meta.
"""

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from src.common import CARD_IDX, CARD_KIND, document_card_text
from src.common.doc_meta import doc_meta_schema
from src.indexer import indexer as indexer_module
from src.indexer.indexer import Index
from src.server.model import SearchRequest
from src.server.router import search as search_endpoint
from tests.test_indexing_live import index, isolated_config  # noqa: F401

# путь → свойства документа; у каждого по два чанка и ни одной карточки.
OLD_DOCS = {
    "/docs/ЦБ_21 положение.txt": {"author": "Центробанк"},
    "/docs/резервы 1.txt": {"title": "Резервы банка"},
    "/docs/резервы 2.txt": {},
}


def _old_index_rows(embed) -> pa.Table:
    rows = [
        (path, i, f"Резервы банка, найди файл, раздел {i}")
        for path in OLD_DOCS
        for i in range(2)
    ]
    vecs = np.asarray(embed([t for *_, t in rows]), dtype=np.float32)
    return pa.table(
        {
            "idx": pa.array([i for _, i, _ in rows], pa.int64()),
            "text": pa.array([t for *_, t in rows], pa.string()),
            "kind": pa.array(["text"] * len(rows), pa.string()),
            "meta": pa.array(
                [
                    {"original_name": Path(p).name, "sha256": p, **OLD_DOCS[p]}
                    for p, *_ in rows
                ],
                pa.struct(doc_meta_schema),
            ),
            "path": pa.array([p for p, *_ in rows], pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(vecs.ravel(), vecs.shape[1]),
        }
    )


async def _old_index(rag: Index) -> None:
    store = rag.cfg.lance.files
    await store.add_batch(_old_index_rows(rag.cfg.ov_model.encode))
    store.create_fts(force=True)


def _cards(rag: Index) -> dict[str, list[str]]:
    rows = (
        rag.cfg.lance.files.table.search()
        .where(f"kind = '{CARD_KIND}'")
        .select(["path", "text", "idx"])
        .to_list()
    )
    assert all(r["idx"] == CARD_IDX for r in rows)
    cards: dict[str, list[str]] = {}
    for row in rows:
        cards.setdefault(row["path"], []).append(row["text"])
    return cards


async def _search(rag: Index, query: str, query_type: str = "hybrid"):
    request = SearchRequest(query=query, limit=3, query_type=query_type)
    return (await search_endpoint(request, rag)).results


@pytest.mark.asyncio
async def test_code_from_name_works_on_index_without_cards(index: Index):  # noqa: F811
    await _old_index(index)
    assert _cards(index) == {}

    for query_type in ("hybrid", "vector", "text"):
        items = await _search(index, "Найди файл ЦБ_21", query_type)
        assert items[0].absolute_path == "/docs/ЦБ_21 положение.txt", query_type
        assert items[0].match[0] == "name_code", query_type


@pytest.mark.asyncio
async def test_migration_writes_the_cards_indexing_would_write(index: Index):  # noqa: F811
    await _old_index(index)

    assert await index.migrate_document_cards() == len(OLD_DOCS)

    meta = {p: {"original_name": Path(p).name, **m} for p, m in OLD_DOCS.items()}
    assert _cards(index) == {p: [document_card_text(meta[p])] for p in OLD_DOCS}
    assert await index.migrate_document_cards() == 0, "второй раз дописывать нечего"
    assert len(_cards(index)) == len(OLD_DOCS)

    # Карточка ищется: автор есть только в ней, не в тексте чанков.
    (item,) = [
        i
        for i in await _search(index, "Центробанк", "text")
        if i.absolute_path == "/docs/ЦБ_21 положение.txt"
    ]
    assert item.match == ["card"]


@pytest.mark.asyncio
async def test_first_indexing_pass_migrates_cards_once(index: Index, monkeypatch):  # noqa: F811
    await _old_index(index)
    calls = []
    migrate = index.migrate_document_cards

    async def counted():
        calls.append(1)
        return await migrate()

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(index, "migrate_document_cards", counted)
    monkeypatch.setattr(index, "reconcile_deleted", nothing)
    monkeypatch.setattr(index, "_run_pass", nothing)
    monkeypatch.setattr(index, "start_emails_polling", nothing)

    await index._scan_all()
    assert set(_cards(index)) == set(OLD_DOCS)
    await index._scan_all()
    await index._scan_all(emails_only=True)
    assert calls == [1], "сверка карточек — раз на процесс, только в файловом проходе"


@pytest.mark.asyncio
async def test_migration_leaves_no_card_of_file_removed_meanwhile(
    index: Index,  # noqa: F811
    monkeypatch,
):
    """Файл, снятый с индекса, пока считаются векторы карточек, без карточки.

    Миграция читает первые чанки, минутами считает векторы и только потом
    пишет. Карточку удалённого за это время файла никто больше не снимет:
    сверка удалённых ходит по ключам LSM, а ключа уже нет.
    """
    await _old_index(index)
    gone, *kept = OLD_DOCS
    embed = indexer_module.async_embed_documents_into_batches

    def removed_while_embedding(rows, **kwargs):
        async def batches():
            # Watcher: файл удалён с диска, пока миграция считает векторы.
            await index.delete_path(gone)
            async for batch in embed(rows, **kwargs):
                yield batch

        return batches()

    monkeypatch.setattr(
        indexer_module, "async_embed_documents_into_batches", removed_while_embedding
    )

    assert await index.migrate_document_cards() == len(kept)

    paths = set(index.cfg.lance.files.table.to_arrow().column("path").to_pylist())
    assert gone not in paths, "карточка удалённого файла осталась в поиске"
    assert set(_cards(index)) == set(kept)
