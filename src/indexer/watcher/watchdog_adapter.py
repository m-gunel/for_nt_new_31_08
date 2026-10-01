import asyncio
import errno
import os
from collections.abc import Callable
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
from watchfiles import Change, DefaultFilter

from src.utils import get_logger

from ..exclude import exclude_pattern

lg = get_logger(__name__, slice=-1)

# Потолок очереди событий. При массовом копировании файлов события сыплются
# быстрее, чем индексатор их разбирает; без потолка очередь росла бы
# неограниченно.
DEFAULT_MAX_QUEUE = 10_000
# Окно, в течение которого собираем события в один батч, чтобы не гонять
# полный цикл парсинга и эмбеддинга на каждое отдельное событие.
DEFAULT_BATCH_WINDOW = 1.0
DEFAULT_MAX_BATCH = 200


def _dirs_for_watch(root: Path, max_depth: int | None) -> list[Path]:
    """Каталоги, на которые ставить inotify-наблюдение.

    None — только корень: watchdog с recursive=True всё равно обходит дерево
    сам и ставит по одному watch на каждый подкаталог, а здесь мы не
    дублируем обход. Число — обходим дерево сами до max_depth включительно и
    возвращаем каждый каталог на этих уровнях: на каждый из них ставится
    отдельное наблюдение с recursive=False, то есть общее число watch'ей
    ограничено числом каталогов до глубины, а не всем домашним деревом.

    Исключаемые каталоги (`.cache`, `node_modules`, `.git`, `venv`) в список
    не попадают: наблюдение за ними всё равно бесполезно — их содержимое
    отсеет фильтр, — а лимит `fs.inotify.max_user_watches` они выбирают
    наравне с полезными. На домашнем каталоге это основная статья расхода.
    """
    if max_depth is None:
        return [root]

    watched: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        watched.append(current)
        if depth >= max_depth:
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
    return watched


class WatchEvent(NamedTuple):
    change: Change
    path: str
    is_directory: bool


class MyEventHandler(FileSystemEventHandler):
    """Обработчик событий watchdog.

    ВАЖНО: методы вызываются из потока watchdog.Observer, а не из потока
    event loop, поэтому класть в asyncio.Queue напрямую нельзя — она не
    потокобезопасна, и пробуждение ожидающего потребителя может потеряться.
    Всё уходит в цикл через `call_soon_threadsafe`.
    """

    def __init__(self, emit: Callable[[WatchEvent], None]) -> None:
        super().__init__()
        self._emit = emit

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        self._emit(WatchEvent(Change.added, event.src_path, event.is_directory))

    def on_deleted(self, event: DirDeletedEvent | FileDeletedEvent) -> None:
        self._emit(WatchEvent(Change.deleted, event.src_path, event.is_directory))

    def on_moved(self, event: DirMovedEvent | FileMovedEvent) -> None:
        # Пустой путь отбрасываем: os.path.abspath("") даёт текущий рабочий
        # каталог, и удаление/индексация уходили бы по нему. watchdog отдаёт
        # пустой src_path для перемещения ИЗВНЕ наблюдаемого дерева.
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
        # Пути, на которых наблюдение уже стоит: watch_dir зовут на каждый
        # появившийся каталог, и без этого один и тот же путь получал бы по
        # эмиттеру на событие.
        self._watched: set[str] = set()
        self.observer = Observer()
        # Наблюдения ставятся не здесь, а в _start_observer: до старта
        # observer'а отказ по одному пути срывает постановку всех остальных
        # (см. комментарий там же).

    # Типы событий, на которые подписываемся. Вынесено в атрибут класса, чтобы
    # список не расходился между стартовой постановкой и watch_dir.
    EVENT_FILTER = [
        FileModifiedEvent,
        FileMovedEvent,
        FileDeletedEvent,
        FileCreatedEvent,
        # Каталоги нужны для удаления: перемещение папки в корзину —
        # это один rename, и событий по вложенным файлам может не
        # прийти вовсе, а без них записи о них остаются в индексе.
        DirDeletedEvent,
        DirMovedEvent,
        # ...и для появления: без DirCreatedEvent факт «в наблюдаемом дереве
        # возник каталог» до адаптера не доходил вообще, поэтому каталог,
        # перенесённый снаружи внутрь, оставался непроиндексированным.
        DirCreatedEvent,
    ]

    def watch_dir(self, path: str | Path) -> bool:
        """Поставить наблюдение на каталог, появившийся после старта.

        Нужно из-за особенности inotify: у `IN_MOVED_TO` нет бита `IN_CREATE`,
        поэтому watchdog не добавляет watch на каталог, ПЕРЕМЕЩЁННЫЙ внутрь
        наблюдаемого дерева (обычный `mkdir` при этом работает — расхождение
        неочевидное и потому опасное). Без этого файлы, созданные в таком
        каталоге уже после переноса, не замечаются до перезапуска демона.
        """
        directory = str(path)
        if directory in self._watched:
            return False
        try:
            self.observer.schedule(
                self._handler,
                directory,
                recursive=self._recursive,
                event_filter=self.EVENT_FILTER,
            )
        except OSError as exc:
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
        return True

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
        """Блокирующий старт наблюдения. Зовётся либо из потока, либо из теста.

        Observer поднимается ПУСТЫМ, наблюдения ставятся после — по одному.
        Раньше всё расписание создавалось в конструкторе, и первый же отказ на
        старте (упёрлись в `fs.inotify.max_user_watches`, каталог исчез,
        нет прав) выключал наблюдение СРАЗУ ПО ВСЕМ путям. На домашнем
        каталоге упор в лимит вероятен, и терять из-за него слежение за всеми
        папками нельзя: лучше наблюдать за частью, чем ни за чем.
        """
        try:
            self.observer.start()
        except OSError:
            # Часть эмиттеров могла успеть подняться: без остановки их потоки
            # переживут неудачный старт, а close() вызван не будет.
            self.observer.stop()
            self._loop = None
            raise
        scheduled = 0
        skipped = 0
        for root in self._roots:
            for directory in _dirs_for_watch(root, self._max_depth):
                if self.watch_dir(directory):
                    scheduled += 1
                else:
                    skipped += 1
        if not scheduled:
            # Ни одного наблюдения — это уже не деградация, а отказ: пусть
            # вызывающая сторона решает, что с этим делать.
            self.observer.stop()
            self._loop = None
            raise OSError(
                errno.ENOSPC,
                "не удалось поставить ни одного наблюдения "
                f"(путей: {len(self._roots)})",
            )
        log = lg.warning if skipped else lg.info
        log("наблюдение: поставлено %s, пропущено %s", scheduled, skipped)

    async def start_async(self):
        """Поднять наблюдение, не занимая event loop.

        `observer.start()` для inotify выполняет обход всех подкаталогов и по
        системному вызову на каждый, причём в потоке вызывающего: watchdog зовёт
        `on_thread_start()` синхронно, до создания собственного потока. В
        корутине это означает, что на старте демона и на каждом POST /config не
        отвечают ни /health, ни /status, ни /search.

        Цикл захватывается здесь, а не в рабочем потоке: `call_soon_threadsafe`
        из колбэков watchdog'а должен знать, куда слать события.
        """
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_observer)
        return self

    def close(self):
        self.observer.stop()
        self._loop = None

    def __enter__(self):
        # Синхронный вход остаётся для вызова вне event loop: цикл здесь тоже
        # нужен, поэтому требуется работающий loop.
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
                # Каталоги мимо фильтра: он отбирает файлы по расширению,
                # а у каталога расширения нет — иначе удаление папки терялось.
                batch = [
                    e
                    for e in batch
                    if e.is_directory or self.watch_filter(e.change, e.path)
                ]
            if batch:
                yield batch
