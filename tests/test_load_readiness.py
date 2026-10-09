"""Выполнимость сценариев нагрузочного тестирования индексатора.

Каждый тест — шаг методики НТ или гипотеза из `scripts/nt/hunt_crash.py`:
красный тест значит, что часовой прогон на стенде даст негодные цифры или
оборвётся.
"""

import asyncio
import shutil
from collections import namedtuple
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio as pa_fixture
from pytest import fixture

from src.config import Config, EmailConfig
from src.indexer.indexer import Index, _is_missing_fts_index
from src.indexer.watcher.watcher import Watcher
from src.server.router import update_config

# Журнал stop/restart watcher'а и тело запроса, которое реально шлёт стенд.
from tests.test_config_reload import _nt_body, journal  # noqa: F401

# Сколько поисков считаем «вспышкой пользователя» и стрессом.
_BURST = 4
_STRESS = 12


@fixture
def corpus(tmp_path):
    """Маленький корпус: НТ важна одновременность, а не объём."""
    d = tmp_path / "corpus"
    d.mkdir()
    for i in range(6):
        (d / f"документ_{i}.txt").write_text(
            f"Договор номер {i}. Квартальный отчёт о выручке и поставках.",
            encoding="utf-8",
        )
    return d


@fixture
def nt_config(config: Config, tmp_path, corpus, monkeypatch):
    """Конфиг стенда: своё хранилище, почта выключена, корпус под рукой."""
    storage = tmp_path / "storage"
    monkeypatch.setattr(config, "lsm_root", str(storage))
    monkeypatch.setattr(config, "lsm_path", str(storage / "lsm.ldb"))
    monkeypatch.setattr(config, "ld_db_path", str(storage / "lance"))
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    monkeypatch.setattr(config, "index_refresh_delay", 0.05)
    monkeypatch.setattr(config, "email", EmailConfig(enabled=False))
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(corpus)])
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)
    yield config
    for cached in ("lance", "user_conf_path", "data_directory_path"):
        config.__dict__.pop(cached, None)


@pa_fixture.fixture
async def nt_index(nt_config: Config):
    idx = Index(nt_config)
    async with idx:
        try:
            yield idx
        finally:
            if idx._index_task is not None:
                idx._index_task.cancel()
            await idx.stop_emails_polling()


async def _search(index: Index, query: str = "договор отчёт"):
    """Один поисковый запрос стенда: hybrid, top_k=10."""
    return await index.search(query, top_k=10, query_type="hybrid")


def _slow_stream(index: Index, hold: asyncio.Event, entered: asyncio.Event):
    """Растянуть полный проход, чтобы успеть подать нагрузку параллельно.

    Тормозится только полный проход: watcher зовёт `process_stream(count=False)`,
    и его задержка заблокировала бы тест — доиндексация ждала бы события,
    которое выставляется после неё.
    """
    real = index.process_stream

    async def slow(*args, **kwargs):
        if kwargs.get("count", True):
            entered.set()
            await hold.wait()
        return await real(*args, **kwargs)

    index.process_stream = slow  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_search_works_during_full_pass(nt_index: Index):
    """Пользователь ищет, пока идёт индексация.

    Ошибки поиска под индексацией сделали бы долю ошибок в отчёте НТ мерой
    дефекта, а не производительности.
    """
    hold, entered = asyncio.Event(), asyncio.Event()
    _slow_stream(nt_index, hold, entered)

    scan = asyncio.create_task(nt_index.scan_all())
    await asyncio.wait_for(entered.wait(), 30)
    assert nt_index.main_index, "проход должен идти, иначе тест ничего не проверяет"

    results = await asyncio.gather(
        *(_search(nt_index) for _ in range(_BURST)), return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, BaseException)]

    hold.set()
    await asyncio.wait_for(scan, 60)

    assert not failures, f"поиск под индексацией не должен падать: {failures}"


@pytest.mark.asyncio
async def test_burst_of_searches_does_not_error(nt_index: Index):
    """Фаза СТРЕСС: пачка одновременных запросов не должна давать ошибок.

    Поиск ходит в свой пул на четыре воркера (`_SEARCH_EXECUTOR`); запросов
    больше, чем воркеров, — очередь должна ждать, а не рваться.
    """
    await asyncio.wait_for(nt_index.scan_all(), 120)

    results = await asyncio.gather(
        *(_search(nt_index, f"договор {i}") for i in range(_STRESS)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]

    assert not failures, f"стресс поиском не должен давать ошибок: {failures}"


@pytest.mark.asyncio
async def test_search_works_before_indexes_are_built(nt_index: Index):
    """Строки уже записаны, индексы ещё не пересобраны — поиск уже отвечает.

    Запись идёт в обход `_run_pass`: тот строит индексы сам.
    """
    await nt_index.files_scanner.scan()
    await nt_index.process_stream(nt_index.files_scanner.iter(endless=False))
    assert await nt_index.files_store.count_rows() > 0, "строки должны быть"

    result = await _search(nt_index)

    assert len(result) == 1 and result[0] is not None, (
        "поиск обязан отвечать до пересборки индексов"
    )
    names = [Path(p).name for p in result[0]["path"].to_list()]
    assert any(n.startswith("документ_") for n in names), (
        f"в выдаче должны быть документы корпуса: {names}"
    )


def test_missing_fts_error_is_recognized():
    """Классификатор «FTS-индекса ещё нет» отделяет деградацию от 500.

    Сообщение LanceDB сверяется по тексту и молча ломается при обновлении
    библиотеки — тогда гибридный поиск поднимал бы исключение вместо перехода
    на векторный.
    """
    missing = RuntimeError(
        "Cannot perform full text search unless an INVERTED index "
        "has been created on field text"
    )
    assert _is_missing_fts_index(missing), "штатная ошибка должна опознаваться"
    assert not _is_missing_fts_index(RuntimeError("connection reset by peer")), (
        "посторонний сбой нельзя глушить деградацией — он должен дойти наружу"
    )


@pytest.mark.asyncio
async def test_watcher_indexes_new_file_during_full_pass(
    nt_index: Index, nt_config: Config, corpus
):
    """Новый файл от watcher'а во время полного прохода попадает в индекс.

    Обе ветки записи одновременно работают с одной таблицей состояния и одной
    таблицей Lance.
    """
    hold, entered = asyncio.Event(), asyncio.Event()
    _slow_stream(nt_index, hold, entered)
    scan = asyncio.create_task(nt_index.scan_all())
    await asyncio.wait_for(entered.wait(), 30)

    fresh = corpus / "срочный_документ.txt"
    fresh.write_text("Уникальный маркер ЛОРЕМИПСУМ7788 внутри", encoding="utf-8")
    watcher = Watcher(nt_config, nt_index)

    await watcher._handle_files({str(fresh)})

    hold.set()
    await asyncio.wait_for(scan, 60)

    indexed = await nt_index.lsm_collection.indexed.async_get(str(fresh))
    assert indexed is not None, "файл от watcher'а должен попасть в индекс"


@pytest.mark.asyncio
async def test_status_counts_watcher_work(nt_index: Index, nt_config: Config, corpus):
    """`/status` обязан показывать работу watcher'а.

    Стенд завершает фазу индексации по первому `is_indexing: false`: без учёта
    пачки от watcher'а фаза обрывалась бы раньше, и длительность занижалась бы.
    """
    fresh = corpus / "новый.txt"
    fresh.write_text("Ещё один договор поставки", encoding="utf-8")
    watcher = Watcher(nt_config, nt_index)

    seen_busy = False
    real_stream = nt_index.process_stream

    async def watching_stream(*args, **kwargs):
        nonlocal seen_busy
        seen_busy = (await nt_index.status()).is_indexing
        return await real_stream(*args, **kwargs)

    nt_index.process_stream = watching_stream  # type: ignore[method-assign]
    await watcher._handle_files({str(fresh)})

    assert seen_busy, "во время разбора пачки от watcher'а статус не может быть idle"


@pytest.mark.asyncio
async def test_config_change_during_active_indexing(
    nt_index: Index,
    nt_config: Config,
    journal,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Гипотеза `active_indexing` из hunt_crash: смена папки посреди прохода.

    Так стенд нацеливает индексатор на корпус, иногда посреди индексации.
    """
    hold, entered = asyncio.Event(), asyncio.Event()
    _slow_stream(nt_index, hold, entered)
    scan = asyncio.create_task(nt_index.scan_all())
    nt_index.task = scan  # как это делает lifespan(): stop_index() её отменит
    await asyncio.wait_for(entered.wait(), 30)

    new_dir = tmp_path / "другая_папка"
    new_dir.mkdir()

    async def fake_reindex():
        journal.append("start_reindex")

    monkeypatch.setattr(nt_index, "start_reindex", fake_reindex)

    response = await update_config(_nt_body(str(new_dir)), nt_config, nt_index)
    hold.set()
    with suppress(asyncio.CancelledError):
        await scan

    assert response.status_code == 200
    assert "restart_watcher" in journal, f"watcher не поднят обратно: {journal}"


@pytest.mark.asyncio
async def test_parallel_config_requests_do_not_interleave(
    nt_index: Index,
    nt_config: Config,
    journal,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Два POST /config одновременно: стенд и пользователь могут совпасть.

    `reconfigure_lock` обязан развести их полностью. Признак поломки —
    два `stop_watcher` подряд: значит второй запрос влез в середину первого
    и оставил общее состояние (_watcher_task/_index) повреждённым.
    """
    first = tmp_path / "папка_1"
    second = tmp_path / "папка_2"
    for d in (first, second):
        d.mkdir()

    async def fake_reindex():
        journal.append("start_reindex")

    monkeypatch.setattr(nt_index, "start_reindex", fake_reindex)

    responses = await asyncio.gather(
        update_config(_nt_body(str(first)), nt_config, nt_index),
        update_config(_nt_body(str(second)), nt_config, nt_index),
    )

    assert [r.status_code for r in responses] == [200, 200]

    watcher_steps = [s for s in journal if s in ("stop_watcher", "restart_watcher")]
    assert watcher_steps == ["stop_watcher", "restart_watcher"] * 2, (
        f"шаги двух запросов перемешались: {watcher_steps}"
    )


@pytest.mark.asyncio
async def test_repeated_config_on_same_folder_is_stable(
    nt_index: Index,
    nt_config: Config,
    journal,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Гипотезы `config_same_dir` и `double_config`: конфиг шлют повторно.

    Клиенты присылают конфиг целиком на каждое изменение настроек, поэтому
    «та же папка ещё раз» — рядовой запрос, а не крайний случай.
    """

    async def fake_reindex():
        journal.append("start_reindex")

    monkeypatch.setattr(nt_index, "start_reindex", fake_reindex)
    same = tmp_path / "одна_и_та_же"
    same.mkdir()

    for _ in range(3):
        response = await update_config(_nt_body(str(same)), nt_config, nt_index)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_indexing_refuses_when_disk_is_low(nt_index: Index, monkeypatch):
    """Меньше 1 ГиБ свободного места — проход не запускается.

    Молча заполнить диск пользователя индексом хуже, чем не проиндексировать:
    БТ требует отсутствия влияния на работу остальной системы.
    """
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: usage(total=10**9, used=10**9, free=1024)
    )
    collected = False
    real = nt_index._preindex_files

    async def spy():
        nonlocal collected
        collected = True
        return await real()

    nt_index._preindex_files = spy  # type: ignore[method-assign]

    await asyncio.wait_for(nt_index.scan_all(), 30)

    assert not collected, "при нехватке места индексация запускаться не должна"


@pytest.mark.asyncio
async def test_failed_pass_does_not_wedge_indexing(nt_index: Index):
    """Сбой одного прохода не запирает индексацию до рестарта."""

    real_pass = nt_index._run_pass

    async def boom(*_a, **_kw):
        raise RuntimeError("сбой прохода")

    # Не через monkeypatch: его undo() снял бы заодно изоляцию хранилища из
    # фикстуры nt_config, и продолжение теста ушло бы в реальный ~/.files-search.
    nt_index._run_pass = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await nt_index.scan_all()

    assert not nt_index._scan_lock.locked(), "блокировка прохода не снята"

    nt_index._run_pass = real_pass  # type: ignore[method-assign]
    await asyncio.wait_for(nt_index.scan_all(), 120)
    assert not nt_index.main_index, "следующий проход обязан отработать до конца"
