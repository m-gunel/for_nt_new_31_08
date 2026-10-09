import asyncio
import errno
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.utils import UnsupportedLibcError
from watchfiles import Change, DefaultFilter

from src.utils import get_logger

from ..exclude import exclude_pattern

try:
    # Только Linux: на других ОС модуль не импортируется (в libc нет inotify).
    from watchdog.observers.inotify import InotifyObserver as _InotifyObserver
    from watchdog.observers.inotify_c import Inotify as _Inotify
except (ImportError, OSError, UnsupportedLibcError):
    _InotifyObserver = None  # type: ignore[assignment,misc]
    _Inotify = None  # type: ignore[assignment,misc]

lg = get_logger(__name__, slice=-1)

# Потолок очереди событий: при массовом копировании они приходят быстрее,
# чем индексатор их разбирает.
DEFAULT_MAX_QUEUE = 10_000
# Окно сбора событий в один батч: не гонять парсинг и эмбеддинг на каждое.
DEFAULT_BATCH_WINDOW = 1.0
DEFAULT_MAX_BATCH = 200


def _walk_dirs(root: Path, max_depth: int | None) -> Iterator[Path]:
    """Каталоги поддерева root до max_depth включительно (None — без предела).

    Исключаемые каталоги (`.cache`, `node_modules`, `.git`, `venv`) и
    символьные ссылки пропускаем вместе с поддеревом: их содержимое всё равно
    отсеет фильтр, а лимит `fs.inotify.max_user_watches` на домашнем каталоге
    уходит в основном на них.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        yield current
        if max_depth is not None and depth >= max_depth:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        # Каталог исчез во время обхода или нет прав — пропускаем.
                        continue
                    if exclude_pattern.match(Path(entry.path).name):
                        continue
                    stack.append((Path(entry.path), depth + 1))
        except OSError:
            # Каталог недоступен — на него всё равно поставим watch (если он
            # станет доступен, событие появится).
            continue


def _dirs_for_watch(root: Path, max_depth: int | None) -> list[Path]:
    """Каталоги под отдельное наблюдение.

    Нужны observer'у без inotify и inotify без ожидаемых внутренностей watchdog.

    None — только корень: наблюдение рекурсивное. Число — каталоги до
    max_depth включительно (см. `_walk_dirs`), на каждый — recursive=False.
    """
    if max_depth is None:
        return [root]
    return list(_walk_dirs(root, max_depth))


def _close_leaked_inotify(exc: BaseException) -> None:
    """Закрыть inotify-экземпляр, брошенный watchdog'ом при отказе эмиттера.

    `Inotify.__init__` открывает экземпляр и ставит watch'и до того, как
    эмиттер попадёт в observer; отказ на середине (ENOSPC) оставляет fd
    открытым до выхода процесса вместе с уже поставленными watch'ами.
    Ссылка на недостроенный объект есть только в трассировке.
    """
    if _Inotify is None:
        return
    tb = exc.__traceback__
    while tb is not None:
        obj = tb.tb_frame.f_locals.get("self")
        if isinstance(obj, _Inotify):
            for name in ("_inotify_fd", "_kill_r", "_kill_w"):
                fd = vars(obj).get(name)
                if isinstance(fd, int):
                    with suppress(OSError):
                        os.close(fd)
            return
        tb = tb.tb_next


class WatchEvent(NamedTuple):
    change: Change
    path: str
    is_directory: bool


class MyEventHandler(FileSystemEventHandler):
    """Обработчик событий watchdog.

    Методы вызываются из потока watchdog.Observer, а asyncio.Queue не
    потокобезопасна (пробуждение потребителя может потеряться), поэтому
    события уходят в цикл через `call_soon_threadsafe`.
    """

    def __init__(self, emit: Callable[[WatchEvent], None]) -> None:
        super().__init__()
        self._emit = emit

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        self._emit(WatchEvent(Change.added, event.src_path, event.is_directory))

    def on_deleted(self, event: DirDeletedEvent | FileDeletedEvent) -> None:
        self._emit(WatchEvent(Change.deleted, event.src_path, event.is_directory))

    def on_moved(self, event: DirMovedEvent | FileMovedEvent) -> None:
        # При перемещении извне дерева watchdog отдаёт пустой src_path, а
        # os.path.abspath("") — это рабочий каталог процесса.
        if event.src_path:
            self._emit(WatchEvent(Change.deleted, event.src_path, event.is_directory))
        if event.dest_path:
            self._emit(WatchEvent(Change.added, event.dest_path, event.is_directory))

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        self._emit(WatchEvent(Change.modified, event.src_path, event.is_directory))


class WatchDogAdapter:
    def __init__(
        self,
        *paths: Path,
        watch_filter: Callable[[Change, str], bool] | None = DefaultFilter(),
        ignore_permission_denied=True,
        max_queue: int = DEFAULT_MAX_QUEUE,
        batch_window: float = DEFAULT_BATCH_WINDOW,
        max_batch: int = DEFAULT_MAX_BATCH,
        max_depth: int | None = None,
    ) -> None:
        self.queue: asyncio.Queue[WatchEvent] = asyncio.Queue(max_queue)
        self.watch_filter = watch_filter
        self.batch_window = batch_window
        self.max_batch = max_batch
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._ignore_permission_denied = ignore_permission_denied
        self._handler = MyEventHandler(self._emit)
        self._roots = [Path(path) for path in paths]
        self._max_depth = max_depth
        self._recursive = max_depth is None
        # Пути с собственным эмиттером: watch_dir зовут на каждый появившийся
        # каталог, и без этого путь получал бы по эмиттеру на событие.
        self._watched: set[str] = set()
        # ObservedWatch по пути — снять эмиттер исчезнувшего каталога.
        self._watches: dict[str, object] = {}
        # Постановка наблюдений — под замком, флаг остановки ставится до него:
        # поток _start_observer, ещё обходящий дерево, после close() не
        # заводит эмиттеров, которые уже никто не остановит.
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._last_errno: int | None = None
        self.observer = Observer()
        # Наблюдения ставит _start_observer — по одному, после старта observer'а.

    EVENT_FILTER = [
        FileModifiedEvent,
        FileMovedEvent,
        FileDeletedEvent,
        FileCreatedEvent,
        # Перенос папки (в корзину или внутрь дерева извне) — один rename без
        # событий по вложенным файлам: удаление и появление видны только по
        # событию самого каталога.
        DirDeletedEvent,
        DirMovedEvent,
        DirCreatedEvent,
    ]

    def _inotify_mode(self) -> bool:
        return _InotifyObserver is not None and isinstance(
            self.observer, _InotifyObserver
        )

    def _inotify_for(self, root: str):
        """inotify-экземпляр эмиттера корня или None (не inotify, эмиттер снят).

        В watchdog нет API добавить каталог в уже работающий эмиттер; цепочка
        `InotifyEmitter._inotify._inotify` закреплена тестом
        tests/test_watcher_instances.py.
        """
        watch = self._watches.get(root)
        if watch is None or _Inotify is None:
            return None
        for emitter in list(getattr(self.observer, "emitters", ())):
            if emitter.watch == watch:
                inotify = getattr(getattr(emitter, "_inotify", None), "_inotify", None)
                return inotify if isinstance(inotify, _Inotify) else None
        return None

    def _root_of(self, directory: str) -> tuple[str | None, int]:
        """Ближайший корень, под которым лежит каталог, и глубина от него."""
        best: str | None = None
        best_abs = ""
        for root in self._roots:
            root_abs = os.path.abspath(root)
            prefix = root_abs.rstrip(os.sep) + os.sep
            if directory == root_abs or directory.startswith(prefix):
                if len(root_abs) > len(best_abs):
                    best, best_abs = str(root), root_abs
        if best is None:
            return None, 0
        rel = os.path.relpath(directory, best_abs)
        return best, 0 if rel == os.curdir else len(Path(rel).parts)

    def _schedule(self, directory: str, recursive: bool) -> bool:
        """Отдельный эмиттер на каталог. Звать под self._lock."""
        try:
            watch = self.observer.schedule(
                self._handler,
                directory,
                recursive=recursive,
                event_filter=self.EVENT_FILTER,
            )
        except OSError as exc:
            _close_leaked_inotify(exc)
            self._last_errno = exc.errno
            level = (
                lg.debug
                if self._ignore_permission_denied and exc.errno == errno.EACCES
                else lg.warning
            )
            level(
                "не удалось поставить наблюдение на %s (errno=%s %s)",
                directory,
                exc.errno,
                exc.strerror,
            )
            return False
        self._watched.add(directory)
        self._watches[directory] = watch
        return True

    def _unschedule(self, directory: str) -> None:
        """Снять эмиттер каталога. Звать под self._lock."""
        self._watched.discard(directory)
        watch = self._watches.pop(directory, None)
        unschedule = getattr(self.observer, "unschedule", None)
        if watch is None or unschedule is None:
            return
        try:
            unschedule(watch)
        except (KeyError, OSError):
            # Эмиттер уже снят вместе с остановленным observer'ом.
            lg.debug("наблюдение за %s уже снято", directory)

    def _add_watches(
        self, inotify, top: Path, max_depth: int | None
    ) -> tuple[int, int]:
        """Добавить каталоги поддерева в inotify-экземпляр эмиттера корня.

        Без нового экземпляра: `fs.inotify.max_user_instances` (128) общий на
        всего пользователя. Повторный add_watch на уже наблюдаемый каталог
        безвреден — ядро возвращает тот же watch.
        """
        added = failed = 0
        for directory in _walk_dirs(top, max_depth):
            with self._lock:
                if self._closing.is_set():
                    break
                try:
                    inotify.add_watch(os.fsencode(directory))
                except OSError as exc:
                    failed += 1
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                        # Каталог исчез во время обхода.
                        continue
                    self._last_errno = exc.errno
                    # ENOSPC — исчерпан fs.inotify.max_user_watches: дальше
                    # отказы пойдут на каждый каталог.
                    lg.warning(
                        "не удалось поставить наблюдение на %s (errno=%s %s), "
                        "остальная часть дерева — только периодическим проходом",
                        directory,
                        exc.errno,
                        exc.strerror,
                    )
                    break
            added += 1
        return added, failed

    def _watch_tree(self, root: Path) -> tuple[int, int] | None:
        """inotify: один экземпляр на корень, подкаталоги — add_watch в него.

        Возвращает (поставлено, пропущено); None — эмиттер без ожидаемых
        внутренностей watchdog: тогда на каждый каталог ставится свой эмиттер.
        """
        top = str(root)
        with self._lock:
            if self._closing.is_set():
                return 0, 0
            if top in self._watched:
                return 0, 1
            # recursive=False: обход со своими исключениями делаем сами,
            # рекурсия watchdog'а их не знает.
            if not self._schedule(top, recursive=False):
                return 0, 1
        inotify = self._inotify_for(top)
        if inotify is None:
            with self._lock:
                self._unschedule(top)
            return None
        return self._add_watches(inotify, root, self._max_depth)

    def watch_dir(self, path: str | Path) -> bool:
        """Поставить наблюдение на каталог, появившийся после старта.

        У `IN_MOVED_TO` нет бита `IN_CREATE`, поэтому watchdog не ставит watch
        на каталог, ПЕРЕМЕЩЁННЫЙ внутрь дерева. На inotify каталог и его
        поддерево добавляются в экземпляр корня, а не в новый эмиттер; без
        inotify рекурсивное наблюдение корня уже покрывает каталог. Каталоги
        глубже max_depth не наблюдаются, как и на старте.
        """
        directory = str(path)
        root, depth = self._root_of(os.path.abspath(directory))
        if root is not None and self._max_depth is not None:
            if depth > self._max_depth:
                return False
        inotify_mode = self._inotify_mode()
        inotify = self._inotify_for(root) if root is not None and inotify_mode else None
        if inotify is not None:
            limit = None if self._max_depth is None else self._max_depth - depth
            added, _failed = self._add_watches(inotify, Path(directory), limit)
            return added > 0
        with self._lock:
            if self._closing.is_set():
                return False
            # На inotify без доступа к экземпляру корня рекурсия watchdog'а не
            # покрывает каталог, перенесённый извне, — ему нужен свой эмиттер.
            if (
                root is not None
                and self._recursive
                and root in self._watched
                and not inotify_mode
            ):
                return False
            if directory in self._watched:
                return False
            return self._schedule(directory, recursive=self._recursive)

    def unwatch_dir(self, path: str | Path) -> None:
        """Снять эмиттеры исчезнувшего каталога и его поддерева.

        Иначе путь числится наблюдаемым навсегда, а `schedule()` по тому же
        пути новый эмиттер не создаёт, пока старый лежит в observer: каталог,
        созданный заново, остался бы без наблюдения. Корни не трогаем.
        """
        directory = str(path)
        prefix = directory.rstrip(os.sep) + os.sep
        roots = {str(root) for root in self._roots}
        with self._lock:
            gone = [
                p
                for p in self._watched
                if (p == directory or p.startswith(prefix)) and p not in roots
            ]
            for p in gone:
                self._unschedule(p)

    def _emit(self, event: WatchEvent) -> None:
        """Вызывается из потока watchdog — только через call_soon_threadsafe."""
        loop = self._loop
        if loop is None:
            return

        def _put() -> None:
            if self.queue.full():
                # Свежие события важнее самых старых: старое всё равно будет
                # подхвачено периодическим полным сканированием.
                try:
                    self.queue.get_nowait()
                    self._dropped += 1
                    if self._dropped % 100 == 1:
                        lg.warning(
                            "очередь событий переполнена, отброшено %s событий",
                            self._dropped,
                        )
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(event)

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            # Цикл уже остановлен — гасим, чтобы не шуметь при shutdown.
            pass

    def _start_observer(self):
        """Блокирующий старт наблюдения. Зовётся из потока или из теста.

        Observer поднимается пустым, наблюдения ставятся после — по одному:
        отказ на одном пути (лимит `fs.inotify.max_user_watches`, каталог
        исчез, нет прав) иначе снял бы наблюдение со всех остальных.
        """
        with self._lock:
            if self._closing.is_set():
                return
            try:
                self.observer.start()
            except OSError:
                # Часть эмиттеров могла успеть подняться: без остановки их потоки
                # переживут неудачный старт, а close() вызван не будет.
                self.observer.stop()
                self._loop = None
                raise
        inotify_mode = self._inotify_mode()
        scheduled = 0
        skipped = 0
        for root in self._roots:
            counts = self._watch_tree(root) if inotify_mode else None
            if counts is not None:
                scheduled += counts[0]
                skipped += counts[1]
                continue
            for directory in _dirs_for_watch(root, self._max_depth):
                if self.watch_dir(directory):
                    scheduled += 1
                else:
                    skipped += 1
        if self._closing.is_set():
            return
        if not scheduled:
            # Ни одного наблюдения — уже отказ, а не деградация: решает
            # вызывающая сторона. errno — настоящей причины: по нему run()
            # выбирает подсказку (max_user_watches или max_user_instances).
            self.observer.stop()
            self._loop = None
            raise OSError(
                self._last_errno or errno.ENOSPC,
                "не удалось поставить ни одного наблюдения "
                f"(путей: {len(self._roots)})",
            )
        log = lg.warning if skipped else lg.info
        log("наблюдение: поставлено %s, пропущено %s", scheduled, skipped)

    async def start_async(self):
        """Поднять наблюдение, не занимая event loop.

        Постановка обходит дерево и делает по системному вызову на каждый
        каталог; в корутине на это время не отвечали бы ни /health, ни
        /status, ни /search — на старте демона и на каждом POST /config.

        Цикл захватывается здесь, а не в рабочем потоке: `call_soon_threadsafe`
        из колбэков watchdog'а должен знать, куда слать события.
        """
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_observer)
        return self

    def close(self):
        """Остановить наблюдение. Блокирует до конца текущей постановки —
        из корутины звать через asyncio.to_thread."""
        self._closing.set()
        with self._lock:
            # Дождаться постановки, идущей прямо сейчас: иначе её эмиттер
            # переживёт observer.stop().
            pass
        self.observer.stop()
        self._loop = None

    def __enter__(self):
        # Блокирующий вариант start_async: обход дерева идёт в потоке event loop.
        self._loop = asyncio.get_running_loop()
        self._start_observer()
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    async def __aiter__(self):
        """Отдаёт события пачками, схлопывая всплески файловой активности."""
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self.queue.get()]
            deadline = loop.time() + self.batch_window
            while len(batch) < self.max_batch:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    batch.append(
                        await asyncio.wait_for(self.queue.get(), timeout=timeout)
                    )
                except TimeoutError:
                    break
            if self.watch_filter:
                # Каталоги мимо фильтра: он отбирает файлы по расширению, и
                # удаление папки иначе терялось бы.
                batch = [
                    e
                    for e in batch
                    if e.is_directory or self.watch_filter(e.change, e.path)
                ]
            if batch:
                yield batch
