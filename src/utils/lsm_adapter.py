import asyncio
import concurrent
import concurrent.futures
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, ExitStack
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import (
    Literal,
    Optional,
    ParamSpec,
    Self,
    TypeVar,
    overload,
)

from lsm import LSM

from src.common import FileMetadata
from src.utils import get_logger

lg = get_logger(__name__, slice=-1)

# Единственный поток, которому разрешено работать с lsm_db.
#
# Хендл lsm_db однопоточный: он хранит собственный номер читателя (iReader),
# список открытых курсоров (pCsr) и глубину транзакций (nTransOpen), а на входе
# в каждую операцию проверяет их согласованность (assert_db_state,
# lsm_main.c:43). Обращение из нескольких потоков это состояние рассогласует —
# в отладочной сборке процесс убивается через abort(), в release состояние
# портится молча.
#
# Поэтому инвариант модуля: к self.lsm обращаются либо из этого executor'а
# (через to_thread ниже), либо синхронно под LSMTable._lock. Дефолтный пул
# asyncio (asyncio.to_thread), _ITER_EXECUTOR и _INDEX_BUILD_EXECUTOR к LSM
# не допускаются.
executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="lsm")

P = ParamSpec("P")
R = TypeVar("R")

# СТ, п.3.3 «Надёжность»: повтор при сбоях чтения LSM — до 10 попыток с паузой 1с.
LSM_READ_RETRIES = 10
LSM_RETRY_DELAY = 1.0
# Транзиентные ошибки движка/ФС. KeyError сюда намеренно не входит:
# «ключа нет» — это штатный результат, а не сбой.
_RETRYABLE_ERRORS = (RuntimeError, OSError)


def retry_read(foo: Callable[[], R]) -> R:
    """Повторить чтение LSM при транзиентном сбое.

    Вызывается уже внутри worker-потока `executor`, поэтому блокирующий
    `time.sleep` здесь не задевает event loop. Блокировку таблицы берёт сам
    `foo` — на время паузы между попытками она отпущена, иначе десять секунд
    ретраев запирали бы все остальные обращения к БД.
    """
    for attempt in range(LSM_READ_RETRIES):
        try:
            return foo()
        except _RETRYABLE_ERRORS as exc:
            if attempt == LSM_READ_RETRIES - 1:
                lg.error("LSM read failed after %s attempts: %s", LSM_READ_RETRIES, exc)
                raise
            lg.warning(
                "LSM read failed (attempt %s/%s): %s — retrying in %ss",
                attempt + 1,
                LSM_READ_RETRIES,
                exc,
                LSM_RETRY_DELAY,
            )
            time.sleep(LSM_RETRY_DELAY)
    raise RuntimeError("unreachable")  # pragma: no cover


async def to_thread(foo: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(foo, *args, **kwargs))


async def wait_idle() -> None:
    """Дождаться, пока в LSM-потоке не останется незавершённой работы.

    Отмена корутины НЕ отменяет уже отданный в executor вызов: он доигрывает
    в потоке, пока вызывающая сторона считает, что всё остановлено. Барьер
    нужен там, где следующий шаг рассчитывает на покой БД — в первую очередь
    в POST /config (stop_index -> clear -> start_reindex).

    Executor однопоточный, поэтому пустая задача выполняется строго после
    всего, что было поставлено в очередь раньше.
    """
    await to_thread(lambda: None)


async def check_meta_changed(
    table: "LSMTable[FileMetadata]", meta: FileMetadata
) -> bool:
    """Изменился ли файл с момента последней индексации.

    True — файл новый или изменился (нужно индексировать), False — не менялся.
    При ошибке чтения считаем изменившимся: лучше лишний раз переиндексировать,
    чем молча потерять файл.
    """
    try:
        stored = await table.async_get(meta.path)
        if stored is None:
            return True
        return stored.size != meta.size or stored.mtime_seconds != meta.mtime_seconds
    except Exception as e:
        lg.error("Error checking metadata for %s: %s", meta.path, e, exc_info=True)
        return True


def _encode_file_meta(meta: FileMetadata) -> bytes:
    return struct.pack("<QL", meta.size, meta.mtime_seconds)


def _decode_file_meta(key: str, data: bytes) -> FileMetadata:
    if len(data) != 12:
        raise ValueError(f"Invalid data length: {len(data)}")
    else:
        size, mtime_seconds = struct.unpack("<QL", data)
        return FileMetadata(path=key, size=size, mtime_seconds=mtime_seconds)


def _encode_int(value: int) -> bytes:
    return value.to_bytes(length=8, byteorder="big", signed=False)


def _decode_int(key: str, data: bytes) -> int:
    if len(data) != 8:
        raise ValueError(f"Invalid data length: {len(data)}")
    else:
        return int.from_bytes(data, byteorder="big", signed=False)


class LSMTable[T](AbstractContextManager):
    def __init__(
        self,
        path: Path,
        encoder: Callable[[T], bytes],
        decoder: Callable[[str, bytes], T],
    ) -> None:
        self.encoder = encoder
        self.name = path.name
        self.path = path
        self.decoder = decoder
        self.exit_stack = ExitStack()
        # RLock, а не Lock: методы класса вызывают друг друга (delete_prefix
        # держит блокировку и внутри неё удаляет ключи).
        #
        # Создаётся ДО connect(): открытие БД тоже идёт под блокировкой.
        self._lock = threading.RLock()
        self.lsm = self.connect()

    def close(self):
        with self._lock:
            self.exit_stack.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        self.close()
        return None

    def __enter__(self) -> Self:
        return self

    def _read_raw(self, key: str) -> bytes | None:
        """Сырое чтение с повтором. None — ключа нет (это не ошибка)."""

        def _read() -> bytes | None:
            with self._lock:
                try:
                    return self.lsm[key.encode()]  # type: ignore
                except KeyError:
                    return None

        return retry_read(_read)

    def is_in(self, key: str) -> bool:
        return self._read_raw(key) is not None

    async def async_is_in(self, key: str) -> bool:
        return await to_thread(self.is_in, key)

    @overload
    def get(self, key: str, decode: Literal[True] = True) -> T: ...
    @overload
    def get(self, key: str, decode: Literal[False] = False) -> None: ...

    def get(self, key: str, decode: Literal[True, False] = True) -> T | None:
        data = self._read_raw(key)
        if data is None:
            return None
        if decode:
            return self.decoder(key, data)
        return None

    @overload
    async def async_get(self, key: str, decode: Literal[True] = True) -> T: ...

    @overload
    async def async_get(self, key: str, decode: Literal[False] = False) -> None: ...

    async def async_get(
        self,
        key: str,
        decode: bool = True,
    ) -> T | None:
        return await to_thread(
            self.get,
            key=key,
            decode=decode,  # type: ignore
        )

    def set(self, key: str, value: T) -> None:
        data = self.encoder(value)
        with self._lock:
            self.lsm[key.encode()] = data

    async def async_set(self, key, value: T) -> None:
        await to_thread(self.set, key, value)

    def delete(self, key: str) -> None:
        with self._lock:
            try:
                self.lsm.delete(key.encode())
            except KeyError:
                pass

    async def async_delete(self, key: str) -> None:
        await to_thread(self.delete, key)

    def _keys_with_prefix(self, b_prefix: bytes) -> list[bytes]:
        """Ключи с этим префиксом. Курсора после себя не оставляет.

        Полный обход `keys()`, а не курсор с `seek`, хотя seek и дешевле:
        на ПУСТОЙ базе lsm вообще не даёт открыть курсор
        (`RuntimeError: Invalid cursor`), а пустая база — штатное состояние
        (свежая установка, только что очищенный индекс). `keys()` этот случай
        переживает.

        Прежний вариант — срез `self.lsm[b_prefix:]` с выходом по `break` —
        оставлял курсор открытым на неопределённый срок, а следующая же
        строка открывала на том же хендле транзакцию: ровно та пара
        «курсор + транзакция», согласованность которой проверяет
        assert_db_state. Здесь генератор вычитывается до конца, поэтому
        курсор закрывается сразу.

        Цена — обход всех ключей вместо только совпавших. Это допустимо:
        delete_prefix зовут при смене списка папок и при выключении почты,
        а полный обход по таблице и так делает keys_snapshot() на каждом
        проходе индексации.
        """
        with self._lock:
            return [key for key in self.lsm.keys() if key.startswith(b_prefix)]  # type: ignore

    def delete_prefix(self, prefix: str):
        b_prefix = prefix.encode()
        with self._lock:
            keys_to_delete = self._keys_with_prefix(b_prefix)
            if not keys_to_delete:
                return
            with self.lsm.transaction():
                for k in keys_to_delete:
                    del self.lsm[k]

    async def async_delete_prefix(self, prefix: str):
        await to_thread(self.delete_prefix, prefix)

    def keys_snapshot(self) -> list[str]:
        """Все ключи таблицы одним снимком.

        Значения не декодируются: вызывающей стороне (сверка удалённых файлов)
        нужны только пути, а декодирование каждой записи на корпусе в сотню
        тысяч файлов — лишняя работа.
        """
        with self._lock:
            return [key.decode() for key in self.lsm.keys()]  # type: ignore

    def items_snapshot(self) -> list[tuple[str, T]]:
        with self._lock:
            return [
                (key.decode(), self.decoder(key.decode(), data))
                for key, data in self.lsm.items()  # type: ignore
            ]

    def items(self) -> Iterator[tuple[str, T]]:
        """Содержимое таблицы.

        Именно снимок, а не ленивый курсор: курсор, живущий через `yield`,
        держит открытым читателя БД всё время, пока вызывающая сторона
        обрабатывает очередной элемент, — а брошенный на полпути генератор
        оставляет его открытым навсегда.
        """
        yield from self.items_snapshot()

    async def async_items(self) -> AsyncIterator[tuple[str, T]]:
        for key, value in await to_thread(self.items_snapshot):
            yield key, value

    def connect(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self.exit_stack.enter_context(LSM(str(self.path)))

    def reset(self) -> None:
        """Очистить таблицу целиком: закрыть, удалить файл, переоткрыть пустой.

        missing_ok: сброс вызывают в том числе на уже пустом состоянии
        (повторный POST /reset, сброс сразу после установки), и отсутствие
        файла здесь — штатный случай, а не ошибка.
        """
        with self._lock:
            self.close()
            self.path.unlink(missing_ok=True)
            self.exit_stack = ExitStack()
            self.lsm = self.connect()


class TableDescriptor[T]:
    def __init__(
        self,
        encoder: Callable[[T], bytes],
        decoder: Callable[[str, bytes], T],
    ) -> None:
        self.encoder = encoder
        self.decoder = decoder

    def __set_name__(self, owner: type["LSMCollection"], name: str) -> None:
        self.name = name
        owner.__tables__.append(name)

    def __get__(
        self, instance: Optional["LSMCollection"], owner: type["LSMCollection"]
    ) -> LSMTable[T]:
        if instance is None:
            raise NotImplementedError()
        if (table := instance.cached_tables.get(self.name)) is None:
            from src.utils import get_config

            root = get_config().lsm_root
            instance.cached_tables[self.name] = table = LSMTable(
                path=Path(root) / f"{self.name}.lsm",
                encoder=self.encoder,
                decoder=self.decoder,
            )
        return table


class LSMCollection(AbstractContextManager):
    __tables__ = []

    indexed = TableDescriptor[FileMetadata](
        encoder=_encode_file_meta,
        decoder=_decode_file_meta,
    )

    def __init__(self) -> None:
        self.cached_tables: dict[str, LSMTable] = {}

    def __enter__(self) -> Self:
        for name in self.__tables__:
            getattr(self, name)  # просто вызываем, чтобы инициализировать
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ):
        for table in self.cached_tables.values():
            table.close()
