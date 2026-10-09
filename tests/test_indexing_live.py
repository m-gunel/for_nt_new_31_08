"""Индексация «на лету»: файл, добавленный во время активной индексации,
находится поиском, а удалённый — исчезает из выдачи.
"""

import asyncio
import concurrent.futures as cf
import os

import numpy as np
import pyarrow as pa
import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.common import FileMetadata
from src.common.doc_meta import doc_meta_schema
from src.config import Config
from src.indexer.indexer import Index
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.utils.lance_wrapper import LanceWrapper

EMBED_DIM = 8


def _chunk_table(path: str, text: str, dim: int = EMBED_DIM) -> pa.Table:
    """Одна строка в схеме таблицы файлов."""
    return pa.table(
        {
            "idx": pa.array([0], pa.int64()),
            "text": pa.array([text], pa.string()),
            "meta": pa.array([None], pa.struct(doc_meta_schema)),
            "path": pa.array([path], pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(
                np.zeros(dim, dtype=np.float32), dim
            ),
        }
    )


def test_token_len_is_thread_safe(config: Config):
    """Сплиттер считает токены из нескольких потоков парсинга.

    Общий InferRequest на модель даёт «RuntimeError: Infer Request is busy»,
    поэтому `_ov_token_len` идёт через count_tokens со своим InferRequest на
    поток.
    """
    text = "Проверка потокобезопасности подсчёта токенов. " * 20
    assert config._ov_token_len(text) > 0  # прогрев, модель грузится лениво

    errors: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            for _ in range(20):
                config._ov_token_len(f"{worker} {text}")
        except BaseException as exc:  # noqa: BLE001 — тест ловит любой сбой
            errors.append(exc)

    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(work, range(8)))

    assert errors == []


async def _fail_calls(path) -> list[bool]:
    from src.parsers import _process_path

    stat = path.stat()
    meta = FileMetadata(str(path), stat.st_size, int(stat.st_mtime))
    calls: list[bool] = []

    async def fail_cb(_meta: FileMetadata, *, permanent: bool) -> None:
        calls.append(permanent)

    async for _ in _process_path(meta, fail_cb=fail_cb):
        pass
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("сбой инференса"), OSError("файл занят")])
async def test_transient_parse_error_does_not_mark_indexed(tmp_path, monkeypatch, error):
    """Временный или непредвиденный сбой разбора не помечает файл обработанным.

    Иначе повторная попытка не случится никогда — даже после перезапуска
    демона, потому что размер и mtime не изменились.
    """
    from src.parsers import DocxParser

    def fail(self, file):
        raise error

    monkeypatch.setattr(DocxParser, "parse_io", fail)
    doc = tmp_path / "doc.docx"
    doc.write_bytes(b"PK\x03\x04")
    assert await _fail_calls(doc) == [False], "сбой разбора не должен считаться окончательным"


@pytest.mark.asyncio
async def test_corrupt_file_is_skipped_with_one_warning_line(tmp_path, caplog):
    """Испорченное содержимое — окончательный пропуск до изменения файла (дописанный
    файл сменит размер и время и вернётся сам) и одна строка WARNING без стека."""
    broken = tmp_path / "broken.docx"
    broken.write_bytes("это не docx, а мусор".encode())
    with caplog.at_level("WARNING"):
        assert await _fail_calls(broken) == [True]
    lines = [r for r in caplog.records if str(broken) in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == "WARNING" and lines[0].exc_info is None
    assert all(r.exc_info is None for r in caplog.records), "в журнале не должно быть стека"


@pytest.mark.asyncio
async def test_empty_file_is_marked_indexed(tmp_path):
    """Успешно разобранный пустой файл помечается обработанным."""
    from src.parsers import _process_path

    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    stat = empty.stat()
    meta = FileMetadata(str(empty), stat.st_size, int(stat.st_mtime))

    calls: list[bool] = []

    async def fail_cb(_meta: FileMetadata, *, permanent: bool) -> None:
        calls.append(permanent)

    async for _ in _process_path(meta, fail_cb=fail_cb):
        pass

    assert calls == [True]


def test_token_len_cache_keeps_chunking_identical(config: Config):
    """Кэш подсчёта токенов не должен менять границы чанков.

    Сплиттер меряет длину нейросетевым токенизатором и переспрашивает одни и те
    же фрагменты сотни раз. Кэш убирает повторы, но разбиение, а с ним и
    выдача, меняться не должны.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    reference = RecursiveCharacterTextSplitter(
        length_function=config._ov_token_len,
        **config.embed_splitter_cfg.model_dump(),
    )
    for text in (
        "Отчёт о работе. " * 5,
        "Протокол совещания рабочей группы утверждён руководством. " * 80,
        "",
        "а" * 3000,
    ):
        assert config.embed_splitter.split_text(text) == reference.split_text(text)


def test_search_limit_is_bounded():
    """limit задаёт пул LanceDB — без границы можно запросить что угодно."""
    from pydantic import ValidationError

    from src.server.model import SearchRequest

    assert SearchRequest(query="q").limit == 10
    assert SearchRequest(query="q", limit=500).limit == 500
    for bad in (0, -1, 1_000_000):
        with pytest.raises(ValidationError):
            SearchRequest(query="q", limit=bad)


def test_pq_sub_vectors_divide_embedding_dim():
    """num_sub_vectors обязан делить размерность нацело, иначе индекс не строится."""
    from src.utils.lance_wrapper import pq_num_sub_vectors

    for dim in (312, 384, 768, 1024):
        n = pq_num_sub_vectors(dim)
        assert dim % n == 0, f"{dim} не делится на {n}"
    # для rubert-tiny2 — 39, как в уже построенных индексах
    assert pq_num_sub_vectors(312) == 39


@pytest.mark.asyncio
async def test_search_sees_async_writes(tmp_path):
    """Синхронный хендл обязан видеть записи через async-соединение.

    Таблицы Lance версионированы, а кэшированный хендл держит снимок: без
    checkout_latest поиск отвечал бы по состоянию на момент старта демона.
    """
    wrapper = LanceWrapper(
        ld_db_path=str(tmp_path / "lance"),
        embed_dim=EMBED_DIM,
        table_name="files",
    )
    assert wrapper.table.count_rows() == 0  # хендл открыт и закэширован

    await wrapper.add_batch(_chunk_table("/tmp/новый.txt", "свежий документ"))
    assert wrapper.table.count_rows() == 1, "добавление не видно поиску"

    await wrapper.delete_where("path = '/tmp/новый.txt'")
    assert wrapper.table.count_rows() == 0, "удаление не видно поиску"


class _RecordingWatcher(AbstractFilesWatcher):
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.upserted: list[str] = []
        self.deleted_dirs: list[str] = []

    async def on_delete(self, file: FileMetadata) -> None:
        self.deleted.append(file.path)

    async def on_delete_dir(self, path: str) -> None:
        self.deleted_dirs.append(path)

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        self.upserted.extend(f.path for f in files)


@pytest.mark.asyncio
async def test_watcher_decides_by_existence_not_event_order(tmp_path):
    """Судьбу пути определяет файловая система, а не тип последнего события.

    Порядок и склейка событий различаются между FSEvents, inotify и Windows;
    `deleted` не должен теряться, если следом по тому же пути приходит `modified`.
    """
    alive = tmp_path / "alive.txt"
    alive.write_text("на месте", encoding="utf-8")
    gone = tmp_path / "gone.txt"

    watcher = _RecordingWatcher()
    await watcher._handle_files({str(alive), str(gone)})

    assert watcher.deleted == [str(gone)]
    assert watcher.upserted == [str(alive)]


@pytest.mark.asyncio
async def test_watcher_removes_vanished_directory(tmp_path):
    """Исчезнувший каталог снимается с индекса целиком.

    Перемещение папки в корзину — один rename, событий по вложенным файлам
    может не прийти вовсе.
    """
    present = tmp_path / "present"
    present.mkdir()
    vanished = tmp_path / "vanished"

    watcher = _RecordingWatcher()
    await watcher._handle_missing_dirs({str(present), str(vanished)})

    assert watcher.deleted_dirs == [str(vanished)]


@fixture
def isolated_config(config: Config, tmp_path, monkeypatch):
    """Конфиг, у которого хранилище указывает во временный каталог."""
    monkeypatch.setattr(config, "lsm_root", str(tmp_path))
    monkeypatch.setattr(config, "lsm_path", str(tmp_path / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(tmp_path / "lance"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config.embed_model_cfg, "batch_size", 2)
    # cached_property держит соединения предыдущего теста
    config.__dict__.pop("lance", None)
    yield config
    config.__dict__.pop("lance", None)


@pa_fixture.fixture
async def index(isolated_config: Config):
    idx = Index(isolated_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()


@pytest.mark.asyncio
async def test_refresh_survives_main_index(index: Index):
    """Пересбор индексов не теряется, если идёт полный проход.

    Пометку в `_dirty_stores` нельзя снимать до сна: на ветке `main_index`
    цикл завершится, и изменения от watcher'а не попадут в индекс.
    """
    built = 0

    async def fake_create_index(store=None, force_fts=False, force_vector=False):
        nonlocal built
        built += 1

    index.create_index = fake_create_index  # type: ignore[method-assign]

    index.main_index = True
    await index.ensure_index()
    await asyncio.sleep(0.2)
    assert built == 0, "во время полного прохода индекс пересобирать не нужно"
    assert index._dirty_stores, "но запрос на пересбор терять нельзя"

    index.main_index = False
    await asyncio.sleep(0.2)
    assert built >= 1, "после полного прохода пересбор должен состояться"


@pytest.mark.asyncio
async def test_compaction_never_runs_during_indexing(index: Index, monkeypatch):
    """Полная компакция не должна запускаться во время активной индексации.

    Она склеивает фрагменты и чистит версии — дорого по CPU и памяти на
    рабочей станции пользователя, поэтому только в простое. Дешёвое
    дообновление индексов идёт отдельным путём.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        index.cfg.lance.files, "compact", lambda: calls.append("compact")
    )
    monkeypatch.setattr(index.cfg, "index_optimize_interval", 0.0)

    index.main_index = True
    index._maybe_compact(index.files_store)
    assert calls == [], "во время полного прохода компакции быть не должно"

    index.main_index = False
    index.watch_pending = 5
    index._maybe_compact(index.files_store)
    assert calls == [], "пока watcher разбирает пачку — тоже не должно"

    index.watch_pending = 0
    index._maybe_compact(index.files_store)
    assert calls == ["compact"], "в простое компакция обязана состояться"


@pytest.mark.asyncio
async def test_reconcile_removes_stale(index: Index, tmp_path):
    """Файл, удалённый мимо watcher'а, вычищается сверкой."""
    alive = tmp_path / "alive.txt"
    alive.write_text("на месте", encoding="utf-8")
    gone = str(tmp_path / "gone.txt")

    for path in (str(alive), gone):
        stat_size = 10
        await index.lsm_collection.indexed.async_set(
            path, FileMetadata(path, stat_size, 1)
        )
        await index.cfg.lance.files.add_batch(
            _chunk_table(path, "содержимое", index.cfg.embed_dim)
        )
    # у писем файла на диске нет — сверка не должна их трогать
    await index.lsm_collection.indexed.async_set(
        "email_INBOX:0:10", FileMetadata("email_INBOX:0:10", 10, 1)
    )

    removed = await index.reconcile_deleted()

    assert removed == 1
    paths = index.cfg.lance.files.table.search().limit(100).to_list()
    assert {row["path"] for row in paths} == {str(alive)}
    assert await index.lsm_collection.indexed.async_get(gone) is None
    assert await index.lsm_collection.indexed.async_get("email_INBOX:0:10") is not None
    assert not os.path.exists(gone)


@pytest.mark.asyncio
async def test_reconcile_removes_file_grown_past_max_size(
    index: Index, tmp_path, monkeypatch
):
    """Файл, выросший сверх max_size, снимается с индекса сверкой.

    Обход и watcher отсеивают его до очереди (filter_meta), и прежние строки
    иначе остались бы в поиске навсегда: файл на месте, а LSM с диском уже не
    сверяется.
    """
    from src.indexer.utils import filter_meta

    monkeypatch.setattr(index.cfg.file_scan_cfg, "max_size", 16)
    monkeypatch.setattr(index.cfg.file_scan_cfg, "filter_meta", filter_meta)
    grown = tmp_path / "вырос.txt"
    grown.write_text("строка " * 10, encoding="utf-8")
    small = tmp_path / "мал.txt"
    small.write_text("коротко", encoding="utf-8")
    for path in (grown, small):
        # В LSM — размер, с которым файл индексировали (он был в лимите).
        await index.lsm_collection.indexed.async_set(
            str(path), FileMetadata(str(path), 8, 1)
        )
        await index.cfg.lance.files.add_batch(
            _chunk_table(str(path), "содержимое", index.cfg.embed_dim)
        )

    removed = await index.reconcile_deleted()

    assert removed == 1
    rows = index.cfg.lance.files.table.search().limit(100).to_list()
    assert {row["path"] for row in rows} == {str(small)}
    assert await index.lsm_collection.indexed.async_get(str(grown)) is None
    assert await index.lsm_collection.indexed.async_get(str(small)) is not None
