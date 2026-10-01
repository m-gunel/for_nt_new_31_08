"""Регрессия на /status: итог прохода не должен теряться при его завершении.

`files_indexed` и `progress` считались только пока поднят `main_index`, а он
снимается в тот же момент, когда проход завершается. Клиент, дождавшийся
`is_indexing=false`, читал «проиндексировано 0»: в НТ 27.08 стенд записал
`bulk_index_end {'indexed': 0, 'progress': 0.0}` для прогона, где реально
обработано 680 файлов.
"""

import pytest

from src.indexer.indexer import Index

# Фикстуры поднимают Index на временном хранилище — переиспользуем их.
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
