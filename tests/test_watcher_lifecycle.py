"""Жизненный цикл watcher'а: остановка, сбои индексации, пакетное удаление.

Остановка во время стартового обхода не должна занимать event loop и заводить
наблюдения после close(); сбой индексации появившегося каталога не должен
убивать наблюдение; удаления из одной пачки событий снимаются одним фильтром.
"""

import asyncio
import errno
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from watchdog.observers.api import BaseObserver, EventEmitter
from watchfiles import Change

import src.indexer.watcher.watchdog_adapter as mod
from src.common import FileMetadata
from src.indexer.watcher import base_watcher
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.indexer.watcher.watchdog_adapter import WatchEvent
from src.indexer.watcher.watcher import Watcher

WATCHER_LOGGER = "app.indexer.watcher"


def _cfg(*roots: Path, max_depth=None) -> SimpleNamespace:
    """Минимум конфига, который читают run() и _collect_dir_files."""
    return SimpleNamespace(
        scan_paths=[str(r) for r in roots],
        scan_max_depth=max_depth,
        file_scan_cfg=SimpleNamespace(
            filter_files=None, filter_meta=None, follow_symlinks=True
        ),
    )


class _Watcher(AbstractFilesWatcher):
    """Watcher со своим конфигом; индексацию записывает, а не выполняет."""

    def __init__(self, cfg: SimpleNamespace) -> None:
        self._cfg = cfg
        self.upserted: list[list[str]] = []
        self.fail_upsert = 0

    @property
    def cfg(self):  # type: ignore[override]
        return self._cfg

    @property
    def filter(self):
        return None

    async def on_delete(self, file: FileMetadata) -> None:
        return None

    async def on_delete_dir(self, path: str) -> None:
        return None

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        self.upserted.append(sorted(f.path for f in files))
        if self.fail_upsert:
            self.fail_upsert -= 1
            raise OSError(errno.ENOSPC, "No space left on device")


class _SlowEmitter(EventEmitter):
    """Старт эмиттера длится как обход большого дерева (под observer._lock)."""

    delay = 1.0

    def on_thread_start(self) -> None:
        time.sleep(self.delay)

    def queue_events(self, timeout: float) -> None:
        time.sleep(timeout)


class _RecordingObserver(BaseObserver):
    """Настоящий BaseObserver, запоминающий пути schedule()."""

    def __init__(self) -> None:
        super().__init__(_SlowEmitter, timeout=0.05)
        self.scheduled: list[str] = []

    def schedule(self, event_handler, path, **kwargs):
        self.scheduled.append(str(path))
        return super().schedule(event_handler, path, **kwargs)


@pytest.mark.asyncio
async def test_cancel_during_start_does_not_block_event_loop(tmp_path, monkeypatch):
    """Отмена watcher'а посреди стартового обхода не замораживает цикл.

    observer.stop() ждёт, пока поток постановки отпустит observer: в потоке
    цикла это минуты на большом дереве — /health, /status, /search молчат.
    """
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(mod, "Observer", _RecordingObserver)

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    task = asyncio.create_task(_Watcher(_cfg(root)).run())
    await asyncio.sleep(0.1)  # обход уже идёт в рабочем потоке
    task.cancel()  # как stop_watcher(): /deactivate, POST /config, остановка
    with pytest.raises(asyncio.CancelledError):
        await task
    stop.set()
    await beat

    assert max(gaps) < 0.5, f"цикл событий стоял {max(gaps):.2f} с"


@pytest.mark.asyncio
async def test_cancelled_start_does_not_schedule_remaining_roots(tmp_path, monkeypatch):
    """После отмены поток постановки не заводит наблюдения за остальными
    корнями: такой эмиттер уже никто не остановит."""
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    observers: list[_RecordingObserver] = []

    def make() -> _RecordingObserver:
        observers.append(_RecordingObserver())
        return observers[-1]

    monkeypatch.setattr(mod, "Observer", make)
    monkeypatch.setattr(_SlowEmitter, "delay", 0.5)

    task = asyncio.create_task(_Watcher(_cfg(*roots)).run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.0)  # даём потоку постановки доработать

    observer = observers[0]
    assert observer.scheduled == [str(roots[0])], observer.scheduled
    assert not any(e.is_alive() for e in observer.emitters)


class _TwoBatchAdapter:
    """Адаптер: пачка с появившимся каталогом, затем пачка с файлом."""

    def __init__(self, first: str, second: str) -> None:
        self._first, self._second = first, second

    def watch_dir(self, path) -> bool:
        return True

    def close(self) -> None:
        return None

    async def start_async(self):
        return self

    async def __aiter__(self):
        yield [WatchEvent(Change.added, self._first, True)]
        yield [WatchEvent(Change.added, self._second, False)]


@pytest.mark.asyncio
async def test_index_error_in_new_dir_does_not_stop_watcher(tmp_path, monkeypatch):
    """Сбой индексации появившегося каталога (нет места, LanceDB, инференс)
    не завершает watcher: следующая пачка событий обрабатывается."""
    root = tmp_path / "root"
    (root / "moved").mkdir(parents=True)
    (root / "moved" / "a.txt").write_text("x", encoding="utf-8")
    later = root / "later.txt"
    later.write_text("y", encoding="utf-8")
    monkeypatch.setattr(
        base_watcher,
        "WatchDogAdapter",
        lambda *a, **k: _TwoBatchAdapter(str(root / "moved"), str(later)),
    )
    watcher = _Watcher(_cfg(root))
    watcher.fail_upsert = 1

    await watcher.run()  # не должно бросить

    assert watcher.upserted == [[str(root / "moved" / "a.txt")], [str(later)]]


class _StubIndex:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.single: list[str] = []
        self.ensure_calls = 0

    async def delete_paths(self, paths: list[str]) -> None:
        self.batches.append(list(paths))

    async def delete_path(self, path: str) -> None:
        self.single.append(path)

    async def ensure_index(self, store=None) -> None:
        self.ensure_calls += 1


@pytest.mark.asyncio
async def test_deleted_files_are_removed_in_one_batch(config, tmp_path):
    """Удаления из одной пачки — один delete_paths и один ensure_index.

    По файлу за раз перенос папки в корзину давал сотни версий Lance и
    deletion-файлов.
    """
    gone = {str(tmp_path / f"удалён-{i}.txt") for i in range(3)}
    index = _StubIndex()
    watcher = Watcher(config, index)  # type: ignore[arg-type]

    await watcher._handle_files(gone)

    assert index.single == [], "удаление ушло поштучно"
    assert len(index.batches) == 1 and set(index.batches[0]) == gone
    assert index.ensure_calls == 1


class _FailingStartAdapter:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def start_async(self):
        raise OSError(errno.EMFILE, "inotify instance limit reached")

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_names_instance_limit_on_emfile(tmp_path, monkeypatch, caplog):
    """EMFILE от inotify_init — лимит экземпляров, а не watch'ей: подсказка
    про max_user_watches отправляла бы поднимать не тот лимит."""
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FailingStartAdapter)

    with caplog.at_level(logging.ERROR, logger=WATCHER_LOGGER):
        await _Watcher(_cfg(root)).run()

    assert f"errno={errno.EMFILE}" in caplog.text
    assert "max_user_instances" in caplog.text
    assert "max_user_watches" not in caplog.text


class _FailAllObserver:
    def __init__(self, error: int) -> None:
        self.error = error

    def schedule(self, *args, **kwargs):
        raise OSError(self.error, os.strerror(self.error))

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


@pytest.mark.asyncio
async def test_total_failure_keeps_real_errno(tmp_path, monkeypatch):
    """Отказ по всем путям сообщается с errno настоящей причины: по нему
    run() выбирает подсказку."""
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(mod, "Observer", lambda: _FailAllObserver(errno.EMFILE))

    with pytest.raises(OSError) as exc:
        await mod.WatchDogAdapter(root).start_async()

    assert exc.value.errno == errno.EMFILE
