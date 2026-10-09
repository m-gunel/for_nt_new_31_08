"""Расход inotify: экземпляры, watch'и, исключаемые каталоги, пересоздание.

`fs.inotify.max_user_instances` (128) и `max_user_watches` — лимиты на всего
пользователя, общие с файловым менеджером, IDE и `systemd --user`. Поэтому на
корень — один inotify-экземпляр, подкаталоги добавляются в него же, мусорные
каталоги наблюдения не получают, а упор в лимит не гасит наблюдение целиком.

Живого inotify на macOS нет, поэтому здесь работают НАСТОЯЩИЕ классы watchdog
(`InotifyObserver` → `InotifyEmitter` → `InotifyBuffer` → `Inotify`), у которых
подменены только три вызова libc. Так тесты одинаково идут на Linux и macOS и
считают экземпляры и watch'и точно.
"""

import asyncio
import ctypes
import errno
import itertools
import logging
import os
import shutil
import struct
import sys
import threading
import time
import types
from contextlib import suppress
from pathlib import Path

import pytest
import watchdog
from watchdog.observers.api import BaseObserver, EventEmitter
from watchfiles import Change

import src.indexer.watcher.watchdog_adapter as mod
from src.common import FileMetadata
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.indexer.watcher.watchdog_adapter import WatchDogAdapter

WATCHER_LOGGER = "app.indexer.watcher"
_ids = itertools.count()


class FakeKernel:
    """Эмуляция inotify_init/inotify_add_watch/inotify_rm_watch с лимитом watch'ей."""

    def __init__(self, limit: int = 10**9) -> None:
        self.limit = limit
        self.write_ends: dict[int, int] = {}  # fd экземпляра -> пишущий конец pipe
        self.tables: dict[int, dict[bytes, int]] = {}  # fd -> {путь: wd}
        self.calls: list[tuple[int, str]] = []  # все вызовы add_watch
        self._wd = itertools.count(1)

    def inotify_init(self) -> int:
        # Читающий конец pipe: poll() на нём ждёт, как на inotify-fd.
        r, w = os.pipe()
        self.write_ends[r] = w
        self.tables[r] = {}
        return r

    def inotify_add_watch(self, fd: int, path: bytes, mask: int) -> int:
        self.calls.append((fd, os.fsdecode(path)))
        table = self.tables.get(fd)
        if table is None:
            ctypes.set_errno(errno.EBADF)
            return -1
        if path in table:
            return table[path]
        if sum(len(t) for t in self.tables.values()) >= self.limit:
            ctypes.set_errno(errno.ENOSPC)
            return -1
        table[path] = next(self._wd)
        return table[path]

    def inotify_rm_watch(self, fd: int, wd: int) -> int:
        table = self.tables.get(fd, {})
        for path, value in list(table.items()):
            if value == wd:
                del table[path]
        return 0

    def deliver(self, fd: int, directory: Path, name: str, mask: int) -> None:
        """Событие ядра по watch'у каталога — как его читает Inotify.read_events."""
        wd = self.tables[fd][os.fsencode(str(directory))]
        raw = os.fsencode(name) + b"\0"
        raw += b"\0" * (-len(raw) % 4)
        os.write(self.write_ends[fd], struct.pack("iIII", wd, mask, 0, len(raw)) + raw)

    def forget(self, path: Path) -> None:
        """Как ядро после rmdir: watch снят вместе с inode."""
        for table in self.tables.values():
            table.pop(os.fsencode(str(path)), None)

    def paths(self, fd: int | None = None) -> set[str]:
        return {p for f, p in self.calls if fd is None or f == fd}

    @staticmethod
    def is_open(fd: int) -> bool:
        try:
            os.fstat(fd)
        except OSError:
            return False
        return True

    def close(self) -> None:
        for w in self.write_ends.values():
            with suppress(OSError):
                os.close(w)


def _load_watchdog_inotify(kernel: FakeKernel) -> types.SimpleNamespace:
    """Модули inotify из watchdog, у которых libc заменена на `kernel`."""
    base = Path(watchdog.__file__).parent / "observers"
    tag = f"_fake_inotify_{next(_ids)}"
    names = {
        "watchdog.observers.inotify_c": f"{tag}_c",
        "watchdog.observers.inotify_buffer": f"{tag}_buffer",
    }

    def load(file: str, name: str, source: str, extra: dict) -> types.ModuleType:
        for real, fake in names.items():
            source = source.replace(real, fake)
        module = types.ModuleType(name)
        module.__dict__.update(extra)
        sys.modules[name] = module
        exec(compile(source, str(base / file), "exec"), module.__dict__)
        return module

    c_source = (base / "inotify_c.py").read_text()
    start = c_source.index("libc = ctypes.CDLL(None)")
    marker = (
        'inotify_init = ctypes.CFUNCTYPE(c_int, use_errno=True)(("inotify_init", libc))'
    )
    end = c_source.index(marker) + len(marker)
    c_mod = load(
        "inotify_c.py",
        names["watchdog.observers.inotify_c"],
        c_source[:start] + c_source[end:],
        {
            "inotify_init": kernel.inotify_init,
            "inotify_add_watch": kernel.inotify_add_watch,
            "inotify_rm_watch": kernel.inotify_rm_watch,
        },
    )
    load(
        "inotify_buffer.py",
        names["watchdog.observers.inotify_buffer"],
        (base / "inotify_buffer.py").read_text(),
        {},
    )
    inotify_mod = load(
        "inotify.py", f"{tag}_emitter", (base / "inotify.py").read_text(), {}
    )
    return types.SimpleNamespace(
        names=[*names.values(), f"{tag}_emitter"],
        Inotify=c_mod.Inotify,
        InotifyObserver=inotify_mod.InotifyObserver,
    )


@pytest.fixture
def kernel(monkeypatch):
    """Адаптер работает на inotify из watchdog поверх эмулированного ядра."""
    fake = FakeKernel()
    loaded = _load_watchdog_inotify(fake)
    monkeypatch.setattr(mod, "Observer", loaded.InotifyObserver)
    monkeypatch.setattr(mod, "_InotifyObserver", loaded.InotifyObserver, raising=False)
    monkeypatch.setattr(mod, "_Inotify", loaded.Inotify, raising=False)
    yield fake
    fake.close()
    for name in loaded.names:
        sys.modules.pop(name, None)


@pytest.fixture
def adapters():
    """Закрыть все поднятые адаптеры, даже если тест упал на assert."""
    started: list[WatchDogAdapter] = []
    yield started
    for adapter in started:
        adapter.close()


def _start(adapters, *roots: Path, **kwargs) -> WatchDogAdapter:
    adapter = WatchDogAdapter(*roots, **kwargs)
    adapters.append(adapter)
    adapter._start_observer()
    return adapter


def _root_fd(kernel: FakeKernel, root: Path) -> int:
    return next(fd for fd, path in kernel.calls if path == str(root))


def test_new_dirs_do_not_open_inotify_instances(tmp_path, kernel, adapters):
    """Каталог, появившийся в наблюдаемом дереве, не заводит своего экземпляра.

    Каждый экземпляр — два потока и место в `max_user_instances` всего
    пользователя: распаковка архива с сотней папок выбирала лимит у файлового
    менеджера и IDE.
    """
    root = tmp_path / "docs"
    (root / "есть").mkdir(parents=True)
    adapter = _start(adapters, root)
    threads = threading.active_count()

    for i in range(20):
        new_dir = root / f"новая-{i}"
        new_dir.mkdir()
        adapter.watch_dir(str(new_dir))

    assert len(kernel.tables) == 1, f"экземпляров inotify: {len(kernel.tables)}"
    assert threading.active_count() == threads, "новые каталоги завели потоки"


def test_moved_in_dir_is_added_to_root_instance(tmp_path, kernel, adapters):
    """Каталог, перенесённый извне, наблюдается — тем же экземпляром корня.

    На IN_MOVED_TO без пары watchdog watch не ставит: без watch_dir файлы,
    положенные в такой каталог позже, не замечались бы.
    """
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "Загрузки" / "архив"
    (outside / "глава" / "раздел").mkdir(parents=True)
    adapter = _start(adapters, root)
    root_fd = _root_fd(kernel, root)

    moved = root / "архив"
    os.rename(outside, moved)
    adapter.watch_dir(str(moved))

    expected = {str(moved), str(moved / "глава"), str(moved / "глава" / "раздел")}
    assert expected <= kernel.paths(root_fd), kernel.calls
    assert len(kernel.tables) == 1


def test_bounded_depth_uses_one_instance_per_root(tmp_path, kernel, adapters):
    """max_depth не означает экземпляр на каталог: иначе >100 каталогов до
    заданной глубины выбирают `max_user_instances` прямо на старте."""
    root = tmp_path / "docs"
    for i in range(30):
        (root / f"d{i}" / "глубже").mkdir(parents=True)

    _start(adapters, root, max_depth=1)

    assert len(kernel.tables) == 1
    root_fd = _root_fd(kernel, root)
    assert {str(root / f"d{i}") for i in range(30)} <= kernel.paths(root_fd)
    assert not any(p.endswith("глубже") for p in kernel.paths()), (
        "каталоги глубже max_depth наблюдаться не должны"
    )


def test_new_dir_deeper_than_max_depth_is_not_watched(tmp_path, kernel, adapters):
    """watch_dir соблюдает max_depth и для каталогов, появившихся после старта."""
    root = tmp_path / "docs"
    (root / "a").mkdir(parents=True)
    adapter = _start(adapters, root, max_depth=1)

    deep = root / "a" / "новый"
    deep.mkdir()

    assert adapter.watch_dir(str(deep)) is False
    assert str(deep) not in kernel.paths()
    assert len(kernel.tables) == 1


def _make_home(base: Path) -> Path:
    home = base / "home"
    for rel in (
        "Документы/Проект",
        "Загрузки",
        ".cache/mozilla/a/b",
        ".local/share/Trash/files",
        "proj/node_modules/pkg/lib",
        "proj/venv/lib/site-packages/m",
        "proj/.git/objects/aa",
        ".files-search/files",
    ):
        (home / rel).mkdir(parents=True, exist_ok=True)
    return home


def test_excluded_dirs_are_not_watched_in_default_mode(tmp_path, kernel, adapters):
    """Режим по умолчанию (max_depth не задан): мусор не получает watch'ей.

    Иначе на домашнем каталоге лимит `max_user_watches` уходит на `.cache`,
    профили браузеров, `node_modules`, venv, `.git` и собственный индекс.
    """
    home = _make_home(tmp_path)

    _start(adapters, home)

    rel = {os.path.relpath(p, home) for p in kernel.paths()}
    assert {"Документы", "Документы/Проект", "Загрузки", "proj"} <= rel
    junk = {
        p
        for p in rel
        if any(
            part.startswith(".") or part in ("node_modules", "venv")
            for part in Path(p).parts
        )
    }
    assert not junk, f"под наблюдением мусорные каталоги: {sorted(junk)}"


def test_watch_limit_keeps_partial_watching(tmp_path, kernel, adapters, caplog):
    """Упор в `max_user_watches` — деградация, а не отказ: корень наблюдается,
    экземпляр не теряется и закрывается вместе с адаптером."""
    home = _make_home(tmp_path)
    kernel.limit = 3

    with caplog.at_level(logging.WARNING, logger=WATCHER_LOGGER):
        adapter = _start(adapters, home)

    assert any(e.is_alive() for e in adapter.observer.emitters), (
        "наблюдение выключено целиком из-за лимита"
    )
    assert "max_user_watches" in caplog.text or "limit" in caplog.text
    fds = list(kernel.tables)
    adapter.close()
    assert not any(kernel.is_open(fd) for fd in fds), "inotify-экземпляр утёк"


def test_failed_root_does_not_leak_inotify_instance(tmp_path, kernel, adapters):
    """watchdog не закрывает inotify-fd, если корень не встал: он открыт до
    выхода процесса, а каждый POST /config или /activate добавлял бы ещё один."""
    root = tmp_path / "docs"
    root.mkdir()
    kernel.limit = 0
    adapter = WatchDogAdapter(root)
    adapters.append(adapter)

    with pytest.raises(OSError):
        adapter._start_observer()

    assert kernel.tables, "эмуляция не дошла до inotify_init"
    assert not any(kernel.is_open(fd) for fd in kernel.tables), (
        "inotify-экземпляр отказавшего корня остался открытым"
    )


def test_recreated_dir_is_watched_again_inotify(tmp_path, kernel, adapters):
    """Каталог, удалённый и созданный заново по тому же пути, снова наблюдается.

    Watch живёт на inode: после rmdir ядро его снимает, и новый каталог без
    повторной постановки остался бы без наблюдения.
    """
    root = tmp_path / "docs"
    (root / "A").mkdir(parents=True)
    adapter = _start(adapters, root, max_depth=1)
    a = str(root / "A")
    before = [c for c in kernel.calls if c[1] == a]

    shutil.rmtree(a)
    kernel.forget(Path(a))
    os.mkdir(a)
    adapter.watch_dir(a)

    after = [c for c in kernel.calls if c[1] == a]
    assert len(after) > len(before), "пересозданный каталог не поставлен на наблюдение"
    assert os.fsencode(a) in kernel.tables[_root_fd(kernel, root)]


def test_unknown_watchdog_internals_fall_back_to_old_scheme(
    tmp_path, kernel, adapters, monkeypatch
):
    """Внутренности watchdog не найдены (другая версия) — наблюдение ставится
    по-старому: рекурсивный эмиттер на корень и свой эмиттер на каталог,
    перенесённый извне. Ни корень, ни такой каталог не остаются без наблюдения.
    """

    class _Unknown:
        pass

    monkeypatch.setattr(mod, "_Inotify", _Unknown)
    root = tmp_path / "docs"
    (root / "отчёты").mkdir(parents=True)
    outside = tmp_path / "Загрузки" / "архив"
    outside.mkdir(parents=True)
    adapter = _start(adapters, root)

    live = [e for e in adapter.observer.emitters if e.is_alive()]
    assert [(e.watch.path, e.watch.is_recursive) for e in live] == [(str(root), True)]
    assert str(root / "отчёты") in kernel.paths(), "рекурсия корня не встала"

    moved = root / "архив"
    os.rename(outside, moved)
    assert adapter.watch_dir(str(moved)) is True
    assert any(
        e.watch.path == str(moved) and e.is_alive() for e in adapter.observer.emitters
    ), "каталог, перенесённый извне, остался без наблюдения"


# --- Observer без inotify (FSEvents на macOS, тестовые заглушки) -------------


class _CountingEmitter(EventEmitter):
    """Эмиттер-счётчик: инициализация в on_thread_start, как у InotifyEmitter."""

    starts: list[str] = []

    def on_thread_start(self) -> None:
        type(self).starts.append(self.watch.path)

    def queue_events(self, timeout: float) -> None:
        time.sleep(timeout)


@pytest.fixture
def counting_observer(monkeypatch):
    _CountingEmitter.starts = []
    monkeypatch.setattr(
        mod, "Observer", lambda: BaseObserver(_CountingEmitter, timeout=0.05)
    )
    return _CountingEmitter


def test_recursive_root_covers_new_dirs_without_emitters(
    tmp_path, counting_observer, adapters
):
    """Рекурсивное наблюдение корня уже покрывает новые каталоги: отдельный
    эмиттер на каждый — лишние потоки и двойные события."""
    root = tmp_path / "docs"
    root.mkdir()
    adapter = _start(adapters, root)

    for i in range(5):
        (root / f"n{i}").mkdir()
        adapter.watch_dir(str(root / f"n{i}"))

    assert counting_observer.starts == [str(root)]
    assert len(adapter.observer.emitters) == 1


class _NoopWatcher(AbstractFilesWatcher):
    async def on_delete(self, file: FileMetadata) -> None:
        return None

    async def on_delete_dir(self, path: str) -> None:
        return None

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        return None


@pytest.mark.asyncio
async def test_recreated_dir_is_watched_again(tmp_path, counting_observer, adapters):
    """После исчезновения каталога его эмиттер снимается (`unschedule`).

    Одной очистки учёта мало: пока остановленный эмиттер лежит в observer,
    `schedule()` по тому же пути новый не создаёт.
    """
    root = tmp_path / "docs"
    (root / "A").mkdir(parents=True)
    adapter = _start(adapters, root, max_depth=1)
    a = str(root / "A")
    emitter = next(e for e in adapter.observer.emitters if e.watch.path == a)

    shutil.rmtree(a)
    emitter.stop()  # так InotifyEmitter реагирует на IN_DELETE_SELF
    emitter.join(1)
    watcher = _NoopWatcher()
    watcher._adapter = adapter  # так его выставляет run()
    await watcher._handle_missing_dirs({a})

    os.mkdir(a)
    starts = len(counting_observer.starts)
    assert adapter.watch_dir(a) is True
    assert len(counting_observer.starts) == starts + 1, "новый эмиттер не поднят"
    assert any(e.watch.path == a and e.is_alive() for e in adapter.observer.emitters)


IN_CREATE = 0x100


@pytest.mark.asyncio
async def test_events_from_added_dirs_carry_their_paths(tmp_path, kernel, adapters):
    """Событие из подкаталога, добавленного в экземпляр корня, приходит с его
    путём: и для каталога со старта, и для перенесённого извне."""
    root = tmp_path / "docs"
    (root / "отчёты").mkdir(parents=True)
    outside = tmp_path / "Загрузки" / "архив"
    outside.mkdir(parents=True)
    adapter = WatchDogAdapter(root, batch_window=0.05)
    adapters.append(adapter)
    await adapter.start_async()
    moved = root / "архив"
    os.rename(outside, moved)
    adapter.watch_dir(str(moved))
    root_fd = _root_fd(kernel, root)

    kernel.deliver(root_fd, root / "отчёты", "план.txt", IN_CREATE)
    kernel.deliver(root_fd, moved, "глава.txt", IN_CREATE)

    seen: set[str] = set()
    events = adapter.__aiter__()
    while len(seen) < 2:
        batch = await asyncio.wait_for(events.__anext__(), timeout=5)
        seen |= {e.path for e in batch if e.change == Change.added}
    assert seen == {str(root / "отчёты" / "план.txt"), str(moved / "глава.txt")}
