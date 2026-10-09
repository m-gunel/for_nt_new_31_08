"""Авторизация моста: какой токен он предъявляет, кому и что видит агент на 401.

Токен — носитель секрета, а не настройка: мост берёт его сам из
tokens/<агент>.token рядом с порт-файлом по несекретному ярлыку LQRAG_AGENT.
Отдаёт его только нашему демону: не запасному 127.0.0.1:8000, не внешнему
LQRAG_URL, не чужому слушателю порта и не по редиректу. Битый файл токена не
попадает ни в заголовок, ни в текст ошибки.

Сквозные тесты гоняют мост подпроцессом против заглушки, правила выбора
адресата — вызовом mcp_server напрямую, с поддельными таблицами /proc/net.
"""

import ipaddress
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

import mcp_server
from tests.mcp_fixture import (  # noqa: F401 — фикстуры pytest
    _QuietHTTPServer,
    bridge_factory,
    call_text,
    stub,
)

SEARCH = "/api/v1/search"
AGENT = "gigacode"
# Как secrets.token_urlsafe: ASCII из [A-Za-z0-9_-], не короче 20 символов.
TOKEN = "T-secret-token-0000000001"
OTHER_TOKEN = "T-secret-token-0000000002"
# Кусок секрета в битом файле: его не должно быть ни в ответе, ни в stderr.
SECRET_PART = "Zq7SecretPart9Zq7SecretPart9"

OURS = os.getuid()
THEIRS = OURS + 1
PORT = 8123
LISTEN = "0A"
ESTABLISHED = "01"
# Адреса так, как их печатает ядро little-endian машины (x86, arm64).
V4_LOOPBACK = "0100007F"
V4_ANY = "00000000"
V6_ANY = "0" * 32
V6_LOOPBACK = "00000000000000000000000001000000"
V6_MAPPED_LOOPBACK = "0000000000000000FFFF00000100007F"
_PROC_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode\n"
)
little_endian = pytest.mark.skipif(
    sys.byteorder != "little", reason="адреса таблиц записаны как у little-endian"
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Ни дом разработчика, ни его LQRAG_*, ни настоящий /proc не решают исход."""
    for key in [key for key in os.environ if key.startswith("LQRAG_")]:
        monkeypatch.delenv(key)
    # Мосты-подпроцессы наследуют это: настоящую службу не трогаем.
    monkeypatch.setenv("LQRAG_AUTOSTART", "0")
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", None)
    monkeypatch.setattr(mcp_server, "LQRAG_TOKEN_FILE", None)
    monkeypatch.setattr(mcp_server, "AGENT_NAME", "")
    monkeypatch.setattr(
        mcp_server,
        "_port_file_candidates",
        lambda: [str(tmp_path / "нет-дома" / "port")],
    )
    missing = tmp_path / "нет-proc"
    monkeypatch.setattr(
        mcp_server, "PROC_NET_TABLES", (str(missing / "tcp"), str(missing / "tcp6"))
    )
    monkeypatch.setattr(mcp_server, "_autostart", mcp_server._autostart_state())


def _sent_headers(recorder, method="POST", path=SEARCH):
    return [
        r["headers"]
        for r in recorder.requests
        if r["method"] == method and r["path"] == path
    ]


def _answer_ok(sidecar):
    sidecar.set("POST", SEARCH, {"results": [], "total": 0})


def _write_token(path, content=TOKEN):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    return path


def _publish(root, port, pid):
    """Порт и pid — как их публикует демон; возвращает путь порт-файла."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "port").write_text("%d\n" % port, encoding="utf-8")
    (root / "pid").write_text("%d\n" % pid, encoding="utf-8")
    return root / "port"


def _port(sidecar):
    return int(sidecar.url.rsplit(":", 1)[1])


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _foreign_pid():
    """pid процесса другой учётки: init, если сигналить ему нам не положено."""
    try:
        os.kill(1, 0)
    except PermissionError:
        return 1
    pytest.skip("pid 1 принадлежит этой учётке (root или контейнер)")


def _fake_proc(monkeypatch, tmp_path, sockets):
    """Таблицы /proc/net/tcp и tcp6 из (адрес, порт, uid, состояние)."""
    rows = {8: [_PROC_HEADER], 32: [_PROC_HEADER]}
    for number, (address, port, uid, state) in enumerate(sockets):
        remote = "0" * len(address) + ":0000"
        rows[len(address)].append(
            "%4d: %s:%04X %s %s 00000000:00000000 00:00000000 00000000 %5d "
            "0 %d 1 0000000000000000 100 0 0 10 0\n"
            % (number, address, port, remote, state, uid, 10000 + number)
        )
    tcp, tcp6 = tmp_path / "tcp", tmp_path / "tcp6"
    tcp.write_text("".join(rows[8]), encoding="ascii")
    tcp6.write_text("".join(rows[32]), encoding="ascii")
    monkeypatch.setattr(mcp_server, "PROC_NET_TABLES", (str(tcp), str(tcp6)))


def _use_token_file(monkeypatch, tmp_path):
    path = _write_token(tmp_path / "bridge.token")
    monkeypatch.setattr(mcp_server, "LQRAG_TOKEN_FILE", str(path))


# --- что мост предъявляет: сквозные тесты подпроцессом ---


def test_explicit_token_file_becomes_bearer(stub, bridge_factory, tmp_path):  # noqa: F811
    secret = _write_token(tmp_path / "bridge.token", TOKEN + "\n")
    bridge = bridge_factory(stub.url, LQRAG_TOKEN_FILE=secret)
    _answer_ok(stub)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is False
    assert _sent_headers(stub)[0].get("Authorization") == "Bearer " + TOKEN


def test_token_comes_from_port_file_root(stub, bridge_factory, tmp_path):  # noqa: F811
    # Продовый путь: в конфиге агента только ярлык, токен — в tokens/ того же
    # корня, где демон опубликовал порт и pid.
    root = tmp_path / "home" / ".files-search"
    port_file = _publish(root, _port(stub), os.getpid())
    _write_token(root / "tokens" / (AGENT + ".token"))
    bridge = bridge_factory("", LQRAG_PORT_FILE=port_file, LQRAG_AGENT=AGENT)
    _answer_ok(stub)

    bridge.call("local_search", {"query": "x"})

    assert _sent_headers(stub)[0].get("Authorization") == "Bearer " + TOKEN


def test_token_reloads_without_restart(stub, bridge_factory, tmp_path):  # noqa: F811
    # Ротация подменяет файл, а не перезапускает агент: следующий запрос
    # уходит уже с новым токеном.
    secret = _write_token(tmp_path / "bridge.token", TOKEN)
    bridge = bridge_factory(stub.url, LQRAG_TOKEN_FILE=secret)
    _answer_ok(stub)
    bridge.call("local_search", {"query": "x"})

    _write_token(secret, OTHER_TOKEN)
    bridge.call("local_search", {"query": "x"})

    sent = _sent_headers(stub)
    assert sent[0]["Authorization"] == "Bearer " + TOKEN
    assert sent[-1]["Authorization"] == "Bearer " + OTHER_TOKEN


def test_no_agent_sends_no_header(stub, bridge_factory):  # noqa: F811
    # Без ярлыка мост ничего не выдумывает: решение пускать или нет — за демоном.
    _answer_ok(stub)
    bridge = bridge_factory(stub.url)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is False
    assert "Authorization" not in _sent_headers(stub)[0]


def test_missing_token_file_sends_nothing(stub, bridge_factory, tmp_path):  # noqa: F811
    # Явный путь эксклюзивен: нет файла — нет заголовка, а не «поищем по домам».
    bridge = bridge_factory(
        stub.url,
        LQRAG_TOKEN_FILE=tmp_path / "несуществующий.token",
        LQRAG_AGENT=AGENT,
    )
    _answer_ok(stub)

    bridge.call("local_search", {"query": "x"})

    assert "Authorization" not in _sent_headers(stub)[0]


def test_401_becomes_explaining_error_text(stub, bridge_factory):  # noqa: F811
    # Агент должен понять 401 как «перезапусти меня или зови --doctor».
    stub.set("POST", SEARCH, {"detail": "требуется токен агента"}, status=401)
    bridge = bridge_factory(stub.url, LQRAG_AGENT=AGENT)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "токена агента" in text
    assert "register_mcp.py" in text
    assert "Bearer" not in text


def test_401_text_never_echoes_token(stub, bridge_factory, tmp_path):  # noqa: F811
    # Даже если ответ повторяет заголовок, агенту уходит только рецепт.
    secret = _write_token(tmp_path / "bridge.token")
    stub.set("POST", SEARCH, {"detail": "Bearer " + TOKEN}, status=401)
    bridge = bridge_factory(stub.url, LQRAG_TOKEN_FILE=secret)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is True
    assert TOKEN not in call_text(response)


# --- битый файл токена и ярлык агента ---


@pytest.mark.parametrize(
    "content",
    [
        pytest.param((SECRET_PART + "\n" + SECRET_PART).encode(), id="две-строки"),
        pytest.param(b"\xff" + SECRET_PART.encode(), id="не-utf8"),
        pytest.param((SECRET_PART + "ж").encode("utf-8"), id="кириллица"),
        pytest.param(b"short-token", id="короткий"),
        pytest.param(b"", id="пустой"),
    ],
)
def test_broken_token_file_is_reported(stub, bridge_factory, tmp_path, content):  # noqa: F811
    # Перевод строки в заголовке http.client вернул бы в ValueError вместе с
    # токеном, а текст ошибки уходит агенту и в stderr.
    secret = _write_token(tmp_path / "bridge.token", content)
    bridge = bridge_factory(stub.url, LQRAG_TOKEN_FILE=secret)
    _answer_ok(stub)

    response = bridge.call("local_search", {"query": "x"})
    bridge.close()

    text = call_text(response)
    assert response["result"]["isError"] is True
    assert "повреждён" in text and str(secret) in text
    assert stub.requests == []
    assert SECRET_PART not in text + bridge.stderr()


def test_unreadable_token_file_is_reported(stub, bridge_factory, tmp_path):  # noqa: F811
    bridge = bridge_factory(stub.url, LQRAG_TOKEN_FILE=tmp_path)
    _answer_ok(stub)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is True
    assert "повреждён" in call_text(response)
    assert stub.requests == []


def test_agent_label_cannot_escape_tokens_dir(stub, bridge_factory, tmp_path):  # noqa: F811
    # «../x» вывел бы путь из tokens/ к любому .token рядом.
    root = tmp_path / "home" / ".files-search"
    port_file = _publish(root, _port(stub), os.getpid())
    (root / "tokens").mkdir()
    _write_token(root / "escaped.token")
    bridge = bridge_factory("", LQRAG_PORT_FILE=port_file, LQRAG_AGENT="../escaped")
    _answer_ok(stub)

    bridge.call("local_search", {"query": "x"})
    bridge.close()

    assert "Authorization" not in _sent_headers(stub)[0]
    stderr = bridge.stderr()
    assert "LQRAG_AGENT не имя агента" in stderr
    assert "../escaped" not in stderr


# --- кому токен не положен ---


class _Redirect(BaseHTTPRequestHandler):
    target = None

    def _redirect(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(302)
        self.send_header("Location", self.target + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        self._redirect()

    def do_POST(self):  # noqa: N802
        self._redirect()

    def log_message(self, *args):
        """Не засорять вывод pytest."""


@pytest.fixture
def redirector(stub):  # noqa: F811
    """Наш loopback-адрес, который отвечает 302 на заглушку."""
    handler = type("_Bound", (_Redirect,), {"target": stub.url})
    server = _QuietHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def test_redirect_is_not_followed(stub, redirector, bridge_factory, tmp_path):  # noqa: F811
    # urllib перенёс бы Authorization на адрес из Location.
    secret = _write_token(tmp_path / "bridge.token")
    bridge = bridge_factory(redirector, LQRAG_TOKEN_FILE=secret)
    _answer_ok(stub)

    response = bridge.call("local_search", {"query": "x"})

    assert response["result"]["isError"] is True
    assert stub.requests == []


def test_fallback_address_never_gets_token(monkeypatch, stub, tmp_path):  # noqa: F811
    # Порт-файла нет: на запасном 8000 может слушать кто угодно.
    monkeypatch.setattr(mcp_server, "FALLBACK_URL", stub.url)
    _use_token_file(monkeypatch, tmp_path)
    _answer_ok(stub)

    mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert "Authorization" not in _sent_headers(stub)[0]


@pytest.mark.parametrize(
    "url, gets_token",
    [
        ("http://127.0.0.1:8123", True),
        ("http://[::1]:8123", True),
        ("http://10.0.0.5:8123", False),
        ("http://rag.example.org:8123", False),
    ],
)
def test_only_loopback_url_gets_token(monkeypatch, tmp_path, url, gets_token):
    _use_token_file(monkeypatch, tmp_path)

    headers = mcp_server._route_auth("env", url, None)

    assert ("Authorization" in headers) is gets_token


@pytest.mark.parametrize(
    "url, pinned",
    [
        ("http://localhost:8123", "http://127.0.0.1:8123"),
        ("http://LocalHost:8123/", "http://127.0.0.1:8123"),
        ("http://localhost", "http://127.0.0.1"),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
        ("http://[::1]:8123", "http://[::1]:8123"),
    ],
)
def test_localhost_url_is_pinned_to_checked_address(monkeypatch, url, pinned):
    # По имени urllib мог бы уйти на ::1, владельца которого мост не проверял.
    monkeypatch.setenv("LQRAG_URL", url)

    assert mcp_server._resolve_base_url() == (pinned, "env")


@pytest.mark.parametrize(
    "make_pid",
    [pytest.param(_dead_pid, id="мёртвый"), pytest.param(_foreign_pid, id="чужой")],
)
def test_stale_port_file_gets_no_token(monkeypatch, stub, tmp_path, make_pid):  # noqa: F811
    # Устаревший порт-файл: pid умер или достался чужой учётке, порт мог
    # занять кто угодно.
    root = tmp_path / "home" / ".files-search"
    port_file = _publish(root, _port(stub), make_pid())
    _write_token(root / "tokens" / (AGENT + ".token"))
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    monkeypatch.setattr(mcp_server, "AGENT_NAME", AGENT)
    _answer_ok(stub)

    mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert "Authorization" not in _sent_headers(stub)[0]


def test_token_follows_chosen_port_file_not_first_home(monkeypatch, stub, tmp_path):  # noqa: F811
    # Устаревший порт-файл в $HOME со своим токеном, живой демон — в pw_dir:
    # демону pw_dir уходит токен pw_dir, а не первый найденный по домам.
    home = tmp_path / "home" / ".files-search"
    pw_dir = tmp_path / "pwdir" / ".files-search"
    _publish(home, _closed_port(), _dead_pid())
    _write_token(home / "tokens" / (AGENT + ".token"), OTHER_TOKEN)
    _publish(pw_dir, _port(stub), os.getpid())
    _write_token(pw_dir / "tokens" / (AGENT + ".token"), TOKEN)
    candidates = [str(home / "port"), str(pw_dir / "port")]
    monkeypatch.setattr(mcp_server, "_port_file_candidates", lambda: candidates)
    monkeypatch.setattr(mcp_server, "AGENT_NAME", AGENT)
    _answer_ok(stub)

    mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert _sent_headers(stub)[0].get("Authorization") == "Bearer " + TOKEN


# --- владелец слушающего порта (Linux, /proc/net) ---


@little_endian
def test_foreign_listener_gets_nothing(monkeypatch, stub, tmp_path):  # noqa: F811
    # Порт слушает другая учётка: ни токена, ни самого запроса.
    _fake_proc(monkeypatch, tmp_path, [(V4_LOOPBACK, _port(stub), THEIRS, LISTEN)])
    monkeypatch.setenv("LQRAG_URL", stub.url)
    _use_token_file(monkeypatch, tmp_path)
    _answer_ok(stub)

    with pytest.raises(mcp_server.TransportError) as caught:
        mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert "занят чужим процессом" in str(caught.value)
    assert stub.requests == []


@little_endian
def test_foreign_listener_gets_no_request_even_without_token(
    monkeypatch,
    stub,  # noqa: F811
    tmp_path,
):
    # Запасной адрес без токена: ответ чужой учётки — это выдача поиска и
    # папки, которыми ограничено чтение файлов.
    monkeypatch.setattr(mcp_server, "FALLBACK_URL", stub.url)
    _fake_proc(monkeypatch, tmp_path, [(V4_LOOPBACK, _port(stub), THEIRS, LISTEN)])
    _answer_ok(stub)

    with pytest.raises(mcp_server.TransportError):
        mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert stub.requests == []


@little_endian
def test_probe_does_not_trust_foreign_listener(monkeypatch, stub, tmp_path):  # noqa: F811
    # Чужой ответ на health — не наш демон: проба его не шлёт и зовёт systemd.
    monkeypatch.setattr(mcp_server, "FALLBACK_URL", stub.url)
    _fake_proc(monkeypatch, tmp_path, [(V4_LOOPBACK, _port(stub), THEIRS, LISTEN)])
    stub.set("GET", "/api/v1/health", {"status": "ok"})
    asked = []
    monkeypatch.setattr(mcp_server, "_request_systemd", lambda: asked.append(True))

    mcp_server._probe_and_autostart()

    assert stub.requests == []
    assert asked == [True]


@little_endian
def test_own_listener_gets_token(monkeypatch, stub, tmp_path):  # noqa: F811
    _fake_proc(monkeypatch, tmp_path, [(V4_LOOPBACK, _port(stub), OURS, LISTEN)])
    monkeypatch.setenv("LQRAG_URL", stub.url)
    _use_token_file(monkeypatch, tmp_path)
    _answer_ok(stub)

    mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert _sent_headers(stub)[0].get("Authorization") == "Bearer " + TOKEN


@little_endian
def test_unseen_listener_gets_no_token(monkeypatch, stub, tmp_path):  # noqa: F811
    # В таблице слушателя нет — его владельца не проверить, токен не шлём.
    _fake_proc(monkeypatch, tmp_path, [])
    monkeypatch.setenv("LQRAG_URL", stub.url)
    _use_token_file(monkeypatch, tmp_path)
    _answer_ok(stub)

    mcp_server._http_json("POST", SEARCH, {"query": "x"})

    assert "Authorization" not in _sent_headers(stub)[0]


@little_endian
@pytest.mark.parametrize(
    "sockets, expected",
    [
        pytest.param([(V4_LOOPBACK, PORT, OURS, LISTEN)], "ours", id="наш"),
        pytest.param([(V4_LOOPBACK, PORT, THEIRS, LISTEN)], "foreign", id="чужой"),
        pytest.param(
            [(V4_LOOPBACK, PORT, OURS, LISTEN), (V6_ANY, PORT, THEIRS, LISTEN)],
            "ours",
            id="точный-адрес-важнее-подстановочного",
        ),
        pytest.param([(V4_ANY, PORT, THEIRS, LISTEN)], "foreign", id="чужой-0.0.0.0"),
        pytest.param([(V6_ANY, PORT, OURS, LISTEN)], "ours", id="наш-dual-stack"),
        pytest.param(
            [(V6_MAPPED_LOOPBACK, PORT, THEIRS, LISTEN)], "foreign", id="чужой-mapped"
        ),
        pytest.param(
            [(V6_LOOPBACK, PORT, THEIRS, LISTEN)], "absent", id="::1-не-127.0.0.1"
        ),
        pytest.param(
            [(V4_LOOPBACK, PORT + 1, THEIRS, LISTEN)], "absent", id="другой-порт"
        ),
        pytest.param(
            [(V4_LOOPBACK, PORT, THEIRS, ESTABLISHED)], "absent", id="не-LISTEN"
        ),
    ],
)
def test_listener_owner_from_proc_tables(monkeypatch, tmp_path, sockets, expected):
    _fake_proc(monkeypatch, tmp_path, sockets)

    owner = mcp_server._listener_owner([ipaddress.ip_address("127.0.0.1")], PORT)

    assert owner == expected


def test_listener_owner_unknown_without_proc():
    # macOS-разработка: таблиц нет, проверка пропускается.
    assert mcp_server._listener_owner([ipaddress.ip_address("127.0.0.1")], PORT) is None
