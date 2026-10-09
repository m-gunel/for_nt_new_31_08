"""/status: итог прохода не теряется при его завершении.

`main_index` снимается в момент завершения прохода: если считать
`files_indexed` и `progress` только при нём, клиент, дождавшийся
`is_indexing=false`, прочитает «проиндексировано 0».
"""

from types import SimpleNamespace

import pytest

from src.common import FileMetadata
from src.indexer import indexer as indexer_module
from src.indexer.indexer import Index

# Index на временном хранилище.
from tests.test_indexing_live import index, isolated_config  # noqa: F401


@pytest.mark.asyncio
async def test_status_reports_last_pass_after_completion(
    index: Index,  # noqa: F811
    monkeypatch,
):
    """После завершения прохода статус отдаёт его итог, а не нули."""

    async def fake_collect():
        index.queued_files = 5
        return 5

    async def fake_count_to_index():
        index.preindex_cnt = 5
        index.counter.reset(5)

    async def fake_process_stream(stream, count=True, store=None):
        # Проход «обработал» всю очередь.
        index.counter.cnt = 0

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(index, "count_to_index", fake_count_to_index)
    monkeypatch.setattr(index, "process_stream", fake_process_stream)
    monkeypatch.setattr(index, "ensure_index", noop)
    monkeypatch.setattr(index, "create_index", noop)

    await index._run_pass(fake_collect, index.files_scanner, index.files_store)

    status = await index.status()
    assert status.is_indexing is False
    assert status.type == "idle"
    assert status.files_indexed == 5, "итог завершённого прохода потерян"
    assert status.total_files == 5
    assert status.progress == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_status_is_zero_before_any_pass(index: Index):  # noqa: F811
    """До первого прохода отдаём честные нули, а не выдуманный прогресс."""
    status = await index.status()
    assert status.is_indexing is False
    assert status.files_indexed == 0
    assert status.total_files == 0
    assert status.progress == 0.0


@pytest.mark.asyncio
async def test_status_is_idle_after_pass_with_repeated_path(
    index: Index,  # noqa: F811
    monkeypatch,
):
    """Путь, дважды попавший в очередь с той же meta, не держит «индексация идёт».

    Счётчик выставляется по размеру очереди вместе с повтором, а повтор
    отбрасывается до разбора. Если его не вычесть, `/status` и MCP
    `local_index_status` после прохода рапортуют работу до следующего прохода
    (по умолчанию час), а `local_trigger_rescan` всё это время отказывает.
    """
    b = FileMetadata(path="/дом/b.txt", size=0, mtime_seconds=1)
    c = FileMetadata(path="/дом/c.txt", size=0, mtime_seconds=1)

    async def fake_collect():
        index.queued_files = 3
        return 3

    async def fake_count_to_index():
        index.preindex_cnt = 3
        index.counter.reset(3)

    async def queue():
        for meta in (b, c, b):
            yield meta

    async def empty_files(files, *, fail_cb, with_emails: bool = False):
        # Разбор без текста: чанков нет, файл уходит в fail_cb.
        async for meta in files:
            await fail_cb(meta, permanent=True)
        return
        yield  # асинхронный генератор, как настоящий async_parse_files

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(index, "count_to_index", fake_count_to_index)
    monkeypatch.setattr(index, "ensure_index", noop)
    monkeypatch.setattr(index, "create_index", noop)
    monkeypatch.setattr(indexer_module, "async_parse_files", empty_files)
    scanner = SimpleNamespace(iter=lambda endless=False: queue())

    await index._run_pass(fake_collect, scanner, index.files_store)

    status = await index.status()
    assert status.files_to_index == 0
    assert status.is_indexing is False, "проход закончен, а статус «индексация идёт»"
    assert status.type == "idle"
