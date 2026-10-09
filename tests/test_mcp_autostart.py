"""Мост сам находит и поднимает демон: порт-файл в pw_dir и автозапуск службы.

Терминал SberOS живёт с HOME=/home/work/<LOGIN@DOMAIN>, а служба systemd
--user публикует порт в домашнем каталоге из passwd — мост ищет в обоих. Если
демон не поднят, мост сам зовёт systemctl (не чаще раза в AUTOSTART_RETRY_S) и
отвечает раньше таймаута агента.

Живость демона — по pid рядом с портом: устаревший порт-файл не заслоняет
живой, а живой, но ещё не слушающий демон — это прогрев, systemd не трогаем.

systemctl всегда заглушка (LQRAG_SYSTEMCTL): она пишет argv и XDG_RUNTIME_DIR
в файл, по которому видно, звали ли её, сколько раз и с чем.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

import mcp_server
from tests.mcp_fixture import (  # noqa: F401 — фикстуры pytest
    bridge_factory,
    call_text,
    stub,
)

UNIT_ARGS = "--user start --no-block lib-local-rag.service"
SHOW_ARGS = "--user show -p ActiveState -p SubState -p NRestarts lib-local-rag.service"
WARMING = "запускается или прогревается"
STUCK = "Индексатор LocalRAG не поднимается"
MISSING = "Служба lib-local-rag.service недоступна для пользователя (не найдена)"
MASKED = (
    "Служба LocalRAG временно остановлена: идёт установка или обновление пакета. "
    "Повторите запрос через минуту."
)
MASKED_STDERR = (
    "Failed to start lib-local-rag.service: Unit lib-local-rag.service is masked."
)
# Таймаут MCP-запроса у WSC по умолчанию — 5 с; ответ должен успеть раньше.
AGENT_DEADLINE_S = 4.0

# Настоящая функция — до того, как autouse-фикстура подменит атрибут модуля.
_real_candidates = mcp_server._port_file_candidates


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Ни дом разработчика, ни его LQRAG_* не должны влиять на результат."""
    for key in [key for key in os.environ if key.startswith("LQRAG_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", None)
    monkeypatch.setattr(
        mcp_server,
        "_port_file_candidates",
        lambda: [str(tmp_path / "нет-дома" / "port")],
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


def _write_port(path, port):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("%d\n" % port, encoding="utf-8")
    return path


def _write_pid(port_file, pid):
    """pid рядом с порт-файлом — как его публикует демон (src/port_file.py)."""
    (port_file.parent / "pid").write_text("%d\n" % pid, encoding="utf-8")


def _dead_pid():
    """pid процесса, который уже завершился и прибран."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _fake_passwd(monkeypatch, pw_dir):
    import pwd

    entry = type("PwEntry", (), {"pw_dir": str(pw_dir)})()
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: entry)


def _systemctl_stub(tmp_path, stderr_text="", code=0, delay=0):
    """Заглушка systemctl. Мусор в stdout — проверка, что он не доедет до агента.

    На `show` отвечает состоянием упавшей службы, остальное — как задано.
    """
    calls = tmp_path / "systemctl-calls.log"
    script = tmp_path / "systemctl"
    lines = [
        "#!/bin/sh",
        'printf \'%s|%s\\n\' "$*" "${XDG_RUNTIME_DIR:-}" >> "' + str(calls) + '"',
        "echo 'мусор в stdout systemctl'",
        'if [ "$2" = show ]; then',
        "  printf 'ActiveState=failed\\nSubState=failed\\nNRestarts=5\\n'",
        "  exit 0",
        "fi",
    ]
    if delay:
        lines.append("sleep %s" % delay)
    if stderr_text:
        lines.append("echo '%s' >&2" % stderr_text)
    lines.append("exit %d" % code)
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script, calls


def _calls(calls):
    if not calls.exists():
        return []
    return calls.read_text(encoding="utf-8").splitlines()


def _starts(calls):
    return [line for line in _calls(calls) if line.startswith(UNIT_ARGS)]


def _shows(calls):
    return [line for line in _calls(calls) if line.startswith(SHOW_ARGS)]


def _wait_for_calls(calls, count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_calls(calls)) >= count:
            return
        time.sleep(0.05)


def _bridge(bridge_factory, port_file, systemctl, **env):  # noqa: F811
    """Мост без LQRAG_URL: адрес из порт-файла, как в поставке."""
    # Пустой LQRAG_URL — то же, что его отсутствие; фабрика без адреса не умеет.
    return bridge_factory(
        "", LQRAG_PORT_FILE=str(port_file), LQRAG_SYSTEMCTL=str(systemctl), **env
    )


def _initialize(bridge):
    return bridge.request(
        "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
    )


def test_candidates_cover_home_and_pw_dir(monkeypatch, tmp_path):
    home = tmp_path / "work" / "u@example"
    pw_dir = tmp_path / "example@u"
    monkeypatch.setenv("HOME", str(home))
    _fake_passwd(monkeypatch, pw_dir)
    assert _real_candidates() == [
        str(home / ".files-search" / "port"),
        str(pw_dir / ".files-search" / "port"),
    ]


def test_candidates_are_deduplicated_by_realpath(monkeypatch, tmp_path):
    """$HOME — симлинк на pw_dir: один и тот же файл дважды не проверяем."""
    pw_dir = tmp_path / "real"
    pw_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(pw_dir)
    monkeypatch.setenv("HOME", str(link))
    _fake_passwd(monkeypatch, pw_dir)
    assert _real_candidates() == [str(link / ".files-search" / "port")]


def test_pw_dir_port_file_is_used_when_home_has_none(monkeypatch, tmp_path):
    """Главный случай SberOS: мост из терминала находит порт, опубликованный службой."""
    home = tmp_path / "work" / "u@example"
    pw_dir = tmp_path / "example@u"
    home.mkdir(parents=True)
    _write_port(pw_dir / ".files-search" / "port", 8125)
    monkeypatch.setenv("HOME", str(home))
    _fake_passwd(monkeypatch, pw_dir)
    monkeypatch.setattr(mcp_server, "_port_file_candidates", _real_candidates)
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:8125", "port-file")


def test_home_port_file_wins_over_pw_dir(monkeypatch, tmp_path):
    """При равной живости (pid-файлов нет) порядок: $HOME, затем pw_dir."""
    home = tmp_path / "work" / "u@example"
    pw_dir = tmp_path / "example@u"
    _write_port(home / ".files-search" / "port", 8126)
    _write_port(pw_dir / ".files-search" / "port", 8127)
    monkeypatch.setenv("HOME", str(home))
    _fake_passwd(monkeypatch, pw_dir)
    monkeypatch.setattr(mcp_server, "_port_file_candidates", _real_candidates)
    assert mcp_server._resolve_base_url() == ("http://127.0.0.1:8126", "port-file")


def test_explicit_port_file_is_exclusive(monkeypatch, tmp_path):
    """Явный LQRAG_PORT_FILE не дополняется поиском по домам — иначе чужой порт."""
    pw_dir = tmp_path / "example@u"
    _write_port(pw_dir / ".files-search" / "port", 8128)
    monkeypatch.setenv("HOME", str(pw_dir))
    _fake_passwd(monkeypatch, pw_dir)
    monkeypatch.setattr(mcp_server, "_port_file_candidates", _real_candidates)
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(tmp_path / "нет" / "port"))
    assert mcp_server._resolve_base_url() == (mcp_server.FALLBACK_URL, "fallback")


def test_explicit_port_file_from_env_is_exclusive(bridge_factory, tmp_path):  # noqa: F811
    """То же через переменную окружения в свежем процессе: порт в $HOME не берётся."""
    home = tmp_path / "home"
    _write_port(home / ".files-search" / "port", 8129)
    script, calls = _systemctl_stub(tmp_path)
    bridge = _bridge(
        bridge_factory,
        tmp_path / "нет" / "port",
        script,
        HOME=str(home),
        LQRAG_AUTOSTART="0",
    )
    bridge.close()
    stderr = bridge.proc.stderr.read()
    assert "127.0.0.1:8129" not in stderr
    assert "ПРЕДУПРЕЖДЕНИЕ" in stderr


def _two_homes(monkeypatch, tmp_path):
    """$HOME терминала SberOS и pw_dir службы — настоящий перебор кандидатов."""
    home = tmp_path / "work" / "u@example"
    pw_dir = tmp_path / "example@u"
    home.mkdir(parents=True)
    pw_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _fake_passwd(monkeypatch, pw_dir)
    monkeypatch.setattr(mcp_server, "_port_file_candidates", _real_candidates)
    return home / ".files-search" / "port", pw_dir / ".files-search" / "port"


def test_stale_home_does_not_shadow_live_pw_dir(stub, monkeypatch, tmp_path):  # noqa: F811
    """Демон из терминала умер без уборки, служба в pw_dir жива — идём к службе."""
    stub.set("GET", "/api/v1/status", {"is_indexing": False, "type": "idle"})
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    home_port, pw_port = _two_homes(monkeypatch, tmp_path)
    _write_port(home_port, _closed_port())
    _write_pid(home_port, _dead_pid())
    _write_port(pw_port, int(stub.url.rsplit(":", 1)[1]))
    _write_pid(pw_port, os.getpid())

    assert mcp_server._resolve_base_url() == (stub.url, "port-file")
    assert mcp_server._http_json("GET", "/api/v1/status")["type"] == "idle"
    assert _calls(calls) == []


def test_refused_port_file_falls_through(stub, monkeypatch, tmp_path):  # noqa: F811
    """pid неизвестен у обоих: отказ по первому — ещё не повод звать systemd."""
    stub.set("POST", "/api/v1/search", {"results": []})
    stub.set("GET", "/api/v1/health", {"status": "ok"})
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    home_port, pw_port = _two_homes(monkeypatch, tmp_path)
    _write_port(home_port, _closed_port())
    _write_port(pw_port, int(stub.url.rsplit(":", 1)[1]))

    assert mcp_server._resolve_base_url()[0] != stub.url
    assert mcp_server._http_json("POST", "/api/v1/search", {"q": "x"}) == {
        "results": []
    }
    mcp_server._probe_and_autostart()
    assert ("GET", "/api/v1/health") in [
        (r["method"], r["path"]) for r in stub.requests
    ]
    assert _calls(calls) == []
    assert mcp_server._autostart["down_since"] is None


def test_dead_pid_candidate_is_not_retried(stub, monkeypatch, tmp_path):  # noqa: F811
    """По порту мёртвого демона если кто и слушает, то уже не наш сайдкар."""
    stub.set("GET", "/api/v1/status", {"type": "idle"})
    home_port, pw_port = _two_homes(monkeypatch, tmp_path)
    _write_port(home_port, _closed_port())
    _write_pid(home_port, os.getpid())
    _write_port(pw_port, int(stub.url.rsplit(":", 1)[1]))
    _write_pid(pw_port, _dead_pid())
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    assert WARMING in str(exc.value)
    assert stub.requests == []


def test_autostart_runs_systemctl_once(monkeypatch, bridge_factory, tmp_path):  # noqa: F811
    """initialize и два вызова инструмента — а systemctl позван ровно один раз.

    Повтор — не раньше AUTOSTART_RETRY_S (3 минуты), здесь до него далеко.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    script, calls = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")

    _initialize(bridge)
    _wait_for_calls(calls, 1)
    assert _calls(calls) == [UNIT_ARGS + "|/run/user/%d" % os.getuid()]

    for _ in range(2):
        response = bridge.call("local_search", {"query": "договор"})
        assert response["result"]["isError"] is True
    assert len(_calls(calls)) == 1, "повторный автозапуск раньше интервала — лишний"

    bridge.close()
    stderr = bridge.proc.stderr.read()
    assert stderr.count("автозапуск lib-local-rag.service") == 1


def test_autostart_from_tool_call_without_initialize(bridge_factory, tmp_path):  # noqa: F811
    script, calls = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    bridge.call("local_index_status")
    assert [line.split("|")[0] for line in _calls(calls)] == [UNIT_ARGS]


def test_autostart_keeps_existing_xdg_runtime_dir(monkeypatch, tmp_path):
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
    assert mcp_server._start_service()[0] == "started"
    assert _calls(calls) == [UNIT_ARGS + "|/custom/runtime"]


def test_autostart_disabled_does_not_call_systemctl(bridge_factory, tmp_path):  # noqa: F811
    script, calls = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="0")
    _initialize(bridge)
    response = bridge.call("local_search", {"query": "договор"})
    time.sleep(0.3)  # фоновой пробе с initialize даём шанс ошибиться
    assert _calls(calls) == []
    # Живого pid нет, а автозапуск запрещён: вместо «прогрева» — почему демон
    # никто не поднимет.
    text = call_text(response)
    assert WARMING not in text
    assert "Автозапуск: отключён (LQRAG_AUTOSTART=0)" in text
    assert str(port_file) in text


def test_live_pid_means_warming_without_systemctl(bridge_factory, tmp_path):  # noqa: F811
    """Демон жив, но ещё не слушает — прогрев. systemd не трогаем вовсе.

    Даже если юнита нет (ручной запуск из zip-поставки): «служба не найдена»
    с путём к --doctor здесь было бы неправдой.
    """
    script, calls = _systemctl_stub(
        tmp_path,
        stderr_text="Failed to start lib-local-rag.service: Unit lib-local-rag.service not found.",
        code=5,
    )
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, os.getpid())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    _initialize(bridge)
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    time.sleep(0.3)
    assert WARMING in text
    assert MISSING not in text
    assert _calls(calls) == []


def test_dead_pid_triggers_autostart(bridge_factory, tmp_path):  # noqa: F811
    script, calls = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, _dead_pid())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    assert WARMING in text
    assert [line.split("|")[0] for line in _calls(calls)] == [UNIT_ARGS]


def test_masked_unit_says_package_is_being_installed(bridge_factory, tmp_path):  # noqa: F811
    """prerm замаскировал службу на время обновления — не «служба не найдена»."""
    script, calls = _systemctl_stub(tmp_path, stderr_text=MASKED_STDERR, code=1)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    response = bridge.call("local_search", {"query": "договор"})
    text = call_text(response)
    assert response["result"]["isError"] is True
    assert MASKED in text
    assert "не найдена" not in text and WARMING not in text
    assert len(_starts(calls)) == 1


def test_masked_answer_is_retried_sooner(monkeypatch, tmp_path):
    """Маска снимается в postinst: новая попытка — через MASKED_RETRY_S, не через 3 мин."""
    script, calls = _systemctl_stub(tmp_path, stderr_text=MASKED_STDERR, code=1)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    monkeypatch.setattr(
        mcp_server,
        "LQRAG_PORT_FILE",
        str(_write_port(tmp_path / "port", _closed_port())),
    )
    for _ in range(2):
        with pytest.raises(mcp_server.TransportError) as exc:
            mcp_server._http_json("GET", "/api/v1/status")
        assert MASKED in str(exc.value)
    assert len(_starts(calls)) == 1
    mcp_server._autostart["attempt_at"] -= mcp_server.MASKED_RETRY_S + 1
    with pytest.raises(mcp_server.TransportError):
        mcp_server._http_json("GET", "/api/v1/status")
    assert len(_starts(calls)) == 2


@pytest.mark.parametrize(
    "stderr_text, code, expected",
    [
        (
            "Failed to connect to bus: No such file or directory",
            1,
            "Автозапуск: systemctl завершился с кодом 1: Failed to connect to bus",
        ),
        (None, 0, "Автозапуск: systemctl не найден"),
    ],
)
def test_port_file_with_failed_autostart_names_the_reason(
    monkeypatch, tmp_path, stderr_text, code, expected
):
    """Порт-файл есть, pid не жив, systemd не помог — причина в тексте, без «прогрева»."""
    if stderr_text is None:
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
    else:
        script, _ = _systemctl_stub(tmp_path, stderr_text=stderr_text, code=code)
        monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    port_file = _write_port(tmp_path / "port", _closed_port())
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    text = str(exc.value)
    assert expected in text
    assert WARMING not in text
    assert str(port_file) in text


def test_autostart_without_systemctl_is_skipped(monkeypatch, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert mcp_server._start_service()[0] == "no-systemctl"


def test_autostart_survives_broken_systemctl_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(tmp_path / "нет-такого-systemctl"))
    state, note = mcp_server._start_service()
    assert state == "failed"
    assert "нет-такого-systemctl" in note


def test_explicit_url_never_triggers_autostart(bridge_factory, tmp_path):  # noqa: F811
    """LQRAG_URL — демон не наша служба: поднимать её незачем, «прогревается» — ложь."""
    script, calls = _systemctl_stub(tmp_path)
    bridge = bridge_factory(
        "http://127.0.0.1:%d" % _closed_port(),
        LQRAG_SYSTEMCTL=str(script),
        LQRAG_AUTOSTART="1",
    )
    _initialize(bridge)
    text = call_text(bridge.call("local_search", {"query": "договор"}))
    time.sleep(0.3)
    assert _calls(calls) == []
    assert "недоступен" in text and WARMING not in text


def test_live_daemon_is_not_restarted(stub, bridge_factory, tmp_path):  # noqa: F811
    """Проба на initialize видит живой /health — единственную попытку не тратим."""
    stub.set("GET", "/api/v1/health", {"status": "ok"})
    stub.set("GET", "/api/v1/status", {"is_indexing": False, "type": "idle"})
    script, calls = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", int(stub.url.rsplit(":", 1)[1]))
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    _initialize(bridge)
    response = bridge.call("local_index_status")
    time.sleep(0.3)
    assert response["result"]["isError"] is False
    assert _calls(calls) == []
    assert ("GET", "/api/v1/health") in [
        (r["method"], r["path"]) for r in stub.requests
    ]


def test_initialize_is_not_delayed_by_slow_systemctl(bridge_factory, tmp_path):  # noqa: F811
    script, calls = _systemctl_stub(tmp_path, delay=5)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    started = time.monotonic()
    result = _initialize(bridge)["result"]
    assert time.monotonic() - started < 2.5
    assert result["serverInfo"]["name"] == "localrag-mcp"


def test_refused_connection_answers_warming_fast(bridge_factory, tmp_path):  # noqa: F811
    script, _ = _systemctl_stub(tmp_path)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(
        bridge_factory, port_file, script, LQRAG_AUTOSTART="1", LQRAG_TIMEOUT="110"
    )
    started = time.monotonic()
    response = bridge.call("local_search", {"query": "договор"})
    elapsed = time.monotonic() - started
    assert elapsed < AGENT_DEADLINE_S, elapsed
    assert response["result"]["isError"] is True
    text = call_text(response)
    assert "Индексатор LocalRAG " + WARMING in text
    assert "Повторите запрос через минуту" in text
    # Диагностика адреса — в подробностях.
    assert "недоступен" in text and str(port_file) in text


def test_hanging_systemctl_does_not_hold_the_answer(bridge_factory, tmp_path):  # noqa: F811
    """systemctl завис — агент всё равно получает ответ до своего таймаута."""
    script, _ = _systemctl_stub(tmp_path, delay=6)
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    started = time.monotonic()
    response = bridge.call("local_search", {"query": "договор"})
    assert time.monotonic() - started < AGENT_DEADLINE_S
    assert WARMING in call_text(response)


def test_missing_unit_points_to_doctor(bridge_factory, tmp_path):  # noqa: F811
    """Юнит не виден пользователю, живого демона нет — не «прогревается».

    pid-файла рядом с портом нет: живость неизвестна, и мост идёт к systemd.
    """
    script, calls = _systemctl_stub(
        tmp_path,
        stderr_text="Failed to start lib-local-rag.service: Unit lib-local-rag.service not found.",
        code=5,
    )
    port_file = _write_port(tmp_path / "port", _closed_port())
    bridge = _bridge(bridge_factory, port_file, script, LQRAG_AUTOSTART="1")
    response = bridge.call("local_search", {"query": "договор"})
    text = call_text(response)
    assert response["result"]["isError"] is True
    assert (
        "Служба lib-local-rag.service недоступна для пользователя (не найдена)" in text
    )
    assert "python3 /usr/lib/lib-local-rag/mcp/register_mcp.py --doctor" in text
    assert WARMING not in text
    assert len(_calls(calls)) == 1


def test_fallback_with_accepted_start_answers_warming(monkeypatch, tmp_path):
    """Порт-файла нет, systemd принял задание — служба как раз поднимается."""
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    monkeypatch.setattr(
        mcp_server, "FALLBACK_URL", "http://127.0.0.1:%d" % _closed_port()
    )
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    assert WARMING in str(exc.value)
    assert len(_calls(calls)) == 1


def test_fallback_with_failed_start_keeps_the_old_hint(monkeypatch, tmp_path):
    """Порт-файла нет и systemd отказал — честно: не запущен, и вот почему."""
    script, _ = _systemctl_stub(
        tmp_path,
        stderr_text="Failed to connect to bus: No such file or directory",
        code=1,
    )
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    monkeypatch.setattr(
        mcp_server, "FALLBACK_URL", "http://127.0.0.1:%d" % _closed_port()
    )
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    text = str(exc.value)
    assert WARMING not in text
    assert "systemctl --user status lib-local-rag.service" in text
    assert "Failed to connect to bus" in text


def _long_outage(seconds=None, last_refused_ago=1.0):
    """Отказы идут подряд дольше порога, первая попытка автозапуска была тогда же."""
    back = seconds if seconds is not None else mcp_server.STUCK_AFTER_S + 20
    now = time.monotonic()
    mcp_server._autostart["down_since"] = now - back
    mcp_server._autostart["last_refused"] = now - last_refused_ago
    mcp_server._autostart["attempt_at"] = now - back
    mcp_server._autostart["result"] = ("started", "принят")


def test_long_outage_reports_stuck_and_retries_autostart(monkeypatch, tmp_path):
    """Демон падает на прогреве раз за разом — «прогревается» бесконечно было бы ложью."""
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, _dead_pid())
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    _long_outage()

    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    text = str(exc.value)
    assert text.startswith(STUCK + " (соединение отвергается уже ")
    assert "служба: ActiveState=failed, SubState=failed, NRestarts=5" in text
    assert "Автозапуск: systemctl --user start --no-block" in text
    assert WARMING not in text
    # Снимок состояния — до start, иначе failed сменился бы на activating.
    assert [line.split("|")[0] for line in _calls(calls)] == [SHOW_ARGS, UNIT_ARGS]

    # Сразу следом — снова «не поднимается», но start не чаще интервала.
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    assert STUCK in str(exc.value)
    assert len(_starts(calls)) == 1


def test_long_outage_with_live_pid_never_starts_service(monkeypatch, tmp_path):
    """Живой, но не слушающий демон: состояние службы показываем, start не зовём."""
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, os.getpid())
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    _long_outage()

    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    text = str(exc.value)
    assert STUCK in text and WARMING not in text
    assert "NRestarts=5" in text
    assert _starts(calls) == []
    assert len(_shows(calls)) == 1


def test_short_outage_is_still_warming(monkeypatch, tmp_path):
    script, calls = _systemctl_stub(tmp_path)
    monkeypatch.setenv("LQRAG_SYSTEMCTL", str(script))
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, os.getpid())
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    _long_outage(mcp_server.STUCK_AFTER_S - 30)
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    assert WARMING in str(exc.value)
    assert _calls(calls) == []


def test_long_pause_between_refusals_starts_a_new_outage(monkeypatch, tmp_path):
    """К мосту долго не обращались — непрерывности отказов никто не видел."""
    port_file = _write_port(tmp_path / "port", _closed_port())
    _write_pid(port_file, os.getpid())
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", str(port_file))
    _long_outage(
        mcp_server.STUCK_AFTER_S * 3, last_refused_ago=mcp_server.STUCK_AFTER_S + 10
    )
    with pytest.raises(mcp_server.TransportError) as exc:
        mcp_server._http_json("GET", "/api/v1/status")
    assert WARMING in str(exc.value) and STUCK not in str(exc.value)


def test_answer_from_sidecar_ends_the_outage(stub, monkeypatch, tmp_path):  # noqa: F811
    """Отсчёт «не поднимается» — только по отказам подряд."""
    stub.set("GET", "/api/v1/status", {"type": "idle"})
    monkeypatch.setattr(
        mcp_server,
        "LQRAG_PORT_FILE",
        str(_write_port(tmp_path / "port", int(stub.url.rsplit(":", 1)[1]))),
    )
    _long_outage()
    mcp_server._http_json("GET", "/api/v1/status")
    assert mcp_server._autostart["down_since"] is None
