"""Watcher переживает несуществующий путь в конфиге.

inotify отказывает по отсутствующему каталогу при старте эмиттера, и падает
весь observer — вместе с наблюдением за живыми папками: одного мусорного пути
("string" из Swagger) хватило бы, чтобы остаться без слежения до конца сессии.
"""

import asyncio

import pytest
from pytest import fixture

from src.common import FileMetadata
from src.config import Config
from src.indexer.watcher.base_watcher import AbstractFilesWatcher


class _NoopWatcher(AbstractFilesWatcher):
    """Реализация абстрактных методов — сами события здесь не проверяются."""

    async def on_delete(self, file: FileMetadata) -> None:
        return None

    async def on_delete_dir(self, path: str) -> None:
        return None

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        return None


class _FakeAdapter:
    """Подмена WatchDogAdapter: запоминает пути и сразу закрывает поток."""

    instances: list["_FakeAdapter"] = []

    def __init__(self, *paths, **kwargs):
        self.paths = [str(p) for p in paths]
        type(self).instances.append(self)

    async def start_async(self):
        return self

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    async def __aiter__(self):
        if False:  # pragma: no cover — генератор без единого события
            yield []


@fixture
def watcher_config(config: Config, tmp_path, monkeypatch):
    """Конфиг с одним живым каталогом и одним несуществующим."""
    live = tmp_path / "docs"
    live.mkdir()
    dead = tmp_path / "нет-такого"
    # data_directory во временный каталог: иначе scan_paths прочитает
    # общий user_conf.yml
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(live), str(dead)])
    yield config, live, dead
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


@fixture
def fake_adapter(monkeypatch):
    import src.indexer.watcher.base_watcher as base_watcher

    _FakeAdapter.instances = []
    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FakeAdapter)
    return _FakeAdapter


def test_missing_paths_filtered_out(watcher_config):
    cfg, live, dead = watcher_config

    paths = _NoopWatcher()._watch_paths()

    assert [str(p) for p in paths] == [str(live)]


@pytest.mark.asyncio
async def test_run_watches_only_live_paths(watcher_config, fake_adapter):
    """Мёртвый путь не должен лишать наблюдения живой."""
    _cfg, live, _dead = watcher_config

    await _NoopWatcher().run()

    assert len(fake_adapter.instances) == 1
    assert fake_adapter.instances[0].paths == [str(live)]


@pytest.mark.asyncio
async def test_run_exits_quietly_without_valid_paths(
    watcher_config, fake_adapter, monkeypatch
):
    """Совсем невалидный список — штатный выход, а не трейсбек."""
    cfg, _live, dead = watcher_config
    monkeypatch.setattr(cfg.file_scan_cfg, "paths", [str(dead)])

    await _NoopWatcher().run()

    assert fake_adapter.instances == [], "наблюдать нечего — адаптер не нужен"


@pytest.mark.asyncio
async def test_run_survives_disappearing_path(watcher_config, monkeypatch):
    """Каталог исчез между проверкой и стартом эмиттера — не роняем задачу."""
    import src.indexer.watcher.base_watcher as base_watcher

    class _FailingAdapter(_FakeAdapter):
        async def start_async(self):
            raise OSError(2, "No such file or directory")

    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FailingAdapter)

    await _NoopWatcher().run()  # не должно бросить


class _FailingObserver:
    """Observer, падающий на start() — как inotify при ENOSPC или ENOTDIR.

    На macOS (FSEvents) наблюдение за несуществующим путём поднимается без
    ошибки, и без подмены ветка обработки OSError не исполнялась бы.
    """

    def __init__(self):
        self.stopped = False

    def schedule(self, *args, **kwargs):
        return None

    def start(self):
        raise OSError(28, "No space left on device")

    def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_adapter_stops_observer_when_start_fails(tmp_path):
    """Упавший start() не должен оставлять observer поднятым."""
    from src.indexer.watcher.watchdog_adapter import WatchDogAdapter

    live = tmp_path / "docs"
    live.mkdir()
    adapter = WatchDogAdapter(live)
    observer = _FailingObserver()
    adapter.observer = observer  # type: ignore[assignment]

    with pytest.raises(OSError):
        adapter.__enter__()

    assert observer.stopped, "observer остался поднятым после неудачного входа"
    assert adapter._loop is None


@pytest.mark.asyncio
async def test_run_reports_errno_when_start_fails(watcher_config, monkeypatch, caplog):
    """В логе обязан быть errno: ENOSPC лечится не так, как пропавший каталог."""
    import logging

    import src.indexer.watcher.base_watcher as base_watcher

    class _FailingAdapter(_FakeAdapter):
        async def start_async(self):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FailingAdapter)

    with caplog.at_level(logging.ERROR):
        await _NoopWatcher().run()

    assert "errno=28" in caplog.text
    assert "max_user_watches" in caplog.text


@pytest.mark.asyncio
async def test_start_async_does_not_block_event_loop(tmp_path):
    """Постановка наблюдения не должна занимать цикл событий.

    Для inotify observer.start() обходит всё дерево и делает системный вызов на
    каждый каталог в потоке вызывающего: в корутине это заморозило бы /health,
    /status и /search на старте демона и на каждом POST /config.
    """
    import threading
    import time

    from src.indexer.watcher.watchdog_adapter import WatchDogAdapter

    live = tmp_path / "docs"
    live.mkdir()
    adapter = WatchDogAdapter(live)

    started_in: list[int] = []

    class _SlowObserver:
        def schedule(self, *args, **kwargs):
            return None

        def start(self):
            started_in.append(threading.get_ident())
            time.sleep(0.3)

        def stop(self):
            return None

    adapter.observer = _SlowObserver()  # type: ignore[assignment]
    loop_ident = threading.get_ident()

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await adapter.start_async()
    beat.cancel()

    assert started_in and started_in[0] != loop_ident, "старт остался в потоке цикла"
    assert ticks > 5, f"цикл событий стоял во время старта (тиков: {ticks})"
    adapter.close()
