"""Сторож systemd: READY=1 после прогрева, пинги — пока инференс не завис."""

import asyncio
import importlib
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI

from src.utils.openvino_encoder import (
    INFER_STALL_LIMIT_S,
    OpenVINOEmbedder,
    _InferChannel,
)

# Сокет «systemd» и чтение того, что на него пришло.
from tests.test_systemd_notify import notify_socket, received  # noqa: F401

# Сам модуль: пакет src.server экспортирует под тем же именем объект FastAPI.
app_module = importlib.import_module("src.server.app")

# --- канал инференса ---------------------------------------------------------


class _Request:
    """InferRequest, у которого wait() держится, пока тест его не отпустит."""

    def __init__(
        self, started: threading.Event, release: threading.Event, timeout: float
    ) -> None:
        self.started = started
        self.release = release
        self.timeout = timeout
        self.output_tensors = [SimpleNamespace(data=np.zeros((1, 2, 3)))]

    def start_async(self, inputs) -> None:
        self.started.set()

    def wait(self) -> None:
        if not self.release.wait(self.timeout):
            raise TimeoutError("тест не отпустил инференс")


class _Compiled:
    outputs = [SimpleNamespace(any_name="input_ids")]

    def __init__(
        self, started: threading.Event, release: threading.Event, timeout: float
    ) -> None:
        self.started = started
        self.release = release
        self.timeout = timeout

    def create_infer_request(self) -> _Request:
        return _Request(self.started, self.release, self.timeout)


def _channel(
    release: threading.Event, timeout: float = 5.0
) -> tuple[_InferChannel, threading.Event]:
    started = threading.Event()
    compiled = _Compiled(started, release, timeout)
    return _InferChannel("search", compiled, compiled), started


def test_running_inference_reports_its_age():
    release = threading.Event()
    channel, started = _channel(release)
    worker = threading.Thread(target=channel.infer, args=(["текст"],))
    worker.start()
    started.wait(5)

    age = channel.busy_seconds(time.monotonic() + 10)

    release.set()
    worker.join(5)
    assert age >= 10
    assert channel.busy_seconds(time.monotonic()) == 0.0


def test_failed_inference_does_not_look_stalled():
    channel, _ = _channel(threading.Event(), timeout=0.01)

    with pytest.raises(TimeoutError):
        channel.infer(["текст"])

    assert channel.busy_seconds(time.monotonic()) == 0.0


@pytest.mark.parametrize(
    "age, alive",
    [(0.0, True), (INFER_STALL_LIMIT_S - 1, True), (INFER_STALL_LIMIT_S, False)],
    ids=["idle", "slow", "stalled"],
)
def test_inference_alive_until_stall_limit(age, alive):
    model = SimpleNamespace(oldest_infer_age=lambda: age)

    assert OpenVINOEmbedder.inference_alive(model) is alive


# --- app_life ----------------------------------------------------------------


class _Model:
    """Заглушка cfg.ov_model: прогрев и здоровье инференса.

    На каждом прогреве запоминает, что к этому моменту дошло до systemd.
    """

    def __init__(self, systemd: socket.socket) -> None:
        self.systemd = systemd
        self.alive = True
        self.sent_before_warmup: list[str] = []

    async def async_encode(self, texts, role: str = "search"):
        self.sent_before_warmup += received(self.systemd)
        return np.zeros((1, 3))

    def inference_alive(self) -> bool:
        return self.alive


@pytest.fixture
def daemon(config, notify_socket, monkeypatch):  # noqa: F811
    """app_life без индексатора: модель-заглушка и пустой serve_indexer."""

    @asynccontextmanager
    async def no_indexer():
        yield

    model = _Model(notify_socket)
    monkeypatch.setitem(config.__dict__, "ov_model", model)
    monkeypatch.setattr(app_module, "serve_indexer", no_indexer)
    return model


@pytest.fixture
def watchdog_10ms(monkeypatch):
    # WatchdogSec=20 мс: пинг каждые 10 мс.
    monkeypatch.setenv("WATCHDOG_USEC", "20000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))


@pytest.mark.asyncio
async def test_ready_after_warmup_and_stopping_on_exit(daemon, notify_socket):  # noqa: F811
    async with app_module.app_life(FastAPI()):
        inside = received(notify_socket)

    assert daemon.sent_before_warmup == [], "READY=1 — только после прогрева"
    assert inside == ["READY=1"]
    assert received(notify_socket) == ["STOPPING=1"]


@pytest.mark.asyncio
async def test_watchdog_pinged_while_inference_is_alive(
    daemon,
    notify_socket,  # noqa: F811
    watchdog_10ms,
):
    async with app_module.app_life(FastAPI()):
        await asyncio.sleep(0.1)
        states = received(notify_socket)

    assert "WATCHDOG=1" in states


@pytest.mark.asyncio
async def test_watchdog_not_pinged_when_inference_stalls(
    daemon,
    notify_socket,  # noqa: F811
    watchdog_10ms,
):
    daemon.alive = False

    async with app_module.app_life(FastAPI()):
        await asyncio.sleep(0.1)
        states = received(notify_socket)

    assert "WATCHDOG=1" not in states
