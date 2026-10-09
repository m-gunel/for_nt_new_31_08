"""Протокол sd_notify (src/utils/systemd_notify.py) на настоящем сокете."""

import asyncio
import os
import shutil
import socket
import sys
import tempfile

import pytest

from src.utils.systemd_notify import keep_alive, notify, watchdog_period


@pytest.fixture
def notify_socket(monkeypatch):
    """Сокет «systemd»: $NOTIFY_SOCKET указывает на него."""
    # Короткий каталог: путь AF_UNIX на macOS не длиннее 104 байт, а tmp_path длиннее.
    directory = tempfile.mkdtemp(prefix="sd", dir="/tmp")
    path = os.path.join(directory, "notify")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield sock
    sock.close()
    shutil.rmtree(directory)


def received(sock: socket.socket) -> list[str]:
    """Всё, что пришло на сокет к этому моменту."""
    states = []
    while True:
        try:
            states.append(sock.recv(256).decode())
        except BlockingIOError:
            return states


def test_notify_sends_state(notify_socket):
    notify("READY=1")

    assert received(notify_socket) == ["READY=1"]


def test_notify_outside_systemd_is_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    notify("READY=1")  # не бросает


def test_notify_to_vanished_socket_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone"))

    notify("WATCHDOG=1")  # отказ сокета не роняет демон


@pytest.mark.skipif(sys.platform != "linux", reason="абстрактные сокеты — только Linux")
def test_notify_reaches_abstract_socket(monkeypatch):
    name = f"lib-local-rag-test-{os.getpid()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)

        notify("READY=1")

        assert sock.recv(256) == b"READY=1"


@pytest.mark.parametrize(
    "usec, pid_offset, expected",
    [("120000000", 0, 60.0), ("120000000", 1, None), (None, 0, None)],
    ids=["ours", "other-process", "no-watchdog"],
)
def test_watchdog_period(monkeypatch, usec, pid_offset, expected):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    if usec is not None:
        monkeypatch.setenv("WATCHDOG_USEC", usec)
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + pid_offset))

    assert watchdog_period() == expected


@pytest.mark.asyncio
async def test_keep_alive_pings_only_while_healthy(notify_socket):
    healthy = True
    task = asyncio.create_task(keep_alive(0.01, lambda: healthy))
    await asyncio.sleep(0.1)
    pings_while_healthy = received(notify_socket)

    healthy = False
    await asyncio.sleep(0.05)  # пинг, отправленный до смены, мог ещё лететь
    received(notify_socket)
    await asyncio.sleep(0.1)
    pings_while_stalled = received(notify_socket)
    task.cancel()

    assert pings_while_healthy and set(pings_while_healthy) == {"WATCHDOG=1"}
    assert pings_while_stalled == []
