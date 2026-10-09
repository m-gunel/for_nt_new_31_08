"""Протокол MCP-моста: то, без чего клиент к нему просто не подключится.

Сайдкар здесь не нужен — ни один тест не доходит до HTTP. Проверяем рукопожатие,
состав контракта и живучесть разбора JSON-RPC.
"""

import json

import pytest

from tests.mcp_fixture import bridge_factory, stub  # noqa: F401 — фикстуры pytest

READ_TOOL_NAMES = ["local_search", "local_index_status", "local_read_file"]
WRITE_TOOL_NAMES = [
    "local_list_paths",
    "local_add_path",
    "local_remove_path",
    "local_trigger_rescan",
]


@pytest.fixture
def bridge(bridge_factory):  # noqa: F811
    return bridge_factory()


def test_initialize_keeps_known_protocol_version(bridge):
    """Клиенту отвечают его версией, если мы её поддерживаем."""
    result = bridge.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "t"},
        },
    )["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "localrag-mcp"


def test_initialize_falls_back_on_unknown_version(bridge):
    """Неизвестную версию не отражаем эхом: иначе обещаем то, чего не умеем."""
    result = bridge.request(
        "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}}
    )["result"]
    assert result["protocolVersion"] == "2025-06-18"


def test_capabilities_do_not_promise_list_changed(bridge):
    """listChanged заявлять нельзя: уведомление мост никогда не шлёт."""
    caps = bridge.request("initialize", {"protocolVersion": "2024-11-05"})["result"][
        "capabilities"
    ]
    assert caps["tools"].get("listChanged") is not True


def test_default_surface_is_all_tools(bridge):
    """Пульт у агента один: write-инструменты видны без дополнительных флагов."""
    assert [
        tool["name"] for tool in bridge.tools()
    ] == READ_TOOL_NAMES + WRITE_TOOL_NAMES


def test_write_tools_hidden_only_by_explicit_opt_out(bridge_factory):  # noqa: F811
    bridge = bridge_factory(LQRAG_ALLOW_WRITE="0")
    assert [tool["name"] for tool in bridge.tools()] == READ_TOOL_NAMES


def test_disabled_write_tool_is_refused_with_a_reason(bridge_factory):  # noqa: F811
    """Спрятанный инструмент должен объяснять, почему он недоступен."""
    bridge = bridge_factory(LQRAG_ALLOW_WRITE="0")
    error = bridge.call("local_add_path", {"path": "/tmp"})["error"]
    assert error["code"] == -32602
    assert "LQRAG_ALLOW_WRITE" in error["message"]


def test_tool_schemas_are_well_formed(bridge_factory):  # noqa: F811
    """Схема — единственное, по чему агент понимает, как звать инструмент."""
    bridge = bridge_factory(LQRAG_ALLOW_WRITE="1")
    for tool in bridge.tools():
        schema = tool["inputSchema"]
        assert schema["type"] == "object", tool["name"]
        assert isinstance(schema.get("properties"), dict), tool["name"]
        # Закрытая схема — часть контракта: лишний аргумент должен быть виден
        # как ошибка вызова, а не проглатываться.
        assert schema.get("additionalProperties") is False, tool["name"]
        for name in schema.get("required", []):
            assert name in schema["properties"], (tool["name"], name)
        assert tool["description"].strip(), tool["name"]


def test_search_contract_speaks_of_documents(bridge):
    """limit, total и match считаются в документах, как у демона."""
    (search,) = [tool for tool in bridge.tools() if tool["name"] == "local_search"]
    assert search["inputSchema"]["properties"]["limit"]["description"] == (
        "Сколько документов вернуть"
    )
    output = search["outputSchema"]["properties"]
    assert "документов" in output["total"]["description"]
    match = output["results"]["items"]["properties"]["match"]
    assert match["type"] == "array" and match["items"] == {"type": "string"}
    assert "name_code" in match["description"] and "card" in match["description"]
    assert "код из имени" in search["description"]


def test_fragment_shares_are_fair_and_never_crumbs():
    """Раскладка первых фрагментов: поровну с переливом, без обрывков."""
    from mcp_server import MIN_CHUNK_CHARS, _first_fragment_sizes, _share

    # Короткий берёт своё, остаток делят остальные.
    assert _share([50, 600, 600], 650) == [50, 300, 300]
    assert _share([600, 600], 5000) == [600, 600]
    # Хватает на всех — все получают, урезанные поровну.
    assert _first_fragment_sizes([600] * 4, [6] * 4, 1224) == [300] * 4
    # Не хватает — фрагменты у верхних, не меньше порога; у остальных нет.
    sizes = _first_fragment_sizes([600] * 10, [6] * 10, 1000)
    assert sum(sizes) == 1000 - 7 * 6 and sizes[7:] == [0] * 3
    assert all(136 <= size <= 137 for size in sizes[:7])
    assert min(s for s in sizes if s) >= MIN_CHUNK_CHARS
    # Фрагмент короче порога порогом не отсекается.
    assert _first_fragment_sizes([40, 40], [6, 6], 92) == [40, 40]
    assert _first_fragment_sizes([600], [6], 50) == [0]


def test_descriptions_carry_no_personal_data(bridge_factory):  # noqa: F811
    """Описания уезжают в промпт модели — личных путей в них быть не должно."""
    bridge = bridge_factory(LQRAG_ALLOW_WRITE="1")
    dump = json.dumps(bridge.tools(), ensure_ascii=False)
    for leak in (
        "news_briefing",
        "spec-master",
        "agentanal",
        "sigma.sbrf.ru",
        "/home/work",
    ):
        assert leak not in dump, leak


def test_notifications_get_no_response(bridge):
    """Ответ с id=null на нотификацию клиент вправе счесть поломкой протокола."""
    bridge.notify("notifications/initialized")
    bridge.notify("notifications/cancelled", {"requestId": 1})
    bridge.notify("совершенно неизвестный метод")
    bridge.expect_silence()
    # После нотификаций мост продолжает отвечать на запросы.
    assert bridge.request("ping")["result"] == {}


def test_unknown_method_and_unknown_tool(bridge):
    """Метода нет — -32601; инструмента нет — неверный параметр name
    существующего метода tools/call, -32602 (MCP, Tools → Error Handling)."""
    assert bridge.request("resources/list")["error"]["code"] == -32601
    assert bridge.call("local_nonexistent")["error"]["code"] == -32602


def test_broken_json_does_not_kill_the_bridge(bridge):
    """Одно мусорное сообщение не должно ронять мост для всей сессии."""
    bridge.send_raw("это не json")
    bridge.send_raw("")
    assert bridge.request("ping")["result"] == {}


def test_batch_is_answered_per_message(bridge):
    bridge.send(
        [
            {"jsonrpc": "2.0", "id": 100, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 101, "method": "ping"},
        ]
    )
    ids = sorted([bridge.read()["id"], bridge.read()["id"]])
    assert ids == [100, 101]
    bridge.expect_silence()


def test_eof_terminates_the_process(bridge):
    bridge.proc.stdin.close()
    assert bridge.proc.wait(timeout=5) == 0


@pytest.mark.parametrize(
    "method, params",
    [
        ("tools/call", [1]),
        ("tools/call", "local_search"),
        ("tools/call", {"name": ["local_search"]}),
        ("initialize", ["2025-06-18"]),
    ],
)
def test_malformed_params_do_not_kill_the_bridge(bridge, method, params):
    """params-массив или name-список — ошибка вызова, а не падение моста."""
    bridge.send({"jsonrpc": "2.0", "id": 900, "method": method, "params": params})
    response = bridge.read()
    assert response["id"] == 900 and response["error"]["code"] == -32602
    assert bridge.request("ping")["result"] == {}


def test_unexpected_failure_answers_internal_error(monkeypatch):
    """Непредусмотренный сбой разбора — -32603 на запрос, нотификации — тишина."""
    import mcp_server

    def boom(msg):
        raise RuntimeError("сбой разбора")

    monkeypatch.setattr(mcp_server, "_handle", boom)
    response = mcp_server._safe_handle({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert response["id"] == 7 and response["error"]["code"] == -32603
    assert mcp_server._safe_handle({"jsonrpc": "2.0", "method": "x"}) is None


def test_bridge_scripts_stay_on_python36():
    """Нижней границы python3 в debian/control нет: мост и register_mcp.py
    работают на любом системном python3, конструкции новее 3.6 им нельзя."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("mcp_server.py", "scripts/register_mcp.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        newer = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.NamedExpr, ast.Match))
            # date/datetime.fromisoformat — с 3.7.
            or (isinstance(node, ast.Attribute) and node.attr == "fromisoformat")
        ]
        assert newer == [], (name, newer)


def test_dates_accept_only_ascii_digits():
    """strptime, в отличие от date.fromisoformat, читает и «２０２４»: такие
    даты по-прежнему отвергаются, обычные разбираются как раньше."""
    import mcp_server

    assert mcp_server._optional_date({"d": "2024-02-29"}, "d") == "2024-02-29"
    for value in ("２０２４-01-01", "٢٠٢٤-01-01", "2023-02-29", "2024-13-01"):
        with pytest.raises(mcp_server.InvalidParams):
            mcp_server._optional_date({"d": value}, "d")
