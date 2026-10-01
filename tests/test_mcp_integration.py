"""Полный цикл через настоящий сайдкар: добавить папку, дождаться индексации,
найти документ и прочитать его — теми же вызовами, что сделает агент.

Маркер `integration`: тест поднимает демон подпроцессом и ждёт прогрева
OpenVINO (десятки секунд), поэтому в `make tests` не входит. Запуск:

    uv run pytest tests/test_mcp_integration.py -m integration -v
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.mcp_fixture import Bridge, call_text

ROOT = Path(__file__).resolve().parent.parent
# Прогрев модели на холодной машине занимает до полутора минут.
STARTUP_TIMEOUT = 180.0
INDEXING_TIMEOUT = 180.0
MARKER = "ОРАНЖЕВЫЙ-КАШАЛОТ-482"

pytestmark = pytest.mark.integration


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url, timeout=2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def sidecar(tmp_path):
    """Настоящий демон на эфемерном порту, со своим HOME.

    HOME подменён: индексы и пользовательский конфиг демона живут в
    ~/.files-search и ~/.ragsearch, и без подмены прогон писал бы в каталоги
    разработчика.
    """
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        CONFIG_PATH="cfg/dev.yml",
        RAG_HOST="127.0.0.1",
        RAG_PORT=str(port),
        DEV="0",
        RESET="0",
    )
    log = open(tmp_path / "sidecar.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "main.py"], cwd=str(ROOT), env=env, stdout=log, stderr=log
    )
    url = "http://127.0.0.1:%d" % port
    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        while True:
            if proc.poll() is not None:
                pytest.fail(
                    "сайдкар не запустился:\n%s"
                    % (tmp_path / "sidecar.log").read_text(encoding="utf-8")[-3000:]
                )
            try:
                if _get(url + "/api/v1/health").get("status") == "OK":
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > deadline:
                pytest.fail("сайдкар не поднялся за %s с" % STARTUP_TIMEOUT)
            time.sleep(1.0)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _wait_idle(url, timeout=INDEXING_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get(url + "/api/v1/status")
        if not status.get("is_indexing") and status.get("files_to_index", 0) == 0:
            return status
        time.sleep(1.0)
    pytest.fail("индексация не завершилась за %s с" % timeout)


def test_full_cycle_through_mcp(sidecar, tmp_path):
    """add_path → индексация → search → read_file, всё через инструменты моста."""
    corpus = tmp_path / "корпус"
    corpus.mkdir()
    document = corpus / "договор.txt"
    document.write_text(
        "Договор поставки. Кодовое слово %s. Срок исполнения — декабрь." % MARKER,
        encoding="utf-8",
    )

    bridge = Bridge(sidecar, LQRAG_ALLOW_WRITE="1", LQRAG_TIMEOUT="60")
    try:
        assert bridge.request("initialize", {"protocolVersion": "2025-06-18"})["result"]
        names = [tool["name"] for tool in bridge.tools()]
        assert "local_search" in names and "local_add_path" in names

        added = bridge.call("local_add_path", {"path": str(corpus)}, timeout=60)
        assert added["result"]["isError"] is False, call_text(added)

        _wait_idle(sidecar)

        status = bridge.call("local_index_status")
        assert status["result"]["isError"] is False
        assert "не идёт" in call_text(status)

        found = bridge.call("local_search", {"query": "кодовое слово договора"}, timeout=60)
        assert found["result"]["isError"] is False, call_text(found)
        paths = [item["path"] for item in found["result"]["structuredContent"]["results"]]
        assert str(document) in paths, call_text(found)

        read = bridge.call("local_read_file", {"absolute_path": str(document)})
        assert read["result"]["isError"] is False, call_text(read)
        assert MARKER in call_text(read)

        # Файл вне индексируемых папок остаётся недоступным и на живом демоне.
        outsider = tmp_path / "снаружи.txt"
        outsider.write_text("не для агента", encoding="utf-8")
        refused = bridge.call("local_read_file", {"absolute_path": str(outsider)})
        assert refused["result"]["isError"] is True
        assert "не для агента" not in call_text(refused)
    finally:
        bridge.close()
