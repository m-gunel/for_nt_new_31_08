"""scripts/sberos_check.py: дом демона, порт, прокси и приватность отчёта.

Скрипт лежит в scripts/ и пакетом не является, поэтому грузится по пути.
Дома подменены на tmp: настоящие ~/.files-search, ~/.ragsearch и
~/.thunderbird тест не читает. user_conf.yml пишется так же, как его пишет
демон (Config.update_user_config: yaml.dump(UserConfig.model_dump())).
"""

import base64
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from email import message_from_bytes
from pathlib import Path

import pytest
import yaml

from src.server.model import EmailConfig, UserConfig
from src.thunderbird.find_files import find_email_root
from src.thunderbird.get_body import process_message
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


def _user_conf(home, **email):
    """user_conf.yml так, как его пишет демон."""
    path = home / ".ragsearch" / "user_conf.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    conf = UserConfig(folders=["/home/u/docs"], email=EmailConfig(**email))
    path.write_text(yaml.dump(conf.model_dump()), encoding="utf-8")
    return path


def _mailbox(root, *names):
    """Папки Thunderbird: mbox + .msf, по одному письму в каждой."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_bytes(
            b"From - Mon Jan 1 00:00:00 2024\nSubject: x\n\nbody\n"
        )
        (root / (name + ".msf")).write_bytes(b"")
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
    root = _publish(pwdir, int(sidecar.url.rsplit(":", 1)[1]), os.getpid())
    state = {"version": 2, "folders": {"INBOX": {"offset": 0, "size": 1, "mtime": 0}}}
    (root / "offsets.json").write_text(json.dumps(state), encoding="utf-8")
    _user_conf(pwdir, enabled=True)
    tb = pwdir / ".thunderbird" / "p.default" / "ImapMail" / "imap.sberbank.ru"
    _mailbox(tb, "INBOX")
    package = tmp_path / "opt"
    package.mkdir()

    report = _run_main(monkeypatch, tmp_path, package_dir=package)

    sections = report["sections"]
    assert report["daemon_home"] == "passwd"
    assert sections["runtime"]["verdict"] == "PASS", sections["runtime"]
    assert ("GET", "/api/v1/health") in [
        (r["method"], r["path"]) for r in sidecar.requests
    ]
    assert sections["profile"]["details"]["chosen_host"] == "imap.sberbank.ru"
    assert sections["offsets"]["verdict"] == "PASS", sections["offsets"]
    assert sections["offsets"]["details"]["user_conf"]["email_enabled"] is True
    assert sections["frozen_lance"]["details"]["dist_dir"] == str(package)


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
    monkeypatch.setattr(chk, "check_runtime", lambda report, port: ports.append(port))
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
    monkeypatch.setattr(chk, "check_runtime", lambda report, port: None)
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


def test_mail_root_host_order_matches_daemon(tmp_path, monkeypatch):
    """Два аккаунта без корпоративного: скрипт и демон берут один корень."""
    imap = tmp_path / ".thunderbird" / "p.default" / "ImapMail"
    for host in ("a.example", "b.example"):
        _mailbox(imap / host, "INBOX")
    # Порядок readdir не алфавитный (ext4, APFS) — задаём его явно. Список
    # каждый раз новый: обе стороны очищают dirnames, чтобы не спускаться.
    monkeypatch.setattr(
        os, "walk", lambda top: iter([(str(imap), ["b.example", "a.example"], [])])
    )
    report = {"sections": {}}
    chosen = chk.check_profile(report, str(tmp_path / ".thunderbird"))
    assert chosen == str(find_email_root(tmp_path))
    assert report["sections"]["profile"]["details"]["chosen_host"] == "a.example"


def test_explicit_email_folder_wins_like_config_email_root(
    homes, monkeypatch, tmp_path
):
    _, pwdir = homes
    _publish(pwdir, pid=os.getpid())
    mail = _mailbox(tmp_path / "mbox", "INBOX")
    _user_conf(pwdir, path_to_email_folder=str(mail))
    _mailbox(pwdir / ".thunderbird" / "p.default" / "ImapMail" / "x.example", "Other")
    monkeypatch.setattr(chk, "check_runtime", lambda report, port: None)

    details = _run_main(monkeypatch, tmp_path)["sections"]["profile"]["details"]

    assert details["email_root_source"] == "path_to_email_folder"
    assert "chosen_host" not in details
    assert [f["name"] for f in details["folders"]] == ["INBOX"]


def test_tilde_in_email_folder_is_daemon_home():
    assert chk.expand_home("~/mail", "/home/DOM@user") == "/home/DOM@user/mail"
    assert chk.expand_home("~", "/home/DOM@user") == "/home/DOM@user"
    assert chk.expand_home("/srv/mail", "/home/DOM@user") == "/srv/mail"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/home/u/mail",
        "~/mail",
        "/home/DOMAIN@LOGIN/.thunderbird/abc.default/ImapMail/imap.sberbank.ru",
        "/home/u/My Mail",
        "/home/u/it's",
        "/home/u/a: b",
        "/home/u/Почта",
        # Длинное значение yaml.dump переносит на следующие строки.
        "/home/u/" + "very long folder name with spaces " * 4,
        "/home/u/Документы/Почта/Архив писем за прошлый год",
    ],
)
def test_parse_user_conf_reads_path_to_email_folder(tmp_path, value):
    path = _user_conf(tmp_path, path_to_email_folder=value)
    assert chk.parse_user_conf(str(path))["path_to_email_folder"] == value


# T036: в «безопасный для пересылки» отчёт не попадают имена папок и логин.


def test_offsets_section_masks_folder_names_from_user_conf(tmp_path):
    offsets = {
        "&BBoEOwQ4BDUEPQRCBEs-": 20,
        "INBOX": 10,
        "INBOX.sbd/Proekt-Romashka": 30,
    }
    conf_path = _user_conf(
        tmp_path, offsets=offsets, path_to_email_folder="/home/DOMAIN@LOGIN/mail"
    )
    report = {"sections": {}}
    chk.check_offsets(
        report,
        None,
        str(tmp_path / ".files-search"),
        chk.parse_user_conf(str(conf_path)),
    )
    text = json.dumps(report, ensure_ascii=False)
    for secret in ("&BBoEOwQ4BDUEPQRCBEs-", "Proekt-Romashka", "DOMAIN@LOGIN"):
        assert secret not in text
    details = report["sections"]["offsets"]["details"]
    assert details["user_conf"]["offsets"]["INBOX"] == 10
    assert details["user_conf"]["path_to_email_folder_set"] is True
    assert details["checked"] == 3


def test_profile_fail_note_hides_login(homes):
    _, pwdir = homes
    report = {"sections": {}}
    assert chk.check_profile(report, str(pwdir / ".thunderbird")) is None
    note = report["sections"]["profile"]["details"]["note"]
    assert "~/.thunderbird" in note
    assert str(pwdir) not in note


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


# T086: отчёт не врёт про папки, дубли и «Архив».


def test_folders_count_from_daemon_written_user_conf(tmp_path):
    """yaml.dump пишет элементы списка с начала строки: «- /a»."""
    path = tmp_path / "user_conf.yml"
    conf = UserConfig(folders=["/a", "/b", "/c"])
    path.write_text(yaml.dump(conf.model_dump()), encoding="utf-8")
    assert chk.parse_user_conf(str(path))["folders_count"] == 3


def test_folders_count_with_indented_list(tmp_path):
    path = tmp_path / "user_conf.yml"
    path.write_text(
        "folders:\n  - /a\n  - /b\nemail:\n  enabled: true\n", encoding="utf-8"
    )
    conf = chk.parse_user_conf(str(path))
    assert (conf["folders_count"], conf["email_enabled"]) == (2, True)


def test_archive_folder_is_known_by_its_real_mutf7_name():
    name = "&BBAEQARFBDgEMg-"
    # mUTF-7: base64 от UTF-16BE между «&» и «-».
    assert base64.b64decode(name[1:-1] + "==").decode("utf-16-be") == "Архив"
    assert chk.mask_folder(name) == "Архив (mUTF-7)"


def test_duplicates_note_matches_single_version_indexing(tmp_path):
    """Из multipart/alternative демон индексирует одну версию, не две."""
    message = (
        b"Subject: x\n"
        b"MIME-Version: 1.0\n"
        b'Content-Type: multipart/alternative; boundary="b"\n\n'
        b"--b\nContent-Type: text/plain\n\nplain\n"
        b"--b\nContent-Type: text/html\n\n<p>html</p>\n--b--\n"
    )
    assert len(list(process_message(message_from_bytes(message)))) == 1
    root = tmp_path / "mail"
    root.mkdir()
    (root / "INBOX").write_bytes(b"From - Mon Jan 1 00:00:00 2024\n" + message)
    (root / "INBOX.msf").write_bytes(b"")
    report = {"sections": {}}
    chk.check_mbox_scan(report, str(root))
    details = report["sections"]["duplicates"]["details"]
    assert details["multipart_alternative"] == 1
    assert "дважды" not in details["note"]
    assert "одна версия" in details["note"]
