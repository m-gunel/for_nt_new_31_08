"""Устойчивость наблюдения за файловой системой.

Пять боевых дефектов АРМ, из-за которых демон оставался без слежения за ФС
или сносил лишнее:

* исключаемые каталоги (`.cache`, `node_modules`, `venv`) выбирали
  `fs.inotify.max_user_watches` наравне с полезными;
* один отказавший `observer.schedule()` на старте выключал наблюдение СРАЗУ
  ПО ВСЕМ путям;
* каталог с правами `0o000` проходил проверку `is_dir()` и считался
  наблюдаемым, хотя inotify по нему молча не вставал;
* пустой `src_path` от watchdog превращался в `os.path.abspath("")`, то есть
  в текущий рабочий каталог демона;
* watcher не применял лимит `max_size`, и гигабайтный файл, скопированный в
  наблюдаемую папку, уходил прямо в парсер.

Реальный отказ inotify на macOS невоспроизводим (там FSEvents), поэтому
Observer здесь подменён заглушкой — иначе ветки обработки ошибок не
исполняются ни одним тестом и ломаются незаметно до самого АРМ.
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
# Конфиг с временным data_directory (иначе scan_paths прочитает настоящий
# ~/.ragsearch разработчика) — фикстура общая на три набора тестов про
# watcher, чтобы правки шли пачкой, а не расползались по копиям.
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

    Пишет вызовы в общий журнал `calls`: важен не только факт постановки, но и
    то, что observer поднимается ПУСТЫМ, а наблюдения добавляются после старта.
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


# ------------------------------- дефект 1: исключения при постановке watch'ей


def test_excluded_dirs_do_not_take_inotify_watches(tmp_path):
    """`.cache`/`node_modules`/`venv` не должны попадать в список наблюдения.

    Было: `_dirs_for_watch` собирал ВСЕ подкаталоги до max_depth. На домашнем
    каталоге кэши и зависимости — основная статья расхода
    `fs.inotify.max_user_watches`, и лимит выбирался мусором, а на полезные
    папки watch'ей не оставалось. Их содержимое всё равно отсеивает фильтр.
    """
    root = tmp_path / "docs"
    for name in ("проекты", ".cache", ".git", "node_modules", "venv", "__pycache__"):
        (root / name).mkdir(parents=True)

    watched = _names(_dirs_for_watch(root, 1))

    assert "проекты" in watched, "обычный каталог обязан наблюдаться"
    assert not watched & {".cache", ".git", "node_modules", "venv", "__pycache__"}


def test_subtree_of_excluded_dir_is_not_walked(tmp_path):
    """В исключаемый каталог не спускаемся — иначе экономия мнимая.

    Было: `node_modules/пакет` и всё, что глубже, тоже получало по watch'у.
    """
    root = tmp_path / "docs"
    (root / "node_modules" / "пакет").mkdir(parents=True)
    (root / "проекты" / "вложенный").mkdir(parents=True)

    watched = _names(_dirs_for_watch(root, 2))

    assert "вложенный" in watched, "полезное дерево обходим до max_depth"
    assert "пакет" not in watched
    assert "node_modules" not in watched


# Отдельного адаптерного теста на тот же отбор здесь нет намеренно:
# `_start_observer` только итерирует результат `_dirs_for_watch`, а что он
# ставит по watch'у на каждый возвращённый каталог и с recursive=False —
# проверено в tests/test_watcher_max_depth.py
# (test_adapter_bounded_schedules_each_dir_non_recursive).


# --------------------------- дефект 2: отказ по одному пути гасил наблюдение


def test_one_failing_path_does_not_disable_watching_of_the_rest(
    tmp_path, monkeypatch, caplog
):
    """Отказ по одному каталогу не должен снимать наблюдение с остальных.

    Было: всё расписание создавалось в конструкторе до старта observer'а, и
    первый же OSError (упёрлись в `fs.inotify.max_user_watches`, каталог
    исчез, нет прав) срывал постановку ВСЕХ путей — демон оставался без
    слежения за файловой системой целиком. Стало: observer стартует пустым,
    пути ставятся по одному, отказавший считается пропущенным.
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
    # Ср. tests/test_watcher_new_dirs.py::test_watch_dir_returns_false_on_oserror:
    # там та же инвариантность проверяется на самом watch_dir, здесь — что её
    # не теряет стартовая постановка.
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

    Было: конструктор бросал OSError на первом же пути, и решать было нечего.
    Стало: пустой результат отличается от частичного — `_start_observer`
    гасит observer и поднимает ENOSPC, чтобы base_watcher написал в лог errno
    и подсказал про `fs.inotify.max_user_watches`.

    Поднимаем через `start_async`, а не вызовом `_start_observer` напрямую:
    только он захватывает цикл (`self._loop = asyncio.get_running_loop()`),
    поэтому сброс `_loop` в ветке отказа проверяется по-настоящему.
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


# ------------------------------ дефект 3: нечитаемый каталог считался живым


@pytest.fixture
def scan_paths_config(watcher_config, tmp_path, monkeypatch):  # noqa: F811
    """scan_paths из читаемого каталога и каталога, недоступного на чтение.

    Правами файловой системы в эту ветку не загнать: прогон, который гейтит
    сборку, идёт от root (Dockerfile зовёт pytest, USER не задан), а для root
    `os.access` на каталоге с правами 0o000 всё равно вернёт True — тест
    молча превращался бы в skip. Плюс chmod 0o000 внутри tmp_path мешает
    pytest'у убирать временные каталоги. Поэтому подменяем саму проверку:
    base_watcher резолвит `os.access` из глобалей модуля в момент вызова.
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
    """Каталог без прав не должен считаться наблюдаемым.

    Было: `_watch_paths` проверял только `is_dir()`, а у каталога с правами
    0o000 он возвращает True. inotify по такому пути молча не встаёт
    (watchdog глотает EACCES), и путь выглядел наблюдаемым, не будучи им:
    файлы в нём не индексировались, а в логе не было ни строчки.
    """
    _cfg, readable, closed = scan_paths_config

    with caplog.at_level(logging.WARNING, logger=WATCHER_LOGGER):
        paths = _NoopWatcher()._watch_paths()

    assert [str(p) for p in paths] == [str(readable)]
    assert "без прав на чтение" in caplog.text
    assert str(closed) in caplog.text, "в логе должен быть конкретный путь"


# ---------------------------- дефект 4: пустой путь — это текущий каталог


def _emitted(event) -> list[tuple[Change, str]]:
    """Что MyEventHandler кладёт в очередь по одному событию перемещения."""
    events: list[tuple[Change, str]] = []
    MyEventHandler(lambda e: events.append((e.change, e.path))).on_moved(event)
    return events


def test_move_from_outside_tree_does_not_emit_empty_path(tmp_path):
    """Перемещение ИЗВНЕ дерева не должно порождать событие по пустому пути.

    Было: `on_moved` эмитил оба события всегда, а watchdog для переноса
    снаружи отдаёт пустой `src_path`. Дальше base_watcher звал
    `os.path.abspath("")` — то есть текущий рабочий каталог демона — и мог
    снять с индекса его целиком (`on_delete_dir` по всему поддереву).
    """
    dest = tmp_path / "перенесённый.txt"

    assert _emitted(FileMovedEvent(src_path="", dest_path=str(dest))) == [
        (Change.added, str(dest))
    ]

    # Контроль: обычное перемещение внутри дерева по-прежнему даёт оба события,
    # иначе исчезнувший исходный путь останется в индексе навсегда.
    src = tmp_path / "было.txt"
    assert _emitted(FileMovedEvent(src_path=str(src), dest_path=str(dest))) == [
        (Change.deleted, str(src)),
        (Change.added, str(dest)),
    ]


def test_move_out_of_tree_does_not_emit_empty_path(tmp_path):
    """Симметрично: пустой `dest_path` не должен давать «добавлен» по cwd.

    Было: пустой путь уходил в индексацию, и обход текущего рабочего каталога
    демона попадал в очередь на разбор.
    """
    src = tmp_path / "ушёл.txt"

    assert _emitted(FileMovedEvent(src_path=str(src), dest_path="")) == [
        (Change.deleted, str(src))
    ]


# ----------------------------- дефект 5: watcher не применял лимит размера


class _StubIndex:
    """Минимальный протокол Index для Watcher.on_upsert — без живого индекса.

    `watch_pending` — свойство, а не поле: `Watcher.on_upsert` возвращает
    счётчик обратно в `finally`, поэтому по остатку после вызова отличить
    старое поведение от нового невозможно. Запоминаем пик.
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
    """Файл больше max_size не должен доходить до парсера через watcher.

    Было: лимит применял только полный проход (`FilesScanner.preindex`), а
    `Watcher.on_upsert` — нет. Файл на гигабайты, скопированный в
    наблюдаемую папку, уходил прямо в разбор и съедал память демона.
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
    """Пачка из одних отсеянных файлов не должна дёргать индекс вообще.

    Было: такой файл проходил в `process_stream`, накручивал `watch_pending`
    в `/status` и вызывал пересборку индексов впустую.
    """
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
