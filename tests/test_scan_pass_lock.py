"""Полный проход индексации — один на процесс, и сбой его не запирает.

Index настоящий, на временном хранилище с одной папкой документов.
"""

import asyncio

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config
from src.indexer.indexer import Index


@fixture
def docs_dir(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "отчёт.txt").write_text(
        "Квартальный отчёт по продажам. Выручка выросла на 12 процентов.",
        encoding="utf-8",
    )
    return docs


@fixture
def docs_config(config: Config, docs_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(docs_dir)])
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@pa_fixture.fixture
async def index(docs_config: Config):
    idx = Index(docs_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()


@pytest.mark.asyncio
async def test_second_pass_is_skipped_while_one_is_running(index: Index):
    """Два потребителя очереди заклинивают друг друга — проход должен быть один.

    `iter(endless=False)` проверяет пустоту до блокирующего `get()`, поэтому
    опоздавший ждёт элемент вечно.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    passes = 0
    real_run = index._run_pass

    async def slow_pass(collect, scanner, store):
        nonlocal passes
        passes += 1
        started.set()
        await release.wait()
        return await real_run(collect, scanner, store)

    index._run_pass = slow_pass  # type: ignore[method-assign]
    first = asyncio.create_task(index.scan_all())
    await asyncio.wait_for(started.wait(), 5)

    await index.scan_all()  # должен быть пропущен

    assert passes == 1, "второй проход обязан быть пропущен, а не запущен"
    release.set()
    await asyncio.wait_for(first, 30)


@pytest.mark.asyncio
async def test_pass_lock_is_released_after_failure(index: Index, monkeypatch):
    """Упавший проход не должен запирать индексацию до рестарта демона."""

    async def boom(*args, **kwargs):
        raise RuntimeError("сбой прохода")

    monkeypatch.setattr(index, "_run_pass", boom)
    with pytest.raises(RuntimeError):
        await index.scan_all()

    assert not index._scan_lock.locked()
