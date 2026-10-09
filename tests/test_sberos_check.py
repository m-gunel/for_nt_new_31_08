"""scripts/sberos_check.py: дом демона, порт, прокси, контракт API и приватность отчёта.

Скрипт лежит в scripts/ и пакетом не является, поэтому грузится по пути.
Дома подменены на tmp: настоящий ~/.files-search тест не читает.
"""

import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from tests.mcp_fixture import StubSidecar

_SPEC = importlib.util.spec_from_file_location(
    "sberos_check", Path(__file__).resolve().parents[1] / "scripts" / "sberos_check.py"
)
chk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(chk)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """$HOME терминала и дом из passwd — разные каталоги, как на SberOS."""
    term = tmp_path / "work"
    pwdir = tmp_path / "pwdir"
    term.mkdir()
    pwdir.mkdir()
    monkeypatch.setenv("HOME", str(term))
    monkeypatch.setattr(chk, "home_candidates", lambda: [str(term), str(pwdir)])
    return term, pwdir


@pytest.fixture
def sidecar():
    stub = StubSidecar()
    stub.set("GET", "/api/v1/health", {"status": "ok"})
    yield stub
    stub.close()


def _dead_pid():
    """pid процесса, который уже завершился и прибран."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _publish(home, port=None, pid=None):
    """port и pid — так, как их кладёт демон (src/port_file.py)."""
    root = home / ".files-search"
    root.mkdir(parents=True, exist_ok=True)
    if port is not None:
        (root / "port").write_text("%d\n" % port, encoding="utf-8")
    if pid is not None:
        (root / "pid").write_text("%d\n" % pid, encoding="utf-8")
    return root


def _run_main(monkeypatch, tmp_path, *argv, package_dir=None):
    """main() целиком: отчёт — в tmp, а не рядом со скриптом.

    Тесты публикуют pid своего процесса: без него find_daemon_pid пошёл бы
    по /proc/*/cmdline и мерил бы чужой процесс.
    """
    report_file = tmp_path / "report.json"
    monkeypatch.setattr(chk, "REPORT_FILE", str(report_file))
    monkeypatch.setattr(chk, "PACKAGE_DIR", str(package_dir or tmp_path / "no-package"))
    monkeypatch.setattr(
        sys, "argv", ["sberos_check.py", "--resource-seconds", "0", *argv]
    )
    chk.main()
    return json.loads(report_file.read_text(encoding="utf-8"))


# T031: данные демона — в его доме, порт из порт-файла, pid, сборка в /opt.


def test_daemon_home_prefers_live_service_over_stale_terminal_home(homes):
    term, pwdir = homes
    _publish(term, 8131, _dead_pid())
    _publish(pwdir, 8132, os.getpid())
    assert chk.find_daemon_home() == str(pwdir)


def test_daemon_home_without_port_files_is_the_one_with_data(homes):
    term, pwdir = homes
    assert chk.find_daemon_home() == str(term)
    _publish(pwdir)
    assert chk.find_daemon_home() == str(pwdir)


def test_default_run_reads_everything_from_daemon_home(
    homes, sidecar, monkeypatch, tmp_path
):
    """Запуск по README без флагов: служба живёт в доме из passwd."""
    _, pwdir = homes
    _publish(pwdir, int(sidecar.url.rsplit(":", 1)[1]), os.getpid())
    package = tmp_path / "opt"
    package.mkdir()

    report = _run_main(monkeypatch, tmp_path, package_dir=package)

    sections = report["sections"]
    assert report["daemon_home"] == "passwd"
    assert set(sections) == {"inotify", "runtime", "resources", "frozen_lance"}
    assert sections["runtime"]["verdict"] == "PASS", sections["runtime"]
    assert ("GET", "/api/v1/health") in [
        (r["method"], r["path"]) for r in sidecar.requests
    ]
    assert sections["frozen_lance"]["details"]["dist_dir"] == str(package)


def test_runtime_uses_current_api_contract(sidecar):
    """search — без устаревшего sources; других ручек, кроме четырёх, не зовёт."""
    sidecar.set("GET", "/api/v1/status", {"is_indexing": False, "files_to_index": 0})
    sidecar.set("GET", "/api/v1/config", {"folders": ["/home/u/a", "/home/u/b"]})
    hit = {"id": "f1", "type": "file", "title": "t", "chunks": []}
    sidecar.set("POST", "/api/v1/search", {"results": [hit], "total": 1})
    report = {"sections": {}}

    chk.check_runtime(report, int(sidecar.url.rsplit(":", 1)[1]))

    assert {r["path"] for r in sidecar.requests} == {
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/config",
        "/api/v1/search",
    }
    assert sidecar.bodies("POST", "/api/v1/search") == [
        {"query": "тест", "limit": 5, "query_type": "hybrid"}
    ]
    details = report["sections"]["runtime"]["details"]
    assert details["config"] == {"status": 200, "folders_count": 2}
    assert details["search"] == {"status": 200, "total": 1, "result_types": {"file": 1}}


# Токен агента: без него API, кроме health, отвечает 401, и это не PASS.

GUARDED = (
    ("GET", "/api/v1/status"),
    ("GET", "/api/v1/config"),
    ("POST", "/api/v1/search"),
)
TOKEN = "T-secret-token-0000000001"


def _deny_without_token(sidecar):
    for method, path in GUARDED:
        sidecar.set(method, path, {"detail": "требуется токен агента"}, status=401)


def test_runtime_without_token_is_warn_not_pass(sidecar):
    _deny_without_token(sidecar)
    report = {"sections": {}}

    chk.check_runtime(report, int(sidecar.url.rsplit(":", 1)[1]))

    runtime = report["sections"]["runtime"]
    assert runtime["verdict"] == "WARN"
    assert "--token-file" in runtime["details"]["note"]


def _token_file(tmp_path):
    token_file = tmp_path / "wsc.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    return str(token_file)


def _sent_token(monkeypatch, tmp_path, *argv, owner="ours"):
    """(токен, причина отказа), с которыми main() зовёт check_runtime.

    owner — кого «видно» в /proc/net на порту: на Linux таблицы настоящие.
    """
    monkeypatch.setattr(chk.mcp_server, "_listener_owner", lambda targets, port: owner)
    seen = []
    monkeypatch.setattr(
        chk,
        "check_runtime",
        lambda report, port, token, refusal: seen.append((token, refusal)),
    )
    _run_main(monkeypatch, tmp_path, "--token-file", _token_file(tmp_path), *argv)
    return seen[0]


def test_token_file_option_reaches_runtime_check(homes, monkeypatch, tmp_path):
    term, _ = homes
    _publish(term, 43123, os.getpid())

    assert _sent_token(monkeypatch, tmp_path) == (TOKEN, None)


@pytest.mark.parametrize(
    "port, pid",
    [
        pytest.param(None, os.getpid(), id="порт-8000-по-умолчанию"),
        pytest.param(43123, None, id="нет-pid"),
        pytest.param(43123, "dead", id="мёртвый-pid"),
    ],
)
def test_token_not_sent_to_unverified_port(homes, monkeypatch, tmp_path, port, pid):
    # На 8000 или на порту устаревшего файла может слушать кто угодно.
    term, _ = homes
    _publish(term, port, _dead_pid() if pid == "dead" else pid)

    token, refusal = _sent_token(monkeypatch, tmp_path)

    assert token is None
    assert refusal


def test_token_not_sent_to_foreign_listener(homes, monkeypatch, tmp_path):
    term, _ = homes
    _publish(term, 43123, os.getpid())

    token, refusal = _sent_token(monkeypatch, tmp_path, owner="foreign")

    assert token is None
    assert "другой учётки" in refusal


def test_refused_token_is_explained_in_report(sidecar):
    _deny_without_token(sidecar)
    report = {"sections": {}}

    chk.check_runtime(report, int(sidecar.url.rsplit(":", 1)[1]), None, "причина")

    runtime = report["sections"]["runtime"]
    assert runtime["verdict"] == "WARN"
    assert "причина" in runtime["details"]["note"]


def test_runtime_puts_token_into_guarded_requests_only(sidecar):
    report = {"sections": {}}

    chk.check_runtime(report, int(sidecar.url.rsplit(":", 1)[1]), TOKEN)

    sent = {(r["method"], r["path"]): r["headers"] for r in sidecar.requests}
    assert "Authorization" not in sent[("GET", "/api/v1/health")]
    assert {sent[op].get("Authorization") for op in GUARDED} == {"Bearer " + TOKEN}


def test_rejected_token_is_fail(sidecar):
    _deny_without_token(sidecar)
    report = {"sections": {}}

    chk.check_runtime(report, int(sidecar.url.rsplit(":", 1)[1]), TOKEN)

    assert report["sections"]["runtime"]["verdict"] == "FAIL"


@pytest.mark.parametrize("content", ["", "короткий", TOKEN + "\nвторая строка"])
def test_broken_token_file_stops_without_echoing_it(tmp_path, content):
    token_file = tmp_path / "broken.token"
    token_file.write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit) as stop:
        chk.read_token(str(token_file))

    assert TOKEN not in str(stop.value) and "строка" not in str(stop.value)


@pytest.mark.parametrize(
    "argv, published, expected",
    [
        ((), 43123, 43123),
        (("--port", "9123"), 43123, 9123),
        ((), None, 8000),
    ],
)
def test_port_option_then_port_file_then_8000(
    homes, monkeypatch, tmp_path, argv, published, expected
):
    term, _ = homes
    _publish(term, published, os.getpid())
    ports = []
    monkeypatch.setattr(
        chk, "check_runtime", lambda report, port, token, refusal: ports.append(port)
    )
    _run_main(monkeypatch, tmp_path, *argv)
    assert ports == [expected]


@pytest.mark.parametrize("installed", [True, False])
def test_dist_defaults_to_package_dir_when_installed(
    homes, monkeypatch, tmp_path, installed
):
    term, _ = homes
    _publish(term, pid=os.getpid())
    package = tmp_path / "opt"
    if installed:
        package.mkdir()
    dists = []
    monkeypatch.setattr(chk, "check_runtime", lambda report, port, token, refusal: None)
    monkeypatch.setattr(chk, "check_frozen_lance", lambda report, d: dists.append(d))
    _run_main(monkeypatch, tmp_path, package_dir=package)
    assert dists == [str(package) if installed else "dist/main"]


@pytest.mark.parametrize(
    "content, expected",
    [("8123\n", 8123), ("мусор", None), ("0", None), ("70000", None)],
)
def test_read_port_ignores_garbage(tmp_path, content, expected):
    (tmp_path / "port").write_text(content, encoding="utf-8")
    assert chk.read_port(str(tmp_path)) == expected


def test_pid_comes_from_daemon_pid_file(tmp_path):
    root = _publish(tmp_path, 8130, os.getpid())
    assert chk.find_daemon_pid(str(root)) == os.getpid()


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        ("/opt/lib-local-rag/main\0", True),  # пакет: ExecStart юнита
        ("/home/u/lib-local-rag/main/main\0", True),  # zip: $DIR/main/main
        ("main/main\0", True),
        ("/home/u/proj/dist/main/main\0", True),  # сборка из чекаута
        (".venv/bin/python\0main.py\0", True),  # ручной запуск
        ("/opt/lib-local-rag/main\0--ocr-worker\0", False),  # дочерний OCR
        ("/usr/lib/domain/main\0", False),
        ("python3\0scripts/sberos_check.py\0", False),
    ],
)
def test_daemon_cmdline(cmdline, expected):
    assert chk.is_daemon_cmdline(cmdline) is expected


# T036: в «безопасный для пересылки» отчёт не попадают логин и пути.


@pytest.mark.parametrize("exists", [True, False])
def test_dist_path_in_report_hides_login(homes, monkeypatch, tmp_path, exists):
    _, pwdir = homes
    _publish(pwdir, pid=os.getpid())
    dist = pwdir / "lib-local-rag" / "main"
    if exists:
        dist.mkdir(parents=True)
    monkeypatch.setattr(chk, "check_runtime", lambda report, port, token, refusal: None)

    report = _run_main(monkeypatch, tmp_path, "--dist", str(dist))

    assert str(pwdir) not in json.dumps(report, ensure_ascii=False)
    lance = report["sections"]["frozen_lance"]["details"]
    shown = lance["dist_dir"] if exists else lance["note"]
    assert "~/lib-local-rag/main" in shown


# T057: к демону — мимо системного прокси.


def test_http_json_bypasses_proxy_from_env(sidecar, monkeypatch):
    """http_proxy в окружении, no_proxy не покрывает 127.0.0.1 — как на АРМ."""
    proxy = StubSidecar()  # без маршрутов: запрос через него получил бы 404
    try:
        for name in ("http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(name, "localhost")
        monkeypatch.delenv("REQUEST_METHOD", raising=False)
        # urlopen собирает общий opener один раз на процесс: сброс, чтобы
        # прокси из окружения увидел и штатный путь.
        monkeypatch.setattr(urllib.request, "_opener", None)
        result = chk.http_json("GET", sidecar.url + "/api/v1/health")
    finally:
        proxy.close()
    assert result == (200, {"status": "ok"})
    assert proxy.requests == []
