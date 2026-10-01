"""Три молчаливых дефекта: ручка /config, MCP-мост и деградация поиска.

Общее у них одно — наружу всё выглядело успехом. POST /config со сменой
глубины отвечал 200 и не менял ничего; мост отдавал агенту indexing=false
посреди первой индексации; гибридный поиск после апгрейда LanceDB отдавал бы
500 вместо привычной деградации до векторного. Ни один из трёх случаев не
виден ни в логе ошибок, ни в кодах ответов, поэтому проверяются они здесь.

Живой индекс не поднимаем нигде: ручка проверяется на заглушке индекса из
tests/test_config_reload.py, мост — на заглушке сайдкара из
tests/mcp_fixture.py, а поиск — на подставной таблице LanceDB. Ни модель
OpenVINO, ни настоящая база при этом не открываются, но сами пакеты в
окружении нужны: `src.indexer.indexer` тянет lancedb на уровне импорта, а
соседний набор — numpy и pyarrow.
"""

import logging
from types import SimpleNamespace

import polars as pl
import pytest

from src.indexer.indexer import Index, _is_missing_fts_index
from src.server.model import UserConfig
from src.server.router import update_config
# Стенд MCP-моста: заглушка сайдкара и мост подпроцессом.
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401
# Фикстуры вокруг POST /config и заглушка индекса — из соседнего набора.
from tests.test_config_reload import RecordingIndex, _nt_body, cfg, journal  # noqa: F401
# Пути, `_hit` и фикстуру моста берём оттуда, где написан контракт моста, а не
# копируем: две копии `_hit` разъехались бы при первой же смене схемы
# SearchResponse — и разъехались бы молча, оставив оба набора зелёными на
# разных представлениях выдачи.
from tests.test_mcp_tools_contract import SEARCH, STATUS, _hit, bridge  # noqa: F401


# ---------------------------------------------------------------------------
# Дефект 1: POST /config {"max_depth": N} не применялся
# ---------------------------------------------------------------------------
def _depth_body(depth: int) -> UserConfig:
    """Тело запроса, в котором задана ТОЛЬКО глубина обхода."""
    return UserConfig.model_validate({"max_depth": depth})


def _next_depth(cfg) -> int:  # noqa: F811
    """Заведомо другая глубина, чем стоит сейчас."""
    return (cfg.scan_max_depth or 0) + 2


@pytest.mark.asyncio
async def test_max_depth_change_alone_restarts_watching(
    cfg,  # noqa: F811
    journal,  # noqa: F811
    tmp_path,
):
    """Смена одной только глубины обязана поднять наблюдение заново.

    Боевой дефект: перезапуск watcher'а был привязан к
    `folders_changed = user_config.folders is not None`, а глубина читается
    ровно один раз — в AbstractFilesWatcher.run(). Поэтому POST /config
    {"max_depth": N} отвечал 200, значение ложилось в user_conf.yml и не
    применялось до следующего /activate или рестарта демона: пользователь
    видел успех, а inotify продолжал сидеть на всём дереве.

    Переиндексацию при этом запускать НЕЛЬЗЯ: max_depth ограничивает только
    постановку наблюдений, полный проход iter_file_metadata глубину не
    учитывает — обход всего диска был бы чистой платой ни за что.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    fake_index = RecordingIndex(journal)

    # Сначала обычный полный конфиг — так его и шлёт клиент.
    await update_config(_nt_body(str(docs)), cfg, fake_index)
    journal.clear()

    depth = _next_depth(cfg)
    response = await update_config(_depth_body(depth), cfg, fake_index)

    assert response.status_code == 200
    assert "stop_watcher" in journal, (
        f"наблюдение обязано остановиться, иначе новая глубина не вступит "
        f"в силу; журнал: {journal}"
    )
    assert "restart_watcher" in journal, (
        f"наблюдение обязано подняться обратно; журнал: {journal}"
    )
    assert journal.index("stop_watcher") < journal.index("restart_watcher")
    assert "start_reindex" not in journal, (
        "полный проход глубину не учитывает — переиндексация здесь лишняя"
    )
    assert "stop_index" not in journal, "индексацию ради глубины не останавливают"
    # Настройка должна была сохраниться, а список папок — уцелеть: запрос
    # частичный, folders в нём не приходили.
    assert cfg.scan_max_depth == depth
    assert cfg.scan_paths == [str(docs)]


@pytest.mark.asyncio
async def test_same_max_depth_does_not_restart_watching(
    cfg,  # noqa: F811
    journal,  # noqa: F811
):
    """Повторный POST с той же глубиной наблюдение не трогает.

    Клиенты шлют конфиг целиком и помногу раз подряд. Перезапуск watcher'а
    на каждый такой запрос — это снятие и постановка тысяч inotify-watch'ей
    впустую, поэтому первый POST (со сменой значения) обязан наблюдение
    поднять, а второй, с тем же самым значением, — нет.
    """
    fake_index = RecordingIndex(journal)
    depth = _next_depth(cfg)

    await update_config(_depth_body(depth), cfg, fake_index)
    # Первый запрос действительно меняет глубину — до фикса не было и этого.
    assert "restart_watcher" in journal, f"журнал первого запроса: {journal}"

    journal.clear()
    await update_config(_depth_body(depth), cfg, fake_index)

    assert journal == [], (
        f"глубина не менялась, трогать наблюдение и индексацию незачем: {journal}"
    )


@pytest.mark.asyncio
async def test_max_depth_change_does_not_wake_deactivated_index(
    cfg,  # noqa: F811
    journal,  # noqa: F811
):
    """На деактивированном индексе наблюдение не поднимают — БТ §5.2.

    Настройка сохраняется, наблюдение остаётся выключенным до
    POST /api/v1/activate. Важно, что ветка глубины при этом всё-таки
    отрабатывает (stop_watcher зовётся): до фикса POST с max_depth не делал
    вообще ничего, и отличить «сохранили и не подняли» от «не заметили
    смену» было невозможно.
    """
    fake_index = RecordingIndex(journal)
    fake_index.active = False
    depth = _next_depth(cfg)

    await update_config(_depth_body(depth), cfg, fake_index)

    assert "stop_watcher" in journal, (
        f"смену глубины обязаны заметить даже на выключенной индексации: {journal}"
    )
    assert "restart_watcher" not in journal, (
        "деактивированный индекс не должен поднимать наблюдение сам"
    )
    assert "start_reindex" not in journal
    assert cfg.scan_max_depth == depth, "настройка обязана сохраниться"


# ---------------------------------------------------------------------------
# Дефект 2: мост врал про индексацию на непустой выдаче
# ---------------------------------------------------------------------------
def test_search_hits_report_ongoing_indexing(stub, bridge):  # noqa: F811
    """Непустая выдача во время индексации обязана быть помечена неполной.

    Боевой дефект: в непустой ветке `_tool_search` поле
    structuredContent["indexing"] было захардкожено False, статус не
    спрашивался вовсе. Агент, получивший три документа посреди первой
    индексации, считал список исчерпывающим и отвечал пользователю, что
    больше ничего нет, — то самое ложное «не найдено», от которого пустая
    ветка была защищена с самого начала.

    Вторая половина теста стережёт обратную ошибку: когда индексация не
    идёт, флага и предупреждения быть не должно, иначе агент начнёт
    оговариваться на каждом ответе и предупреждение обесценится.
    """
    stub.set("POST", SEARCH, {"results": [_hit("/docs/договор.docx")], "total": 1})
    stub.set("GET", STATUS, {"is_indexing": True, "type": "index", "files_to_index": 42})

    response = bridge.call("local_search", {"query": "договор"})

    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["indexing"] is True, (
        "выдача получена во время индексации, список может быть неполным"
    )
    text = call_text(response)
    assert "договор.docx" in text, "сами находки никуда деться не должны"
    assert "индексация ещё идёт" in text, (
        "предупреждать надо и текстом: structuredContent читает не всякий клиент"
    )
    assert "42" in text, (
        "счётчик очереди взят из ответа /status — значит статус реально спрошен"
    )

    # Индексация закончилась: тот же запрос, никаких оговорок.
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    response = bridge.call("local_search", {"query": "договор"})
    assert response["result"]["structuredContent"]["indexing"] is False
    assert "индексация ещё идёт" not in call_text(response)


def test_search_survives_unavailable_status(stub, bridge):  # noqa: F811
    """Недоступный /status не должен ломать сам поиск.

    Статус спрашивается ради честного флага, но поиск уже отработал, и
    отдать его результат обязаны в любом случае — потому статус и берётся
    через `_safe_status()`, а не прямым `_http_json`. Здесь маршрута
    /api/v1/status у заглушки нет вовсе (404), и вызов всё равно обязан
    вернуть находки, а не ошибку инструмента.

    Первым делом убеждаемся, что статус вообще спрошен: без этого тест был бы
    зелёным и на старом коде, где непустая ветка `_tool_search` в /status не
    ходила вовсе, а indexing была захардкожена False.
    """
    stub.set("POST", SEARCH, {"results": [_hit("/docs/акт.pdf")], "total": 1})

    response = bridge.call("local_search", {"query": "акт"})

    assert any(r["method"] == "GET" and r["path"] == STATUS for r in stub.requests), (
        f"мост обязан спросить статус даже при непустой выдаче, иначе флаг "
        f"indexing не на чём основан; пришедшие запросы: {stub.requests}"
    )
    result = response["result"]
    assert result["isError"] is False, "поиск не должен зависеть от доступности статуса"
    assert result["structuredContent"]["indexing"] is False
    assert "акт.pdf" in call_text(response)


# ---------------------------------------------------------------------------
# Дефект 3: деградация поиска держалась на формулировке ошибки LanceDB
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "message",
    [
        # Формулировка, на которую проверка была написана изначально.
        "Cannot perform full text search unless an INVERTED index has been created",
        # Варианты, которые встречаются в других версиях LanceDB: старая
        # проверка ("full text search" и "index" одновременно) их не узнавала.
        "No inverted index found on column text",
        "FTS index not found",
        "There is no index with name 'text_idx'",
    ],
)
def test_missing_fts_index_is_recognised_by_any_wording(message):
    """Отсутствие FTS-индекса опознаётся во всех известных формулировках.

    Боевой риск: проверка требовала подстроку «full text search», то есть
    держалась на одной конкретной фразе LanceDB. Апгрейд библиотеки со
    сменой текста ошибки превратил бы штатную деградацию поиска в 500 — и
    ни в одном тесте это бы не всплыло.
    """
    assert _is_missing_fts_index(Exception(message)), (
        f"формулировка не опознана как «нет FTS-индекса»: {message!r}"
    )


def test_unrelated_error_is_not_taken_for_missing_fts_index():
    """Посторонняя ошибка не должна выдаваться за отсутствие индекса.

    Иначе широкий набор маркеров начнёт прятать настоящие сбои: текстовый
    поиск молча отдавал бы пустую выдачу вместо ошибки. Второй случай —
    ровно про широту маркеров: слово «index» в тексте есть, но это чужая
    поломка, а не отсутствие полнотекстового индекса.

    Оговорка: на старом коде этот тест тоже зелёный — старая узкая проверка
    («full text search» и «index» одновременно) этих строк не узнавала. Это
    ограничение на НОВЫЕ маркеры, а не сторож дефекта.
    """
    assert not _is_missing_fts_index(Exception("out of memory"))
    assert not _is_missing_fts_index(IndexError("list index out of range"))


_VECTOR_FRAME = pl.DataFrame({"path": ["/tmp/договор.txt"], "text": ["договор поставки"]})


class _QueryBuilder:
    """Билдер запроса LanceDB: любой метод цепочки возвращает сам билдер.

    Через `__getattr__`, а не списком методов: цепочка в indexer.search
    (nprobes/distance_type/vector/text/limit) время от времени меняется, и
    заглушка не должна падать из-за нового звена. Но раз по имени методы не
    проверяются, аргументы записываем в `calls`: иначе пропавший
    `.limit(top_k)` нигде бы не всплыл, а поиск начал бы тянуть таблицу
    целиком.
    """

    def __init__(self, finish):
        self.calls = []
        self._finish = finish

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return record

    def to_polars(self):
        return self._finish()


class _FakeLanceTable:
    """Таблица, у которой гибридный поиск падает, а векторный работает."""

    def __init__(self, hybrid_error: Exception):
        self.hybrid_error = hybrid_error
        self.vector_searches = 0
        # Билдер на каждый вид запроса — чтобы проверить, что до него дошло.
        self.builders: dict = {}

    def search(self, *args, **kwargs):
        query_type = kwargs.get("query_type")
        if query_type == "hybrid":
            def _boom():
                raise self.hybrid_error

            builder = _QueryBuilder(_boom)
        elif query_type == "vector":
            self.vector_searches += 1
            builder = _QueryBuilder(lambda: _VECTOR_FRAME)
        else:
            raise AssertionError(f"неожиданный query_type={query_type!r}")
        self.builders[query_type] = builder
        return builder


def _index_with_table(table) -> SimpleNamespace:
    """Минимум, который нужен Index.search: модель эмбеддингов и хранилища."""

    async def async_encode(texts, role=None):
        return [[0.0] * 8]

    store = SimpleNamespace(table=table, has_table=True, table_name="files")
    return SimpleNamespace(
        cfg=SimpleNamespace(ov_model=SimpleNamespace(async_encode=async_encode)),
        files_store=store,
        email_store=store,
    )


@pytest.mark.asyncio
async def test_hybrid_search_degrades_on_unknown_lance_error(caplog):
    """Незнакомая ошибка гибридного поиска — деградация, а не 500.

    Боевой дефект: в ветке hybrid стояло `if not _is_missing_fts_index(exc):
    raise`, то есть любая ошибка с неузнанным текстом уходила клиенту
    пятисоткой. Векторный индекс у пользователя при этом рабочий, и выдача
    по нему всегда лучше отказа. Незнакомую ошибку обязаны записать на
    уровне ERROR со стеком — иначе настоящая поломка растворится в фоне под
    видом штатной деградации.
    """
    table = _FakeLanceTable(RuntimeError("lance internal panic: unexpected state"))
    index = _index_with_table(table)

    with caplog.at_level(logging.ERROR):
        frames = await Index.search(index, "договор поставки", top_k=5)

    assert table.vector_searches == 1, "должны были откатиться к векторному поиску"
    assert len(frames) == 1
    assert frames[0]["path"].to_list() == ["/tmp/договор.txt"]
    assert ("limit", (5,)) in table.builders["vector"].calls, (
        f"top_k обязан дойти до отката, иначе поиск потянет таблицу целиком: "
        f"{table.builders['vector'].calls}"
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "незнакомая ошибка обязана попасть в лог на уровне ERROR"
    assert any(r.exc_info for r in errors), "в логе должен быть стек, а не только текст"


@pytest.mark.asyncio
async def test_hybrid_search_degrades_on_new_missing_index_wording(caplog):
    """Новая формулировка «нет FTS-индекса» — тоже деградация, и без ERROR.

    Это штатное состояние первой индексации: строки в таблице уже есть,
    полнотекстовый индекс ещё строится. До фикса такая формулировка не
    распознавалась (в ней нет «full text search») и запрос падал в 500.
    """
    table = _FakeLanceTable(RuntimeError("No inverted index found on column text"))
    index = _index_with_table(table)

    with caplog.at_level(logging.WARNING):
        frames = await Index.search(index, "договор поставки", top_k=5)

    assert table.vector_searches == 1
    assert frames[0]["path"].to_list() == ["/tmp/договор.txt"]
    assert any(
        r.levelno == logging.WARNING and "FTS" in r.getMessage() for r in caplog.records
    ), f"деградация обязана оставить след в логе: {caplog.text!r}"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "распознанное отсутствие индекса — это WARNING, а не ошибка со стеком"
    )
