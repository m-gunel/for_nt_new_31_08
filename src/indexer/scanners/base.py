import abc
import asyncio
import os
import shutil
import time
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import Any

from persistqueue import AsyncQueue

from src.common import FileMetadata
from src.utils import ConfigMixin, get_logger
from src.utils.lsm_adapter import LSMTable, check_meta_changed

lg = get_logger("app.scanner")


class AbstractBaseScanner(AbstractAsyncContextManager, ConfigMixin, abc.ABC):
    def __init__(
        self,
        queue_path: Path,
        lsm_checker: LSMTable[FileMetadata],
        reset: bool = False,
    ) -> None:
        super().__init__()
        self.queu_path = queue_path
        if reset and (p := queue_path).exists():
            shutil.rmtree(p, ignore_errors=True)
        self.queue = AsyncQueue(str(queue_path))
        self.stack = AsyncExitStack()
        self.lsm_checker = lsm_checker
        # Очередь пересоздаётся на ходу (clear() из POST /config), а читают и
        # пишут в неё разные задачи. Без блокировки clear() успевает подменить
        # self.queue между get() и task_done() в iter().
        self._queue_lock = asyncio.Lock()

    async def __aenter__(self):
        await self.stack.enter_async_context(self.queue)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback,
    ) -> bool | None:
        await self.stack.aclose()

    async def clear(self):
        retired: Path | None = None
        async with self._queue_lock:
            await self.__aexit__(None, None, None)
            self.stack = AsyncExitStack()
            # Каталог живой БД не удаляем на месте: отменённые корутины могут
            # ещё держать открытые хендлы на q00000/info, а новая очередь
            # создаётся по тому же пути и подхватила бы их файлы. Уводим
            # старый каталог в сторону, а удаляем уже вне блокировки, когда
            # ссылок на него не осталось.
            old_path = self.queu_path
            candidate = old_path.with_name(
                f"{old_path.name}.retired.{os.getpid()}.{time.time_ns()}"
            )
            try:
                os.replace(old_path, candidate)
            except OSError:
                # Каталога ещё нет (первый запуск) либо переименование
                # недоступно — запасной путь остаётся прежним.
                shutil.rmtree(old_path, ignore_errors=True)
            else:
                retired = candidate
            self.queue = AsyncQueue(str(old_path))
            await self.__aenter__()
        if retired is not None:
            await asyncio.to_thread(shutil.rmtree, retired, ignore_errors=True)

    async def check_meta(self, meta: FileMetadata) -> bool:
        """Изменился ли файл с момента последней индексации."""
        return await check_meta_changed(self.lsm_checker, meta)

    @abc.abstractmethod
    def preindex(self) -> AsyncGenerator[FileMetadata, Any]:
        raise NotImplementedError()

    async def scan(self, check_index: bool = True):
        async for meta in self.preindex():
            if check_index and not (await self.check_meta(meta)):
                lg.debug("skip indexing %s, already indexed", meta.path)
                continue
            async with self._queue_lock:
                await self.queue.put(meta)

    async def iter(self, endless: bool = True):
        while True:
            async with self._queue_lock:
                queue = self.queue
                # Пустоту проверяем под той же блокировкой, что и снятие:
                # иначе между empty() и get() очередь может опустеть, и
                # блокирующий get() зависнет навсегда.
                if not endless:
                    if await queue.empty():
                        break
                    item: FileMetadata = await queue.get()
            if endless:
                # Здесь get() ждёт нового элемента сколько угодно долго —
                # держать на нём блокировку нельзя, иначе clear() встанет.
                item = await queue.get()
            # yield — ВНЕ блокировки: разбор и эмбеддинг элемента идут
            # секундами, и всё это время clear() не смог бы отработать.
            yield item
            async with self._queue_lock:
                if queue is not self.queue:
                    # Очередь пересоздали, пока мы обрабатывали элемент
                    # (POST /config -> clear()). Отмечать выполнение в НОВОЙ
                    # очереди нельзя: это «task_done() called too many
                    # times», а старая уже закрыта и удалена.
                    break
                await queue.task_done()

    async def total(self):
        async with self._queue_lock:
            return await self.queue.qsize()
