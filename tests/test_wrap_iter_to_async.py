"""wrap_iter_to_async: синхронный итератор → асинхронный без потери элементов."""

import asyncio
import logging
import queue
import time

import pytest

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


def test_producer_oserror_is_left_to_consumer(caplog):
    """Штатный OSError производителя (файл исчез, нет прав) обёртка не пишет
    в журнал ERROR со стеком: исключение уходит потребителю, а что писать в
    журнал, решает он (_process_path — одна строка WARNING)."""

    def vanished():
        raise FileNotFoundError(2, "No such file or directory", "/нет/отчёт.txt")
        yield  # pragma: no cover — это генератор

    async def collect():
        async for _ in tools.wrap_iter_to_async(vanished()):
            pass

    caplog.set_level(logging.DEBUG)
    with pytest.raises(FileNotFoundError):
        asyncio.run(collect())
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_vanished_file_gives_one_warning_without_traceback(tmp_path, caplog):
    """Файл, удалённый между обходом и разбором: одна строка WARNING от
    _process_path, без ERROR и трассировки; повтор на следующем проходе."""
    from src.common import FileMetadata
    from src.parsers import _process_path

    meta = FileMetadata(path=str(tmp_path / "отчёт.txt"), size=10, mtime_seconds=1)
    fails = []

    async def fail_cb(m, *, permanent):
        fails.append(permanent)

    caplog.set_level(logging.DEBUG)
    assert [c async for c in _process_path(meta, fail_cb)] == []
    assert fails == [False]
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.exc_info) for r in loud] == [(logging.WARNING, None)]
