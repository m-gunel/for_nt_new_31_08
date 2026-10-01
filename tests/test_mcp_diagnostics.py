"""Диагностика адреса сайдкара: откуда мост берёт URL и как предупреждает.

Главная жалоба пользователя поставки .deb звучит так: «инструменты MCP есть, а
поиск падает с отказом соединения». Причина почти всегда в том, что демон
LocalRAG не запущен, а мост молча адресует сайдкар на исторический
http://127.0.0.1:8000. Эти тесты фиксируют поведение, которое раньше было
тихим: источник адреса и явное предупреждение в стартовом логе.

mcp_server.py при импорте использует только stdlib, поэтому тестировать его
можно напрямую, без OpenVINO и индекса.

От машины тесты не зависят: кандидаты порт-файла подменены на tmp (иначе
живой ~/.files-search/port разработчика решал бы, какой source получится), а
автозапуск выключен — на машине с systemd мост иначе дёргал бы настоящую службу.
"""

import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

import mcp_server
from tests.mcp_fixture import (  # noqa: F401 — фикстуры pytest
    _QuietHTTPServer,
    bridge_factory,
    call_text,
    stub,
)

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"

FALLBACK = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Без явного LQRAG_PORT_FILE мост смотрит в tmp, а не в дом разработчика."""
    monkeypatch.setenv("LQRAG_AUTOSTART", "0")
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", None)
    monkeypatch.setattr(
        mcp_server,
        "_port_file_candidates",
        lambda: [str(tmp_path / "home" / "port"), str(tmp_path / "pwdir" / "port")],
    )
    # Состояние автозапуска общее на процесс — у каждого теста оно своё.
    monkeypatch.setattr(mcp_server, "_autostart", mcp_server._autostart_state())


def _closed_port():
    """Порт, на котором заведомо никто не слушает: отказ соединения мгновенный."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _set_env(monkeypatch, url=None, port_file=None):
    """Настроить окружение источника адреса. url=None — переменной нет вовсе."""
    env_keys = [key for key in os.environ if key.startswith("LQRAG_")]
    for key in env_keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LQRAG_AUTOSTART", "0")
    if url is not None:
        monkeypatch.setenv("LQRAG_URL", url)
    if port_file is not None:
        monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", port_file)


# ---------------------------------------------------------------------------
# _resolve_base_url: откуда берётся адрес
# ---------------------------------------------------------------------------
def test_url_from_env_wins_over_everything(monkeypatch):
    port_file = "/нет/такого/файла"
    _set_env(monkeypatch, url="http://10.0.0.5:7000", port_file=port_file)
    assert mcp_server._resolve_base_url() == ("http://10.0.0.5:7000", "env")


def test_url_reads_port_file(monkeypatch, tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("8123", encoding="utf-8")
    _set_env(monkeypatch, port_file=str(port_file))
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:8123", "port-file")


def test_url_trims_trailing_slash_from_env(monkeypatch):
    _set_env(monkeypatch, url="http://127.0.0.1:9000/")
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:9000", "env")


def test_url_falls_back_when_no_file_and_no_env(monkeypatch, tmp_path):
    missing = tmp_path / "port"
    _set_env(monkeypatch, port_file=str(missing))
    # Файла нет, LQRAG_URL нет — исторический 8000 и признак «fallback».
    assert mcp_server._resolve_base_url() == (FALLBACK, "fallback")


def test_url_falls_back_on_garbage_port_file(monkeypatch, tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("не число", encoding="utf-8")
    _set_env(monkeypatch, port_file=str(port_file))
    assert mcp_server._resolve_base_url() == (FALLBACK, "fallback")


def test_url_falls_back_on_out_of_range_port(monkeypatch, tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("999999", encoding="utf-8")
    _set_env(monkeypatch, port_file=str(port_file))
    assert mcp_server._resolve_base_url() == (FALLBACK, "fallback")


# ---------------------------------------------------------------------------
# Живость демона по pid рядом с порт-файлом
# ---------------------------------------------------------------------------
def _dead_pid():
    """pid процесса, который уже завершился и прибран."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _publish(directory, port, pid=None):
    """Порт (и pid, если задан) — так, как их публикует демон."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "port").write_text("%d\n" % port, encoding="utf-8")
    if pid is not None:
        (directory / "pid").write_text("%d\n" % pid, encoding="utf-8")
    return str(directory / "port")


@pytest.mark.parametrize(
    "content, expected",
    [
        ("self", True),
        ("dead", False),
        (None, None),
        ("не число", None),
        ("0", None),
        ("-1", None),
    ],
)
def test_pid_alive_reads_neighbour_pid_file(tmp_path, content, expected):
    port_file = _publish(tmp_path, 8130)
    if content is not None:
        value = {"self": str(os.getpid()), "dead": str(_dead_pid())}.get(
            content, content
        )
        (tmp_path / "pid").write_text(value, encoding="utf-8")
    assert mcp_server._pid_alive(port_file) is expected


def test_live_pid_candidate_wins_over_stale_home(tmp_path):
    """Устаревший порт-файл в $HOME не заслоняет живой демон в pw_dir."""
    _publish(tmp_path / "home", 8131, _dead_pid())
    _publish(tmp_path / "pwdir", 8132, os.getpid())
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:8132", "port-file")


def test_unknown_pid_ranks_above_dead(tmp_path):
    _publish(tmp_path / "home", 8133, _dead_pid())
    _publish(tmp_path / "pwdir", 8134)
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:8134", "port-file")


def test_dead_port_file_is_still_a_port_file(tmp_path):
    """Все демоны мертвы — адрес всё равно из файла: путь к нему нужен в ошибке."""
    stale = _publish(tmp_path / "home", 8135, _dead_pid())
    _publish(tmp_path / "pwdir", 8136, _dead_pid())
    url, source, port_file, _ = mcp_server._resolve_address()
    assert (url, source, port_file) == ("http://127.0.0.1:8135", "port-file", stale)


def test_startup_log_reports_pid_liveness(tmp_path):
    port_file = _publish(tmp_path, 8137, _dead_pid())
    stderr, _ = _run_bridge(LQRAG_PORT_FILE=port_file)
    assert "pid: мёртв" in stderr


# ---------------------------------------------------------------------------
# Стартовый лог: предупреждение о недоступном сайдкаре
# ---------------------------------------------------------------------------
def _run_bridge(**extra_env):
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("LQRAG_")]:
        env.pop(key)
    env["LQRAG_AUTOSTART"] = "0"
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    return proc.stderr, proc.returncode


def test_startup_log_warns_when_sidecar_missing(monkeypatch, tmp_path):
    """Демон не запущен (файла порта нет) — мост обязан сказать об этом в логе."""
    stderr, code = _run_bridge(LQRAG_PORT_FILE=str(tmp_path / "port"))
    assert code == 0
    assert "ПРЕДУПРЕЖДЕНИЕ" in stderr
    assert FALLBACK in stderr
    assert "systemctl --user" in stderr
    assert "lib-local-rag.service" in stderr


def test_startup_log_is_quiet_with_explicit_url(monkeypatch, tmp_path):
    """LQRAG_URL задан — источник определён, никого пугать не надо."""
    stderr, _ = _run_bridge(
        LQRAG_URL="http://127.0.0.1:7000", LQRAG_PORT_FILE=str(tmp_path / "port")
    )
    assert "ПРЕДУПРЕЖДЕНИЕ" not in stderr


def test_startup_log_is_quiet_with_port_file(monkeypatch, tmp_path):
    port_file = tmp_path / "port"
    port_file.write_text("8123", encoding="utf-8")
    stderr, _ = _run_bridge(LQRAG_PORT_FILE=str(port_file))
    assert "ПРЕДУПРЕЖДЕНИЕ" not in stderr
    assert "127.0.0.1:8123" in stderr


def test_startup_log_names_the_port_file(tmp_path):
    """Какой порт-файл выбран — видно из лога без отладчика."""
    port_file = tmp_path / "port"
    port_file.write_text("8124", encoding="utf-8")
    stderr, _ = _run_bridge(LQRAG_PORT_FILE=str(port_file))
    assert str(port_file) in stderr


def test_startup_warning_lists_checked_port_files(tmp_path):
    missing = tmp_path / "нет" / "port"
    stderr, _ = _run_bridge(LQRAG_PORT_FILE=str(missing))
    assert str(missing) in stderr


# ---------------------------------------------------------------------------
# TransportError: подсказка в тексте ошибки
# ---------------------------------------------------------------------------
def test_transport_error_hint_when_fallback(monkeypatch, tmp_path):
    """Падение обращения при fallback должно вести к проверке сервиса."""
    from mcp_server import TransportError, _http_json

    missing = tmp_path / "port"
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(missing))
    # Настоящий 8000 на машине разработчика может быть занят — берём закрытый.
    fallback = "http://127.0.0.1:%d" % _closed_port()
    monkeypatch.setattr(mcp_server, "FALLBACK_URL", fallback)
    with pytest.raises(TransportError) as exc:
        _http_json("GET", "/api/v1/health")
    text = str(exc.value)
    assert fallback in text
    assert str(missing) in text
    assert "systemctl --user status lib-local-rag.service" in text
    # Автозапуск выключен: обещать «запускается» при отсутствующем порт-файле
    # нельзя — служба не поднимается.
    assert "запускается" not in text
    assert "LQRAG_AUTOSTART=0" in text


# ---------------------------------------------------------------------------
# Прокси из окружения агента сайдкару не нужен
# ---------------------------------------------------------------------------
class _BadGateway(BaseHTTPRequestHandler):
    """Корпоративный прокси, который локальный адрес не пропускает."""

    hits = None

    def _answer(self):
        self.hits.append(self.path)
        data = b"<html>bad gateway</html>"
        self.send_response(502)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        self._answer()

    def do_POST(self):  # noqa: N802
        self._answer()

    def log_message(self, *args):
        """Не засорять вывод pytest."""


@pytest.fixture
def bad_proxy():
    hits = []
    handler = type("_Bound", (_BadGateway,), {"hits": hits})
    server = _QuietHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1], hits
    server.shutdown()
    server.server_close()


def test_sidecar_bypasses_proxy_from_env(stub, bridge_factory, bad_proxy, tmp_path):  # noqa: F811
    """http_proxy в окружении агента, а no_proxy не покрывает 127.0.0.1.

    Раньше запрос уходил на прокси, его 502 мост принимал за ответ живого
    демона: проба не звала автозапуск, а агент получал «LocalRAG API вернул 502».
    """
    proxy_url, hits = bad_proxy
    stub.set("GET", "/api/v1/health", {"status": "ok"})
    stub.set("GET", "/api/v1/status", {"is_indexing": False, "type": "idle"})
    port_file = _publish(tmp_path / "home", int(stub.url.rsplit(":", 1)[1]))
    bridge = bridge_factory(
        "",
        LQRAG_PORT_FILE=port_file,
        LQRAG_AUTOSTART="0",
        http_proxy=proxy_url,
        HTTP_PROXY=proxy_url,
        no_proxy="",
        NO_PROXY="",
    )
    bridge.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    response = bridge.call("local_index_status")
    assert response["result"]["isError"] is False, call_text(response)
    assert "502" not in call_text(response)
    assert ("GET", "/api/v1/status") in [
        (r["method"], r["path"]) for r in stub.requests
    ]
    assert hits == []
