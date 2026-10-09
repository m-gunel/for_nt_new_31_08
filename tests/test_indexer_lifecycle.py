"""Остановка и перезапуск фоновых задач индексатора.

Проход живёт в одной из двух задач (self.task, rescan_task). Проверки — на
настоящем Index во временном хранилище; ручки роутера зовутся напрямую,
watcher подменён.
"""

import asyncio
import os
import time
from contextlib import suppress

import pytest
import pytest_asyncio
from pytest import fixture

import src.indexer.scanners.files_scanner as files_scanner_module
from src.config import Config
from src.indexer.indexer import Index, _await_stopped
from src.server.model import UserConfig
from src.server.router import (
    deactivate_indexing,
    reset_indexing,
    update_config,
)
from src.utils.lsm_adapter import to_thread as lsm_to_thread

_TICK = 0.01


async def _wait_until(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_TICK)
    return bool(predicate())


async def _fail_passes(index: Index) -> asyncio.Task:
    """Проход падает так же, как в бою: scan_all логирует ошибку и пробрасывает."""

    async def boom():
        raise RuntimeError("Infer Request is busy")

    index._scan_all = boom  # type: ignore[method-assign]
    await index.start_reindex()
    await asyncio.wait({index.task})
    assert isinstance(index.task.exception(), RuntimeError)
    return index.task


def _rows(store, prefix: str) -> int:
    return store.count_matching(f"starts_with(path, '{prefix}')")


async def _lsm_keys(index: Index, prefix: str) -> list[str]:
    keys = await lsm_to_thread(index.lsm_collection.indexed.keys_snapshot)
    return [k for k in keys if k.startswith(prefix)]


@fixture
def cfg(config: Config, tmp_path, monkeypatch):
    """Хранилище и user_conf.yml — во временном каталоге."""
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    monkeypatch.setattr(config, "lsm_root", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "storage" / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "storage" / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config.file_scan_cfg, "paths", [])
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)
    yield config
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)


@fixture
def no_watcher(monkeypatch):
    import src.server.router as router_module

    async def nothing():
        pass

    monkeypatch.setattr(router_module, "stop_watcher", nothing)
    monkeypatch.setattr(router_module, "restart_watcher", nothing)


async def _serve(cfg: Config):
    idx = Index(cfg)
    async with idx:
        try:
            yield idx
        finally:
            # Ни одна фоновая задача теста не переживает индекс.
            await idx.deactivate()


@pytest_asyncio.fixture
async def index(cfg: Config):
    async for idx in _serve(cfg):
        yield idx


# --- T008: остановка не поднимает ошибку упавшей задачи ---------------------


@pytest.mark.asyncio
async def test_stop_index_survives_failed_pass(index: Index):
    """Упавший проход не ломает ни первую, ни повторную остановку.

    Ошибка прохода уже в логе; поднятая из stop_index(), она отвечала бы 500
    на /config, /deactivate и /reset до рестарта: упавшая задача так и лежит
    в self.task.
    """
    await _fail_passes(index)

    await index.stop_index()
    await index.stop_index()


@pytest.mark.asyncio
async def test_control_endpoints_work_after_failed_pass(
    cfg: Config, index: Index, no_watcher, tmp_path
):
    """После упавшего прохода ручки управления отвечают 200 и доделывают своё.

    Иначе POST /config успевал записать конфиг и падал до delete_prefix:
    повтор видел prev_paths == new_paths, и снятая папка оставалась в
    индексе навсегда.
    """
    kept, removed = tmp_path / "A", tmp_path / "B"
    for d in (kept, removed):
        d.mkdir()
    cfg.update_user_config(UserConfig(folders=[str(kept), str(removed)]))
    deleted: list[str] = []
    real_delete = index.delete_prefix

    async def spy(prefix: str) -> None:
        deleted.append(prefix)
        await real_delete(prefix)

    index.delete_prefix = spy  # type: ignore[method-assign]
    await _fail_passes(index)

    response = await update_config(UserConfig(folders=[str(kept)]), cfg, index)

    assert response.status_code == 200
    assert deleted == [str(removed) + os.sep], "снятая папка не снята с индекса"

    # start_reindex() внутри /config снова упал — /deactivate и /reset на нём.
    await asyncio.wait({index.task})
    assert (await deactivate_indexing(index)).active is False
    assert (await reset_indexing(index, cfg, keep_config=True)).changed is True
    assert not index.files_store.has_table, "сброс не дошёл до дропа таблиц"


@pytest.mark.asyncio
async def test_lifespan_exit_survives_failed_pass(cfg: Config, monkeypatch):
    """Остановка демона не вылетает исключением упавшего прохода."""
    monkeypatch.setattr(cfg, "autostart_indexing", False)
    index = Index(cfg)
    async with index.lifespan():
        await _fail_passes(index)


# --- T013: остановка гасит идущий проход ------------------------------------


@pytest.mark.asyncio
async def test_removed_folder_is_not_reindexed_by_running_periodic_pass(
    cfg: Config, index: Index, no_watcher, tmp_path, monkeypatch
):
    """Снятая папка не возвращается в индекс идущим периодическим проходом.

    Периодический проход обходит список папок, взятый на старте, и после
    delete_prefix записал бы снятую папку обратно — навсегда.
    """
    kept, removed = tmp_path / "A", tmp_path / "B"
    for d in (kept, removed):
        d.mkdir()
        for i in range(3):
            (d / f"doc{i}.txt").write_text(
                f"Документ {d.name} номер {i}. " * 20, encoding="utf-8"
            )
    cfg.update_user_config(UserConfig(folders=[str(kept), str(removed)]))
    await index.scan_all()
    removed_prefix = str(removed) + os.sep
    assert _rows(index.files_store, removed_prefix) > 0

    real_iter = files_scanner_module.iter_file_metadata

    def slow_iter(*args, **kwargs):
        for meta in real_iter(*args, **kwargs):
            time.sleep(0.2)
            yield meta

    monkeypatch.setattr(files_scanner_module, "iter_file_metadata", slow_iter)
    # Как run_periodic_rescan: полный проход в rescan_task.
    periodic = asyncio.create_task(index.scan_all())
    index.rescan_task = periodic
    assert await _wait_until(lambda: index.preindex), "проход не начался"

    response = await update_config(UserConfig(folders=[str(kept)]), cfg, index)

    assert response.status_code == 200
    with suppress(asyncio.CancelledError):
        await periodic
    await asyncio.wait({index.task})
    assert _rows(index.files_store, removed_prefix) == 0, "снятая папка снова в Lance"
    assert await _lsm_keys(index, removed_prefix) == [], "снятая папка снова в LSM"


@pytest.mark.asyncio
async def test_folder_change_keeps_periodic_rescan_alive(
    cfg: Config, index: Index, no_watcher, tmp_path, monkeypatch
):
    """stop_index() гасит и периодический проход, а start_reindex() поднимает его."""
    monkeypatch.setattr(cfg, "full_rescan_interval", 3600.0)

    async def nothing():
        return None

    index._scan_all = nothing  # type: ignore[method-assign]
    await index.start_periodic_rescan()
    folder = tmp_path / "docs"
    folder.mkdir()

    await update_config(UserConfig(folders=[str(folder)]), cfg, index)

    assert index.rescan_task is not None and not index.rescan_task.done(), (
        "после смены папок периодический проход не поднят"
    )


# --- T095: закрытие индекса гасит фоновые задачи ---------------------------


@pytest.mark.asyncio
async def test_closing_index_stops_index_refresh(cfg: Config, monkeypatch):
    """После закрытия индекса пересборка не ходит в закрытые LSM и LanceDB."""
    monkeypatch.setattr(cfg, "index_refresh_delay", 30.0)
    index = Index(cfg)
    async with index:
        await index.ensure_index()
        refresh = index._index_task
    try:
        assert refresh.done(), "отложенная пересборка пережила закрытие индекса"
    finally:
        refresh.cancel()
        with suppress(asyncio.CancelledError):
            await refresh


@pytest.mark.asyncio
async def test_await_stopped_keeps_cancel_of_waiter():
    """Отмена того, кто ждёт остановки задачи, не теряется.

    Иначе ожидающий, отменённый на время свёртки задачи, продолжил бы работу.
    """
    winding_down = asyncio.Event()

    async def stage():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            winding_down.set()
            await asyncio.sleep(0.2)
            raise

    inner = asyncio.create_task(stage())
    await asyncio.sleep(0)
    inner.cancel()

    async def waiter():
        await _await_stopped(inner, "этап")
        return "продолжил"

    outer = asyncio.create_task(waiter())
    await winding_down.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_await_stopped_swallows_cancel_of_stopped_task():
    """Отмена самой останавливаемой задачи — штатный исход, не ошибка."""
    inner = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.sleep(0)
    inner.cancel()
    await _await_stopped(inner, "этап")
    assert inner.cancelled()
