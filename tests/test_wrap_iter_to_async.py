"""wrap_iter_to_async: синхронный итератор → асинхронный без потери элементов."""

import asyncio
import queue
import time

from src.utils import tools


def test_late_timeout_does_not_drop_items(monkeypatch):
    """Таймаут ожидания доходит до потребителя после того, как производитель всё
    отдал и завершился: элементы в очереди не должны потеряться."""

    class LateQueue(queue.Queue):
        late = True

        def get(self, block=True, timeout=None):
            if LateQueue.late and timeout is not None:
                LateQueue.late = False
                time.sleep(0.5)  # производитель тем временем отдаёт всё и выходит
                raise queue.Empty
            return super().get(block, timeout)

    monkeypatch.setattr(tools.queue, "Queue", LateQueue)

    async def collect():
        return [x async for x in tools.wrap_iter_to_async(iter(range(5)))]

    assert asyncio.run(collect()) == list(range(5))


def test_producer_error_reaches_consumer():
    def broken():
        yield 1
        raise ValueError("сбой разбора")

    async def collect():
        out = []
        try:
            async for x in tools.wrap_iter_to_async(broken()):
                out.append(x)
        except ValueError:
            return out, True
        return out, False

    assert asyncio.run(collect()) == ([1], True)
