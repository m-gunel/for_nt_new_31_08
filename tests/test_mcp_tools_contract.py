"""Контракт инструментов MCP-моста на заглушке сайдкара.

Проверяем три вещи, которые ломаются молча: что уходит в API, что возвращается
агенту и как мост ведёт себя, когда сайдкар отвечает плохо или не отвечает.
Индекс, OpenVINO и LanceDB для этого не нужны — заглушка отдаёт ровно те
структуры, что и настоящий `SearchResponse`.
"""

import time
from pathlib import Path

import pytest

from tests.mbox_fixture import minimal_docx, minimal_pptx, minimal_xlsx
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401

CONFIG = "/api/v1/config"
SEARCH = "/api/v1/search"
STATUS = "/api/v1/status"


def _hit(path, chunks=("фрагмент один",), score=0.81):
    return {
        "id": path,
        "title": Path(path).name,
        "type": "file",
        "score": score,
        "absolute_path": path,
        "chunks": [{"content": text, "score": score} for text in chunks],
    }


@pytest.fixture
def bridge(stub, bridge_factory):  # noqa: F811
    return bridge_factory(stub.url)


@pytest.fixture
def write_bridge(stub, bridge_factory):  # noqa: F811
    return bridge_factory(stub.url, LQRAG_ALLOW_WRITE="1")


# ---------------------------------------------------------------------------
# Поиск: что уходит в API
# ---------------------------------------------------------------------------
def test_search_passes_arguments_verbatim(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [_hit("/docs/a.docx")], "total": 1})
    bridge.call(
        "local_search",
        {"query": "договор", "limit": 3, "query_type": "text", "sources": ["files", "email"]},
    )
    assert stub.bodies("POST", SEARCH) == [
        {
            "query": "договор",
            "limit": 3,
            "query_type": "text",
            "sources": ["files", "email"],
        }
    ]


def test_search_defaults_are_explicit(stub, bridge):  # noqa: F811
    """Умолчания подставляет мост, а не API — иначе они разъезжаются."""
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    bridge.call("local_search", {"query": "что-нибудь"})
    body = stub.bodies("POST", SEARCH)[0]
    assert body["limit"] == 10
    assert body["query_type"] == "hybrid"
    assert body["sources"] == ["files"]


# ---------------------------------------------------------------------------
# Поиск: что возвращается агенту
# ---------------------------------------------------------------------------
def test_search_returns_text_and_structured_content(stub, bridge):  # noqa: F811
    stub.set(
        "POST",
        SEARCH,
        {"results": [_hit("/docs/отчёт.docx", ("итоги квартала",))], "total": 1},
    )
    response = bridge.call("local_search", {"query": "итоги"})
    result = response["result"]
    assert result["isError"] is False
    text = call_text(response)
    assert "отчёт.docx" in text and "/docs/отчёт.docx" in text
    assert "итоги квартала" in text
    structured = result["structuredContent"]
    assert structured["total"] == 1 and structured["shown"] == 1
    assert structured["results"][0]["path"] == "/docs/отчёт.docx"


def test_search_output_fits_the_budget(stub, bridge):  # noqa: F811
    """limit=50 × фрагменты по 600 символов иначе уезжают в контекст целиком."""
    hits = [_hit("/docs/f%02d.txt" % i, ("я" * 900,)) for i in range(50)]
    stub.set("POST", SEARCH, {"results": hits, "total": 50})
    response = bridge.call("local_search", {"query": "много", "limit": 50})
    text = call_text(response)
    assert len(text) < 15000, "ответ не влез в бюджет"
    assert "показано" in text
    assert response["result"]["structuredContent"]["shown"] < 50


def test_empty_result_during_indexing_is_not_a_denial(stub, bridge):  # noqa: F811
    """Главная защита от ложного «такого документа нет»."""
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, {"is_indexing": True, "type": "index", "files_to_index": 42})
    response = bridge.call("local_search", {"query": "договор"})
    text = call_text(response)
    assert "индексация ещё идёт" in text
    assert "42" in text
    assert response["result"]["structuredContent"]["indexing"] is True


def test_empty_result_when_idle_is_a_denial(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    assert "Индексация не идёт" in text


def test_empty_result_with_unknown_status_stays_honest(stub, bridge):  # noqa: F811
    """Не сумев спросить статус, нельзя утверждать, что индекс актуален."""
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    assert "проверить не удалось" in text


def test_email_hits_are_marked_as_unreadable(stub, bridge):  # noqa: F811
    """Ключ письма — не путь на диске, и агент не должен нести его в чтение."""
    letter = {
        "id": "email_INBOX:10:20:body",
        "title": "Тема письма",
        "type": "email",
        "score": 0.5,
        "absolute_path": "email_INBOX:10:20:body",
        "chunks": [{"content": "текст письма", "score": 0.5}],
    }
    stub.set("POST", SEARCH, {"results": [letter], "total": 1})
    assert "не файл на диске" in call_text(bridge.call("local_search", {"query": "письмо"}))


# ---------------------------------------------------------------------------
# Валидация: молча править вход нельзя
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({}, "query"),
        ({"query": "   "}, "query"),
        ({"query": "x", "limit": 900}, "limit"),
        ({"query": "x", "limit": "10"}, "limit"),
        ({"query": "x", "query_type": "магия"}, "query_type"),
        ({"query": "x", "sources": []}, "sources"),
        ({"query": "x", "sources": ["files", "files"]}, "повторяться"),
        ({"query": "x", "sources": ["диск"]}, "sources"),
        ({"query": "x", "top_k": 5}, "неизвестные параметры"),
    ],
)
def test_bad_arguments_are_rejected(stub, bridge, arguments, expected):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    error = bridge.call("local_search", arguments)["error"]
    assert error["code"] == -32602
    assert expected in error["message"]
    assert stub.bodies("POST", SEARCH) == [], "невалидный вызов не должен доходить до API"


# ---------------------------------------------------------------------------
# Деградация: сайдкар отвечает плохо или не отвечает
# ---------------------------------------------------------------------------
def test_api_error_becomes_tool_error(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"detail": "внутренняя ошибка"}, status=500)
    response = bridge.call("local_search", {"query": "x"})
    assert response["result"]["isError"] is True
    assert "500" in call_text(response)


def test_non_json_response_becomes_tool_error(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, b"<html>proxy error</html>")
    response = bridge.call("local_search", {"query": "x"})
    assert response["result"]["isError"] is True
    assert "не-JSON" in call_text(response)


def test_dead_sidecar_becomes_tool_error(bridge_factory):  # noqa: F811
    """Порт закрыт — мост обязан объяснить это, а не упасть."""
    bridge = bridge_factory("http://127.0.0.1:9")
    response = bridge.call("local_search", {"query": "x"})
    assert response["result"]["isError"] is True
    assert "недоступен" in call_text(response)
    assert bridge.request("ping")["result"] == {}, "мост должен пережить отказ"


def test_slow_sidecar_hits_the_timeout(stub, bridge_factory):  # noqa: F811
    def _slow(_body):
        time.sleep(3)
        return 200, {"results": [], "total": 0}

    stub.set("POST", SEARCH, _slow)
    bridge = bridge_factory(stub.url, LQRAG_TIMEOUT="1")
    response = bridge.call("local_search", {"query": "x"}, timeout=20)
    assert response["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Статус индекса
# ---------------------------------------------------------------------------
def test_index_status_reports_phase(stub, bridge):  # noqa: F811
    stub.set(
        "GET",
        STATUS,
        {
            "is_indexing": True,
            "type": "preindex",
            "files_to_index": 7,
            "queued_files": 7,
            "queued_emails": 0,
            "total_files": 100,
        },
    )
    response = bridge.call("local_index_status")
    text = call_text(response)
    assert "идёт" in text and "preindex" in text
    assert response["result"]["structuredContent"]["files_to_index"] == 7


# ---------------------------------------------------------------------------
# Чтение файла: песочница
# ---------------------------------------------------------------------------
@pytest.fixture
def sandbox(tmp_path, stub):  # noqa: F811
    """Индексируемая папка, как её видит мост через GET /config."""
    root = (tmp_path / "indexed").resolve()
    root.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(root)], "email": {"enabled": False}})
    return root


def test_read_file_inside_indexed_folder(sandbox, bridge):
    (sandbox / "заметка.md").write_text("# Заголовок\nтекст", encoding="utf-8")
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / "заметка.md")}
    )
    assert response["result"]["isError"] is False
    text = call_text(response)
    assert "# Заголовок" in text and "начало содержимого" in text


def test_read_file_outside_indexed_folder_is_refused(sandbox, tmp_path, bridge):
    outsider = tmp_path / "чужое.txt"
    outsider.write_text("секрет", encoding="utf-8")
    response = bridge.call("local_read_file", {"absolute_path": str(outsider)})
    assert response["result"]["isError"] is True
    assert "вне индексируемых папок" in call_text(response)
    assert "секрет" not in call_text(response)


def test_read_file_follows_no_symlink_out_of_the_sandbox(sandbox, tmp_path, bridge):
    """Симлинк из индексируемой папки наружу — обход песочницы через abspath."""
    secret = tmp_path / "секрет.txt"
    secret.write_text("токен", encoding="utf-8")
    link = sandbox / "ссылка.txt"
    link.symlink_to(secret)
    response = bridge.call("local_read_file", {"absolute_path": str(link)})
    assert response["result"]["isError"] is True
    assert "токен" not in call_text(response)


def test_read_file_refuses_traversal(sandbox, tmp_path, bridge):
    secret = tmp_path / "секрет.txt"
    secret.write_text("токен", encoding="utf-8")
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / ".." / "секрет.txt")}
    )
    assert response["result"]["isError"] is True
    assert "токен" not in call_text(response)


def test_read_file_refuses_secret_extensions(sandbox, bridge):
    """.env лежит в индексируемой папке, но выдавать его наружу нельзя."""
    (sandbox / "prod.env").write_text("TOKEN=123", encoding="utf-8")
    response = bridge.call("local_read_file", {"absolute_path": str(sandbox / "prod.env")})
    assert response["result"]["isError"] is True
    assert "TOKEN=123" not in call_text(response)


def test_read_file_refuses_hidden_files(sandbox, bridge):
    (sandbox / ".секрет.txt").write_text("скрытое", encoding="utf-8")
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / ".секрет.txt")}
    )
    assert response["result"]["isError"] is True
    assert "скрытые файлы" in call_text(response)


def test_read_file_fails_closed_without_the_sidecar(tmp_path, bridge_factory):  # noqa: F811
    """Границы песочницы неизвестны — читать нельзя, даже если файл есть."""
    target = tmp_path / "файл.txt"
    target.write_text("содержимое", encoding="utf-8")
    bridge = bridge_factory("http://127.0.0.1:9")
    response = bridge.call("local_read_file", {"absolute_path": str(target)})
    assert response["result"]["isError"] is True
    assert "чтение запрещено" in call_text(response)
    assert "содержимое" not in call_text(response)


def test_read_file_refuses_email_keys(sandbox, bridge):
    response = bridge.call(
        "local_read_file", {"absolute_path": "email_INBOX:10:20:body"}
    )
    assert response["result"]["isError"] is True
    assert "ключ письма" in call_text(response)


def test_read_file_reports_missing_file(sandbox, bridge):
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / "нет-такого.txt")}
    )
    assert response["result"]["isError"] is True
    assert "не найден" in call_text(response)


# ---------------------------------------------------------------------------
# Чтение файла: офисные форматы на чистом stdlib
# ---------------------------------------------------------------------------
def test_read_file_opens_docx_without_office_libraries(sandbox, bridge):
    """В поставке мост работает на системном python3 без docx2txt — .docx
    разбирается stdlib-кодом самого моста и обязан читаться одинаково везде."""
    document = sandbox / "отчёт.docx"
    document.write_bytes(minimal_docx("Итоги квартала"))
    response = bridge.call("local_read_file", {"absolute_path": str(document)})
    assert response["result"]["isError"] is False
    assert "Итоги квартала" in call_text(response)


def test_read_file_opens_pptx_and_keeps_slide_order(sandbox, bridge):
    slides = [["слайд номер %d" % number] for number in range(1, 12)]
    deck = sandbox / "дека.pptx"
    deck.write_bytes(minimal_pptx(slides))
    response = bridge.call("local_read_file", {"absolute_path": str(deck)})
    assert response["result"]["isError"] is False
    text = call_text(response)
    assert "--- слайд 2 ---" in text and "--- слайд 10 ---" in text
    # Лексикографическая сортировка имён файлов ставила бы slide10 перед slide2.
    assert text.index("--- слайд 2 ---") < text.index("--- слайд 10 ---")


def test_read_file_opens_xlsx_with_titles_and_all_cell_kinds(sandbox, bridge):
    book = sandbox / "таблица.xlsx"
    book.write_bytes(
        minimal_xlsx([("Отчёт", [["итог", 402, ("inline", "примечание")]])])
    )
    response = bridge.call("local_read_file", {"absolute_path": str(book)})
    assert response["result"]["isError"] is False
    text = call_text(response)
    assert "--- лист Отчёт ---" in text
    # Общая строка, число и inlineStr из одной строки идут через таб.
    assert "итог\t402\tпримечание" in text


def test_read_file_truncates_huge_xlsx(sandbox, bridge):
    """Граница строк на листе при локальном чтении — тысяча (MAX_XLSX_ROWS моста)."""
    rows = [["строка %d" % number] for number in range(1050)]
    book = sandbox / "большая.xlsx"
    book.write_bytes(minimal_xlsx([("Данные", rows)]))
    response = bridge.call("local_read_file", {"absolute_path": str(book)})
    assert response["result"]["isError"] is False
    text = call_text(response)
    assert "лист обрезан на 1000 строках" in text
    assert "строка 1049" not in text


# ---------------------------------------------------------------------------
# Чтение файла: два слоя — парсеры демона, stdlib-фолбэк моста
# ---------------------------------------------------------------------------
def test_read_file_prefers_daemon_parsers_for_office(sandbox, bridge, stub):
    """Офисные форматы мост читает в первую очередь парсерами демона:
    у него в бинаре pymupdf и конвертер Р7, которых у моста нет."""
    book = sandbox / "таблица.xlsx"
    book.write_bytes(minimal_xlsx([("Отчёт", [["итог", 402]])]))
    stub.set(
        "POST",
        "/api/v1/read_file",
        {"text": "текст из парсеров демона", "total_chars": 24, "truncated": False},
    )
    response = bridge.call("local_read_file", {"absolute_path": str(book)})
    assert response["result"]["isError"] is False
    assert "текст из парсеров демона" in call_text(response)
    # Демону уходит ровно тот путь, что дал агент.
    assert stub.bodies("POST", "/api/v1/read_file") == [{"absolute_path": str(book)}]


def test_read_file_propagates_daemon_verdicts(sandbox, bridge, stub):
    """Ответ демона по существу файла (413 — больше лимита чтения) — это его
    вердикт, и мост обязан показать его агенту, а не подменять молчаливой
    локальной попыткой разбора того же файла."""
    book = sandbox / "таблица.xlsx"
    book.write_bytes(minimal_xlsx([("Отчёт", [["итог", 402]])]))
    stub.set(
        "POST",
        "/api/v1/read_file",
        {"detail": "файл больше лимита чтения"},
        status=413,
    )
    response = bridge.call("local_read_file", {"absolute_path": str(book)})
    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "LocalRAG API вернул 413" in text
    assert "файл больше лимита чтения" in text
    # Локальный разбор не случился — вердикт демона не подменён содержимым книги.
    assert "--- лист Отчёт ---" not in text


def test_read_file_refuses_huge_files_without_daemon(sandbox, bridge):
    """Больше лимита чтения (как у демона) мост отвергает и без демона:
    раньше такой файл уходил в неограниченный локальный разбор в память."""
    huge = sandbox / "огромный.txt"
    with open(huge, "wb") as handle:
        handle.seek(64 * 1024 * 1024)
        handle.write(b"x")
    response = bridge.call("local_read_file", {"absolute_path": str(huge)})
    assert response["result"]["isError"] is True
    assert "файл больше лимита чтения" in call_text(response)


def test_read_file_legacy_xls_refuses_without_daemon(sandbox, bridge):
    """Легаси .xls умеет только демон; старый демон без ручки — честный отказ
    с подсказкой обновить, а не мусор текстового чтения бинарника."""
    fixture_xls = Path(__file__).parent / "fixtures" / "legacy.xls"
    if not fixture_xls.exists():
        pytest.skip("нет фикстуры legacy.xls — тест не проверяет, а не падает")
    legacy = sandbox / "старина.xls"
    legacy.write_bytes(fixture_xls.read_bytes())
    response = bridge.call("local_read_file", {"absolute_path": str(legacy)})
    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "демон" in text and "обновите lib-local-rag" in text


# ---------------------------------------------------------------------------
# Управление индексацией (агент — единственный пульт)
# ---------------------------------------------------------------------------
def test_list_paths_marks_missing_folders(stub, write_bridge, tmp_path):  # noqa: F811
    existing = (tmp_path / "есть").resolve()
    existing.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(existing), "/нет/такой/папки"]})
    text = call_text(write_bridge.call("local_list_paths"))
    assert "Индексируемых папок: 2" in text
    assert "✓" in text and "✗" in text


def test_add_path_appends_to_the_existing_list(stub, write_bridge, tmp_path):  # noqa: F811
    """Список папок — единственная ручка, и её нельзя затирать целиком."""
    old = (tmp_path / "старая").resolve()
    new = (tmp_path / "новая").resolve()
    old.mkdir()
    new.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(old)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_add_path", {"path": str(new)})
    assert response["result"]["isError"] is False
    assert stub.bodies("POST", CONFIG) == [{"folders": [str(old), str(new)]}]


def test_add_path_is_idempotent(stub, write_bridge, tmp_path):  # noqa: F811
    folder = (tmp_path / "папка").resolve()
    folder.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(folder)]})
    stub.set("POST", CONFIG, None)
    text = call_text(write_bridge.call("local_add_path", {"path": str(folder)}))
    assert "уже индексируется" in text
    assert stub.bodies("POST", CONFIG) == [], "повторное добавление не должно писать конфиг"


def test_remove_path_keeps_the_rest(stub, write_bridge, tmp_path):  # noqa: F811
    first = (tmp_path / "первая").resolve()
    second = (tmp_path / "вторая").resolve()
    first.mkdir()
    second.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(first), str(second)]})
    stub.set("POST", CONFIG, None)
    write_bridge.call("local_remove_path", {"path": str(second)})
    assert stub.bodies("POST", CONFIG) == [{"folders": [str(first)]}]


def test_removing_the_last_path_is_refused(stub, write_bridge, tmp_path):  # noqa: F811
    """Пустой список не выключает индексацию, а молча возвращает пути из YAML."""
    only = (tmp_path / "единственная").resolve()
    only.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(only)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_remove_path", {"path": str(only)})
    assert response["result"]["isError"] is True
    assert "последнюю папку" in call_text(response)
    assert stub.bodies("POST", CONFIG) == []


def test_removing_unknown_path_is_refused(stub, write_bridge, tmp_path):  # noqa: F811
    folder = (tmp_path / "папка").resolve()
    folder.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(folder)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_remove_path", {"path": "/совсем/другая"})
    assert response["result"]["isError"] is True
    assert stub.bodies("POST", CONFIG) == []


def test_set_email_sends_a_partial_update(stub, write_bridge):  # noqa: F811
    """В теле только email: список папок этим запросом затирать нельзя."""
    stub.set("GET", CONFIG, {"folders": ["/tmp"], "email": {"enabled": True}})
    stub.set("POST", CONFIG, None)
    write_bridge.call("local_set_email", {"enabled": True})
    assert stub.bodies("POST", CONFIG) == [{"email": {"enabled": True}}]


def test_set_email_requires_a_boolean(stub, write_bridge):  # noqa: F811
    stub.set("POST", CONFIG, None)
    error = write_bridge.call("local_set_email", {"enabled": "да"})["error"]
    assert error["code"] == -32602
    assert stub.bodies("POST", CONFIG) == []


def test_rescan_reposts_the_same_folders(stub, write_bridge):  # noqa: F811
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    stub.set("GET", CONFIG, {"folders": ["/a", "/b"]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_trigger_rescan")
    assert response["result"]["isError"] is False
    assert stub.bodies("POST", CONFIG) == [{"folders": ["/a", "/b"]}]


def test_rescan_is_refused_while_indexing(stub, write_bridge):  # noqa: F811
    """POST /config во время индексации — то, на чём hunt_crash.py ловил падения."""
    stub.set("GET", STATUS, {"is_indexing": True, "type": "index", "files_to_index": 500})
    stub.set("GET", CONFIG, {"folders": ["/a"]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_trigger_rescan")
    assert response["result"]["isError"] is True
    assert "уже идёт" in call_text(response)
    assert stub.bodies("POST", CONFIG) == []
