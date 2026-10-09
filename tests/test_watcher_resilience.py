"""Устойчивость наблюдения за файловой системой.

Исключаемые каталоги не тратят `fs.inotify.max_user_watches`, отказ по одному
пути не гасит наблюдение остальных, нечитаемый каталог не считается
наблюдаемым, пустой путь от watchdog не становится cwd демона, watcher
применяет лимит `max_size`.

Реальный отказ inotify на macOS невоспроизводим (там FSEvents), поэтому
Observer подменён заглушкой — иначе ветки обработки ошибок не исполнялись бы
ни одним тестом.
"""

import errno
import logging
import os
from pathlib import Path

import pytest
from watchdog.events import FileMovedEvent
from watchfiles import Change

from src.common import FileMetadata
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.indexer.watcher.watchdog_adapter import (
    MyEventHandler,
    WatchDogAdapter,
    _dirs_for_watch,
)
# Конфиг с временным data_directory (иначе scan_paths прочитает общий
# user_conf.yml) — общая фикстура тестов watcher'а.
from tests.test_watcher_max_depth import watcher_config  # noqa: F401

# Логгеры watchdog_adapter, base_watcher и watcher срезаны до одного имени.
WATCHER_LOGGER = "app.indexer.watcher"


class _NoopWatcher(AbstractFilesWatcher):
    """Реализация абстрактных методов — здесь проверяется только отбор путей."""

    async def on_delete(self, file: FileMetadata) -> None:
        return None

    async def on_delete_dir(self, path: str) -> None:
        return None

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        return None


class _FlakyObserver:
    """Observer, у которого schedule() отказывает по заданным путям.

    Журнал `calls` показывает, что observer поднимается пустым, а наблюдения
    добавляются после старта.
    """

    def __init__(self, failing=(), fail_all: bool = False):
        self.failing = {str(p) for p in failing}
        self.fail_all = fail_all
        self.calls: list[str] = []
        self.scheduled: list[tuple[str, bool]] = []
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, recursive: bool = False, **kwargs):
        self.calls.append(f"schedule:{path}")
        if self.fail_all or str(path) in self.failing:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        self.scheduled.append((str(path), recursive))
        return object()

    def start(self):
        self.calls.append("start")
        self.started = True

    def stop(self):
        self.calls.append("stop")
        self.stopped = True


def _adapter(monkeypatch, observer: _FlakyObserver, *paths, **kwargs):
    """WatchDogAdapter с подменённым Observer (настоящий inotify не поднимаем)."""
    import src.indexer.watcher.watchdog_adapter as mod

    monkeypatch.setattr(mod, "Observer", lambda: observer)
    return WatchDogAdapter(*paths, **kwargs)


def _names(paths: list[Path]) -> set[str]:
    return {p.name for p in paths}


def test_excluded_dirs_do_not_take_inotify_watches(tmp_path):
    """`.cache`/`node_modules`/`venv` не попадают в список наблюдения.

    На домашнем каталоге кэши и зависимости — основная статья расхода
    `fs.inotify.max_user_watches`, а их содержимое всё равно отсеивает фильтр.
    """
    root = tmp_path / "docs"
    for name in ("проекты", ".cache", ".git", "node_modules", "venv", "__pycache__"):
        (root / name).mkdir(parents=True)

    watched = _names(_dirs_for_watch(root, 1))

    assert "проекты" in watched, "обычный каталог обязан наблюдаться"
    assert not watched & {".cache", ".git", "node_modules", "venv", "__pycache__"}


def test_subtree_of_excluded_dir_is_not_walked(tmp_path):
    """В исключаемый каталог не спускаемся — иначе экономия мнимая."""
    root = tmp_path / "docs"
    (root / "node_modules" / "пакет").mkdir(parents=True)
    (root / "проекты" / "вложенный").mkdir(parents=True)

    watched = _names(_dirs_for_watch(root, 2))

    assert "вложенный" in watched, "полезное дерево обходим до max_depth"
    assert "пакет" not in watched
    assert "node_modules" not in watched


# Что _start_observer ставит watch на каждый каталог из `_dirs_for_watch` с
# recursive=False, проверено в tests/test_watcher_max_depth.py.


def test_one_failing_path_does_not_disable_watching_of_the_rest(
    tmp_path, monkeypatch, caplog
):
    """Отказ по одному каталогу не снимает наблюдение с остальных.

    OSError по одному пути (лимит `fs.inotify.max_user_watches`, каталог исчез,
    нет прав) штатен: observer стартует пустым, пути ставятся по одному,
    отказавший считается пропущенным.
    """
    root = tmp_path / "docs"
    for name in ("a", "b", "c"):
        (root / name).mkdir(parents=True)
    observer = _FlakyObserver(failing=[str(root / "b")])
    adapter = _adapter(monkeypatch, observer, root, max_depth=1)

    with caplog.at_level(logging.INFO, logger=WATCHER_LOGGER):
        adapter._start_observer()

    assert observer.calls[0] == "start", "observer обязан подниматься пустым"
    assert sorted(p for p, _ in observer.scheduled) == sorted(
        [str(root), str(root / "a"), str(root / "c")]
    )
    assert not observer.stopped, "частичный отказ — не повод гасить наблюдение"
    assert str(root / "b") not in adapter._watched, (
        "путь, на который watch не встал, нельзя считать наблюдаемым: "
        "повторная попытка из watch_dir тогда не состоится"
    )
    assert "поставлено 3" in caplog.text and "пропущено 1" in caplog.text, (
        f"в логе должны быть счётчики наблюдений, а там: {caplog.text!r}"
    )


@pytest.mark.asyncio
async def test_total_failure_is_reported_as_oserror(tmp_path, monkeypatch):
    """Ни одного наблюдения — это отказ, а не деградация.

    `_start_observer` гасит observer и поднимает ENOSPC, чтобы base_watcher
    написал errno и подсказку про `fs.inotify.max_user_watches`.

    Запуск через `start_async`: только он захватывает цикл, и сброс `_loop` в
    ветке отказа проверяется по-настоящему.
    """
    root = tmp_path / "docs"
    root.mkdir()
    observer = _FlakyObserver(fail_all=True)
    adapter = _adapter(monkeypatch, observer, root, max_depth=1)

    with pytest.raises(OSError) as exc:
        await adapter.start_async()

    assert exc.value.errno == errno.ENOSPC
    assert observer.stopped, "поднятый observer нельзя оставлять без наблюдений"
    assert adapter._loop is None, (
        "цикл обязан сбрасываться вместе с погашенным observer'ом: иначе "
        "_emit продолжит слать события в loop через мёртвое наблюдение"
    )


@pytest.fixture
def scan_paths_config(watcher_config, tmp_path, monkeypatch):  # noqa: F811
    """scan_paths из читаемого каталога и каталога, недоступного на чтение.

    Правами ФС сюда не загнать: прогон в Docker идёт от root, а для root
    `os.access` на 0o000 вернёт True; к тому же chmod 0o000 мешает pytest
    убирать tmp_path. Поэтому подменяется сама проверка: base_watcher резолвит
    `os.access` из глобалей модуля в момент вызова.
    """
    config, readable = watcher_config
    closed = tmp_path / "закрытый"
    closed.mkdir()
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(readable), str(closed)])
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode, *args, **kwargs: (
            False
            if str(path) == str(closed)
            else real_access(path, mode, *args, **kwargs)
        ),
    )
    return config, readable, closed


def test_unreadable_dir_is_not_counted_as_watched(scan_paths_config, caplog):
    """Каталог без прав не считается наблюдаемым.

    `is_dir()` для каталога 0o000 возвращает True, а inotify по нему молча не
    встаёт (watchdog глотает EACCES).
    """
    _cfg, readable, closed = scan_paths_config

    with caplog.at_level(logging.WARNING, logger=WATCHER_LOGGER):
        paths = _NoopWatcher()._watch_paths()

    assert [str(p) for p in paths] == [str(readable)]
    assert "без прав на чтение" in caplog.text
    assert str(closed) in caplog.text, "в логе должен быть конкретный путь"


def _emitted(event) -> list[tuple[Change, str]]:
    """Что MyEventHandler кладёт в очередь по одному событию перемещения."""
    events: list[tuple[Change, str]] = []
    MyEventHandler(lambda e: events.append((e.change, e.path))).on_moved(event)
    return events


def test_move_from_outside_tree_does_not_emit_empty_path(tmp_path):
    """Перемещение извне дерева не порождает событие по пустому пути.

    watchdog для переноса снаружи отдаёт пустой `src_path`, а
    `os.path.abspath("")` — это cwd демона: `on_delete_dir` снял бы с индекса
    всё его поддерево.
    """
    dest = tmp_path / "перенесённый.txt"

    assert _emitted(FileMovedEvent(src_path="", dest_path=str(dest))) == [
        (Change.added, str(dest))
    ]

    # Контроль: обычное перемещение внутри дерева даёт оба события,
    # иначе исчезнувший исходный путь останется в индексе навсегда.
    src = tmp_path / "было.txt"
    assert _emitted(FileMovedEvent(src_path=str(src), dest_path=str(dest))) == [
        (Change.deleted, str(src)),
        (Change.added, str(dest)),
    ]


def test_move_out_of_tree_does_not_emit_empty_path(tmp_path):
    """Симметрично: пустой `dest_path` не даёт «добавлен» по cwd демона."""
    src = tmp_path / "ушёл.txt"

    assert _emitted(FileMovedEvent(src_path=str(src), dest_path="")) == [
        (Change.deleted, str(src))
    ]


class _StubIndex:
    """Минимальный протокол Index для Watcher.on_upsert.

    `watch_pending` — свойство с запоминанием пика: on_upsert возвращает
    счётчик обратно в `finally`, и по остатку после вызова ничего не видно.
    """

    def __init__(self) -> None:
        self._watch_pending = 0
        self.peak_pending = 0
        self.processed: list[FileMetadata] = []
        self.stream_calls = 0
        self.ensure_calls = 0

    @property
    def watch_pending(self) -> int:
        return self._watch_pending

    @watch_pending.setter
    def watch_pending(self, value: int) -> None:
        self._watch_pending = value
        self.peak_pending = max(self.peak_pending, value)

    async def check_meta(self, meta: FileMetadata) -> bool:
        return True

    async def process_stream(self, batch, count: bool = True, store=None) -> None:
        self.stream_calls += 1
        self.processed.extend(batch)

    async def ensure_index(self, store=None) -> None:
        self.ensure_calls += 1


@pytest.fixture
def size_limited_watcher(config, monkeypatch):
    """Watcher с заглушкой индекса и боевым filter_meta с низким порогом."""
    # Импорт внутри фикстуры: src.indexer.utils тянет Index, а он нужен не всем.
    from src.indexer.utils import filter_meta
    from src.indexer.watcher.watcher import Watcher

    monkeypatch.setattr(config.file_scan_cfg, "max_size", 1024)
    # Так же, как в serve_indexer: боевой фильтр берёт порог из конфига.
    monkeypatch.setattr(config.file_scan_cfg, "filter_meta", filter_meta)
    index = _StubIndex()
    return Watcher(config, index), index  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_oversized_file_from_watcher_is_not_parsed(
    size_limited_watcher, tmp_path
):
    """Файл больше max_size не доходит до парсера и через watcher.

    Не только через `FilesScanner.preindex`: гигабайтный файл, скопированный в
    наблюдаемую папку, съел бы память демона.
    """
    watcher, index = size_limited_watcher
    huge = FileMetadata(path=str(tmp_path / "дамп.txt"), size=2048, mtime_seconds=1)
    small = FileMetadata(path=str(tmp_path / "заметка.txt"), size=10, mtime_seconds=1)

    await watcher.on_upsert([huge, small])

    assert [f.path for f in index.processed] == [small.path], (
        "в разбор обязан уйти только файл в пределах лимита"
    )


@pytest.mark.asyncio
async def test_batch_of_only_oversized_files_does_not_touch_index(
    size_limited_watcher, tmp_path
):
    """Пачка из одних отсеянных файлов не дёргает индекс вообще: ни
    `process_stream`, ни `watch_pending` в `/status`, ни пересборку индексов."""
    watcher, index = size_limited_watcher
    huge = FileMetadata(
        path=str(tmp_path / "образ.txt"), size=10 * 1024, mtime_seconds=1
    )

    await watcher.on_upsert([huge])

    assert index.stream_calls == 0
    assert index.ensure_calls == 0
    assert index.peak_pending == 0, (
        "счётчик /status не должен дёргаться из-за файлов, которые даже не "
        "дошли до разбора"
    )
