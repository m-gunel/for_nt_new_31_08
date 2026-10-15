"""Контракт инструментов MCP-моста на заглушке сайдкара.

Что уходит в API, что возвращается агенту и как мост ведёт себя, когда
сайдкар отвечает плохо или не отвечает. Заглушка отдаёт те же структуры, что и
настоящий `SearchResponse`.
"""

import time
from pathlib import Path

import pytest

from mcp_server import FULL_DOCS, HEAD_CHUNK_CHARS
from tests.mcp_fixture import bridge_factory, call_text, stub  # noqa: F401
from tests.office_fixture import minimal_docx, minimal_pptx, minimal_xlsx

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


def test_search_passes_arguments_verbatim(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [_hit("/docs/a.docx")], "total": 1})
    bridge.call("local_search", {"query": "договор", "limit": 3, "query_type": "text"})
    assert stub.bodies("POST", SEARCH) == [
        {"query": "договор", "limit": 3, "query_type": "text"}
    ]


def test_search_defaults_are_explicit(stub, bridge):  # noqa: F811
    """Умолчания подставляет мост, а не API — иначе они разъезжаются."""
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    bridge.call("local_search", {"query": "что-нибудь"})
    assert stub.bodies("POST", SEARCH) == [
        {"query": "что-нибудь", "limit": 10, "query_type": "hybrid"}
    ]


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


def test_search_shows_every_document(stub, bridge):  # noqa: F811
    """limit=50 — в ответе все 50 документов: бюджета на ответ нет. Фрагменты
    первых FULL_DOCS документов целиком, у остальных — начало фрагмента."""
    hits = [_hit("/docs/f%02d.txt" % i, ("я" * 900,)) for i in range(50)]
    stub.set("POST", SEARCH, {"results": hits, "total": 50})
    response = bridge.call("local_search", {"query": "много", "limit": 50})
    text = call_text(response)
    assert "показано" not in text
    structured = response["result"]["structuredContent"]
    assert structured["total"] == structured["shown"] == 50
    assert all("/docs/f%02d.txt" % i in text for i in range(50))
    for number, result in enumerate(structured["results"], 1):
        (chunk,) = result["chunks"]
        if number <= FULL_DOCS:
            assert chunk["text"] == "я" * 900
        else:
            assert len(chunk["text"]) == HEAD_CHUNK_CHARS
            assert chunk["text"].endswith("…")


def _long_hit(number, fragments=3, size=900):
    """Документ с длинными названием, путём и свойствами — как в выдаче ЦБ."""
    folder = "/home/u/Документы/" + "Нормативные акты Банка России/" * 4
    name = "%02d_Положение о порядке формирования резервов " % number + "и" * 80
    hit = _hit(
        folder + name + ".docx",
        tuple("фрагмент %d.%d " % (number, k) + "я" * size for k in range(fragments)),
    )
    hit["title"] = name + ".docx"
    hit["document"] = {"title": "Очень длинное название " * 30, "author": "Иванов"}
    return hit


def test_long_documents_do_not_cut_the_list(stub, bridge):  # noqa: F811
    """10 документов с длинными названиями и путями — все 10 заголовков и все
    фрагменты: у первых FULL_DOCS целиком, у остальных — начало."""
    hits = [_long_hit(i) for i in range(10)]
    stub.set("POST", SEARCH, {"results": hits, "total": 10})
    response = bridge.call("local_search", {"query": "резервы"})
    text = call_text(response)
    structured = response["result"]["structuredContent"]
    assert structured["shown"] == 10 and "показано" not in text
    for i, hit in enumerate(hits):
        assert "%d. %s" % (i + 1, hit["title"]) in text
        assert hit["absolute_path"] in text, "путь не обрезается: по нему читают файл"
    for number, (hit, result) in enumerate(zip(hits, structured["results"]), 1):
        texts = [chunk["text"] for chunk in result["chunks"]]
        full = [chunk["content"] for chunk in hit["chunks"]]
        if number <= FULL_DOCS:
            assert texts == full, "верхним документам — фрагменты целиком"
        else:
            assert [len(text) for text in texts] == [HEAD_CHUNK_CHARS] * 3
            assert all(f.startswith(t[:-1]) for t, f in zip(texts, full))


def test_text_and_structured_carry_the_same_fragments(stub, bridge):  # noqa: F811
    """Все фрагменты (до трёх на документ) есть и в тексте, и в structured."""
    hits = [
        _hit("/d/%d.txt" % i, ("я%d.1 " % i * 300, "я%d.2 " % i * 300, "кратко %d" % i))
        for i in range(FULL_DOCS + 2)
    ]
    stub.set("POST", SEARCH, {"results": hits, "total": len(hits)})
    response = bridge.call("local_search", {"query": "x"})
    text = call_text(response)
    results = response["result"]["structuredContent"]["results"]
    assert [len(result["chunks"]) for result in results] == [3] * len(hits)
    assert all(
        chunk["text"] in text for result in results for chunk in result["chunks"]
    )
    # Короткий фрагмент и в хвосте выдачи — целиком, без «…».
    assert [result["chunks"][2]["text"] for result in results] == [
        "кратко %d" % i for i in range(len(hits))
    ]


def test_documents_are_never_dropped(stub, bridge):  # noqa: F811
    hits = [_hit("/d/%02d.txt" % i) for i in range(20)]
    stub.set("POST", SEARCH, {"results": hits, "total": 20})
    response = bridge.call("local_search", {"query": "x", "limit": 20})
    structured = response["result"]["structuredContent"]
    assert structured["total"] == structured["shown"] == 20
    assert "показано" not in call_text(response)


def test_match_marks_in_text_and_structured(stub, bridge):  # noqa: F811
    """Документ, найденный по карточке, приходит без фрагментов — отметка
    объясняет агенту, почему он в выдаче."""
    by_code = _hit("/docs/ЦБ_21.pdf", ())
    by_code["match"] = ["name_code", "card"]
    by_card = _hit("/docs/Положение о резервах.docx")
    by_card["match"] = ["card"]
    plain = _hit("/docs/заметка.txt")
    stub.set("POST", SEARCH, {"results": [by_code, by_card, plain], "total": 3})
    response = bridge.call("local_search", {"query": "найди файл ЦБ_21"})
    blocks = call_text(response).split("\n2. ")
    card = "слова запроса есть в имени или свойствах файла (название, автор, тема…)"
    # Не «по имени/названию»: карточка — ещё автор, редактор, тема и прочее.
    assert "   код из имени файла; " + card in blocks[0]
    first, rest = blocks[1].split("\n3. ")
    assert card in first and "код из имени" not in first
    assert "слова запроса" not in rest and "код из имени" not in rest
    results = response["result"]["structuredContent"]["results"]
    assert [result["match"] for result in results] == [
        ["name_code", "card"],
        ["card"],
        [],
    ]
    assert results[0]["chunks"] == []


def test_total_counts_documents_of_the_response(stub, bridge):  # noqa: F811
    """total — число документов ответа: старые демоны считали в total строки."""
    stub.set("POST", SEARCH, {"results": [_hit("/a.txt"), _hit("/b.txt")], "total": 17})
    response = bridge.call("local_search", {"query": "x"})
    assert response["result"]["structuredContent"]["total"] == 2
    assert call_text(response).startswith("Найдено документов: 2\n")


def test_many_copies_are_listed_briefly(stub, bridge):  # noqa: F811
    hit = _hit("/a/отчёт.pdf")
    hit["duplicates"] = ["/копия%d/отчёт.pdf" % i for i in range(8)]
    stub.set("POST", SEARCH, {"results": [hit], "total": 1})
    response = bridge.call("local_search", {"query": "отчёт"})
    text = call_text(response)
    assert "/копия4/отчёт.pdf и ещё 3" in text and "/копия5/" not in text
    results = response["result"]["structuredContent"]["results"]
    assert results[0]["duplicates"] == hit["duplicates"]


def test_empty_result_during_indexing_is_not_a_denial(stub, bridge):  # noqa: F811
    """Главная защита от ложного «такого документа нет»."""
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set(
        "GET", STATUS, {"is_indexing": True, "type": "index", "files_to_index": 42}
    )
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


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({}, "query"),
        ({"query": "   "}, "query"),
        ({"query": "x", "limit": 900}, "limit"),
        ({"query": "x", "limit": "10"}, "limit"),
        ({"query": "x", "query_type": "магия"}, "query_type"),
        ({"query": "x", "top_k": 5}, "неизвестные параметры"),
        ({"query": "x", "sources": ["files"]}, "неизвестные параметры"),
    ],
)
def test_bad_arguments_are_rejected(stub, bridge, arguments, expected):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    error = bridge.call("local_search", arguments)["error"]
    assert error["code"] == -32602
    assert expected in error["message"]
    assert stub.bodies("POST", SEARCH) == [], (
        "невалидный вызов не должен доходить до API"
    )


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


def test_index_status_reports_phase(stub, bridge):  # noqa: F811
    stub.set(
        "GET",
        STATUS,
        {
            "is_indexing": True,
            "type": "preindex",
            "files_to_index": 7,
            "queued_files": 7,
            "total_files": 100,
        },
    )
    response = bridge.call("local_index_status")
    text = call_text(response)
    assert "идёт" in text and "preindex" in text
    assert response["result"]["structuredContent"]["files_to_index"] == 7


def test_index_status_reports_file_queue(stub, bridge):  # noqa: F811
    stub.set(
        "GET",
        STATUS,
        {"is_indexing": True, "type": "index", "files_to_index": 7, "total_files": 9},
    )
    text = call_text(bridge.call("local_index_status"))
    assert "В очереди файлов: 7\n" in text


@pytest.fixture
def sandbox(tmp_path, stub):  # noqa: F811
    """Индексируемая папка, как её видит мост через GET /config."""
    root = (tmp_path / "indexed").resolve()
    root.mkdir()
    stub.set("GET", CONFIG, {"folders": [str(root)]})
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


def test_read_file_reads_through_symlinks_like_the_indexer(sandbox, tmp_path, bridge):
    """Индексатор идёт по ссылкам и хранит путь через ссылку — его absolute_path
    из выдачи поиска читается. Ссылка со скрытым именем — как скрытый каталог."""
    projects = tmp_path / "проекты"
    projects.mkdir()
    (projects / "план.txt").write_text("план за ссылкой", encoding="utf-8")
    (sandbox / "Проекты").symlink_to(projects, target_is_directory=True)
    (sandbox / "ссылка.txt").symlink_to(projects / "план.txt")
    (sandbox / ".скрытая").symlink_to(projects, target_is_directory=True)
    for path in (sandbox / "Проекты" / "план.txt", sandbox / "ссылка.txt"):
        response = bridge.call("local_read_file", {"absolute_path": str(path)})
        assert response["result"]["isError"] is False, call_text(response)
        assert "план за ссылкой" in call_text(response)
    hidden = sandbox / ".скрытая" / "план.txt"
    response = bridge.call("local_read_file", {"absolute_path": str(hidden)})
    assert response["result"]["isError"] is True
    assert "план за ссылкой" not in call_text(response)


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
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / "prod.env")}
    )
    assert response["result"]["isError"] is True
    assert "TOKEN=123" not in call_text(response)


def test_read_file_refuses_hidden_files(sandbox, bridge):
    (sandbox / ".секрет.txt").write_text("скрытое", encoding="utf-8")
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / ".секрет.txt")}
    )
    assert response["result"]["isError"] is True
    assert "скрытые файлы" in call_text(response)


@pytest.mark.parametrize(
    "rel",
    [
        "u/.config/gh/hosts.yml",
        "u/.docker/config.json",
        "u/.gigacode/settings.json",
        "u/.wsc/config/wsc.json",
        "u/.local/share/Trash/files/old.txt",
        "u/.ssh/notes.txt",
        "u/node_modules/x/readme.md",
    ],
)
def test_read_file_refuses_files_under_hidden_and_service_dirs(sandbox, bridge, rel):
    """Корень — как поставочный /home: скрыто не имя файла, а каталог ниже корня.
    Индексатор такие каталоги не обходит, и мост их не читает."""
    target = sandbox / rel
    target.parent.mkdir(parents=True)
    target.write_text("oauth_token: FAKE_TOKEN_123", encoding="utf-8")
    response = bridge.call("local_read_file", {"absolute_path": str(target)})
    assert response["result"]["isError"] is True
    assert "скрытые файлы" in call_text(response)
    assert "FAKE_TOKEN_123" not in call_text(response)


def test_read_file_fails_closed_without_the_sidecar(tmp_path, bridge_factory):  # noqa: F811
    """Границы песочницы неизвестны — читать нельзя, даже если файл есть."""
    target = tmp_path / "файл.txt"
    target.write_text("содержимое", encoding="utf-8")
    bridge = bridge_factory("http://127.0.0.1:9")
    response = bridge.call("local_read_file", {"absolute_path": str(target)})
    assert response["result"]["isError"] is True
    assert "чтение запрещено" in call_text(response)
    assert "содержимое" not in call_text(response)


def test_read_file_reports_missing_file(sandbox, bridge):
    response = bridge.call(
        "local_read_file", {"absolute_path": str(sandbox / "нет-такого.txt")}
    )
    assert response["result"]["isError"] is True
    assert "не найден" in call_text(response)


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


def test_read_file_prefers_daemon_parsers_for_office(sandbox, bridge, stub):  # noqa: F811
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
    # Демону уходит путь, проверенный мостом (здесь он совпадает с путём агента).
    assert stub.bodies("POST", "/api/v1/read_file") == [{"absolute_path": str(book)}]


def test_read_file_sends_the_checked_path_to_the_daemon(stub, bridge_factory, tmp_path):  # noqa: F811
    """~ раскрывает мост: у демона свой HOME (на SberOS — pw_dir), и путь агента
    он раскрыл бы в другой каталог."""
    home = (tmp_path / "дом").resolve()
    docs = home / "docs"
    docs.mkdir(parents=True)
    book = docs / "таблица.xlsx"
    book.write_bytes(minimal_xlsx([("Отчёт", [["итог", 402]])]))
    stub.set("GET", CONFIG, {"folders": [str(docs)]})
    stub.set(
        "POST",
        "/api/v1/read_file",
        {"text": "текст из парсеров демона", "total_chars": 24, "truncated": False},
    )
    bridge = bridge_factory(stub.url, HOME=str(home))
    response = bridge.call("local_read_file", {"absolute_path": "~/docs/таблица.xlsx"})
    assert response["result"]["isError"] is False, call_text(response)
    assert stub.bodies("POST", "/api/v1/read_file") == [{"absolute_path": str(book)}]


def test_read_file_propagates_daemon_verdicts(sandbox, bridge, stub):  # noqa: F811
    """Ответ демона по существу файла (413 — больше лимита чтения) мост
    показывает агенту, а не подменяет локальным разбором того же файла."""
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


@pytest.mark.parametrize("name", ["старина.xls", "отчёт.docx"])
def test_read_file_propagates_daemon_not_found(sandbox, bridge, stub, name):  # noqa: F811
    """404 «файл не найден» — вердикт демона, а не «ручки нет»: локальный разбор
    подменил бы его ложным «обновите lib-local-rag» или текстом файла."""
    document = sandbox / name
    if name.endswith(".docx"):
        document.write_bytes(minimal_docx("Итоги квартала"))
    else:
        document.write_bytes(b"\xd0\xcf\x11\xe0 legacy")
    stub.set(
        "POST",
        "/api/v1/read_file",
        {"detail": "файл не найден или не обычный"},
        status=404,
    )
    response = bridge.call("local_read_file", {"absolute_path": str(document)})
    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "файл не найден или не обычный" in text
    assert "обновите lib-local-rag" not in text
    assert "Итоги квартала" not in text


def test_read_file_reports_slow_daemon_instead_of_local_fallback(
    sandbox,
    stub,  # noqa: F811
    bridge_factory,  # noqa: F811
):
    """Таймаут живого демона — не «демон недоступен»: для .xls локальный разбор
    выдал бы ложный совет обновить пакет."""
    legacy = sandbox / "старина.xls"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0 legacy")

    def _slow(_body):
        time.sleep(3)
        return 200, {"text": "поздно", "total_chars": 6, "truncated": False}

    stub.set("POST", "/api/v1/read_file", _slow)
    bridge = bridge_factory(stub.url, LQRAG_TIMEOUT="1")
    response = bridge.call(
        "local_read_file", {"absolute_path": str(legacy)}, timeout=20
    )
    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "сбой связи с сайдкаром" in text
    assert "обновите lib-local-rag" not in text


def test_read_file_refuses_huge_files_without_daemon(sandbox, bridge):
    """Больше лимита чтения (как у демона) мост отвергает и без демона:
    иначе файл ушёл бы в неограниченный локальный разбор в память."""
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
    assert stub.bodies("POST", CONFIG) == [], (
        "повторное добавление не должно писать конфиг"
    )


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


def test_rescan_reposts_the_same_folders(stub, write_bridge):  # noqa: F811
    stub.set("GET", STATUS, {"is_indexing": False, "type": "idle", "files_to_index": 0})
    stub.set("GET", CONFIG, {"folders": ["/a", "/b"]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_trigger_rescan")
    assert response["result"]["isError"] is False
    assert stub.bodies("POST", CONFIG) == [{"folders": ["/a", "/b"]}]


def test_rescan_is_refused_while_indexing(stub, write_bridge):  # noqa: F811
    """Переиндексация поверх идущей отклоняется: сценарий падений hunt_crash.py."""
    stub.set(
        "GET", STATUS, {"is_indexing": True, "type": "index", "files_to_index": 500}
    )
    stub.set("GET", CONFIG, {"folders": ["/a"]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_trigger_rescan")
    assert response["result"]["isError"] is True
    assert "уже идёт" in call_text(response)
    assert stub.bodies("POST", CONFIG) == []


# Демон жив, но индексация выключена: POST /deactivate, /reset или
# autostart_indexing: false. is_indexing при этом тоже false.
INACTIVE = {"is_indexing": False, "active": False, "type": "idle", "files_to_index": 0}


def test_empty_result_when_indexing_is_off_is_not_a_denial(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [], "total": 0})
    stub.set("GET", STATUS, INACTIVE)
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    assert "Индексация выключена" in text and "POST /api/v1/activate" in text
    assert "индекс актуален" not in text


def test_hits_when_indexing_is_off_may_be_incomplete(stub, bridge):  # noqa: F811
    stub.set("POST", SEARCH, {"results": [_hit("/docs/a.txt")], "total": 1})
    stub.set("GET", STATUS, INACTIVE)
    text = call_text(bridge.call("local_search", {"query": "x"}))
    assert "индексация выключена" in text and "может быть неполным" in text


def test_index_status_reports_indexing_is_off(stub, bridge):  # noqa: F811
    stub.set("GET", STATUS, INACTIVE)
    response = bridge.call("local_index_status")
    text = call_text(response)
    assert text.startswith("Индексация: выключена\n")
    assert "POST /api/v1/activate" in text
    assert response["result"]["structuredContent"]["active"] is False


def test_add_and_remove_path_when_indexing_is_off(stub, write_bridge, tmp_path):  # noqa: F811
    """На выключенной индексации демон только сохраняет список папок."""
    first = (tmp_path / "первая").resolve()
    second = (tmp_path / "вторая").resolve()
    first.mkdir()
    second.mkdir()
    stub.set("GET", STATUS, INACTIVE)
    stub.set("GET", CONFIG, {"folders": [str(first)]})
    stub.set("POST", CONFIG, None)
    added = call_text(write_bridge.call("local_add_path", {"path": str(second)}))
    assert "список сохранён" in added and "перезапущена" not in added
    stub.set("GET", CONFIG, {"folders": [str(first), str(second)]})
    removed = call_text(write_bridge.call("local_remove_path", {"path": str(second)}))
    assert "список сохранён" in removed and "перезапущена" not in removed
    assert stub.bodies("POST", CONFIG) == [
        {"folders": [str(first), str(second)]},
        {"folders": [str(first)]},
    ]


def test_rescan_is_refused_when_indexing_is_off(stub, write_bridge):  # noqa: F811
    stub.set("GET", STATUS, INACTIVE)
    stub.set("GET", CONFIG, {"folders": ["/a"]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_trigger_rescan")
    assert response["result"]["isError"] is True
    assert "выключена" in call_text(response)
    assert stub.bodies("POST", CONFIG) == []


@pytest.fixture
def home_with_nested(tmp_path):
    """Корень как /home в поставке и пользовательская папка внутри него."""
    home = (tmp_path / "home").resolve()
    nested = home / "u" / "Документы"
    nested.mkdir(parents=True)
    return home, nested


def test_add_nested_path_says_it_was_covered(stub, write_bridge, home_with_nested):  # noqa: F811
    """Вложенная папка уже входит в индексацию корня — ответ говорит об этом.

    Запись всё равно добавляется: иначе список не сузить с корня до неё
    (последнюю папку удалить нельзя)."""
    home, nested = home_with_nested
    stub.set("GET", CONFIG, {"folders": [str(home)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_add_path", {"path": str(nested)})
    assert response["result"]["isError"] is False
    assert "внутри индексируемой папки %s" % home in call_text(response)
    assert stub.bodies("POST", CONFIG) == [{"folders": [str(home), str(nested)]}]


def test_add_link_out_of_the_root_is_not_called_covered(stub, write_bridge, tmp_path):  # noqa: F811
    """Вложенность — по realpath: ссылку наружу корень при follow_symlinks: false
    не обходит, называть её покрытой нельзя."""
    home = (tmp_path / "home").resolve()
    outside = (tmp_path / "data").resolve()
    (home / "u").mkdir(parents=True)
    outside.mkdir()
    link = home / "u" / "data"
    link.symlink_to(outside)
    stub.set("GET", CONFIG, {"folders": [str(home)]})
    stub.set("POST", CONFIG, None)
    text = call_text(write_bridge.call("local_add_path", {"path": str(link)}))
    assert "внутри индексируемой" not in text
    assert stub.bodies("POST", CONFIG) == [{"folders": [str(home), str(link)]}]


def test_remove_nested_path_says_documents_stay(stub, write_bridge, home_with_nested):  # noqa: F811
    """Вложенная папка уходит из списка, но её документы покрывает внешний корень."""
    home, nested = home_with_nested
    stub.set("GET", CONFIG, {"folders": [str(home), str(nested)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_remove_path", {"path": str(nested)})
    text = call_text(response)
    assert response["result"]["isError"] is False
    assert "внутри индексируемой папки %s" % home in text
    assert "сняты с индекса" not in text
    assert stub.bodies("POST", CONFIG) == [{"folders": [str(home)]}]


def test_remove_covered_path_names_the_root(stub, write_bridge, home_with_nested):  # noqa: F811
    home, nested = home_with_nested
    stub.set("GET", CONFIG, {"folders": [str(home)]})
    stub.set("POST", CONFIG, None)
    response = write_bridge.call("local_remove_path", {"path": str(nested)})
    assert response["result"]["isError"] is True
    assert "внутри индексируемой папки %s" % home in call_text(response)
    assert stub.bodies("POST", CONFIG) == []
