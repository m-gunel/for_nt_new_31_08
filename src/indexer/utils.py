import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal, TypeAlias

from fastapi import Depends

from src.common import FileMetadata
from src.parsers import file_extension, get_parsers_registry
from src.utils import get_config, get_logger
from src.utils.lsm_adapter import wait_idle as lsm_wait_idle

from .indexer import Index
from .storage_check import ensure_index_usable, remove_email_leftovers
from .watcher import Watcher

lg = get_logger(__name__, slice=-1)


_watcher: Watcher | None = None
_watcher_task: asyncio.Task | None = None
_index: Index | None = None

# Защищает последовательность stop_watcher → stop_index → delete → reindex →
# restart_watcher в POST /config: два параллельных запроса иначе гонятся
# за _watcher_task/_index.
_reconfigure_lock = asyncio.Lock()


def reconfigure_lock() -> asyncio.Lock:
    return _reconfigure_lock


def _get_watcher_task():
    if _watcher_task is None:
        raise ValueError("watcher not started")
    return _watcher_task


def filter_files(path: str) -> bool:
    """Индексируем файл, только если для его расширения есть парсер."""
    return file_extension(path) in get_parsers_registry()


def filter_meta(meta: FileMetadata):
    conf = get_config()
    if meta.size > conf.file_scan_cfg.max_size:
        lg.info(
            f"File {meta.path} filtered out due to size {meta.size} > {conf.file_scan_cfg.max_size}"
        )
        return False
    return True


@asynccontextmanager
async def serve_indexer():
    global _watcher_task, _watcher
    cfg = get_config()
    cfg.file_scan_cfg.filter_files = filter_files
    cfg.file_scan_cfg.filter_meta = filter_meta

    # До Index(cfg): он сразу открывает LSM и очередь. Сбой шага пишем в лог,
    # но демон поднимаем — поиск по тому, что есть, лучше отказа.
    for prepare in (remove_email_leftovers, ensure_index_usable):
        try:
            await asyncio.to_thread(prepare, cfg)
        except Exception:
            lg.exception("подготовка хранилища (%s) не удалась", prepare.__name__)
    index = Index(cfg)
    _watcher = Watcher(cfg, index)
    async with index.lifespan():
        # От прошлого запуска здесь могла остаться мёртвая задача.
        _watcher_task = None
        # При выключенном автозапуске наблюдение тоже не поднимаем: БТ §5.2
        # требует прекратить «обработку новых файлов», а не только обход.
        # Watcher поднимет POST /activate.
        if index.active:
            _watcher_task = asyncio.create_task(_watcher.run())  # type: ignore
        try:
            set_index(index)
            yield index
        except Exception as e:
            lg.error(f"Error occurred while watching files: {e}", exc_info=True)
        finally:
            # Может быть None: автозапуск выключен и активации не было.
            if _watcher_task is not None:
                _watcher_task.cancel()
                with suppress(asyncio.CancelledError):
                    await _watcher_task
            # Обнуляем: переменная глобальная и переживает выход из
            # serve_indexer (тесты поднимают его многократно). Оставленная
            # задача прошлого запуска заставила бы restart_watcher считать
            # наблюдение живым и молча отказаться поднимать новое.
            _watcher_task = None


async def stop_watcher():
    global _watcher_task, _watcher
    # Не assert: под `python -O` он вырезается, и вместо внятной ошибки
    # получаем падение по AttributeError/NoneType где-то ниже.
    if _watcher is None:
        raise RuntimeError("watcher is not started")
    # Уже остановлен — не ошибка: /deactivate обязан быть идемпотентным, а
    # POST /config может прийти на деактивированном индексе.
    if _watcher_task is None:
        lg.debug("watcher уже остановлен")
        return
    lg.info("stop watcher")
    _watcher_task.cancel()
    with suppress(asyncio.CancelledError):
        await _watcher_task
    # Обнуляем: иначе повторный stop_watcher отменяет уже отменённую задачу, а
    # restart_watcher не может отличить работающее наблюдение от мёртвого.
    _watcher_task = None
    # Отмена задачи не отменяет уже отданную в LSM-поток работу: on_upsert
    # успевает поставить туда check_meta/async_set, и они доигрывают уже
    # после того, как watcher считается остановленным.
    await lsm_wait_idle()


async def restart_watcher():
    global _watcher_task
    if _watcher is None:
        raise RuntimeError("watcher is not initialized")
    # Второй задачи на том же наблюдателе быть не должно: обе получали бы одни
    # и те же события, и каждый изменённый файл индексировался бы дважды.
    if _watcher_task is not None and not _watcher_task.done():
        lg.debug("watcher уже работает")
        return
    lg.info("restart watcher")
    _watcher_task = asyncio.create_task(_watcher.run())


def _get_index() -> Index:
    if _index is None:
        raise RuntimeError("index is not initialized")
    return _index


def set_index(index: Index) -> None:
    global _index
    _index = index


AppIndex: TypeAlias = Annotated[Index, Depends(_get_index)]
AppWatcherTask: TypeAlias = Annotated[asyncio.Task, _get_watcher_task]


class UniqueCounter:
    """Счётчик, учитывающий каждое значение ровно один раз за прогон.

    Вытеснение по размеру здесь не годится: вытесненный файл считается новым
    повторно, и `/status` показывает неверный прогресс. Множество очищается
    на каждый прогон (`reset`/`clear_cache`), поэтому бесконечно не растёт.
    """

    def __init__(self, initial_count: int = 0) -> None:
        self.cnt = initial_count
        self.seen: set = set()

    def reset(self, count: int = 0):
        self.cnt = count
        self.seen.clear()

    def clear_cache(self):
        self.seen.clear()

    def count(self, value, mode: Literal["add", "sub"]):
        if value not in self.seen:
            self.seen.add(value)
            self.cnt += 1 if mode == "add" else -1
            self.cnt = max(self.cnt, 0)
        return self.cnt

    def update(self, n=1):
        """Сдвинуть счётчик без учёта значения — для повтора, который уже учтён
        в `seen` первой постановкой. Не ниже нуля, как и `count`."""
        self.cnt = max(self.cnt + n, 0)
