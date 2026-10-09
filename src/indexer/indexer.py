import asyncio
import concurrent.futures
import inspect
import os
import shutil
import time
import typing
from collections.abc import AsyncGenerator, AsyncIterable, Iterable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    suppress,
)
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lancedb.query import MatchQuery
from lancedb.rerankers import RRFReranker

from src.common import (
    CARD_IDX,
    CARD_KIND,
    ChunkWithMeta,
    FileMetadata,
    IndexingStatus,
    document_card_text,
)
from src.malloc_tuning import safe_malloc_trim
from src.parsers import (
    async_parse_files,
    ocr,
)
from src.utils import (
    async_embed_documents_into_batches,
    async_merge_batches,
    get_logger,
    wrap_exception_logger,
    wrap_iter_to_async,
)
from src.utils.lance_wrapper import DELETE_BATCH, LanceWrapper, sql_quote
from src.utils.lsm_adapter import LSMCollection, check_meta_changed
from src.utils.lsm_adapter import to_thread as lsm_to_thread
from src.utils.lsm_adapter import wait_idle as lsm_wait_idle
from src.utils.thread_priority import background_pool_initializer

from .name_codes import NameIndex, pinned_rows, query_codes
from .scanners import FilesScanner

if TYPE_CHECKING:
    import polars as pl

if typing.TYPE_CHECKING:
    from src.config import Config

lg = get_logger(__name__, slice=-1)


GIB = 1024**3

# Через сколько записанных батчей перепроверять свободное место во время
# прохода. disk_usage — системный вызов, на каждый батч он лишний.
_DISK_CHECK_EVERY_BATCHES = 32

# Отдельный поток под перестройку индексов: она долгая и в общем пуле заняла
# бы воркеры остальных вызовов asyncio.to_thread.
_INDEX_BUILD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    1,
    thread_name_prefix="index_build",
    # Перестройка индексов — фоновая работа: уступает CPU пользователю.
    initializer=background_pool_initializer,
)

# Свой пул для запросов /search к LanceDB. В дефолтный (asyncio.to_thread) идут
# и долгие задачи — сверка путей с диском, постановка наблюдения, /read_file с
# конвертацией до 120 с, — и при их наложении запрос ждал бы в очереди дольше
# таймаута агента. Четыре потока — как у дефолтного пула (asyncio_num_workers).
# Без background_pool_initializer: поиск интерактивный.
_SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="search")


# Маркеры ошибки «полнотекстовый индекс ещё не создан». Формулировка меняется
# между версиями LanceDB, и одна фраза после обновления превратила бы штатный
# откат поиска в 500. Внутри кортежа должны совпасть все подстроки.
_MISSING_FTS_MARKERS: tuple[tuple[str, ...], ...] = (
    ("full text search", "index"),
    ("inverted index",),
    ("fts index",),
    ("index not found",),
    ("no index with name",),
)


def _is_missing_fts_index(exc: BaseException) -> bool:
    """Ошибка «полнотекстовый индекс ещё не создан», а не настоящий сбой."""
    msg = str(exc).lower()
    return any(all(part in msg for part in markers) for markers in _MISSING_FTS_MARKERS)


# Пул чанков на канал поиска (вектор и FTS гибрида по отдельности, чистый
# вектор, чистый текст), из которого собирается выдача limit ДОКУМЕНТОВ.
# Пул в limit чанков дал бы меньше документов: длинный документ и копии файла
# занимают места соседей. 50 при limit=10 — лучшее по замеру (100 не лучше);
# кратность — запас документов на больших limit, потолок — чтобы limit=1000
# не тянул тысячи строк на канал.
SEARCH_POOL_MIN = 50
SEARCH_POOL_PER_DOC = 5
SEARCH_POOL_MAX = 500

# Сколько разделов IVF-индекса просматривает векторный поиск. Одно значение
# для чистого вектора и для векторного канала гибрида: иначе гибрид находил бы
# по смыслу меньше, чем query_type=vector на том же индексе.
VECTOR_NPROBES = 64

# Во сколько раз больше кандидатов, чем просили, пересчитывается по полным
# векторам. Без refine порядок задают грубые PQ-коды, и пул заметно расходится
# с точным поиском; число разделов этого не лечит, а refine 10 при nprobes 64
# снимает почти всю потерю ценой ≈3 мс на запрос.
VECTOR_REFINE_FACTOR = 10


def search_pool(limit: int) -> int:
    """Чанков на канал поиска при выдаче limit документов."""
    return min(SEARCH_POOL_MAX, max(SEARCH_POOL_MIN, limit * SEARCH_POOL_PER_DOC))


async def _await_stopped(
    task: asyncio.Task, what: str, timeout: float | None = None
) -> None:
    """Дождаться остановленной задачи, не поднимая её собственную ошибку.

    Задача могла упасть ещё до отмены, и await поднял бы её исключение
    заново: остановка обрывалась бы на полпути, а /config, /deactivate и
    /reset отвечали бы 500 до рестарта. Стек упавшего прохода уже записал
    scan_all, здесь хватает строки.

    Отмену самой ожидающей задачи пробрасываем: иначе отмена, пришедшая во
    время ожидания, терялась бы.
    """
    current = asyncio.current_task()
    cancelling = current.cancelling() if current else 0
    try:
        await asyncio.wait_for(task, timeout)
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > cancelling:
            raise
    except Exception as exc:
        lg.warning("%s: ошибка при остановке — %r", what, exc)


class Index(AbstractAsyncContextManager):
    def __init__(self, cfg: "Config") -> None:
        from .utils import UniqueCounter

        self.cfg = cfg
        self.batch_size = cfg.embed_model_cfg.batch_size
        self.exit_stack = AsyncExitStack()
        self.preindex = False
        self.counter = UniqueCounter()
        self.preindex_cnt = 0
        self.queued_files = 0
        self.main_index = False
        # Периодический полный проход — страховка поверх watcher'а.
        self.rescan_task: asyncio.Task | None = None
        # Здесь, а не в lifespan(): stop_index() до старта иначе падает с
        # AttributeError.
        self.task: asyncio.Task | None = None
        # Включена ли индексация (БТ Библиотеки §5.1/§5.2). По self.task не
        # понять: задача завершается после каждого прохода, и «ждём watcher'а»
        # не отличить от «остановлено».
        self.active: bool = cfg.autostart_indexing
        # Ровно один проход на процесс. Два потребителя одной очереди
        # заклинивают друг друга: iter(endless=False) проверяет пустоту до
        # блокирующего get(), и опоздавший ждёт элемент вечно.
        self._scan_lock = asyncio.Lock()
        # Чтобы несколько батчей от watcher'а не начали строить индекс разом
        self._index_build_lock = asyncio.Lock()
        # Проход, запрошенный во время идущего: его нужно доиграть, а не потерять.
        self._rescan_requested = False
        # Имена таблиц, которым нужен пересбор индексов.
        self._dirty_stores: set[str] = set()
        self._index_task: asyncio.Task | None = None
        # Сколько файлов сейчас разбирает watcher (для /status).
        self.watch_pending = 0
        # Итоги последнего завершённого прохода: без них /status обнулял бы
        # files_indexed и progress вместе с main_index, и клиент, дождавшийся
        # конца индексации, читал бы «проиндексировано 0».
        self.last_pass_total = 0
        self.last_pass_indexed = 0
        # Когда последний раз выполнялась компакция — по таблице отдельно
        self._last_optimize: dict[str, float] = {cfg.ld_files_table: 0.0}
        # Имена документов для поиска файла по коду из имени — по таблице.
        self._name_indexes: dict[str, NameIndex] = {}
        # Карточки сверены с индексом (migrate_document_cards): раз на процесс.
        self._cards_migrated = False
        self.lsm_collection = LSMCollection()
        self.files_scanner = FilesScanner(
            queue_path=Path(cfg.lsm_root) / "files",
            lsm_checker=self.lsm_collection.indexed,
            reset=True,
        )

    async def __aenter__(self):
        self.exit_stack.enter_context(self.lsm_collection)
        await self.exit_stack.enter_async_context(self.files_scanner)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback,
    ) -> bool | None:
        # Фоновые задачи могли быть подняты из activate() или POST /config, а
        # не из lifespan(): без этого они переживают закрытие индекса и потом
        # ходят в уже закрытые LSM и LanceDB.
        await self.stop_periodic_rescan()
        await self._stop_index_refresh()
        try:
            await asyncio.wait_for(self.exit_stack.aclose(), 5)
        except TimeoutError:
            lg.warning("timeout on stopping indexer")

    @asynccontextmanager
    async def lifespan(self):
        # Создаём таблицу и FTS-индекс заранее: иначе первый же поисковый
        # запрос платит за инициализацию LanceDB и упирается в таймаут.
        store = self.files_store
        try:
            await asyncio.to_thread(lambda: store.table)
        except Exception:
            lg.exception("failed to pre-initialize lance table %s", store.table_name)
        async with self:
            # До первого прохода: новые строки должны дописываться уже в индекс
            # с параметрами из конфига.
            try:
                await self.migrate_fts_config()
            except Exception:
                lg.exception("сверка параметров FTS-индексов не удалась")
            if self.active:
                self.task = asyncio.create_task(self.scan_all())
                await self.start_periodic_rescan()
            else:
                # Отладочный режим (autostart_indexing=false): демон поднялся,
                # но пользовательские файлы не трогает до явной активации.
                # Поиск по уже собранному индексу при этом работает.
                lg.info(
                    "автозапуск индексации выключен (autostart_indexing=false): "
                    "ждём POST /api/v1/activate"
                )
            try:
                yield
            finally:
                await self.stop_periodic_rescan()
                if self.task is not None:
                    self.task.cancel()
                    await _await_stopped(self.task, "проход индексации", timeout=5)

    async def stop_index(self):
        if self.task is not None:
            self.task.cancel()
            # Воркер OCR — отдельный процесс: отмена задачи его не останавливает.
            ocr.kill_running()
            await _await_stopped(self.task, "проход индексации")
            lg.info("indexing stopped")
        # Проход идёт и в периодическом проходе. Гасим его до clear(): живой
        # проход обходит прежний список папок и дописал бы его в пересозданную
        # очередь. Поднимает его обратно start_reindex().
        await self.stop_periodic_rescan()
        lg.info("clear index queue")
        await self.files_scanner.clear()
        lg.info("stop indexing")
        # Отмена задачи не отзывает работу, уже отданную в LSM-поток. Ждём её:
        # следом POST /config перезапускает индексацию в ту же БД.
        await lsm_wait_idle()

    async def start_reindex(self):
        """Запустить проход. Идемпотентно: вторую задачу не плодит.

        Иначе ссылка на прежнюю задачу терялась бы, и stop_index() отменил бы
        только последнюю. Нужно для /activate, который приходит когда угодно.
        """
        # stop_index() гасит и периодический проход — поднимаем его обратно.
        await self.start_periodic_rescan()
        if self.task is not None and not self.task.done():
            lg.debug("индексация уже запущена, повторный старт пропущен")
            return
        lg.debug("restart indexing")
        self.task = asyncio.create_task(self.scan_all())

    async def _stop_index_refresh(self, clear_dirty: bool = False) -> None:
        """Погасить отложенную пересборку индексов (`_refresh_index_loop`).

        Без этого задача переживает и остановку, и сброс: после дропа таблиц
        она раз в `index_refresh_delay` либо падает на удалённой таблице, либо
        создаёт её обратно.

        `clear_dirty` — только для сброса. При остановке пометки сохраняем:
        пересобрать индексы всё ещё нужно, и активация вернёт цикл.
        """
        if self._index_task is not None and not self._index_task.done():
            self._index_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._index_task
        self._index_task = None
        if clear_dirty:
            self._dirty_stores.clear()

    async def activate(self) -> bool:
        """Включить индексацию — БТ §5.1. True, если состояние изменилось.

        Идемпотентно: повторный вызов на активном индексе ничего не
        перезапускает, чтобы двойное нажатие кнопки не оборвало идущий проход.
        """
        was_active = self.active
        self.active = True
        await self.start_reindex()
        # Пересборка индексов, отложенная на момент деактивации.
        if self._dirty_stores and (
            self._index_task is None or self._index_task.done()
        ):
            self._index_task = asyncio.create_task(self._refresh_index_loop())
        await self.start_periodic_rescan()
        if not was_active:
            lg.info("индексация активирована")
        return not was_active

    async def deactivate(self) -> bool:
        """Остановить индексацию, СОХРАНИВ индексы — БТ §5.2.

        Освобождает ресурсы (задачи, очередь, поток LSM), но не трогает ни
        LanceDB, ни состояние LSM: поиск по уже собранному продолжает
        работать, а следующая активация продолжит с учётом изменений, а не
        начнёт с нуля.
        """
        was_active = self.active
        self.active = False
        await self.stop_index()
        await self._stop_index_refresh()
        await self.stop_periodic_rescan()
        # Счётчики обнуляем явно: после отмены прохода в них остаётся хвост
        # очереди, а /status считает is_indexing по `pending > 0` — без этого
        # остановленный индексатор вечно рапортует «индексация идёт».
        self.counter.reset()
        self.watch_pending = 0
        # Отложенный проход на остановленном индексе не нужен.
        self._rescan_requested = False
        if was_active:
            lg.info("индексация деактивирована, индексы сохранены")
        return was_active

    async def reset_index(self) -> None:
        """Полная остановка и сброс — БТ §5.3.

        Возвращает индексатор к состоянию «как после установки»: данных нет,
        очереди пусты, состояние LSM чистое. Пользовательский конфиг снимает
        вызывающая сторона: список папок — не забота индексатора.

        После сброса индексация остаётся ВЫКЛЮЧЕННОЙ. Иначе сброс тут же
        запускал бы полный обход диска, а по БТ §5.3 он применяется в том
        числе при смене пользователя — то есть ровно тогда, когда лезть в
        файлы до явного согласия нельзя.
        """
        self.active = False
        await self.stop_index()
        # До дропа таблиц: иначе отложенная пересборка проснётся уже без них.
        await self._stop_index_refresh(clear_dirty=True)
        # И периодический проход: проснувшись после дропа, он завёл бы
        # таблицы обратно.
        await self.stop_periodic_rescan()
        # Кэш OCR — распознанный текст сканов, тоже данные индекса. Чистится
        # после stop_index: убитый воркер к этому моменту уже сохранил своё.
        await asyncio.to_thread(ocr.clear_cache, self.cfg.lsm_root)
        # Сначала LSM, потом таблицы. Обрыв посередине оставит «LSM пуст,
        # таблицы полны» — следующий проход переиндексирует всё и вычистит
        # старые чанки. Обратный порядок дал бы «таблицы пусты, LSM полон»:
        # проход сочтёт всё сделанным, и индекс не восстановится.
        await lsm_to_thread(self.lsm_collection.indexed.reset)
        # Дроп таблиц, а не удаление по фильтру: не остаётся ни deletion
        # vectors, ни партиций IVF-PQ с мёртвыми PQ-кодами.
        for store in self.cfg.lance:
            await asyncio.to_thread(store.drop)
        self.counter.reset()
        self.watch_pending = 0
        self._rescan_requested = False
        self.last_pass_total = 0
        self.last_pass_indexed = 0
        lg.info("индекс сброшен: таблицы удалены, состояние LSM очищено")

    @property
    def files_store(self) -> LanceWrapper:
        return self.cfg.lance.files

    async def _preindex_files(self) -> int:
        """Собрать очередь файлов. Возвращает её размер."""
        self.preindex = True
        try:
            lg.info("collecting files started")
            await self.files_scanner.scan()
        finally:
            self.queued_files = await self.files_scanner.total()
            self.preindex = False
        lg.info("collected %s files", self.queued_files)
        return self.queued_files

    async def delete_prefix(self, prefix: str) -> None:
        """Снять с индекса всё, что лежит под этим префиксом пути.

        `starts_with`, а не `LIKE`: в LIKE `_` и `%` — подстановочные знаки,
        и удаление `/home/u/a_b/` задело бы `/home/u/aXb/`.
        """
        store = self.files_store
        await self.lsm_collection.indexed.async_delete_prefix(prefix=prefix)
        lg.info("delete %r from %s", prefix, store.table_name)
        await store.delete_where(f"starts_with(path, '{sql_quote(prefix)}')")

    async def migrate_fts_config(self) -> list[str]:
        """Пересобрать FTS-индексы, построенные не с параметрами `fts_cfg`.

        Только FTS: `create_index` ещё компактирует таблицу и дообучает вектор,
        а миграции это не нужно. Зовётся до первого прохода, поэтому записи
        параллельно нет; при сбое остаётся рабочий старый индекс. Возвращает
        имена пересобранных таблиц.
        """
        rebuilt = []
        loop = asyncio.get_running_loop()
        for store in self.cfg.lance:
            try:
                diff = await asyncio.to_thread(store.fts_config_diff)
                if not diff:
                    continue
                lg.info(
                    "FTS-индекс %s построен с другими параметрами "
                    "(в индексе, в конфиге): %s — пересобираю",
                    store.table_name,
                    diff,
                )
                started = time.monotonic()
                async with self._index_build_lock:
                    await loop.run_in_executor(
                        _INDEX_BUILD_EXECUTOR, partial(store.create_fts, force=True)
                    )
                rebuilt.append(store.table_name)
                if left := await asyncio.to_thread(store.fts_config_diff):
                    lg.error(
                        "FTS-индекс %s после пересборки расходится с конфигом: %s — "
                        "пересборка будет на каждом старте",
                        store.table_name,
                        left,
                    )
                else:
                    lg.info(
                        "FTS-индекс %s пересобран за %.1f с",
                        store.table_name,
                        time.monotonic() - started,
                    )
            except Exception:
                lg.exception("пересборка FTS-индекса %s не удалась", store.table_name)
        return rebuilt

    async def migrate_document_cards(self) -> int:
        """Дописать карточки документам, проиндексированным до их появления.

        Карточку пишет разбор файла (document_chunks), а неизменённый файл
        проход заново не разбирает (решает size/mtime в LSM), и без миграции
        старый индекс остался бы без карточек. Переразбирать не нужно:
        карточка собирается из meta первого чанка, считаем только её вектор.
        Признак «мигрировать нечего» — карточка есть у каждого документа,
        поэтому прерванная миграция доделается следующим проходом.

        Зовётся из прохода до его записи. Файл, переписанный watcher'ом между
        чтением и записью миграции, получит вторую карточку — безвредно:
        выдача собирает строки по пути, а следующее изменение снесёт обе.
        Векторы карточек считаются минутами, и watcher успевает снять с
        индекса удалённый за это время файл. Его карточку миграция убирает
        сама: сверка удалённых ходит по ключам LSM и такую строку не найдёт.
        """
        store = self.files_store
        if not store.has_table:
            return 0

        def _pending() -> list[dict[str, Any]]:
            table = store.table
            carded = set(
                table.search()
                .where(f"kind = '{CARD_KIND}'")
                .select(["path"])
                .to_arrow()
                .column("path")
                .to_pylist()
            )
            first = table.search().where("idx = 0").select(["path"]).to_arrow()
            if all(p in carded for p in first.column("path").to_pylist()):
                return []
            # meta — только когда есть что дописывать: чтение всех meta в
            # Python дорого, а на каждом проходе после миграции оно не нужно.
            first = table.search().where("idx = 0").select(["path", "meta"]).to_arrow()
            rows = []
            for path, meta in zip(
                first.column("path").to_pylist(),
                first.column("meta").to_pylist(),
                strict=True,
            ):
                if path in carded or not (card := document_card_text(meta)):
                    continue
                carded.add(path)  # первый чанк пути мог записаться дважды
                rows.append(
                    {
                        "idx": CARD_IDX,
                        "text": card,
                        "kind": CARD_KIND,
                        "meta": meta,
                        "path": path,
                    }
                )
            return rows

        rows = await asyncio.to_thread(_pending)
        if not rows:
            return 0
        lg.info("миграция: дописываю карточки %s документам", len(rows))
        started = time.monotonic()

        async def _cards():
            for row in rows:
                yield row

        def _quoted(paths) -> str:
            return ", ".join(f"'{sql_quote(p)}'" for p in paths)

        def _orphans(paths: set[str]) -> list[str]:
            """Пути без чанков: файл сняли с индекса, пока считались векторы."""
            alive = (
                store.table.search()
                .where(f"idx = 0 AND path IN ({_quoted(paths)})")
                .select(["path"])
                .to_arrow()
                .column("path")
                .to_pylist()
            )
            return sorted(paths - set(alive))

        batches = async_embed_documents_into_batches(
            _cards(),
            embedder=partial(
                self.cfg.ov_model.async_encode, task="document", role="index"
            ),
            batch_size=self.cfg.embed_model_cfg.batch_size,
        )
        dropped = 0
        async for batch in async_merge_batches(
            batches, target_size=self.cfg.parquet_record_batch_size
        ):
            await store.add_batch(batch)
            # Проверка после записи, а не до неё: удаление, пришедшее позже,
            # снимет карточку вместе с остальными строками пути.
            paths = set(batch.column("path").to_pylist())
            if orphans := await asyncio.to_thread(_orphans, paths):
                await store.delete_where(
                    f"kind = '{CARD_KIND}' AND path IN ({_quoted(orphans)})"
                )
                dropped += len(orphans)
        # Иначе карточки до следующего прохода живут вне FTS и вектора.
        await self.create_index(store)
        lg.info("миграция: карточки дописаны за %.1f с", time.monotonic() - started)
        return len(rows) - dropped

    async def delete_path(self, path: str) -> None:
        """Убрать файл из индекса и из состояния LSM."""
        await self.delete_paths([path])

    async def delete_paths(self, paths: list[str]) -> None:
        """То же для пачки путей — одним фильтром вместо коммита на файл."""
        for start in range(0, len(paths), DELETE_BATCH):
            batch = paths[start : start + DELETE_BATCH]
            predicate = "path IN ({})".format(
                ", ".join(f"'{sql_quote(p)}'" for p in batch)
            )
            await self.files_store.delete_where(predicate)
            for path in batch:
                await self.lsm_collection.indexed.async_delete(path)

    @staticmethod
    def _filter_stale(
        paths: list[str],
        filter_meta: typing.Callable[[FileMetadata], bool] | None = None,
    ) -> list[str]:
        """Из снимка ключей LSM оставить те, которых больше нет на диске или
        которые не проходят filter_meta (файл вырос сверх max_size).

        Снимок берётся отдельно, на LSM-потоке: тысячи `os.stat` — это тысячи
        syscall'ов, и держать на них блокировку таблицы нельзя.
        """
        stale = []
        for path in paths:
            try:
                st = os.stat(path)
            except (OSError, ValueError):  # как os.path.exists
                stale.append(path)
                continue
            # Обход и watcher такой файл отсеивают до очереди, и прежние
            # строки, кроме сверки, снять некому.
            meta = FileMetadata(
                path=path, size=st.st_size, mtime_seconds=int(st.st_mtime)
            )
            if filter_meta is not None and not filter_meta(meta):
                stale.append(path)
        return stale

    async def reconcile_deleted(self) -> int:
        """Снять с индекса файлы, которых больше нет на диске или которые
        больше не индексируются (выросли сверх max_size).

        Событий watcher'а недостаточно: файл могли удалить при выключенном
        демоне, событие могло потеряться при переполнении очереди, а
        перемещение каталога приходит одним rename. Полный проход умеет только
        добавлять, и без сверки удалённый файл так и находился бы поиском.
        """
        # lsm_to_thread, а не asyncio.to_thread: к lsm_db можно обращаться
        # только из выделенного потока (src/utils/lsm_adapter.py).
        indexed_paths = await lsm_to_thread(self.lsm_collection.indexed.keys_snapshot)
        stale = await asyncio.to_thread(
            self._filter_stale, indexed_paths, self.cfg.file_scan_cfg.filter_meta
        )
        if not stale:
            lg.debug("reconcile: nothing to remove")
            return 0
        await self.delete_paths(stale)
        lg.info("reconciled: removed %s stale paths", len(stale))
        await self.ensure_index()
        return len(stale)

    async def count_to_index(self):
        self.queued_files = await self.files_scanner.total()
        self.preindex_cnt = self.queued_files
        self.counter.reset(self.preindex_cnt)

    def _storage_root(self) -> Path:
        """Путь, на котором реально лежат индексы (LSM + LanceDB).

        Проверять свободное место надо именно здесь, а не в cwd: хранилище
        (`~/.files-search`) может быть на другом разделе, чем рабочая директория
        процесса. Если каталог ещё не создан, поднимаемся к ближайшему
        существующему предку, иначе `shutil.disk_usage` упадёт.
        """
        p = Path(self.cfg.lsm_root).expanduser()
        while not p.exists() and p != p.parent:
            p = p.parent
        return p

    @wrap_exception_logger(lg)
    async def scan_all(self):
        if self._scan_lock.locked():
            # Не ждём в очереди (POST /config сначала зовёт stop_index()), но и
            # не теряем: проход доигрывается на выходе из лока.
            self._rescan_requested = True
            lg.debug("проход индексации уже идёт, пропускаем запуск")
            return
        async with self._scan_lock:
            # Падение прохода не должно съедать отложенный: ошибку запоминаем
            # и поднимаем после доигрывания.
            error: Exception | None = None
            try:
                await self._scan_all()
            except asyncio.CancelledError:
                # Отмена (stop_index из POST /config и /deactivate) — это не
                # сбой: доигрывать нечего и нельзя, иначе отменённая задача
                # продолжит работать.
                raise
            except Exception as exc:
                lg.exception("проход индексации завершился ошибкой")
                error = exc
            # Флаг снимаем ДО прохода: пришедший во время него новый запрос
            # должен поставить его заново, а не потеряться.
            while self._rescan_requested:
                self._rescan_requested = False
                lg.info("доигрываем полный проход, запрошенный во время предыдущего")
                try:
                    await self._scan_all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Второй подряд сбой — дальше доигрывать бессмысленно.
                    lg.exception("доигрывание полного прохода не удалось")
                    break
            if error is not None:
                raise error

    async def _scan_all(self):
        # До проверки места: сверка только освобождает его.
        await self.reconcile_deleted()
        if (free := shutil.disk_usage(self._storage_root()).free) < GIB:
            lg.warning(
                f"free disk space is less then 1GiB ({free / GIB: 0.1f}GiB), refuse indexing"
            )
            return
        # Под замком прохода, а не на старте демона: миграция считает векторы
        # и не должна задерживать готовность HTTP, а запись прохода и так ждёт
        # её конца. Сбой не мешает индексации — доделается следующим проходом.
        if not self._cards_migrated:
            try:
                await self.migrate_document_cards()
                self._cards_migrated = True
            except Exception:
                lg.exception("миграция карточек документов не удалась")
        await self._run_pass(self._preindex_files, self.files_scanner, self.files_store)

    async def _run_pass(self, collect, scanner, store: LanceWrapper) -> None:
        """Один проход: собрать очередь источника и записать её в свою таблицу."""
        lg.info("count total items for %s", store.table_name)
        total = await collect()
        await self.count_to_index()
        if total <= 0:
            lg.info("nothing new for %s", store.table_name)
            return
        lg.info("collected %s items, start indexing into %s", total, store.table_name)
        self.main_index = True
        started = time.monotonic()
        # Генератор закрываем явно: при отмене (POST /config) он висел бы на
        # yield и финализировался после clear(), отправляя task_done() уже в
        # НОВУЮ очередь («task_done() called too many times» или порча
        # unfinished_tasks).
        stream = scanner.iter(endless=False)
        try:
            lg.info("begin indexing")
            # Дальше счётчик вычитает: файлы, оставшиеся в seen, не вычлись бы.
            self.counter.clear_cache()
            # Обновляем индексы и во время прохода: иначе на холодном скане
            # свежие строки до самого конца вне индексов, и поиск идёт перебором.
            await self.ensure_index(store)
            await self.process_stream(stream, store=store)
        finally:
            # CancelledError — BaseException: без него отмена прохода
            # (stop_index) вылетала бы отсюда, и main_index остался бы
            # взведённым навсегда.
            with suppress(Exception, asyncio.CancelledError):
                await stream.aclose()
            # Итоги прохода запоминаем ДО обнуления preindex_cnt.
            self.last_pass_total = self.preindex_cnt
            self.last_pass_indexed = max(
                0, self.preindex_cnt - (self.counter.cnt + self.watch_pending)
            )
            # Флаг снимаем ДО create_index: _maybe_compact выходит по
            # main_index, и изнутри try компакция не наступала бы никогда.
            self.main_index = False
            self.preindex_cnt = 0
        lg.info(
            "indexed %s of %s items into %s за %.1f c",
            self.last_pass_indexed,
            self.last_pass_total,
            store.table_name,
            time.monotonic() - started,
        )
        await self.create_index(store)

    async def start_periodic_rescan(self) -> None:
        """Поднять периодический полный проход, если он ещё не идёт."""
        if self.cfg.full_rescan_interval <= 0:
            lg.info(
                "периодический полный проход выключен (full_rescan_interval=%s)",
                self.cfg.full_rescan_interval,
            )
            return
        if self.rescan_task is not None and not self.rescan_task.done():
            return
        self.rescan_task = asyncio.create_task(self.run_periodic_rescan())

    async def stop_periodic_rescan(self) -> None:
        if self.rescan_task is not None:
            if not self.rescan_task.done():
                self.rescan_task.cancel()
            await _await_stopped(self.rescan_task, "периодический полный проход")
            lg.info("периодический полный проход остановлен")
            self.rescan_task = None

    async def run_periodic_rescan(self) -> None:
        """Полный проход по расписанию — страховка от пропущенных событий ФС.

        События теряются при переполнении очереди (WatchDogAdapter._emit), не
        приходят для каталогов, ставших читаемыми, и оставляют осиротевшие
        пути после перемещений. scan_all внутри зовёт reconcile_deleted и
        заодно снимает с индекса удалённое.

        Запускается только в простое, как компакция (_maybe_compact): полный
        обход домашнего каталога идёт часами, и поверх работающей индексации
        удвоил бы нагрузку.
        """
        interval = self.cfg.full_rescan_interval
        while True:
            await asyncio.sleep(interval)
            if not self.active:
                # БТ §5.2: деактивированный демон новых файлов не обрабатывает.
                lg.debug("индексация деактивирована — периодический проход пропущен")
                continue
            if self.main_index or self.preindex or self.watch_pending:
                lg.debug("индексатор занят — периодический проход отложен")
                continue
            lg.info("периодический полный проход по расписанию")
            try:
                await self.scan_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Задача фоновая и живёт весь срок демона: единичный сбой
                # прохода не должен её убивать, иначе страховки больше нет.
                lg.exception("периодический полный проход не удался")

    async def status(self) -> IndexingStatus:
        # Учитываем и полный проход, и работу watcher'а: иначе статус
        # показывает "idle", пока демон индексирует пачку от watcher'а.
        pending = self.counter.cnt + self.watch_pending
        return IndexingStatus(
            files_to_index=pending,
            # Снимок с этапа preindex; в простое — 0.
            queued_files=self.queued_files if pending else 0,
            # Здесь и ниже в простое — итоги последнего прохода, а не нули
            # (см. last_pass_total).
            total_files=self.preindex_cnt if self.main_index else self.last_pass_total,
            # Сбор очереди — тоже работа: обход папок идёт минутами при нулевом
            # counter, и без preindex статус показывал бы простой.
            is_indexing=pending > 0 or self.preindex or self.main_index,
            type="index"
            if self.main_index
            else "preindex"
            if self.preindex
            else "update"
            if pending > 0
            else "idle",
            progress=(1 - (pending / self.preindex_cnt))
            if self.main_index and self.preindex_cnt
            else (1.0 if self.last_pass_total else 0.0),
            files_indexed=(self.preindex_cnt - pending)
            if self.main_index
            else self.last_pass_indexed,
            # Отдельно от is_indexing: оба состояния «простаиваю» и
            # «остановлено» дают is_indexing=false, и без этого поля клиент
            # не отличит «всё проиндексировано» от «индексация выключена».
            active=self.active,
        )

    async def create_index(
        self,
        store: LanceWrapper | None = None,
        force_fts: bool = False,
        force_vector: bool = False,
    ):
        """Достроить недостающие индексы таблицы и подтянуть свежие данные.

        `force_fts` / `force_vector` — полная пересборка, O(корпус): нужна при
        смене токенизатора или после массового удаления. Новые строки в готовые
        индексы дописывает дешёвый `optimize_indices()`.
        """
        store = store or self.files_store

        def _create():
            if force_fts or not store.fts_created:
                lg.info("building fts index on %s", store.table_name)
                store.create_fts(force=force_fts)
            # Без проверки vector_created: метод сам решает, строить ли, а
            # проверка заморозила бы num_partitions на размере первой сборки.
            store.create_vector_index(force=force_vector)
            # Дёшево и всегда: иначе свежие файлы остаются вне индексов и
            # каждый поиск досматривает их полным перебором.
            store.optimize_indices()
            self._maybe_compact(store)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_INDEX_BUILD_EXECUTOR, _create)

    async def _refresh_indices_only(self, store: LanceWrapper) -> None:
        """Только дообновить индексы новыми строками — без тяжёлых операций."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(_INDEX_BUILD_EXECUTOR, store.optimize_indices)
        except Exception:
            lg.exception("failed to refresh indices during full scan")

    def _maybe_compact(self, store: LanceWrapper) -> None:
        """Полная компакция — только в простое и не чаще интервала.

        Склейка фрагментов стоит CPU и памяти, а демон живёт на рабочей машине.
        """
        if self.main_index or self.watch_pending:
            return
        now = time.monotonic()
        if (
            now - self._last_optimize.get(store.table_name, 0.0)
            < self.cfg.index_optimize_interval
        ):
            return
        self._last_optimize[store.table_name] = now
        try:
            store.compact()
            # Компакция освободила крупные блоки Arrow — вернуть их ОС сейчас,
            # в простое, дешевле всего (no-op вне glibc).
            safe_malloc_trim()
        except Exception:
            lg.exception("failed to compact lance table %s", store.table_name)

    async def ensure_index(self, store: LanceWrapper | None = None):
        """Обновить индексы после доиндексации через watcher (с дебаунсом).

        `create_index` зовётся только в конце `scan_all`, файлы от watcher'а
        туда не попадают — без этого на свежей базе нет FTS-индекса и
        полнотекстовый поиск падает. Запросы схлопываются в один пересбор.
        Пометка по таблице: общий флаг гасила бы пересборка чужого индекса.
        """
        store = store or self.files_store
        self._dirty_stores.add(store.table_name)
        if self._index_task is None or self._index_task.done():
            self._index_task = asyncio.create_task(self._refresh_index_loop())

    async def _refresh_index_loop(self):
        while True:
            # Копим изменения, чтобы не пересобирать индекс на каждое событие ФС.
            await asyncio.sleep(self.cfg.index_refresh_delay)
            if not self._dirty_stores:
                return
            for store in self.cfg.lance:
                if store.table_name not in self._dirty_stores:
                    continue
                if self.main_index:
                    # Полный проход построит индексы в конце, но ждать нельзя:
                    # до тех пор поиск досматривает новые строки перебором.
                    # Дообновление дёшево; пометку не снимаем, иначе пересбор
                    # потеряется вместе с изменениями watcher'а.
                    await self._refresh_indices_only(store)
                    continue
                async with self._index_build_lock:
                    try:
                        await self.create_index(store)
                        self._dirty_stores.discard(store.table_name)
                    except Exception:
                        lg.exception(
                            "failed to refresh indexes on %s after watcher update",
                            store.table_name,
                        )

    async def check_meta(self, meta: FileMetadata) -> bool:
        """Изменился ли файл с момента последней индексации."""
        return await check_meta_changed(self.lsm_collection.indexed, meta)

    async def process_stream(
        self,
        batch: AsyncIterable[FileMetadata] | Iterable[FileMetadata],
        count: bool = True,
        store: LanceWrapper | None = None,
    ) -> None:
        """Разобрать поток метаданных и записать чанки в таблицу."""
        store = store or self.files_store

        if not inspect.isasyncgen(batch):
            def _gen() -> Iterable[FileMetadata]:
                for b in batch:  # type: ignore
                    yield b

            wrapped: AsyncGenerator[FileMetadata, Any] = wrap_iter_to_async(_gen())
        else:
            wrapped: AsyncGenerator[FileMetadata, Any] = batch

        # Пути, уже взятые в этот проход. Очередь переживает прерванный проход,
        # и следующий сбор кладёт тот же путь повторно. Повтор отбрасываем до
        # разбора: чанки пути идут одним куском, и чистка перед первой записью
        # пути не заденет строк этого же прохода. Файл, изменившийся между
        # постановками, не теряется: в LSM ляжет meta первой постановки, и
        # следующий проход увидит расхождение с диском.
        taken: set[str] = set()

        async def _once_per_path(
            metas: AsyncIterable[FileMetadata],
        ) -> AsyncGenerator[FileMetadata, Any]:
            async for meta in metas:
                if meta.path in taken:
                    lg.debug("skip %s: already queued in this pass", meta.path)
                    if count:
                        # Безусловно, а не count(): тот дедуплицирует по meta,
                        # и повтор с той же meta не вычелся бы — /status до
                        # следующего прохода показывал бы «индексация идёт».
                        self.counter.update(-1)
                    continue
                taken.add(meta.path)
                yield meta

        # Файлы, дочитанные ЦЕЛИКОМ и потому готовые к отметке в LSM.
        processed: set[FileMetadata] = set()
        # Файл, чанки которого ещё могут прийти. async_parse_files разбирает
        # файлы строго по одному и отдаёт все чанки файла подряд, поэтому
        # появление чанка другого файла означает, что предыдущий дочитан.
        pending_meta: FileMetadata | None = None
        # Пути, разбор которых упал (permanent=False). Отмечать их в LSM нельзя,
        # даже если парсер успел отдать часть чанков: иначе файл остался бы в
        # индексе наполовину, и проходы к нему больше не вернулись бы.
        failed: set[str] = set()

        def _callback(batch: list[ChunkWithMeta]):
            nonlocal pending_meta
            for b in batch:
                meta = b.get("file_meta")
                if meta is None:
                    continue
                # Дочитанным файл считаем только по чанку следующего файла:
                # его чанки могут лечь в два батча записи, и отметка после
                # первого при обрыве оставила бы в индексе половину файла.
                if pending_meta is not None and meta != pending_meta:
                    processed.add(pending_meta)
                pending_meta = meta
                if count:
                    self.counter.count(meta, mode="sub")

        async def _fail_cb(meta: FileMetadata, *, permanent: bool):
            if permanent:
                # Файл разобран, но текста в нём нет — помечаем обработанным,
                # чтобы не возвращаться. При ошибке разбора (permanent=False)
                # помечать нельзя: файл выпал бы из индексации навсегда.
                if await _drop_previous_version(meta.path):
                    await self.lsm_collection.indexed.async_set(meta.path, meta)
            else:
                failed.add(meta.path)
            if count:
                self.counter.count(meta, mode="sub")

        async def _drop_previous_version(path: str) -> bool:
            """Снять строки прежней версии файла, разобранного без текста.

            Чистка в цикле записи видит только пути из батча, а у такого файла
            батча нет: пустой, битый или зашифрованный файл искался бы по
            старому тексту, а отметка в LSM не пустила бы проход к нему снова.
            False — снять не удалось, отмечать файл нельзя.
            """
            # Строки есть только у пути, который уже индексировали: новый
            # пустой файл не стоит холостого коммита.
            if not await self.lsm_collection.indexed.async_is_in(path):
                return True
            try:
                await store.delete_where(f"path = '{sql_quote(path)}'")
            except Exception:
                lg.exception("failed to drop previous chunks of %s", path)
                return False
            return True

        chunks = async_embed_documents_into_batches(
            async_parse_files(_once_per_path(wrapped), fail_cb=_fail_cb),
            embedder=partial(
                self.cfg.ov_model.async_encode, task="document", role="index"
            ),
            batch_size=self.cfg.embed_model_cfg.batch_size,
            callback=_callback,
        )
        # Пути, для которых уже удалили прежние чанки в этом проходе.
        purged: set[str] = set()
        # Место проверяем и по ходу (раз в _DISK_CHECK_EVERY_BATCHES батчей):
        # проход по домашнему каталогу идёт часами, и диск успевает кончиться.
        batches_written = 0
        # Проход оборван по месту: хвост последнего файла мог не дойти до записи.
        interrupted = False
        # На пустой таблице прежних чанков нет, а повтор пути в проход не
        # попадает (_once_per_path). Строки там могут быть только у файлов,
        # которые за время прохода записал watcher, — их он отметил в LSM,
        # и чистим только их. Иначе на холодном скане шёл бы холостой DELETE
        # на каждый батч — тысячи лишних коммитов.
        try:
            table_was_empty = await store.count_rows() == 0
        except Exception:
            table_was_empty = False
        async for _batch in async_merge_batches(
            chunks, target_size=self.cfg.parquet_record_batch_size
        ):
            # Прежние чанки пути чистим один раз — перед его первой вставкой
            # в этом проходе. Повторно нельзя: файл длиннее батча продолжается
            # в следующем, и чистка снесла бы записанное начало. Отметка
            # «дочитан» для этого не годится: колбэк ставит её в том же батче
            # записи, где лежит хвост файла.
            # Путь, чистка которого не удалась, в LSM не отмечаем: иначе
            # прежняя версия лежала бы рядом с новой до следующего изменения.
            failed.update(
                await self._purge_stale_chunks(
                    _batch, purged, store, known_only=table_was_empty
                )
            )
            await store.add_batch(_batch)
            # Отмечаем файл проиндексированным только ПОСЛЕ успешной записи:
            # иначе упавшая вставка навсегда выкинет его из индексации.
            for meta in processed:
                if meta.path not in failed:
                    await self.lsm_collection.indexed.async_set(meta.path, meta)
            processed.clear()
            # Троттлинг фоновой индексации: пауза между батчами отдаёт CPU
            # интерактивной работе пользователя. 0 — без пауз (по умолчанию).
            if (pause := self.cfg.index_throttle_seconds) > 0:
                await asyncio.sleep(pause)
            batches_written += 1
            if batches_written % _DISK_CHECK_EVERY_BATCHES == 0:
                free = await asyncio.to_thread(
                    lambda: shutil.disk_usage(self._storage_root()).free
                )
                if free < GIB:
                    # Прерываем проход, а не падаем: записанное остаётся
                    # валидным, отметки в LSM расставлены только за полностью
                    # записанные файлы, и следующий проход продолжит с того
                    # же места, когда место освободится.
                    lg.error(
                        "свободного места осталось %.1f ГиБ (< 1 ГиБ) — "
                        "прерываю индексацию, записано батчей: %s",
                        free / GIB,
                        batches_written,
                    )
                    interrupted = True
                    break
        # Последний файл потока: чанка следующего файла не будет, а поток
        # дочитан до конца — значит, файл записан. После обрыва по месту его
        # чанки ещё в конвейере: не отмечаем, следующий проход перепишет файл.
        if (
            pending_meta is not None
            and not interrupted
            and pending_meta.path not in failed
        ):
            await self.lsm_collection.indexed.async_set(
                pending_meta.path, pending_meta
            )

    async def _purge_stale_chunks(
        self,
        batch,
        purged: set[str],
        store: LanceWrapper,
        known_only: bool = False,
    ) -> set[str]:
        """Удалить прежние чанки для путей, впервые встреченных в этом проходе.

        Удаление идёт одним фильтром на батч, а не запросом на путь: каждый
        `delete` — отдельная транзакция с новой версией таблицы.

        known_only — только пути, уже отмеченные в LSM (холодная таблица, см.
        process_stream). Возвращает пути, прежние чанки которых снять не
        удалось.
        """
        try:
            paths = {p for p in batch.column("path").to_pylist() if p}
        except Exception:
            return set()
        fresh = sorted(paths - purged)
        if not fresh:
            return set()
        purged.update(fresh)
        if known_only:
            indexed = self.lsm_collection.indexed
            known = await lsm_to_thread(lambda: [p for p in fresh if indexed.is_in(p)])
            if not known:
                return set()
            fresh = known
        predicate = "path IN ({})".format(", ".join(f"'{sql_quote(p)}'" for p in fresh))
        try:
            await store.delete_where(predicate)
        except Exception:
            lg.exception("failed to purge stale chunks for %s path(s)", len(fresh))
            return set(fresh)
        return set()

    def _pin_name_codes(
        self,
        store: LanceWrapper,
        table,
        codes: list[str],
        frame: "pl.DataFrame | None",
        pinned: list[str],
        where: str | None = None,
    ) -> "pl.DataFrame | None":
        """Найти документы по коду из имени и дочитать тех, кого нет в пуле.

        Сбой поиск не роняет: код из имени — лишь надбавка к выдаче.

        where — фильтр поиска: пул уже отфильтрован, дочитывание идёт с тем же
        условием, поэтому закрепляются только пути со строками в кадре.
        Словарь имён фильтра не знает — «1–3 файла на код» считается по всей
        таблице.

        Дочитываются и документы, от которых в пуле одна карточка (обычно для
        «найди файл …»): иначе такой документ шёл бы первым без фрагментов.
        """
        try:
            index = self._name_indexes.setdefault(store.table_name, NameIndex())
            paths = index.hits(table, codes)
            if not paths:
                return frame
            import polars as pl

            have = set(frame["path"].to_list()) if frame is not None else set()
            # ne_missing: у строк, записанных до появления kind, там null.
            with_chunks = (
                set(frame.filter(pl.col("kind").ne_missing(CARD_KIND))["path"])
                if frame is not None
                else set()
            )
            missing = [p for p in paths if p not in with_chunks]
            if missing:
                extra = pinned_rows(table, missing, where)
                have.update(extra["path"].to_list())
                # diagonal_relaxed: у дочитанных строк нет колонок оценки, и
                # они остаются пустыми — по ним выдача и отличает строки пула.
                frame = (
                    extra
                    if frame is None
                    else pl.concat([frame, extra], how="diagonal_relaxed")
                )
        except Exception:
            lg.exception(
                "поиск файла по коду из имени в %s не удался", store.table_name
            )
            return frame
        pinned.extend(p for p in paths if p in have)
        return frame

    async def search(
        self,
        query: str,
        top_k: int = 10,
        query_type: Literal["hybrid", "vector", "text"] = "hybrid",
        pinned: list[str] | None = None,
        where: str | None = None,
    ) -> "pl.DataFrame | None":
        """Кадр выдачи поиска по индексу файлов; None — искать негде.

        top_k — число ДОКУМЕНТОВ: из таблицы берётся пул `search_pool(top_k)`
        чанков на канал, документы из него собирает SearchResponse.from_dataframe.

        pinned — сюда дописываются пути документов, найденных по коду из имени
        (name_codes.py); их строки дочитываются в кадр, если их нет в пуле, и
        выдача ставит эти документы первыми. None — код из имени не ищется.

        where — фильтр по метаданным (SearchFilters.where), prefilter: отбирает
        строки ДО пула, иначе под фильтром оставались бы крохи пула. Действует
        во всех каналах, в откате гибрида на вектор и в дочитывании по коду.
        """
        codes = query_codes(query) if pinned is not None else []
        vec = (await self.cfg.ov_model.async_encode([query], role="search"))[0]
        store = self.files_store
        pool = search_pool(top_k)
        # FTS-запрос — готовым MatchQuery, а не строкой: строку в кавычках
        # LanceDB разбирает как фразу, а индекс построен без позиций
        # (src/common/fts.py) — гибрид падал бы в вектор, text отдавал бы 500.
        fts_query = MatchQuery(query, "text")

        def _filtered(builder):
            # Без фильтра .where не зовём вовсе: пустое условие LanceDB
            # принимать не обязан.
            return builder if where is None else builder.where(where, prefilter=True)

        def _probed(builder):
            """Разделы IVF и refine для векторного поиска и векторного канала гибрида."""
            builder = builder.refine_factor(VECTOR_REFINE_FACTOR)
            if where is None:
                return builder.nprobes(VECTOR_NPROBES)
            # nprobes(n) ставит и минимум, и МАКСИМУМ разделов: строки под
            # узким фильтром (автор, папка) могут не попасть в VECTOR_NPROBES
            # ближайших разделов, и канал отдал бы пусто. maximum_nprobes(0)
            # даёт LanceDB досматривать разделы, пока пул не наберётся.
            return builder.minimum_nprobes(VECTOR_NPROBES).maximum_nprobes(0)

        def _vector_search(table):
            return (
                _probed(
                    _filtered(
                        table.search(
                            vec,
                            query_type="vector",
                            vector_column_name="vector",
                        )
                    )
                )
                .distance_type("cosine")  # type: ignore
                .limit(pool)
                .to_polars()
            )

        def _search_in(table):
            if query_type == "hybrid":
                try:
                    return (
                        _probed(
                            _filtered(
                                table.search(
                                    query_type="hybrid",
                                    vector_column_name="vector",
                                    # FTS — только по тексту чанка: без
                                    # колонок LanceDB ищет по всем полям с
                                    # FTS-индексом, и новый индекс молча
                                    # вошёл бы в RRF.
                                    fts_columns="text",
                                )
                            )
                        )
                        # Фильтр и nprobes гибрида LanceDB передаёт каналам.
                        .distance_type("cosine")  # type: ignore
                        .vector(vec)
                        .text(fts_query)
                        # RRF по умолчанию, но с оценками каналов: _score есть
                        # только у строк из FTS, и по нему выдача отличает
                        # карточку, совпавшую словами, от соседки по вектору
                        # (отметка "card", src/server/model.py).
                        .rerank(RRFReranker(return_score="all"))
                        # Лимит гибрида — и пул каждого канала до RRF, и
                        # длина слитой выдачи.
                        .limit(pool)
                        .to_polars()
                    )
                except Exception as exc:
                    # Откат на вектор при любой ошибке: FTS-индекса может ещё
                    # не быть (свежая база), и выдача по вектору лучше 500.
                    # Незнакомую ошибку пишем в ERROR со стеком, чтобы
                    # поломка не пряталась под штатным откатом.
                    if _is_missing_fts_index(exc):
                        lg.warning(
                            "FTS-индекс ещё не готов, гибридный поиск деградирует "
                            "до векторного: %s",
                            exc,
                        )
                    else:
                        lg.error(
                            "гибридный поиск упал (%s), деградирую до векторного",
                            exc,
                            exc_info=True,
                        )
                    return _vector_search(table)
            elif query_type == "vector":
                return _vector_search(table)
            elif query_type == "text":
                try:
                    return (
                        _filtered(
                            table.search(
                                fts_query,
                                query_type="fts",
                                # Как в гибриде: только текст чанка.
                                fts_columns="text",
                            )
                        )
                        .limit(pool)
                        .to_polars()
                    )
                except Exception as exc:
                    if not _is_missing_fts_index(exc):
                        raise
                    # Отдаём пустой результат вместо 500: индекса пока нет,
                    # но это штатное состояние на старте.
                    lg.warning("FTS-индекс ещё не готов, текстовый поиск пуст: %s", exc)
                    return None

        def _search_all():
            if not store.has_table:
                # Обращение к store.table СОЗДАЛО бы таблицу, то есть поиск
                # начал бы писать в хранилище.
                lg.debug("таблица %s ещё не создана", store.table_name)
                return None
            table = store.table
            frame = _search_in(table)
            if codes:
                frame = self._pin_name_codes(store, table, codes, frame, pinned, where)
            return frame

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_SEARCH_EXECUTOR, _search_all)
