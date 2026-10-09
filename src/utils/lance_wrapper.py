import asyncio
import concurrent
import concurrent.futures
import math
import threading
from datetime import timedelta
from functools import cached_property
from typing import Any, NamedTuple

import lancedb as ld
import pyarrow as pa
from lancedb.index import FTS

from src.common.fts import FtsConfig

from .logging import get_logger

MB = 1024 * 1024

# Сколько путей класть в один фильтр удаления, чтобы не делать коммит на файл.
DELETE_BATCH = 100

# Ниже этого объёма IVF-PQ индекс не строим: на маленькой таблице полный
# перебор всё равно быстрее, а KMeans на нескольких векторах просто падает
# («cannot train N centroids with M vectors»).
MIN_ROWS_FOR_VECTOR_INDEX = 256

# Во сколько раз должна измениться таблица, чтобы переобучить IVF-PQ.
# `num_partitions` считается от числа строк: на выросшей таблице прежний
# индекс вырождается, и каждая проба читает больше векторов. Порог
# двусторонний — после массового удаления партиции набиты мёртвыми PQ-кодами.
VECTOR_INDEX_REBUILD_FACTOR = 4

lg = get_logger(__name__)

# Сколько держать прежние версии таблицы после компакции. Не ноль: поиск,
# начатый до компакции, дочитывает файлы прежней версии. Без явного значения
# LanceDB держит их 7 дней, и всё это время место под переписанные фрагменты
# не освобождается.
COMPACT_KEEP_VERSIONS = timedelta(hours=1)


# Сколько значений вектора приходится на один подвектор PQ. 8 — обычный
# компромисс между степенью сжатия и потерей точности.
PQ_TARGET_SUB_DIM = 8


def new_lance_session() -> ld.Session:
    """Общий на процесс бюджет кэшей LanceDB."""
    return ld.Session(
        index_cache_size_bytes=MB * 25,
        metadata_cache_size_bytes=MB * 50,
    )


def sql_quote(value: str) -> str:
    """Экранировать одинарные кавычки в значении для фильтра LanceDB."""
    return value.replace("'", "''")


def pq_num_sub_vectors(embed_dim: int, target_sub_dim: int = PQ_TARGET_SUB_DIM) -> int:
    """Число подвекторов IVF-PQ, нацело делящее размерность.

    `Config.embed_dim` штатно возвращает 312, 384 и 1024, поэтому константой
    его задавать нельзя: на неделящейся размерности `create_index` падает,
    а поиск молча уходит в полный перебор.
    """
    divisors = [n for n in range(1, embed_dim + 1) if embed_dim % n == 0]
    return min(divisors, key=lambda n: abs(embed_dim / n - target_sub_dim))


class LanceWrapper:
    """Одна таблица LanceDB: её схема, её индексы, её хендлы.

    Локи и кэши — свои у экземпляра (см. `LanceTables`).
    """

    def __init__(
        self,
        ld_db_path: str,
        embed_dim: int,
        table_name: str = "files",
        session: "ld.Session | None" = None,
        fts: FtsConfig | None = None,
    ) -> None:
        self.ld_db_path = ld_db_path
        self.table_name = table_name
        self.embed_dim = embed_dim
        self.fts = fts or FtsConfig()
        # Сессия задаёт бюджет кэшей LanceDB и делится между таблицами:
        # своя на каждую — это лишние десятки мегабайт RSS.
        self._session = session
        # Свой пул на экземпляр: через него идёт создание таблицы с FTS,
        # и общий на процесс сериализовал бы таблицы между собой.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            1, thread_name_prefix=f"lance_{table_name}"
        )
        self._fts_created = False
        self._has_table = False
        self._vector_created = False
        # Сколько строк было в таблице, когда обучался вектор-индекс.
        # None — индекс либо не строился, либо достался от прошлого запуска
        # процесса (LanceDB не сообщает, на скольких строках он обучен).
        self._vector_rows: int | None = None
        self.vector_index_error: str | None = None
        # Отсутствие модуля lance пишется в журнал один раз, а не каждый цикл.
        self._lance_missing_logged = False
        self._table = None
        self._table_lock = threading.RLock()
        self._create_lock = threading.RLock()
        # Запись и удаление идут через отдельное async-соединение, а поиск —
        # через кэшированный синхронный хендл. Таблицы Lance версионированы:
        # хендл держит снимок и без checkout_latest() не видит чужих коммитов.
        self._stale = False

    @property
    def fts_created(self) -> bool:
        if self._fts_created:
            return True
        if not self.has_table:
            return False
        for i in self.lancedb.open_table(self.table_name).list_indices():
            if i.index_type == "FTS":
                self._fts_created = True
                return True
        return False

    @property
    def vector_created(self) -> bool:
        if self._vector_created:
            return True
        # Без has_table open_table бросает на ещё не созданной таблице, а это
        # свойство читают с путей, которые таблицу создавать не должны.
        if not self.has_table:
            return False
        for i in self.lancedb.open_table(self.table_name).list_indices():
            if i.index_type == "IvfPq":
                self._vector_created = True
                return True
        return False

    @property
    def has_table(self) -> bool:
        if self._has_table:
            return True
        if self.table_name in self.lancedb.table_names():
            self._has_table = True
            return True
        return False

    def _vector_index_outdated(self, rows: int) -> bool:
        """Пора ли переобучать вектор-индекс под текущий размер таблицы."""
        if not self.vector_created:
            return True
        if self._vector_rows is None:
            # Индекс достался от прошлого запуска, размер обучающей выборки
            # LanceDB не хранит. Берём текущий за точку отсчёта, иначе демон
            # переобучал бы индекс на каждом старте — это O(корпус).
            self._vector_rows = rows
            return False
        if self._vector_rows < MIN_ROWS_FOR_VECTOR_INDEX:
            return True
        ratio = rows / self._vector_rows
        return (
            ratio >= VECTOR_INDEX_REBUILD_FACTOR
            or ratio <= 1 / VECTOR_INDEX_REBUILD_FACTOR
        )

    def create_vector_index(self, force: bool = False):
        """Построить или переобучить IVF-PQ, подобрав партиции под объём данных.

        Звать можно на каждом обновлении — метод сам решает, надо ли работать:
        переобучает, когда таблица выросла или сжалась в
        VECTOR_INDEX_REBUILD_FACTOR раз. `force=True` — переобучить сразу,
        после массового удаления: таблица могла сжаться меньше, чем на порог,
        а партиции обучены на прежнем корпусе.
        """
        table = self.table
        rows = table.count_rows()
        if rows < MIN_ROWS_FOR_VECTOR_INDEX:
            lg.info(
                "skip vector index: %s rows < %s, полный перебор дешевле; "
                "индекс построим, когда данных станет больше",
                rows,
                MIN_ROWS_FOR_VECTOR_INDEX,
            )
            return
        if not force and not self._vector_index_outdated(rows):
            return
        # KMeans не может обучить больше центроидов, чем есть векторов.
        num_partitions = max(1, min(256, int(math.sqrt(rows))))
        num_sub_vectors = pq_num_sub_vectors(self.embed_dim)
        trained_on = self._vector_rows
        try:
            # replace=True — дефолт: старый индекс заменяется, удалять не надо.
            table.create_index(
                vector_column_name="vector",
                num_partitions=num_partitions,
                num_sub_vectors=num_sub_vectors,
                metric="cosine",
            )
        except Exception as exc:
            # Не гасим в warning: без индекса поиск деградирует до полного
            # перебора, и об этом надо знать, а не находить случайно в логах.
            self.vector_index_error = str(exc)
            lg.error(
                "failed to build vector index "
                "(rows=%s, num_partitions=%s, num_sub_vectors=%s, dim=%s): %s",
                rows,
                num_partitions,
                num_sub_vectors,
                self.embed_dim,
                exc,
                exc_info=True,
            )
            return
        self.vector_index_error = None
        self._vector_created = True
        self._vector_rows = rows
        self._mark_stale()
        lg.info(
            "vector index built on %s: rows=%s (было %s), "
            "num_partitions=%s, num_sub_vectors=%s",
            self.table_name,
            rows,
            trained_on,
            num_partitions,
            num_sub_vectors,
        )

    def _build_fts(self, replace: bool) -> None:
        """Построить FTS-индекс по параметрам `self.fts`. Без локов.

        Хендл открывается напрямую, а не через self.table: _build_fts зовут из
        create_table под _create_lock, а self.table берёт _table_lock и сама
        зовёт create_table. Встречный порядок захвата двух локов даёт
        AB-BA-дедлок с optimize_indices и create_vector_index.
        """
        lg.info("create fts index on %s: %s", self.table_name, self.fts.model_dump())
        self.lancedb.open_table(self.table_name).create_index(
            "text", config=FTS(**self.fts.model_dump()), replace=replace
        )

    def create_fts(self, force: bool = False):
        if self.fts_created and not force:
            return
        self._build_fts(replace=force)
        # Поиск идёт через закэшированный хендл: без пометки он остался бы на
        # снимке со старым индексом. Под _create_lock этого делать нельзя
        # (_mark_stale берёт _table_lock), поэтому create_table зовёт _build_fts.
        self._mark_stale()

    def fts_config_diff(self) -> dict[str, tuple[Any, Any]] | None:
        """Чем параметры готового FTS-индекса расходятся с `self.fts`.

        None — нет таблицы или индекса, {} — совпадают, иначе
        {параметр: (в индексе, в конфиге)}. Индекс без `index_details` или без
        параметра считается расходящимся: его построила старая версия lancedb.

        Не через self.table: обращение к нему создало бы таблицу.
        Параметры берутся из lancedb, а не из модуля lance: его может не
        оказаться в сборке (см. optimize_indices).
        """
        if not self.has_table:
            return None
        for idx in self.lancedb.open_table(self.table_name).list_indices():
            if idx.index_type == "FTS" and list(idx.columns) == ["text"]:
                details = idx.index_details or {}
                return {
                    key: (details.get(key), want)
                    for key, want in self.fts.model_dump().items()
                    if key not in details or details[key] != want
                }
        return None

    def optimize_indices(self):
        """Дообновить существующие индексы новыми данными. Дёшево.

        Строки, дописанные после построения индекса, в него не попадают, и
        LanceDB досматривает их полным перебором — стоимость поиска растёт
        вместе с объёмом, накопленным с последнего обновления. Эта операция
        приписывает новые данные к уже обученным партициям, не переобучая
        индекс и не переписывая данные (новая версия с дельтой индекса),
        поэтому её можно звать часто.

        Нужен модуль lance (pylance): в lancedb отдельной операции нет, а его
        optimize() ещё и компактирует таблицу. Без lance индексы догоняет
        только compact().
        """
        table = self.table
        try:
            table.to_lance().optimize.optimize_indices()
        except ImportError as exc:
            if not self._lance_missing_logged:
                self._lance_missing_logged = True
                lg.warning(
                    "нет модуля lance (%s): новые строки %s попадут в индексы "
                    "только при компакции, до неё поиск досматривает их перебором",
                    exc,
                    self.table_name,
                )
            return
        except Exception:
            # индексов может ещё не быть — это не повод шуметь на каждый цикл
            lg.debug("optimize_indices пропущен", exc_info=True)
            return
        self._mark_stale()

    def compact(self):
        """Полная компакция: склейка фрагментов и удаление старых версий.

        Дорого и по CPU, и по памяти, поэтому вызывать только в простое.
        Частые мелкие вставки оставляют тысячи фрагментов и версий; без склейки
        они копятся монотонно. Удаляются версии старше COMPACT_KEEP_VERSIONS,
        более свежие уберёт следующая компакция.
        """
        table = self.table
        before = len(table.list_versions())
        table.optimize(cleanup_older_than=COMPACT_KEEP_VERSIONS)
        self._mark_stale()
        lg.info(
            "table compacted: версий было %s, стало %s",
            before,
            len(table.list_versions()),
        )

    def create_table(self):
        # Под локом: синхронный и асинхронный пути обращаются сюда
        # параллельно и иначе создают таблицу и FTS-индекс по два раза.
        with self._create_lock:
            if self.has_table:
                return
            from src.common import chunk_with_meta_schema

            lg.info(f"create table {self.table_name=}")
            self.lancedb.create_table(
                self.table_name,
                exist_ok=True,
                schema=chunk_with_meta_schema.append(
                    pa.field("vector", pa.list_(pa.float32(), self.embed_dim))
                ),
            )
            if not self.fts_created:
                self._build_fts(replace=False)

    @cached_property
    def lance_session(self):
        return self._session or new_lance_session()

    @cached_property
    def lancedb(self):
        return ld.connect(
            self.ld_db_path,
            session=self.lance_session,
        )

    async def lancedb_async(self):
        return await ld.connect_async(
            self.ld_db_path,
            session=self.lance_session,
        )

    async def table_async(self):
        """Свежий async-хендл таблицы; таблицы нет — создать её с FTS-индексом."""
        db = await self.lancedb_async()
        loop = asyncio.get_event_loop()
        if not self.has_table:
            await loop.run_in_executor(self._executor, self.create_table)
        table = await db.open_table(self.table_name)

        return table

    @property
    def table(self):
        """Хендл таблицы, открытый один раз на процесс.

        Таблица не переоткрывается на каждое обращение, но после чужих
        коммитов хендл доводится до актуальной версии — иначе поиск отвечает
        по устаревшему снимку и не видит ни свежих файлов, ни удалённых.
        """
        with self._table_lock:
            if self._table is None:
                self.create_table()
                self._table = self.lancedb.open_table(self.table_name)
            elif self._stale:
                self._table.checkout_latest()
            self._stale = False
            return self._table

    def _mark_stale(self):
        """Пометить, что хранилище изменилось мимо синхронного хендла."""
        with self._table_lock:
            self._stale = True

    async def add_batch(self, data):
        """Добавить батч и пометить синхронный хендл устаревшим."""
        table = await self.table_async()
        await table.add(data)
        self._mark_stale()

    async def delete_where(self, predicate: str):
        """Удалить строки по фильтру и пометить синхронный хендл устаревшим."""
        table = await self.table_async()
        await table.delete(predicate)
        self._mark_stale()

    async def count_rows(self) -> int:
        table = await self.table_async()
        return await table.count_rows()

    def count_matching(self, predicate: str) -> int:
        """Сколько строк подходит под фильтр. 0 — если таблицы ещё нет."""
        if not self.has_table:
            return 0
        return self.table.count_rows(predicate)

    def add_sync(self, data) -> None:
        """Синхронная запись — для путей, которые и так идут в потоке."""
        self.table.add(data)
        self._mark_stale()

    def delete_sync(self, predicate: str) -> None:
        self.table.delete(predicate)
        self._mark_stale()

    def drop(self) -> None:
        """Удалить таблицу целиком и забыть её индексы.

        Дешевле удаления по фильтру: ни deletion vectors, ни компакции следом.
        """
        with self._table_lock:
            if self.table_name in self.lancedb.table_names():
                lg.info("drop table %s", self.table_name)
                self.lancedb.drop_table(self.table_name)
            self.invalidate_table_cache()

    def invalidate_table_cache(self):
        """Сбросить кеш хендла (после пересоздания/удаления таблицы)."""
        with self._table_lock:
            self._table = None
            self._has_table = False
            self._fts_created = False
            self._vector_created = False
            # Обязательно вместе с _vector_created, иначе «обучен на N строк»
            # останется от прежней таблицы и переобучение не наступит.
            self._vector_rows = None
            self._stale = False


class LanceTables(NamedTuple):
    """Таблицы индекса; `for store in cfg.lance` обходит их все."""

    files: LanceWrapper
