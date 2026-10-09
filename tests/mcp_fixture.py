"""Стенд для тестов MCP-моста: мост подпроцессом и стаб сайдкара.

Мост только транслирует вызовы в HTTP, поэтому OpenVINO и индекс не нужны:
стаб отвечает как сайдкар и записывает входящие запросы — тест проверяет и
ответ агенту, и запрос в API.

stdout читает отдельный поток через очередь: `readline()` на мёртвом процессе
блокируется навсегда, и падение моста выглядело бы зависшим прогоном.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"

# Мост — тонкая прослойка над мгновенным стабом, ответ обязан прийти быстро.
READ_TIMEOUT = 15.0
# Сколько ждём, убеждаясь, что ответа НЕ будет (нотификации).
SILENCE_TIMEOUT = 1.0


class _StubHandler(BaseHTTPRequestHandler):
    stub = None

    def _dispatch(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.stub.requests.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                # Заголовки — для тестов авторизации: что мост предъявил.
                "headers": dict(self.headers),
            }
        )

        route = self.stub.routes.get((method, self.path))
        if route is None:
            # Как FastAPI на неизвестный маршрут: по detail "Not Found" мост
            # отличает «ручки нет» (старый демон) от 404 самой ручки.
            self.send_response(404)
            payload = json.dumps(
                {
                    "detail": "Not Found",
                    "stub": "нет заглушки для %s %s" % (method, self.path),
                }
            )
            data = payload.encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        status, payload = route
        if callable(payload):
            status, payload = payload(body)
        if payload is None:
            data = b""
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, *args):
        """Стаб не должен засорять вывод pytest."""


class _QuietHTTPServer(ThreadingHTTPServer):
    """HTTPServer без обратного DNS на старте.

    Штатный `server_bind` зовёт `socket.getfqdn()`, и без быстрого резолвера
    подъём заглушки занимает десятки секунд.
    """

    def server_bind(self):
        from socketserver import TCPServer

        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def handle_error(self, request, client_address):
        """Обрыв клиента посреди ответа — штатная часть проверки таймаута.

        Штатный handle_error печатал бы трейсбек в вывод зелёного прогона.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class StubSidecar:
    """HTTP-заглушка LocalRAG на эфемерном порту."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        handler = type("_Bound", (_StubHandler,), {"stub": self})
        self._server = _QuietHTTPServer(("127.0.0.1", 0), handler)
        # Короткий poll: shutdown() ждёт следующего опроса, дефолтные 0,5 с
        # добавляли бы полсекунды к каждому teardown.
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def set(self, method, path, payload, status=200):
        """Задать ответ. payload может быть callable(body) -> (status, payload)."""
        self.routes[(method, path)] = (status, payload)

    def bodies(self, method, path):
        """Тела запросов, пришедших на этот маршрут — в порядке поступления."""
        return [
            r["body"]
            for r in self.requests
            if r["method"] == method and r["path"] == path
        ]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class Bridge:
    """Запущенный mcp_server.py, с которым говорят как MCP-клиент."""

    def __init__(self, url, **env):
        environment = dict(os.environ)
        # Ярлык и токен из окружения разработчика не должны попасть к заглушке.
        for key in ("LQRAG_AGENT", "LQRAG_TOKEN_FILE"):
            environment.pop(key, None)
        environment["LQRAG_URL"] = url
        environment.setdefault("LQRAG_TIMEOUT", "5")
        for key, value in env.items():
            environment[key] = str(value)
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def send_raw(self, text):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message, ensure_ascii=False))

    def read(self, timeout=READ_TIMEOUT):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                "мост не ответил за %s с. stderr:\n%s" % (timeout, self.stderr())
            ) from None
        if line is None:
            raise AssertionError("мост закрыл stdout. stderr:\n%s" % self.stderr())
        return json.loads(line)

    def expect_silence(self, timeout=SILENCE_TIMEOUT):
        """Убедиться, что ответа не пришло — для нотификаций."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError("на нотификацию пришёл ответ: %s" % line)

    def request(self, method, params=None, timeout=READ_TIMEOUT):
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.read(timeout=timeout)
        assert response.get("id") == self._next_id, response
        return response

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def call(self, name, arguments=None, timeout=READ_TIMEOUT):
        return self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )

    def tools(self):
        return self.request("tools/list")["result"]["tools"]

    def stderr(self):
        """Хвост stderr моста — он туда пишет диагностику."""
        if self.proc.poll() is None:
            return "(процесс жив, stderr не читаем чтобы не блокироваться)"
        return (self.proc.stderr.read() or "")[-2000:]

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def call_text(response):
    """Текст, который увидит агент."""
    return "\n".join(
        part.get("text", "") for part in response["result"].get("content", [])
    )


@pytest.fixture
def stub():
    sidecar = StubSidecar()
    yield sidecar
    sidecar.close()


@pytest.fixture
def bridge_factory():
    """Фабрика мостов: тест сам решает, куда мост смотрит и что ему разрешено."""
    created = []

    def make(url="http://127.0.0.1:9", **env):
        bridge = Bridge(url, **env)
        created.append(bridge)
        return bridge

    yield make
    for bridge in created:
        bridge.close()
