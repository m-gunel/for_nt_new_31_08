"""Фоновые проходы индексатора и момент отметки файла в LSM."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest
import pytest_asyncio

from src.common import FileMetadata, chunk_with_meta_schema
from src.config import Config
from src.indexer import indexer as indexer_module
from src.indexer.indexer import Index
from src.indexer.utils import UniqueCounter
from src.utils.lance_wrapper import LanceWrapper

# Интервал периодического прохода: логика цикла от его величины не зависит.
_TICK = 0.01


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """Дождаться условия, опрашивая его. True — дождались."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_TICK)
    return bool(predicate())


class _Recorder:
    """Журнал событий записи: важен порядок — отметка в LSM идёт после записи
    всех чанков файла, а не между батчами."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def kinds(self, *kinds: str) -> list[tuple]:
        return [e for e in self.events if e[0] in kinds]


class _FakeStore:
    """LanceWrapper ровно в том объёме, в каком его трогает process_stream."""

    def __init__(self, recorder: _Recorder, rows: int = 0) -> None:
        self._recorder = recorder
        self._rows = rows
        self.table_name = "files"

    async def count_rows(self) -> int:
        return self._rows

    async def add_batch(self, data) -> None:
        paths = tuple(data.column("path").to_pylist())
        self._recorder.events.append(("add", paths))

    async def delete_where(self, predicate: str) -> None:
        self._recorder.events.append(("delete", predicate))


class _FakeIndexed:
    """Таблица состояния LSM: важны момент отметки и то, что уже отмечено."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        # Пути, проиндексированные раньше (их задаёт тест) и в этом проходе.
        self.known: set[str] = set()

    async def async_set(self, path: str, meta: FileMetadata) -> None:
        self._recorder.events.append(("set", path))
        self.known.add(path)

    def is_in(self, path: str) -> bool:
        return path in self.known

    async def async_is_in(self, path: str) -> bool:
        return self.is_in(path)


class _FakeEmbedModel:
    """Модель эмбеддингов без OpenVINO: важна только размерность выдачи."""

    dim = 4

    async def async_encode(self, texts, task: str = "query", role: str = "search"):
        return np.zeros((len(texts), self.dim), dtype=np.float32)


class _StubIndex:
    """Настоящие методы Index на лёгком носителе без LSM, LanceDB и модели.

    Методы взяты у класса, а не скопированы: тест ломается вместе с кодом.
    """

    scan_all = Index.scan_all
    start_periodic_rescan = Index.start_periodic_rescan
    stop_periodic_rescan = Index.stop_periodic_rescan
    run_periodic_rescan = Index.run_periodic_rescan
    process_stream = Index.process_stream
    _purge_stale_chunks = Index._purge_stale_chunks
    _storage_root = Index._storage_root
    activate = Index.activate
    deactivate = Index.deactivate

    def __init__(
        self,
        cfg: Config,
        store: _FakeStore | None = None,
        recorder: _Recorder | None = None,
    ) -> None:
        self.cfg = cfg
        self.recorder = recorder or _Recorder()
        # Признаки простоя для периодического прохода.
        self.active = True
        self.main_index = False
        self.preindex = False
        self.watch_pending = 0
        self.rescan_task: asyncio.Task | None = None
        # Полный проход и его доигрывание.
        self._scan_lock = asyncio.Lock()
        self._rescan_requested = False
        self.scan_calls: list[bool] = []
        # Запись чанков.
        self.counter = UniqueCounter()
        self.lsm_collection = SimpleNamespace(indexed=_FakeIndexed(self.recorder))
        self.files_store = store
        # Отдельный объект: `store is self.email_store` решает, открывать ли mbox.
        self.email_store = object()
        # Что трогают activate/deactivate.
        self.calls: list[str] = []
        self.emails_task: asyncio.Task | None = None
        self._dirty_stores: set[str] = set()
        self._index_task: asyncio.Task | None = None

    # Заглушки тяжёлых частей.
    async def _scan_all(self, emails_only: bool = False) -> None:
        self.scan_calls.append(emails_only)

    async def start_reindex(self) -> None:
        self.calls.append("start_reindex")

    async def stop_index(self) -> None:
        self.calls.append("stop_index")

    async def _stop_index_refresh(self) -> None:
        self.calls.append("stop_index_refresh")

    async def start_emails_polling(self, immediate: bool = False) -> None:
        self.calls.append("start_emails_polling")


@pytest_asyncio.fixture
async def stub(config: Config, monkeypatch):
    """Носитель методов Index с крошечным интервалом периодического прохода."""
    monkeypatch.setattr(config, "full_rescan_interval", _TICK)
    idx = _StubIndex(config)
    try:
        yield idx
    finally:
        # Фоновая задача не должна пережить тест, чем бы он ни кончился.
        await idx.stop_periodic_rescan()


def _paths_table(*paths: str) -> pa.Table:
    """Батч записи в том виде, в каком его видит _purge_stale_chunks."""
    return pa.table({"path": pa.array(list(paths), pa.string())})


def _install_chunk_pipeline(
    config: Config,
    monkeypatch,
    chunks_by_path: dict[str, int],
    embed_batch: int = 1,
    write_batch: int = 2,
    fail_after: dict[str, int] | None = None,
    no_text: frozenset[str] = frozenset(),
) -> None:
    """Предсказуемый конвейер разбора вместо парсера, модели и OpenVINO.

    По умолчанию батч эмбеддингов — один чанк, батч записи — две строки:
    только так видно поведение на границе батчей записи.

    `fail_after` — сколько чанков пути отдать, прежде чем разбор упадёт.
    `no_text` — пути, разбор которых прошёл без текста (пустой, битый или
    зашифрованный файл): чанков нет, исход окончательный.
    """
    fail_after = fail_after or {}
    monkeypatch.setattr(config.embed_model_cfg, "batch_size", embed_batch)
    monkeypatch.setattr(config, "parquet_record_batch_size", write_batch)
    monkeypatch.setattr(config, "index_throttle_seconds", 0.0)
    # Модель эмбеддингов кладём в кеш cached_property, иначе process_stream
    # поднимет настоящий OpenVINO ради partial().
    monkeypatch.setitem(config.__dict__, "ov_model", _FakeEmbedModel())

    async def fake_parse(files, *, fail_cb, with_emails: bool = False):
        # Как настоящий async_parse_files: файлы строго по одному, все чанки
        # файла подряд — именно на этом держится определение «дочитан».
        async for meta in files:
            if meta.path in no_text:
                await fail_cb(meta, permanent=True)
                continue
            for i in range(fail_after.get(meta.path, chunks_by_path[meta.path])):
                yield {
                    "idx": i,
                    "text": f"{meta.path} фрагмент {i}",
                    "meta": None,
                    "path": meta.path,
                    "file_meta": meta,
                }
            if meta.path in fail_after:
                # Как _process_path: исключение парсера гасится, файл уходит
                # в fail_cb как временный сбой, а отданные чанки — дальше.
                await fail_cb(meta, permanent=False)

    monkeypatch.setattr(indexer_module, "async_parse_files", fake_parse)


def test_full_rescan_interval_defaults_to_an_hour(config: Config):
    """Периодический полный проход по умолчанию — раз в час.

    Он сводит индекс с диском, когда события ФС потеряны: переполнение очереди,
    каталоги, ставшие читаемыми, осиротевшие после перемещений пути.
    """
    assert config.full_rescan_interval == 3600.0


@pytest.mark.parametrize("interval", [0.0, -1.0], ids=["zero", "negative"])
@pytest.mark.asyncio
async def test_periodic_rescan_is_disabled_by_non_positive_interval(
    stub: _StubIndex, config: Config, monkeypatch, interval: float
):
    """Непозитивный интервал выключает проход, а не означает «каждый тик».

    Условие в коде — `<= 0`: при `== 0` отрицательное значение из YAML или
    RAG_FULL_RESCAN_INTERVAL дало бы `asyncio.sleep(-1)` — обход домашнего
    каталога без пауз.
    """
    monkeypatch.setattr(config, "full_rescan_interval", interval)

    await stub.start_periodic_rescan()

    assert stub.rescan_task is None, "задача поднята при выключенном проходе"


@pytest.mark.asyncio
async def test_periodic_rescan_runs_when_indexer_is_idle(stub: _StubIndex):
    """В простое проход обязан состояться — и именно полный, а не почтовый."""
    calls: list[bool] = []

    async def fake_scan_all(emails_only: bool = False) -> None:
        calls.append(emails_only)

    stub.scan_all = fake_scan_all  # type: ignore[method-assign]

    await stub.start_periodic_rescan()
    assert stub.rescan_task is not None, "задача периодического прохода не создана"

    assert await _wait_until(lambda: bool(calls)), "проход в простое не состоялся"
    assert all(only is False for only in calls), (
        "по расписанию нужен полный проход: почтовый и так повторяет поллинг"
    )


@pytest.mark.parametrize(
    ("attr", "busy", "idle"),
    [
        ("main_index", True, False),
        ("preindex", True, False),
        ("watch_pending", 3, 0),
        ("active", False, True),
    ],
    ids=["main_index", "preindex", "watch_pending", "deactivated"],
)
@pytest.mark.asyncio
async def test_periodic_rescan_skipped_while_indexer_is_busy(
    stub: _StubIndex, attr: str, busy, idle
):
    """Занятый или выключенный индексатор проход не начинает.

    Полный обход идёт часами: поверх работающей индексации он удваивает
    нагрузку, а на деактивированном индексе нарушает БТ §5.2. Вторая половина
    теста проверяет, что задача при этом жива.
    """
    calls: list[bool] = []

    async def fake_scan_all(emails_only: bool = False) -> None:
        calls.append(emails_only)

    stub.scan_all = fake_scan_all  # type: ignore[method-assign]
    setattr(stub, attr, busy)

    await stub.start_periodic_rescan()
    await asyncio.sleep(_TICK * 15)

    assert calls == [], f"проход пошёл, хотя {attr}={busy!r}"

    setattr(stub, attr, idle)
    assert await _wait_until(lambda: bool(calls)), (
        "задача пропустила проход и больше не проснулась"
    )


@pytest.mark.asyncio
async def test_periodic_rescan_survives_failing_pass(stub: _StubIndex):
    """Сбой одного прохода не убивает задачу: следующий тик снова работает.

    Задача — единственная страховка поверх watcher'а: без неё после первой
    ошибки индекс больше не сверялся бы с диском.
    """
    calls: list[bool] = []

    async def flaky(emails_only: bool = False) -> None:
        calls.append(emails_only)
        if len(calls) == 1:
            raise RuntimeError("проход упал")

    stub.scan_all = flaky  # type: ignore[method-assign]

    await stub.start_periodic_rescan()

    assert await _wait_until(lambda: len(calls) >= 2), (
        "после упавшего прохода задача не сделала следующий"
    )
    assert stub.rescan_task is not None and not stub.rescan_task.done()


@pytest.mark.asyncio
async def test_stop_periodic_rescan_cancels_and_clears_task(stub: _StubIndex):
    """Остановка гасит задачу и обнуляет ссылку.

    Иначе start_periodic_rescan не поднял бы задачу заново по мёртвой ссылке,
    а lifespan на выходе ждал бы отменённую.
    """
    await stub.start_periodic_rescan()
    task = stub.rescan_task
    assert task is not None

    await stub.stop_periodic_rescan()

    assert task.done(), "задача пережила остановку"
    assert stub.rescan_task is None, "ссылка на погашенную задачу осталась"
    # Повторная остановка — тихий no-op, а не AttributeError.
    await stub.stop_periodic_rescan()


@pytest.mark.asyncio
async def test_activate_starts_rescan_and_deactivate_stops_it(stub: _StubIndex):
    """Периодический проход гаснет и поднимается вместе с индексацией (БТ §5.2).

    Проверяется переход «выключено → включено»: на активном индексе
    activate() идемпотентен.
    """
    stub.active = False
    assert stub.rescan_task is None

    assert await stub.activate() is True, "переход «выключено → включено» не замечен"

    assert stub.active is True
    task = stub.rescan_task
    assert task is not None and not task.done(), "активация не подняла проход"
    # Активация поднимает и саму индексацию, а не только проход.
    assert stub.calls == ["start_reindex", "start_emails_polling"], (
        f"активация подняла проход, но не индексацию: {stub.calls}"
    )

    assert await stub.deactivate() is True

    assert stub.active is False
    assert stub.rescan_task is None, "деактивация не погасила проход"
    assert task.done()


@pytest.mark.asyncio
async def test_failed_pass_still_replays_requested_rescan(stub: _StubIndex):
    """Упавший проход не съедает отложенный полный проход.

    `@wrap_exception_logger` пробрасывает исключение, и без try/except вокруг
    `_scan_all` до доигрывания не дойти. Саму ошибку не глотаем: она
    поднимается после доигрывания.
    """
    boom = RuntimeError("проход упал на середине")
    calls: list[bool] = []

    async def flaky(emails_only: bool = False) -> None:
        calls.append(emails_only)
        if len(calls) == 1:
            # Запрос на полный проход пришёл, пока текущий ещё шёл.
            stub._rescan_requested = True
            raise boom

    stub._scan_all = flaky  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as excinfo:
        await stub.scan_all()

    assert calls == [False, False], (
        "отложенный полный проход потерян: падение текущего его съело"
    )
    assert excinfo.value is boom, "исходная ошибка прохода подменена или потеряна"
    assert stub._rescan_requested is False, "флаг доигрывания не снят"
    assert not stub._scan_lock.locked(), "лок прохода не отпущен после ошибки"


@pytest.mark.asyncio
async def test_cancelled_pass_does_not_replay_rescan(stub: _StubIndex):
    """Отмена прохода доигрывание не запускает.

    Фиксирует форму обработчика: узкий `except Exception` плюс
    `except asyncio.CancelledError: raise`. С `except BaseException` отмена
    (stop_index на POST /config и /deactivate) стала бы обычной ошибкой, и
    отменённая задача пошла бы доигрывать полный проход.
    """
    calls: list[bool] = []

    async def cancelled(emails_only: bool = False) -> None:
        calls.append(emails_only)
        raise asyncio.CancelledError

    stub._scan_all = cancelled  # type: ignore[method-assign]
    stub._rescan_requested = True

    with pytest.raises(asyncio.CancelledError):
        await stub.scan_all()

    assert calls == [False], "отменённый проход всё-таки доигрывали"
    assert stub._rescan_requested is True, (
        "запрос на полный проход съеден отменой — после активации его никто "
        "не повторит"
    )


@pytest.mark.asyncio
async def test_file_split_across_write_batches_is_marked_after_last_batch(
    config: Config, monkeypatch
):
    """Файл из двух батчей записи отмечается в LSM только после второго.

    Иначе обрыв между батчами оставил бы в индексе половину файла с отметкой
    «сделан», и проходы к нему не возвращались бы.

    Длинный файл даёт три чанка, короткий — один; батч эмбеддингов — один
    чанк, батч записи — две строки. Первая запись — начало длинного файла,
    вторая — его хвост с началом короткого.
    """
    long_file = FileMetadata(path="/дом/длинный.pdf", size=30, mtime_seconds=1)
    short_file = FileMetadata(path="/дом/короткий.pdf", size=10, mtime_seconds=2)
    _install_chunk_pipeline(
        config, monkeypatch, {long_file.path: 3, short_file.path: 1}
    )

    recorder = _Recorder()
    # Таблица НЕ пуста: чистка на холодном скане — предмет отдельных тестов.
    store = _FakeStore(recorder, rows=10)
    idx = _StubIndex(config, store=store, recorder=recorder)

    async def _metas():
        for meta in (long_file, short_file):
            yield meta

    await idx.process_stream(_metas(), store=store)

    marks = recorder.kinds("add", "set")
    assert [kind for kind, _ in marks] == ["add", "add", "set", "set"], (
        f"файл отмечен в LSM между батчами записи: {marks}"
    )
    assert marks[0][1] == (long_file.path, long_file.path), (
        "первый батч записи обязан состоять из одного длинного файла"
    )
    assert marks[1][1] == (long_file.path, short_file.path), (
        "второй батч обязан содержать хвост длинного файла и начало короткого"
    )
    assert marks[2] == ("set", long_file.path)
    # Последний файл потока отмечается явно: чанка следующего файла не будет.
    assert marks[3] == ("set", short_file.path), (
        "последний файл потока не отмечен в LSM"
    )


# Считаем строки в настоящей таблице LanceDB, а не вызовы записи: потеря
# чанков видна только по тому, что осталось после удалений.
_STALE_TEXT = "прежняя версия"


def _lance_store(tmp_path) -> LanceWrapper:
    return LanceWrapper(
        ld_db_path=str(tmp_path / "lance"),
        embed_dim=_FakeEmbedModel.dim,
        table_name="files",
    )


def _stale_rows(rows_by_path: dict[str, int]) -> pa.RecordBatch:
    """Строки прошлого прохода: тёплая таблица, которую есть что чистить."""
    rows = [
        {"idx": i, "text": f"{path} {_STALE_TEXT} {i}", "meta": None, "path": path}
        for path, count in rows_by_path.items()
        for i in range(count)
    ]
    vectors = np.zeros(len(rows) * _FakeEmbedModel.dim, dtype=np.float32)
    batch = pa.RecordBatch.from_pylist(rows, schema=chunk_with_meta_schema)
    return batch.append_column(
        "vector", pa.FixedSizeListArray.from_arrays(vectors, _FakeEmbedModel.dim)
    )


def _texts_by_path(store: LanceWrapper) -> dict[str, list[str]]:
    table = store.table.to_arrow()
    texts: dict[str, list[str]] = {}
    for path, text in zip(
        table.column("path").to_pylist(), table.column("text").to_pylist()
    ):
        texts.setdefault(path, []).append(text)
    return texts


def _spy_deletes(store: LanceWrapper, monkeypatch) -> list[str]:
    deletes: list[str] = []
    real = store.delete_where

    async def spy(predicate: str) -> None:
        deletes.append(predicate)
        await real(predicate)

    monkeypatch.setattr(store, "delete_where", spy)
    return deletes


async def _stream(*metas: FileMetadata):
    for meta in metas:
        yield meta


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.asyncio
async def test_file_split_across_write_batches_keeps_every_chunk(
    config: Config, monkeypatch, tmp_path, warm: bool
):
    """Файл длиннее батча записи ложится в таблицу целиком.

    Батч записи собирается из нескольких батчей эмбеддингов, и к записи хвоста
    файл уже числится дочитанным: хвост нельзя принимать за повторную
    постановку пути и удалять записанное начало.

    Батч эмбеддингов — три чанка, батч записи — четыре строки: граница файлов
    попадает внутрь батча эмбеддингов, а длинные файлы рвутся между батчами
    записи. На тёплой таблице заодно проверено, что прежняя версия файла
    вычищена, а чужие строки целы.
    """
    chunks = {
        "/дом/6406-У.pdf": 11,
        "/дом/4212-У.docx": 7,
        "/дом/отчёт.xlsx": 5,
        "/дом/заметка.txt": 1,
    }
    metas = [
        FileMetadata(path=path, size=count, mtime_seconds=1)
        for path, count in chunks.items()
    ]
    _install_chunk_pipeline(config, monkeypatch, chunks, embed_batch=3, write_batch=4)
    store = _lance_store(tmp_path)
    untouched = "/дом/вне_прохода.pdf"
    expected = dict(chunks)
    if warm:
        await store.add_batch(_stale_rows({metas[0].path: 20, untouched: 2}))
        expected[untouched] = 2
    deletes = _spy_deletes(store, monkeypatch)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]

    await idx.process_stream(_stream(*metas), store=store)

    texts = _texts_by_path(store)
    assert {path: len(rows) for path, rows in texts.items()} == expected, (
        "в таблице не столько строк, сколько чанков выдал парсер"
    )
    assert await store.count_rows() == sum(expected.values())
    stale = [t for path in chunks for t in texts[path] if _STALE_TEXT in t]
    assert stale == [], f"в индексе осталась прежняя версия файла: {stale}"
    if not warm:
        assert deletes == [], (
            "на холодном скане чистить нечего — каждый DELETE здесь холостой "
            f"коммит либо удаление только что записанного: {deletes}"
        )
    # Порядок отметок внутри батча записи не задан: `processed` — множество.
    assert sorted(recorder.kinds("set")) == sorted(("set", m.path) for m in metas)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.asyncio
async def test_path_queued_twice_is_indexed_once(
    config: Config, monkeypatch, tmp_path, warm: bool
):
    """Путь, дважды попавший в очередь прохода, ложится в таблицу один раз.

    Очередь переживает прерванный проход (сбой, нехватка места; почтовая —
    ещё и рестарт), и следующий preindex кладёт тот же путь ещё раз, иногда с
    другим mtime. Повтор отбрасывается до разбора, поэтому удалять записанное
    в этом же проходе не нужно никогда.
    """
    dup = FileMetadata(path="/дом/дважды.pdf", size=50, mtime_seconds=1)
    other = FileMetadata(path="/дом/чужой.pdf", size=20, mtime_seconds=1)
    chunks = {dup.path: 5, other.path: 2}
    _install_chunk_pipeline(config, monkeypatch, chunks, embed_batch=3, write_batch=4)
    store = _lance_store(tmp_path)
    if warm:
        await store.add_batch(_stale_rows({dup.path: 3}))
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]

    await idx.process_stream(
        _stream(dup, other, dup, dup._replace(mtime_seconds=2)), store=store
    )

    texts = _texts_by_path(store)
    assert {path: len(rows) for path, rows in texts.items()} == chunks, (
        f"повторная постановка пути дала дубли или потерю строк: {texts}"
    )
    assert sorted(texts[dup.path]) == sorted(
        f"{dup.path} фрагмент {i}" for i in range(chunks[dup.path])
    )
    assert sorted(recorder.kinds("set")) == [("set", dup.path), ("set", other.path)], (
        "каждый путь отмечается в LSM один раз и с той версией, что записана"
    )


@pytest.mark.asyncio
async def test_purge_cleans_every_new_path_once(config: Config):
    """Чистка тёплого прохода: все новые пути батча одним фильтром, и один раз.

    Повторная чистка пути в том же проходе снесла бы его строки, записанные
    из предыдущего батча записи.
    """
    recorder = _Recorder()
    store = _FakeStore(recorder)
    idx = _StubIndex(config, store=store, recorder=recorder)
    first = "/дом/первый.pdf"
    second = "/дом/второй.pdf"
    purged: set[str] = set()

    await idx._purge_stale_chunks(_paths_table(first, second, first), purged, store)

    deletes = [predicate for kind, predicate in recorder.kinds("delete")]
    assert len(deletes) == 1, "удаление обязано идти одним фильтром на батч"
    assert first in deletes[0] and second in deletes[0]
    assert purged == {first, second}

    recorder.events.clear()
    await idx._purge_stale_chunks(_paths_table(first, second), purged, store)

    assert recorder.kinds("delete") == [], "повторная чистка тех же путей"


@pytest.mark.parametrize("failed_last", [False, True], ids=["mid-stream", "last"])
@pytest.mark.asyncio
async def test_file_failed_mid_parse_is_not_marked_and_redone_next_pass(
    config: Config, monkeypatch, tmp_path, failed_last: bool
):
    """Файл, разбор которого упал посередине, не отмечается готовым.

    `_process_path` гасит исключение парсера и сообщает временный сбой
    (`permanent=False`), но чанки до сбоя уже идут по конвейеру. Отметка в LSM
    оставила бы в индексе только начало файла навсегда. Проверены обе позиции
    упавшего файла.
    """
    broken = FileMetadata(path="/дом/битая_страница.pdf", size=50, mtime_seconds=1)
    whole = FileMetadata(path="/дом/целый.pdf", size=20, mtime_seconds=1)
    chunks = {broken.path: 5, whole.path: 2}
    _install_chunk_pipeline(config, monkeypatch, chunks, fail_after={broken.path: 2})
    store = _lance_store(tmp_path)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]

    order = (whole, broken) if failed_last else (broken, whole)
    await idx.process_stream(_stream(*order), store=store)

    assert recorder.kinds("set") == [("set", whole.path)], (
        "файл с упавшим разбором отмечен готовым — проходы к нему не вернутся"
    )

    # Следующий проход: разбор удался. Начало файла от прошлой попытки
    # вычищается, файл ложится целиком и только теперь отмечается.
    _install_chunk_pipeline(config, monkeypatch, chunks)
    recorder.events.clear()
    await idx.process_stream(_stream(broken), store=store)

    rows = {path: len(texts) for path, texts in _texts_by_path(store).items()}
    assert rows == chunks, f"файл не дописан или задвоен: {rows}"
    assert recorder.kinds("set") == [("set", broken.path)]


@pytest.mark.asyncio
async def test_pass_cut_by_low_disk_leaves_partial_file_unmarked(
    config: Config, monkeypatch, tmp_path
):
    """Обрыв прохода по месту (< 1 ГиБ) не отмечает файл, чей хвост не записан.

    Файл, записанный до обрыва целиком, отмечается: иначе после каждого обрыва
    всё записанное переписывалось бы заново.
    """
    whole = FileMetadata(path="/дом/целый.pdf", size=10, mtime_seconds=1)
    cut = FileMetadata(path="/дом/оборванный.pdf", size=100, mtime_seconds=1)
    chunks = {whole.path: 1, cut.path: 10}
    _install_chunk_pipeline(config, monkeypatch, chunks)
    # Место проверяется после каждого батча записи, и уже первого не хватает.
    monkeypatch.setattr(indexer_module, "_DISK_CHECK_EVERY_BATCHES", 1)
    free = {"bytes": 0}
    monkeypatch.setattr(
        indexer_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=free["bytes"]),
    )
    store = _lance_store(tmp_path)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]

    await idx.process_stream(_stream(whole, cut), store=store)

    rows = {path: len(texts) for path, texts in _texts_by_path(store).items()}
    assert rows[cut.path] < chunks[cut.path], "обрыва не было — проверять нечего"
    assert recorder.kinds("set") == [("set", whole.path)], (
        "недописанный файл отмечен готовым — следующий проход его пропустит"
    )

    # Место освободилось: следующий проход дописывает файл целиком.
    free["bytes"] = 10 * indexer_module.GIB
    recorder.events.clear()
    await idx.process_stream(_stream(cut), store=store)

    rows = {path: len(texts) for path, texts in _texts_by_path(store).items()}
    assert rows == chunks, f"файл не дописан или задвоен: {rows}"
    assert recorder.kinds("set") == [("set", cut.path)]


def _on_disk(path) -> FileMetadata:
    stat = path.stat()
    return FileMetadata(str(path), stat.st_size, int(stat.st_mtime))


@pytest.mark.asyncio
async def test_file_that_lost_its_text_leaves_no_previous_chunks(
    config: Config, monkeypatch, tmp_path
):
    """Файл, ставший пустым, битым или зашифрованным, уходит из поиска.

    Разбор проходит без чанков (`permanent=True`), и файл отмечается в LSM.
    Чистка в цикле записи видит только пути из батча, а у такого файла батча
    нет: без отдельного удаления старый текст находился бы поиском, а отметка
    не пустила бы проход к файлу снова. Парсеры настоящие.
    """
    import pymupdf

    monkeypatch.setitem(config.__dict__, "ov_model", _FakeEmbedModel())
    monkeypatch.setattr(config.embed_model_cfg, "batch_size", 1)
    monkeypatch.setattr(config, "parquet_record_batch_size", 4)
    monkeypatch.setattr(config, "index_throttle_seconds", 0.0)
    empty = tmp_path / "обнулён.txt"
    empty.write_bytes(b"")
    locked = tmp_path / "под_паролем.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "секретный текст")
        doc.save(
            str(locked),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="u",
            owner_pw="o",
        )
    broken = tmp_path / "испорчен.pptx"
    broken.write_bytes(b"PK\x03\x04 not really a zip" * 20)
    files = [empty, locked, broken]
    untouched = "/дом/вне_прохода.pdf"
    store = _lance_store(tmp_path)
    await store.add_batch(_stale_rows({**{str(p): 3 for p in files}, untouched: 2}))
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]
    # Все три файла проиндексированы раньше, когда текст в них был.
    idx.lsm_collection.indexed.known.update(str(p) for p in files)

    await idx.process_stream(_stream(*map(_on_disk, files)), store=store)

    rows = {path: len(texts) for path, texts in _texts_by_path(store).items()}
    assert rows == {untouched: 2}, f"прежняя версия файла осталась в поиске: {rows}"
    assert sorted(recorder.kinds("set")) == sorted(("set", str(p)) for p in files)


@pytest.mark.parametrize(
    "path", ["/дом/новый_пустой.txt", "email_INBOX:0:10"], ids=["new-file", "email"]
)
@pytest.mark.asyncio
async def test_no_text_without_previous_version_costs_no_delete(
    config: Config, monkeypatch, tmp_path, path: str
):
    """Удаление — только для пути, уже лежавшего в индексе, и не для письма.

    Новый пустой файл строк не имеет, а каждый `delete` — коммит с новой
    версией таблицы. У письма ключ в LSM и путь чанка различаются, и удаление
    по префиксу задело бы тело, записанное в этом же проходе.
    """
    meta = FileMetadata(path=path, size=0, mtime_seconds=1)
    _install_chunk_pipeline(config, monkeypatch, {}, no_text=frozenset({path}))
    store = _lance_store(tmp_path)
    await store.add_batch(_stale_rows({"/дом/другой.pdf": 1}))
    deletes = _spy_deletes(store, monkeypatch)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]
    if path.startswith("email_"):
        idx.lsm_collection.indexed.known.add(path)

    await idx.process_stream(_stream(meta), store=store)

    assert deletes == [], f"холостое удаление: {deletes}"
    assert recorder.kinds("set") == [("set", path)]


@pytest.mark.asyncio
async def test_file_whose_previous_version_was_not_dropped_stays_unmarked(
    config: Config, monkeypatch, tmp_path
):
    """Не удалось снять прежнюю версию файла без текста — в LSM не отмечаем.

    Иначе старый текст остался бы в поиске навсегда; без отметки следующий
    проход повторит попытку.
    """
    gone = FileMetadata(path="/дом/зашифрован.docx", size=10, mtime_seconds=2)
    _install_chunk_pipeline(config, monkeypatch, {}, no_text=frozenset({gone.path}))
    store = _lance_store(tmp_path)
    await store.add_batch(_stale_rows({gone.path: 3}))

    async def boom(predicate: str) -> None:
        raise RuntimeError("commit conflict")

    monkeypatch.setattr(store, "delete_where", boom)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]
    idx.lsm_collection.indexed.known.add(gone.path)

    await idx.process_stream(_stream(gone), store=store)

    assert recorder.kinds("set") == [], "файл отмечен, хотя старый текст в поиске"


@pytest.mark.asyncio
async def test_failed_purge_leaves_file_unmarked_and_next_pass_rewrites_it(
    config: Config, monkeypatch, tmp_path
):
    """Сбой чистки прежних чанков не даёт отметить файл в LSM.

    Иначе старая версия лежала бы рядом с новой до следующего изменения
    файла: путь уже в `purged`, а LSM сходится с диском. Без отметки
    следующий проход перепишет файл целиком.
    """
    changed = FileMetadata(path="/дом/изменён.pdf", size=10, mtime_seconds=2)
    _install_chunk_pipeline(config, monkeypatch, {changed.path: 2})
    store = _lance_store(tmp_path)
    await store.add_batch(_stale_rows({changed.path: 3}))
    real_delete = store.delete_where

    async def boom(predicate: str) -> None:
        raise RuntimeError("commit conflict")

    monkeypatch.setattr(store, "delete_where", boom)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]

    await idx.process_stream(_stream(changed), store=store)

    assert recorder.kinds("set") == [], "файл отмечен поверх неснятой версии"

    # Следующий проход: удаление работает, файл переписан и отмечен.
    monkeypatch.setattr(store, "delete_where", real_delete)
    await idx.process_stream(_stream(changed), store=store)

    texts = _texts_by_path(store)[changed.path]
    assert sorted(texts) == [f"{changed.path} фрагмент {i}" for i in range(2)]
    assert recorder.kinds("set") == [("set", changed.path)]


@pytest.mark.asyncio
async def test_cold_pass_purges_file_written_by_watcher_meanwhile(
    config: Config, monkeypatch, tmp_path
):
    """Файл, записанный watcher'ом во время холодного прохода, не задваивается.

    На пустой таблице проход не чистит пути, но watcher пишет параллельно:
    пользователь правит документ, пока первая индексация идёт часами. Когда
    проход доходит до этого файла, его строки от watcher'а надо снять.
    """
    first = FileMetadata(path="/дом/первый.pdf", size=1, mtime_seconds=1)
    edited = FileMetadata(path="/дом/правленый.pdf", size=1, mtime_seconds=1)
    _install_chunk_pipeline(config, monkeypatch, {first.path: 3, edited.path: 3})
    store = _lance_store(tmp_path)
    deletes = _spy_deletes(store, monkeypatch)
    recorder = _Recorder()
    idx = _StubIndex(config, store=store, recorder=recorder)  # type: ignore[arg-type]
    gate = asyncio.Event()

    async def full_pass():
        yield first
        await gate.wait()
        yield edited

    full = asyncio.create_task(idx.process_stream(full_pass(), store=store))
    assert await _wait_until(
        lambda: store.has_table and store.table.count_rows() >= 2
    ), "проход не записал первый батч"
    # Watcher: та же таблица, тот же файл, без счётчика прохода.
    await idx.process_stream([edited], count=False, store=store)
    gate.set()
    await full

    rows = {path: len(texts) for path, texts in _texts_by_path(store).items()}
    assert rows == {first.path: 3, edited.path: 3}, f"дубли или потеря строк: {rows}"
    assert not any(first.path in predicate for predicate in deletes), (
        "холодный проход удалил строки, записанные им самим"
    )


@pytest.mark.asyncio
async def test_repeated_path_with_same_meta_is_subtracted_from_counter(
    config: Config, monkeypatch, tmp_path
):
    """Повтор пути с той же meta вычитается из счётчика `/status`.

    Повтор даёт вложенный корень (`~/Документы` поверх `/home`) и прерванный
    проход. `counter.count` дедуплицирует по значению, и без отдельного
    вычитания счётчик оставался бы больше нуля: `/status` до следующего
    прохода показывал бы «индексация идёт».
    """
    b = FileMetadata(path="/дом/b.pdf", size=1, mtime_seconds=1)
    c = FileMetadata(path="/дом/c.pdf", size=1, mtime_seconds=1)
    _install_chunk_pipeline(config, monkeypatch, {b.path: 2, c.path: 2})
    store = _lance_store(tmp_path)
    idx = _StubIndex(config, store=store)  # type: ignore[arg-type]
    # Как count_to_index и _run_pass: счётчик — по размеру очереди с повтором.
    idx.counter.reset(3)
    idx.counter.clear_cache()

    await idx.process_stream(_stream(b, c, b), store=store)

    assert idx.counter.cnt == 0, "повтор пути не вычтен — /status застрянет"
