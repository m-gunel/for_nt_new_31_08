import asyncio
import concurrent.futures
import inspect
import os
import shutil
import time
import typing
from collections.abc import AsyncGenerator, AsyncIterable, Iterable, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    suppress,
)
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.common import (
    EMAIL_PREFIX,
    ChunkWithMeta,
    FileMetadata,
    IndexingStatus,
    is_email_path,
)
from src.malloc_tuning import safe_malloc_trim
from src.parsers import (
    async_parse_files,
    ocr,
)
from src.thunderbird.find_files import list_email_folders
from src.utils import (
    async_embed_documents_into_batches,
    async_merge_batches,
    get_logger,
    wrap_exception_logger,
    wrap_iter_to_async,
)
from src.utils.lance_wrapper import LanceWrapper, sql_quote
from src.utils.lsm_adapter import LSMCollection, check_meta_changed
from src.utils.lsm_adapter import to_thread as lsm_to_thread
from src.utils.lsm_adapter import wait_idle as lsm_wait_idle
from src.utils.thread_priority import background_pool_initializer

from .name_codes import NameIndex, pinned_rows, query_codes
from .scanners import EmailsScanner, FilesScanner

if TYPE_CHECKING:
    import polars as pl

if typing.TYPE_CHECKING:
    from src.config import Config

lg = get_logger(__name__, slice=-1)


GIB = 1024**3

# Сколько путей класть в один фильтр удаления, чтобы не делать коммит на файл.
_DELETE_BATCH = 100

# Через сколько записанных батчей перепроверять свободное место во время
# прохода. disk_usage — системный вызов, на каждый батч он лишний.
_DISK_CHECK_EVERY_BATCHES = 32

# Отдельный поток под перестройку индексов: она долгая, и её нельзя пускать
# в общий пул, через который ходят поисковые запросы.
_INDEX_BUILD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    1,
    thread_name_prefix="index_build",
    # Перестройка индексов — фоновая работа: уступает CPU пользователю.
    initializer=background_pool_initializer,
)


# Источники поиска. Дефолт — только файлы: письма попали в индекс позже, и
# смешивать их в общую выдачу по умолчанию значит вытеснять ими документы.
FILES_SOURCE = "files"
EMAIL_SOURCE = "email"
SearchSource = Literal["files", "email"]

# starts_with, а не LIKE: в LIKE `_` — «любой символ», и `path like 'email_%'`
# матчит заодно пути вроде `emailX...`.
EMAIL_ROWS_PREDICATE = f"starts_with(path, '{EMAIL_PREFIX}')"


# Маркеры ошибки «полнотекстовый индекс ещё не создан». Формулировка у LanceDB
# менялась от версии к версии, поэтому проверяем несколько вариантов, а не одну
# фразу: узкое условие на апгрейде библиотеки молча превратило бы штатную
# деградацию поиска в 500. Кортеж внутри — все подстроки должны совпасть.
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


# Пул чанков на канал поиска, из которого собирается выдача limit ДОКУМЕНТОВ.
# Канал — вектор и FTS гибрида по отдельности (их сливает RRF), а также
# чисто векторный и чисто текстовый поиск. Пул по limit чанков отдавал меньше
# документов, чем просили (10 чанков — в среднем 6,2 документа: длинный
# документ занимает места соседей), а копии одного файла съедали места ещё
# и сверху. Замер на стенде (1332 запроса): пул 50 при limit=10 — лучший
# (Hit@1 +0,039, MRR +0,020 против 10 чанков), 100 не лучше. Кратность — чтобы
# на больших limit документов в пуле хватало; потолок — чтобы limit=1000 не
# собирал тысячи строк на канал.
SEARCH_POOL_MIN = 50
SEARCH_POOL_PER_DOC = 5
SEARCH_POOL_MAX = 500

# Сколько разделов IVF-индекса просматривает векторный поиск. Одно значение
# для чистого вектора и для векторного канала гибрида: иначе гибрид находил бы
# по смыслу меньше, чем query_type=vector на том же индексе.
VECTOR_NPROBES = 32


def search_pool(limit: int) -> int:
    """Чанков на канал поиска при выдаче limit документов."""
    return min(SEARCH_POOL_MAX, max(SEARCH_POOL_MIN, limit * SEARCH_POOL_PER_DOC))


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
        self.queued_emails = 0
        self.main_index = False
        self.emails_task: asyncio.Task | None = None
        # Периодический полный проход — страховка поверх watcher'а (шаг 15).
        self.rescan_task: asyncio.Task | None = None
        # инициализируем здесь, иначе stop_index() до старта lifespan()
        # падает с AttributeError вместо тихого no-op
        self.task: asyncio.Task | None = None
        # Включена ли индексация — БТ Библиотеки §5.1/§5.2. Отдельно от
        # self.task: задача завершается сама после каждого прохода, и по ней
        # нельзя отличить «прошли и ждём watcher'а» от «остановлено насовсем».
        # Начальное значение — из конфига, чтобы поведение существующих
        # установок не изменилось (autostart_indexing=True по умолчанию).
        self.active: bool = cfg.autostart_indexing
        # Ровно один проход на процесс. Два потребителя одной очереди
        # заклинивают друг друга: iter(endless=False) проверяет пустоту до
        # блокирующего get(), и опоздавший ждёт элемент вечно.
        self._scan_lock = asyncio.Lock()
        # Чтобы несколько батчей от watcher'а не начали строить индекс разом
        self._index_build_lock = asyncio.Lock()
        # Полный проход, вытесненный почтовым: его нужно доиграть, а не
        # потерять. Почтовые проходы флага не требуют — их повторит поллинг
        # через email_poll_interval.
        self._rescan_requested = False
        # Имена таблиц, которым нужен пересбор индексов.
        self._dirty_stores: set[str] = set()
        self._index_task: asyncio.Task | None = None
        # Сколько файлов сейчас разбирает watcher (для честного /status)
        self.watch_pending = 0
        # Итоги последнего завершённого прохода. Без них /status обнуляет
        # files_indexed и progress в тот же момент, когда снимается
        # main_index, и клиент, дождавшийся конца индексации, читает
        # «проиндексировано 0» (НТ 27.08: bulk_index_end indexed=0 на прогоне,
        # где реально обработано 680 файлов).
        self.last_pass_total = 0
        self.last_pass_indexed = 0
        # Когда последний раз выполнялась компакция — по таблице отдельно
        self._last_optimize: dict[str, float] = {
            cfg.ld_files_table: 0.0,
            cfg.ld_email_table: 0.0,
        }
        # Имена документов для поиска файла по коду из имени — по таблице.
        self._name_indexes: dict[str, NameIndex] = {}
        self.lsm_collection = LSMCollection()
        self.files_scanner = FilesScanner(
            queue_path=Path(cfg.lsm_root) / "files",
            lsm_checker=self.lsm_collection.indexed,
            reset=True,
        )
        # Вошли ли мы в exit_stack: сканер, созданный лениво после этого,
        # надо регистрировать в стеке самостоятельно.
        self._entered = False
        # Поиск почтового корня — обход ~/.thunderbird. При выключенной почте
        # он не нужен; включение через API подхватит _ensure_emails_scanner.
        self.emails_scanner = (
            self._create_emails_scanner() if self.cfg.email_enabled else None
        )

    def _create_emails_scanner(self) -> EmailsScanner | None:
        emails_root = self.cfg.email_root
        if emails_root is None:
            return None
        return EmailsScanner(
            queue_path=Path(self.cfg.lsm_root) / "emails",
            email_path=emails_root,
            folders=list_email_folders(emails_root),
            lsm_checker=self.lsm_collection.indexed,
            reset=os.environ.get("RESET") == "1",
        )

    async def _ensure_emails_scanner(self) -> EmailsScanner | None:
        """Актуальный почтовый сканер: досоздать или пересоздать при смене корня.

        Пересоздание нужно, потому что EmailsScanner фиксирует путь и список
        папок в конструкторе. Без него смена `email.path_to_email_folder`
        через API вступала бы в силу только после рестарта демона, а сканер,
        не созданный из-за ещё не появившегося профиля Thunderbird, не
        создавался бы уже никогда.
        """
        root = self.cfg.email_root
        if root is None:
            return self.emails_scanner
        if self.emails_scanner is not None and self.emails_scanner.path == root:
            return self.emails_scanner
        if self.emails_scanner is not None:
            lg.info(
                "почтовый корень сменился (%s -> %s), пересоздаём сканер",
                self.emails_scanner.path,
                root,
            )
            # Сохраняем состояние старого сканера: exit_stack закроет его
            # только на остановке демона, а offsets.json нужен уже сейчас.
            self.emails_scanner._save_state()
        self.emails_scanner = self._create_emails_scanner()
        if self.emails_scanner is not None and self._entered:
            await self.exit_stack.enter_async_context(self.emails_scanner)
        return self.emails_scanner

    async def __aenter__(self):
        self.exit_stack.enter_context(self.lsm_collection)
        await self.exit_stack.enter_async_context(self.files_scanner)
        if self.emails_scanner is not None:
            await self.exit_stack.enter_async_context(self.emails_scanner)
        self._entered = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback,
    ) -> bool | None:
        # Периодический проход мог быть поднят из activate(), а не из
        # lifespan(): без этого задача переживает закрытие индекса и потом
        # ходит в уже закрытые LSM и LanceDB.
        await self.stop_periodic_rescan()
        try:
            await asyncio.wait_for(self.exit_stack.aclose(), 5)
        except TimeoutError:
            lg.warning("timeout on stopping indexer")

    @asynccontextmanager
    async def lifespan(self):
        # Создаём таблицу и FTS-индекс заранее: иначе первый же поисковый
        # запрос платит за инициализацию LanceDB и упирается в таймаут.
        # Почтовую — только при включённой почте: .table её создаёт.
        stores = [self.files_store]
        if self.cfg.email_enabled:
            stores.append(self.email_store)
        for store in stores:
            try:
                await asyncio.to_thread(lambda s=store: s.table)
            except Exception:
                lg.exception(
                    "failed to pre-initialize lance table %s", store.table_name
                )
        async with self:
            # До первого прохода: он считает table_was_empty и строит индексы
            # уже по разъехавшимся таблицам. Сканеры к этому моменту подняты.
            try:
                await self.migrate_emails_out_of_files_table()
            except Exception:
                # Миграция не должна мешать демону стартовать: без неё поиск
                # по файлам просто останется замусоренным письмами.
                lg.exception("миграция почты из таблицы файлов не удалась")
            # Тоже до первого прохода: новые строки должны дописываться уже в
            # индекс с параметрами из конфига. Почтовая миграция выше строит
            # FTS по конфигу, так что после неё пересобирать нечего.
            try:
                await self.migrate_fts_config()
            except Exception:
                lg.exception("сверка параметров FTS-индексов не удалась")
            if self.active:
                self.task = asyncio.create_task(self.scan_all())
                # Поллинг почты не зависит от исхода первого прохода: иначе
                # упавший scan_all оставит почту без подхвата до рестарта.
                await self.start_emails_polling()
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
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(self.task, 5)

    async def stop_index(self):
        if self.task is not None:
            self.task.cancel()
            # Воркер OCR — отдельный процесс: отмена задачи его не останавливает.
            ocr.kill_running()
            with suppress(asyncio.CancelledError):
                await self.task
            # Вне suppress: внутри строка печаталась только когда задача НЕ
            # была отменена, то есть ровно в тех прогонах, где останавливать
            # было нечего. По логу это читалось как «остановка не дошла».
            lg.info("indexing stopped")
        lg.info("clear index queue")
        await self.files_scanner.clear()
        lg.info("stop indexing")
        await self.stop_emails_polling()
        # Отмена задачи не отменяет уже отданную в LSM-поток работу: она
        # доигрывает, пока вызывающая сторона считает, что всё остановлено.
        # Ждём явно — дальше POST /config перезапускает индексацию, и её
        # первый шаг снова идёт в ту же БД.
        await lsm_wait_idle()

    async def start_reindex(self):
        """Запустить проход. Идемпотентно: вторую задачу не плодит.

        Прежде каждый вызов создавал новую задачу, и ссылка на предыдущую
        терялась — stop_index() отменил бы только последнюю. Штатный путь
        (POST /config) всегда зовёт stop_index() первым, поэтому его поведение
        не меняется; защита нужна для /activate, который приходит когда угодно.
        """
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
        # ТОЛЬКО если поллинг не жив: start_emails_polling внутри себя зовёт
        # stop_emails_polling, и на повторной активации это оборвало бы идущий
        # почтовый проход — ровно то, чего обещает не делать докстринг.
        # immediate=True: после остановки ждать целый email_poll_interval
        # (в поставке — час) до первых писем незачем.
        if self.emails_task is None or self.emails_task.done():
            await self.start_emails_polling(immediate=True)
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
        # И периодический проход — по той же причине: он проснулся бы уже
        # после дропа и завёл бы таблицы обратно, отменив сброс.
        await self.stop_periodic_rescan()
        # stop_index чистит только файловую очередь. clear() почтового сканера
        # снимает и очередь, и offsets.json, и позиции в памяти.
        if self.emails_scanner is not None:
            await self.emails_scanner.clear()
        else:
            # Сканера нет (почту не включали или корень не найден), а хвосты
            # от прошлой конфигурации остаться могли. RESET=1 сносит каталог
            # целиком — сброс через API обязан давать то же состояние.
            root = Path(self.cfg.lsm_root)
            await asyncio.to_thread(
                shutil.rmtree, root / "emails", ignore_errors=True
            )
            (root / "offsets.json").unlink(missing_ok=True)
        # ПОРЯДОК ВАЖЕН: сначала LSM, потом таблицы. Обрыв посередине оставит
        # «LSM пуст, таблицы полны» — следующий проход переиндексирует всё и
        # вычистит старые чанки. Обратный порядок дал бы «таблицы пусты, LSM
        # полон», где проход считает всё сделанным и индекс не восстановится.
        await lsm_to_thread(self.lsm_collection.indexed.reset)
        # Дроп таблиц, а не удаление по фильтру: не остаётся ни deletion
        # vectors, ни партиций IVF-PQ с мёртвыми PQ-кодами.
        for store in (self.files_store, self.email_store):
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

    @property
    def email_store(self) -> LanceWrapper:
        return self.cfg.lance.email

    def store_for(self, path: str) -> LanceWrapper:
        """Таблица, в которой живут чанки этого пути."""
        return self.email_store if is_email_path(path) else self.files_store

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

    async def _preindex_emails(self) -> int:
        """Собрать очередь писем. Возвращает её размер.

        Вынесен из файлового сбора: обход mbox идёт минутами, и общий сбор
        задерживал появление новых файлов в поиске.
        """
        if not (self.cfg.email_enabled and self.emails_scanner):
            self.queued_emails = 0
            return 0
        self.preindex = True
        try:
            stale = await asyncio.to_thread(self.emails_scanner.detect_stale_folders)
            for folder in stale:
                # Компакция или исчезновение mbox: старые offset-ключи
                # указывают на чужие байты. Сносим папку, scan() перечитает.
                await self.delete_prefix(f"{EMAIL_PREFIX}{folder}:")
            await self.emails_scanner.scan()
        except Exception:
            # Почта не должна быть способна уронить индексацию файлов.
            lg.exception("email preindex failed, continuing without it")
        finally:
            self.queued_emails = await self.emails_scanner.total()
            self.preindex = False
        lg.info("collected %s emails", self.queued_emails)
        return self.queued_emails

    async def delete_prefix(self, prefix: str) -> None:
        """Снять с индекса всё, что лежит под этим префиксом пути.

        `starts_with`, а не `LIKE`: в LIKE `_` и `%` — подстановочные знаки,
        и удаление `/home/u/a_b/` задевало бы `/home/u/aXb/`.
        """
        store = self.store_for(prefix)
        await self.lsm_collection.indexed.async_delete_prefix(prefix=prefix)
        lg.info("delete %r from %s", prefix, store.table_name)
        await store.delete_where(f"starts_with(path, '{sql_quote(prefix)}')")

    async def migrate_emails_out_of_files_table(self) -> int:
        """Развести письма и файлы по своим таблицам в уже собранном индексе.

        Строки переносятся, а не переиндексируются: схема одна, векторы уже
        посчитаны. Признак «мигрировать нечего» — отсутствие почтовых строк
        в таблице файлов, отдельный маркер версии не нужен.
        """
        pending = await asyncio.to_thread(
            self.files_store.count_matching, EMAIL_ROWS_PREDICATE
        )
        if not pending:
            return 0
        lg.info("миграция: %s почтовых строк в таблице файлов", pending)
        # Почтовая таблица сейчас либо пуста, либо содержит остаток прерванной
        # миграции: авторитетная копия всё ещё в files. Дроп даёт идемпотентность.
        await asyncio.to_thread(self.email_store.drop)
        moved = await asyncio.to_thread(
            self.files_store.move_rows_to, self.email_store, EMAIL_ROWS_PREDICATE
        )
        lg.info(
            "миграция: перенесено %s строк в %s", moved, self.email_store.table_name
        )
        # Обязательно, а не уборка: удаление в Lance мягкое. Иначе партиции
        # IVF-PQ остаются набитыми PQ-кодами перенесённых писем, отсев удалённых
        # идёт после отбора кандидатов, и limit=N вернёт меньше N живых строк.
        await asyncio.to_thread(self.files_store.compact)
        await self.create_index(self.files_store, force_fts=True, force_vector=True)
        await self.create_index(self.email_store)
        return moved

    async def migrate_fts_config(self) -> list[str]:
        """Пересобрать FTS-индексы, построенные не с параметрами `fts_cfg`.

        Например, пресетом ru_stem, который терял слова длиннее 40 байт.
        Пересобирается только FTS: `create_index` ещё компактирует таблицу и
        дообучает вектор, а миграции это не нужно. Зовётся до первого прохода,
        поэтому записи параллельно нет; при сбое остаётся старый индекс, поиск
        по нему работает. Возвращает имена пересобранных таблиц.
        """
        rebuilt = []
        loop = asyncio.get_running_loop()
        for store in (self.files_store, self.email_store):
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

    async def drop_email_index(self) -> None:
        """Убрать почту из поиска целиком — вместе с очередью и состоянием.

        Дроп таблицы, а не удаление по фильтру: не остаётся ни deletion
        vectors, ни партиций IVF-PQ с мёртвыми PQ-кодами.
        """
        if self.emails_scanner is None and not self.email_store.has_table:
            # Сбрасывать нечего: почту не включали, таблицы нет. Выходим до
            # обращения к LSM и Lance — клиенты шлют конфиг целиком, то есть
            # с email.enabled=false на КАЖДЫЙ POST /config, и без этой
            # проверки каждое обновление папок тянуло за собой обход всей
            # таблицы состояния и дроп несуществующей таблицы.
            lg.debug("почтовый индекс уже пуст, сбрасывать нечего")
            return
        if self.emails_scanner is not None:
            await self.emails_scanner.clear()
        await self.lsm_collection.indexed.async_delete_prefix(prefix=EMAIL_PREFIX)
        await asyncio.to_thread(self.email_store.drop)

    async def delete_path(self, path: str) -> None:
        """Убрать файл из индекса и из состояния LSM."""
        await self.delete_paths([path])

    async def delete_paths(self, paths: list[str]) -> None:
        """То же для пачки путей — одним фильтром вместо коммита на файл.

        Только файлы: у письма ключ в LSM и путь чанка в Lance различаются
        (`email_INBOX:0:10` против `...:body`), и точное сравнение промахнётся.
        """
        if any(map(is_email_path, paths)):
            raise ValueError("почта удаляется по префиксу, а не по точному пути")
        for start in range(0, len(paths), _DELETE_BATCH):
            batch = paths[start : start + _DELETE_BATCH]
            predicate = "path IN ({})".format(
                ", ".join(f"'{sql_quote(p)}'" for p in batch)
            )
            await self.files_store.delete_where(predicate)
            for path in batch:
                await self.lsm_collection.indexed.async_delete(path)

    @staticmethod
    def _filter_stale(paths: list[str]) -> list[str]:
        """Из снимка ключей LSM оставить те, которых больше нет на диске.

        Снимок берётся отдельно, на LSM-потоке: тысячи `os.path.exists` —
        это тысячи syscall'ов, и держать на них блокировку таблицы нельзя.
        """
        return [
            path
            for path in paths
            # у писем нет файла на диске — их состояние живёт своей жизнью
            if not is_email_path(path) and not os.path.exists(path)
        ]

    async def reconcile_deleted(self) -> int:
        """Снять с индекса файлы, которых больше нет на диске.

        Realtime-событий watcher'а недостаточно: файл могли удалить при
        выключенном демоне, событие могло потеряться при переполнении очереди,
        а перемещение каталога приходит одним rename. Полный проход такие
        записи не замечает — он умеет только добавлять, — поэтому без сверки
        удалённый файл продолжает находиться поиском сколько угодно долго.
        """
        # Именно lsm_to_thread, а не asyncio.to_thread: обращаться к lsm_db
        # можно только из выделенного потока (см. src/utils/lsm_adapter.py).
        # Через дефолтный пул этот обход шёл параллельно записям в ту же БД.
        indexed_paths = await lsm_to_thread(self.lsm_collection.indexed.keys_snapshot)
        stale = await asyncio.to_thread(self._filter_stale, indexed_paths)
        if not stale:
            lg.debug("reconcile: nothing to remove")
            return 0
        await self.delete_paths(stale)
        lg.info("reconciled: removed %s stale paths", len(stale))
        await self.ensure_index()
        return len(stale)

    async def count_to_index(self):
        # Разбивка нужна наружу: «осталось 25001» читается как число файлов,
        # хотя почти всё в нём — письма.
        self.queued_files = await self.files_scanner.total()
        self.queued_emails = (
            await self.emails_scanner.total() if self.emails_scanner else 0
        )
        self.preindex_cnt = self.queued_files + self.queued_emails
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
    async def scan_all(self, emails_only: bool = False):
        if self._scan_lock.locked():
            # Не ждём в очереди: поллинг почты повторит через
            # email_poll_interval, а POST /config сначала зовёт stop_index().
            if not emails_only:
                # А вот полный проход повторять некому. POST /activate
                # поднимает файловый и почтовый проходы одновременно, и тот,
                # кто не успел взять лок, раньше терялся молча: активация не
                # индексировала ни одного файла до следующего события
                # watcher'а. Запоминаем и доигрываем на выходе из лока.
                self._rescan_requested = True
            lg.debug("проход индексации уже идёт, пропускаем запуск")
            return
        async with self._scan_lock:
            # Падение текущего прохода не должно съедать отложенный.
            # scan_all обёрнут @wrap_exception_logger, а тот ПРОБРАСЫВАЕТ
            # исключение — до цикла доигрывания управление просто не доходило,
            # и запрошенный полный проход терялся до следующего вызова.
            # Исходную ошибку сохраняем и поднимаем после доигрывания, чтобы
            # она не исчезла из виду.
            error: Exception | None = None
            try:
                await self._scan_all(emails_only=emails_only)
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
                lg.info("доигрываем полный проход, вытесненный почтовым")
                try:
                    await self._scan_all(emails_only=False)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Второй подряд сбой — дальше доигрывать бессмысленно.
                    lg.exception("доигрывание полного прохода не удалось")
                    break
            if error is not None:
                raise error

    async def _scan_all(self, emails_only: bool = False):
        if not emails_only:
            # До проверки места: сверка только освобождает его.
            await self.reconcile_deleted()
        if (free := shutil.disk_usage(self._storage_root()).free) < GIB:
            lg.warning(
                f"free disk space is less then 1GiB ({free / GIB: 0.1f}GiB), refuse indexing"
            )
            return
        # Два прохода подряд, а не один общий: поток внутри прохода обязан быть
        # однородным по источнику (см. process_stream). Файлы первыми — чтобы
        # свежий документ находился до того, как демон уйдёт читать ящик.
        if not emails_only:
            await self._run_pass(
                self._preindex_files, self.files_scanner, self.files_store
            )
        if self.cfg.email_enabled and self.emails_scanner is not None:
            await self._run_pass(
                self._preindex_emails, self.emails_scanner, self.email_store
            )
        if self.emails_task is None:
            await self.start_emails_polling()

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
        # Генератор держим в переменной, чтобы закрыть его явно. При отмене
        # задачи (её делает POST /config) он иначе остаётся подвешенным на
        # yield и финализируется когда-нибудь потом — уже после clear(),
        # когда scanner.queue указывает на НОВУЮ очередь. Его task_done()
        # уходил бы не в ту очередь: «task_done() called too many times»
        # либо порча счётчика unfinished_tasks.
        stream = scanner.iter(endless=False)
        try:
            lg.info("begin indexing")
            self.counter.clear_cache()  # очищаем кеш потому что дальше будем вычитать из счетчика
            # Обновляем индексы и во время прохода: иначе на холодном скане
            # свежие строки до самого конца вне индексов, и поиск идёт перебором.
            await self.ensure_index(store)
            await self.process_stream(stream, store=store)
        finally:
            # CancelledError — не Exception, а BaseException: без него отмена
            # прохода (её делает stop_index, то есть POST /config и
            # /deactivate) вылетала бы из finally прямо здесь, и main_index
            # оставался бы взведённым навсегда — /status вечно «индексирую».
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

    async def start_emails_polling(self, immediate: bool = False):
        """Запустить поллинг почты.

        immediate=True — первый проход сразу, не дожидаясь интервала:
        нужен при включении почты через API, чтобы пользователь не ждал
        email_poll_interval до первой индексации.
        """
        if self.cfg.email_enabled:
            if await self._ensure_emails_scanner() is None:
                # Корня может ещё не быть — Thunderbird не завершил первую
                # синхронизацию. Это не повод сдаваться навсегда: цикл
                # поллинга сам повторит попытку через email_poll_interval.
                lg.warning(
                    "почта включена, но почтовый корень не найден — "
                    "повторим попытку через %s c",
                    self.cfg.email_poll_interval,
                )
            await self.stop_emails_polling()
            self.emails_task = asyncio.create_task(
                self.run_email_reindex(immediate=immediate)
            )

    async def stop_emails_polling(self):
        if self.emails_task is not None:
            if not self.emails_task.done():
                self.emails_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.emails_task
            lg.info("email polling stopped")
            self.emails_task = None

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
            with suppress(asyncio.CancelledError):
                await self.rescan_task
            lg.info("периодический полный проход остановлен")
            self.rescan_task = None

    async def run_periodic_rescan(self) -> None:
        """Полный проход по расписанию — то, чего не даёт наблюдение за ФС.

        Событий файловой системы недостаточно по трём независимым причинам:
        они отбрасываются при переполнении очереди (см. WatchDogAdapter._emit,
        комментарий там прямо ссылается на периодическое сканирование),
        не приходят вовсе для каталогов, ставших читаемыми, и оставляют
        осиротевшие пути после перемещений. scan_all внутри зовёт
        reconcile_deleted, поэтому проход заодно снимает с индекса то, чего
        больше нет на диске.

        Запускается ТОЛЬКО в простое. Условие простоя — то же, что у
        компакции (_maybe_compact): полный обход домашнего каталога идёт
        часами, и начинать его поверх работающей индексации значит удвоить
        нагрузку ровно тогда, когда она и так высокая.
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

    async def run_email_reindex(self, immediate: bool = False):
        cfg = self.cfg
        if not immediate:
            lg.debug(f"waiting for {cfg.email_poll_interval=} to poll emails")
            await asyncio.sleep(cfg.email_poll_interval)
        while True:
            if not cfg.email_enabled:
                lg.info("email polling stopped")
                return
            # Сканера могло не быть на старте или он устарел после смены пути
            # в конфиге — проверяем каждый цикл, а не раз при запуске задачи.
            if await self._ensure_emails_scanner() is None:
                lg.debug("почтовый корень всё ещё не найден, ждём следующий цикл")
            else:
                status = await self.status()
                if status.type not in {"preindex", "index"}:
                    lg.debug("begin polling emails")
                    await self.scan_all(emails_only=True)
            lg.debug(f"waiting for {cfg.email_poll_interval=} to poll emails")
            await asyncio.sleep(cfg.email_poll_interval)

    async def status(self) -> IndexingStatus:
        # Учитываем и полный проход, и работу watcher'а: иначе статус
        # показывает "idle", пока демон индексирует пачку от watcher'а.
        pending = self.counter.cnt + self.watch_pending
        # counter общий, поэтому деление pending по типам берём снимком
        # с этапа preindex и обнуляем в простое.
        return IndexingStatus(
            files_to_index=pending,
            queued_files=self.queued_files if pending else 0,
            queued_emails=self.queued_emails if pending else 0,
            # В простое отдаём итоги последнего прохода, а не нули: клиент,
            # дождавшийся is_indexing=false, иначе читает «собрано 0».
            total_files=self.preindex_cnt if self.main_index else self.last_pass_total,
            # Сбор очереди — тоже работа: обход mbox идёт минутами при нулевом
            # counter, и без preindex статус врал бы «простаиваю».
            is_indexing=pending > 0 or self.preindex or self.main_index,
            type="index"
            if self.main_index
            else "preindex"
            if self.preindex
            else "update"
            if pending > 0
            else "idle",
            # Во время прохода — текущий прогресс, после него — результат
            # последнего прохода. Ноль в простое означал бы «ничего не
            # проиндексировано», хотя проход только что завершился успешно.
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
            # Без сторожа по vector_created: метод сам решает, надо ли строить.
            # Сторож замораживал num_partitions на размере первой сборки.
            store.create_vector_index(force=force_vector)
            # Дёшево и всегда: иначе свежие файлы остаются вне индексов и
            # каждый поиск досматривает их полным перебором.
            store.optimize_indices()
            self._maybe_compact(store)

        # Строим индекс в ОТДЕЛЬНОМ потоке, а не в общем пуле: иначе долгая
        # перестройка занимает все воркеры, и поисковые запросы, которые тоже
        # ходят через asyncio.to_thread, висят в очереди до таймаута.
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
        Кулдаун на каждую таблицу свой: общий счётчик означал бы, что компакция
        почты бесконечно отодвигает компакцию файлов.
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
            # Даём накопиться изменениям, чтобы не пересобирать индекс
            # на каждый чих файловой системы.
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
        """Разобрать поток метаданных и записать чанки в одну таблицу.

        Поток обязан быть однородным по источнику: батчинг ниже копит чанки до
        целевого размера и не флашит на границе источников, поэтому смешанный
        поток отдал бы один батч с файлами и письмами сразу.
        """
        store = store or self.files_store

        if not inspect.isasyncgen(batch):
            # преобразуемм в асинхронный генератор
            def _gen() -> Iterable[FileMetadata]:
                for b in batch:  # type: ignore
                    yield b

            wrapped: AsyncGenerator[FileMetadata, Any] = wrap_iter_to_async(_gen())
        else:
            wrapped: AsyncGenerator[FileMetadata, Any] = batch

        # Пути, уже взятые в этот проход. Очередь переживает прерванный проход
        # (сбой, нехватка места; почтовая — ещё и рестарт), и следующий
        # preindex кладёт тот же путь ещё раз. Повтор отбрасываем до
        # разбора — тогда чанки пути приходят в проход одним непрерывным
        # куском, и чистка перед первой записью пути (ниже) не может задеть
        # строки, записанные этим же проходом. Файл, изменившийся между
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
                        self.counter.count(meta, mode="sub")
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
                # Прежде здесь в processed попадал КАЖДЫЙ файл батча, и после
                # первой же записи он считался проиндексированным. Файл, чанки
                # которого попали в два батча записи, помечался готовым после
                # первого: обрыв между батчами оставлял в индексе половину
                # файла с отметкой «сделан», и полный проход к нему больше не
                # возвращался. Отмечаем только после появления чанка
                # следующего файла — тогда все чанки предыдущего уже записаны.
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
                await self.lsm_collection.indexed.async_set(meta.path, meta)
            else:
                failed.add(meta.path)
            if count:
                self.counter.count(meta, mode="sub")

        chunks = async_embed_documents_into_batches(
            async_parse_files(
                _once_per_path(wrapped),
                fail_cb=_fail_cb,
                with_emails=store is self.email_store,
            ),
            embedder=partial(
                self.cfg.ov_model.async_encode, task="document", role="index"
            ),
            batch_size=self.cfg.embed_model_cfg.batch_size,
            callback=_callback,
        )
        # Пути, для которых уже удалили прежние чанки в этом проходе.
        purged: set[str] = set()
        # Порог 1 ГиБ проверяется один раз перед проходом (_scan_all), а сам
        # проход по домашнему каталогу идёт часами — за это время диск успевает
        # кончиться. Проверяем и по ходу, но не на каждый батч: disk_usage —
        # системный вызов, а батчей на большом корпусе тысячи.
        batches_written = 0
        # Проход оборван по месту: хвост последнего файла мог не дойти до записи.
        interrupted = False
        # На пустой таблице чистить нечего: прежних чанков нет, а повтор пути
        # в проход не попадает (_once_per_path). Иначе на холодном скане шёл
        # бы холостой DELETE на каждый батч — тысячи лишних коммитов.
        try:
            table_was_empty = await store.count_rows() == 0
        except Exception:
            table_was_empty = False
        async for _batch in async_merge_batches(
            chunks, target_size=self.cfg.parquet_record_batch_size
        ):
            # Прежние чанки пути чистим один раз — перед его первой вставкой
            # в этом проходе, иначе в индексе останется старое содержимое
            # файла. Повторно — нельзя: файл длиннее батча записи продолжается
            # в следующем батче, и чистка снесла бы его записанное начало.
            # Отметка «дочитан» тут не помощник: колбэк ставит её, едва
            # эмбеддер увидел чанк следующего файла, то есть в том же батче
            # записи, где лежит хвост дочитанного (замер 2026-09-26: из
            # 30 777 чанков в таблице оставалось 14 281).
            if not table_was_empty:
                await self._purge_stale_chunks(_batch, purged, store)
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
        # Последний файл потока: чанка следующего файла не будет, а цикл выше
        # дочитал поток до конца — значит всё, что он дал, уже записано. После
        # обрыва по месту так сказать нельзя: оставшиеся чанки файла ещё в
        # конвейере. Прежде его отмечали и тогда, и файл навсегда оставался
        # в индексе наполовину; теперь следующий проход перепишет его целиком.
        if (
            pending_meta is not None
            and not interrupted
            and pending_meta.path not in failed
        ):
            await self.lsm_collection.indexed.async_set(
                pending_meta.path, pending_meta
            )

    async def _purge_stale_chunks(
        self, batch, purged: set[str], store: LanceWrapper
    ) -> None:
        """Удалить прежние чанки для путей, впервые встреченных в этом проходе.

        Удаление идёт одним фильтром на батч, а не запросом на путь: каждый
        `delete` — отдельная транзакция с новой версией таблицы.
        """
        try:
            paths = {p for p in batch.column("path").to_pylist() if p}
        except Exception:
            return
        fresh = sorted(paths - purged)
        if not fresh:
            return
        purged.update(fresh)
        predicate = "path IN ({})".format(", ".join(f"'{sql_quote(p)}'" for p in fresh))
        try:
            await store.delete_where(predicate)
        except Exception:
            lg.exception("failed to purge stale chunks for %s path(s)", len(fresh))

    def _pin_name_codes(
        self,
        store: LanceWrapper,
        table,
        codes: list[str],
        frame: "pl.DataFrame | None",
        pinned: list[str],
    ) -> "pl.DataFrame | None":
        """Найти документы по коду из имени и дочитать тех, кого нет в пуле.

        Сбой здесь поиск не роняет: код из имени — надбавка к выдаче, и без
        неё ответ остаётся тем же, что был до неё.
        """
        try:
            index = self._name_indexes.setdefault(store.table_name, NameIndex())
            paths = index.hits(table, codes)
            if not paths:
                return frame
            have = set(frame["path"].to_list()) if frame is not None else set()
            missing = [p for p in paths if p not in have]
            if missing:
                import polars as pl

                extra = pinned_rows(table, missing)
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
        pinned.extend(paths)
        return frame

    async def search(
        self,
        query: str,
        top_k: int = 10,
        query_type: Literal["hybrid", "vector", "text"] = "hybrid",
        sources: Sequence[SearchSource] = (FILES_SOURCE,),
        pinned: list[str] | None = None,
    ) -> "list[pl.DataFrame | None]":
        """Искать в каждом запрошенном источнике отдельно, со своим top_k.

        top_k — число ДОКУМЕНТОВ на источник, а не чанков: из таблицы берётся
        пул чанков `search_pool(top_k)` на канал, а документы из него собирает
        SearchResponse.from_dataframes. `.limit()` работает по таблице, поэтому
        общий запрос отдавал бы почте места файлов. Возвращаем кадр на
        источник, а не склейку: схемы расходятся, если по одной из таблиц
        гибридный поиск деградировал до векторного.

        pinned — список, в который дописываются пути документов, найденных по
        коду из имени файла (src/indexer/name_codes.py); их строки, если в
        пуле их нет, дочитываются в кадр источника. Выдача ставит эти
        документы первыми (SearchResponse.from_dataframes, тот же параметр).
        None — код из имени не ищется вовсе.
        """
        codes = query_codes(query) if pinned is not None else []
        vec = (await self.cfg.ov_model.async_encode([query], role="search"))[0]
        stores = [
            self.email_store if source == EMAIL_SOURCE else self.files_store
            for source in dict.fromkeys(sources)
        ]
        pool = search_pool(top_k)

        def _vector_search(table):
            return (
                table.search(
                    vec,
                    query_type="vector",
                    vector_column_name="vector",
                )
                .nprobes(VECTOR_NPROBES)  # type: ignore
                # .refine_factor(20)
                .distance_type("cosine")  # type: ignore
                .limit(pool)
                .to_polars()
            )

        def _search_in(table):
            if query_type == "hybrid":
                try:
                    return (
                        table.search(
                            # vec,
                            query_type="hybrid",
                            vector_column_name="vector",
                            # FTS-часть — только по тексту чанка. Строковый
                            # запрос гибрида без колонок LanceDB ищет по ВСЕМ
                            # полям с FTS-индексом: появись второй индекс
                            # (заголовок, раздел), он молча вошёл бы в RRF.
                            fts_columns="text",
                        )
                        .nprobes(VECTOR_NPROBES)  # type: ignore
                        .distance_type("cosine")  # type: ignore
                        .vector(vec)
                        .text(query)
                        # Лимит гибрида — и пул каждого канала до RRF, и
                        # длина слитой выдачи.
                        .limit(pool)
                        .to_polars()
                    )
                except Exception as exc:
                    # FTS-индекс может быть ещё не построен (свежая база,
                    # индексация только идёт). Это не повод отдавать 500 —
                    # деградируем до чисто векторного поиска.
                    #
                    # Деградируем на ЛЮБОЙ ошибке гибридного запроса, а не
                    # только на распознанной: векторный индекс у пользователя
                    # рабочий, и выдача по нему всегда лучше пятисотки. Но
                    # незнакомую ошибку пишем на ERROR со стеком — иначе
                    # настоящая поломка растворилась бы в фоне под видом
                    # штатной деградации.
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
                        table.search(
                            query,
                            query_type="fts",
                            # Как в гибриде: только текст чанка.
                            fts_columns="text",
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
            # Один поток на запрос независимо от числа источников: поиск ходит
            # в дефолтный пул на asyncio_num_workers воркеров, и поток на
            # источник вдвое резал бы число одновременных запросов.
            results = []
            for store in stores:
                if not store.has_table:
                    # Таблицы может не быть — почту ещё не включали. Обращение
                    # к store.table её бы СОЗДАЛО, то есть поиск начал бы
                    # писать в хранилище.
                    lg.debug("таблица %s ещё не создана", store.table_name)
                    continue
                # Копии одного файла отдельного запроса с запасом больше не
                # требуют: пул и так в несколько раз глубже выдачи, а
                # SearchResponse схлопывает копии до отбора документов.
                table = store.table
                frame = _search_in(table)
                if codes:
                    frame = self._pin_name_codes(store, table, codes, frame, pinned)
                results.append(frame)
            return results

        return await asyncio.to_thread(_search_all)
