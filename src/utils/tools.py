import asyncio
import concurrent
import concurrent.futures
import os
import queue
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable
from contextlib import suppress
from functools import wraps
from logging import Logger
from typing import Any, ParamSpec, TypeVar

import pyarrow as pa

from .logging import get_logger
from .thread_priority import background_pool_initializer

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


lg = get_logger(__name__)
_stop = object()


def wrap_with_converter(
    iterable: Iterable[dict[str, Any]], converter: Callable[[dict[str, Any]], T]
):
    for item in iterable:
        yield converter(item)


def wrap_exception_logger(lg: Logger):
    def decorator(
        foo: Callable[P, Coroutine[None, None, R]],
    ) -> Callable[P, Coroutine[None, None, R]]:
        @wraps(foo)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await foo(*args, **kwargs)
            except Exception as e:
                lg.exception(f"Error in {foo.__name__}: {e}")
                raise

        return wrapper

    return decorator


def wrap_exception_logger_sync(lg: Logger):
    def decorator(foo: Callable[P, R]) -> Callable[P, R]:
        @wraps(foo)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return foo(*args, **kwargs)
            except Exception as e:
                lg.exception(f"Error in {foo.__name__}: {e}")
                raise

        return wrapper

    return decorator


def cached_once(
    foo: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    result: R | None = None

    @wraps(foo)
    async def wrapper(*args: P.args, **kwargs: P.kwargs):
        nonlocal result
        if result is not None:
            return result
        result = await foo(*args, **kwargs)
        return result

    return wrapper


# Общий пул для перекладывания синхронных итераторов в async: свой пул на
# вызов обошёлся бы в пул на каждый просканированный файл.
_ITER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(4, min(16, (os.cpu_count() or 4) * 2)),
    thread_name_prefix="iter_to_async",
    # Пул обслуживает только конвейер индексации (обход ФС, парсинг) —
    # его потоки уступают CPU интерактивной работе пользователя.
    initializer=background_pool_initializer,
)


async def wrap_iter_to_async(
    iterable: Iterable[R],
    max_size: int = 1024,
) -> AsyncGenerator[R, Any]:
    """Отдать синхронный итератор как асинхронный, не блокируя event loop.

    Производитель работает в общем пуле потоков и ВСЕГДА закрывает поток
    сентинелом `_stop`: без него потребитель завершает итератор только по
    таймауту `que.get(timeout=1)`, то есть теряет секунду на каждый
    обёрнутый итератор.
    """
    que: queue.Queue = queue.Queue(max_size)
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()

    @wrap_exception_logger_sync(lg)
    def _produce():
        try:
            for d in iterable:
                if stop_event.is_set():
                    return
                que.put(d)
        finally:
            # Закрываем поток в любом случае, в том числе при исключении,
            # иначе потребитель будет ждать данные, которых уже не будет.
            with suppress(queue.Full):
                que.put(_stop, timeout=5)

    task = loop.run_in_executor(_ITER_EXECUTOR, _produce)
    try:
        while True:
            try:
                # Небольшой таймаут — только страховка на случай, если поток
                # производителя умер, не успев отдать сентинел.
                ret = await loop.run_in_executor(_ITER_EXECUTOR, que.get, True, 0.25)
            except queue.Empty:
                if task.done():
                    break
                continue
            if ret is _stop:
                break
            yield ret
        # Ошибка производителя должна дойти до потребителя: иначе сбой разбора
        # выглядит как «файл просто пустой», вызывающий код помечает его
        # обработанным, и файл выпадает из индексации навсегда.
        await task
    finally:
        stop_event.set()
        # Разблокируем производителя, если он упёрся в заполненную очередь.
        while not task.done():
            try:
                que.get_nowait()
            except queue.Empty:
                break
        with suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), 5)


async def async_merge_batches(
    batches: AsyncGenerator[pa.RecordBatch, None],
    target_size: int = 1024,
):
    _batches = []
    _len = 0
    async for batch in batches:
        _batches.append(batch)
        _len += len(batch)
        if _len >= target_size:
            table = pa.Table.from_batches(_batches)
            yield table
            _batches.clear()
            _len = 0
    if _batches:
        yield pa.Table.from_batches(_batches)
