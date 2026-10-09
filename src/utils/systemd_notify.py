"""Уведомления systemd по протоколу sd_notify — без libsystemd.

Юнит демона — Type=notify с WatchdogSec: systemd ждёт READY=1 и перезапускает
процесс, если пинги WATCHDOG=1 прекратились. Вне systemd (разработка, тесты)
переменных окружения нет, и всё здесь — no-op.
"""

import asyncio
import os
import socket
from collections.abc import Callable

from .logging import get_logger

lg = get_logger(__name__)

_USEC_PER_S = 1_000_000

# Состояния протокола sd_notify.
READY = "READY=1"
WATCHDOG = "WATCHDOG=1"
STOPPING = "STOPPING=1"


def notify(state: str) -> None:
    """Отправить состояние в $NOTIFY_SOCKET (READY=1, WATCHDOG=1, STOPPING=1)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):  # абстрактный сокет Linux
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as exc:
        lg.warning("уведомление systemd %r не отправлено: %s", state, exc)


def watchdog_period() -> float | None:
    """Как часто продлевать сторожа (половина WatchdogSec); None — он не включён."""
    usec = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not usec or (pid and int(pid) != os.getpid()):
        return None
    return int(usec) / _USEC_PER_S / 2


async def keep_alive(period: float, is_healthy: Callable[[], bool]) -> None:
    """Продлевать сторожа, пока процесс здоров.

    Пинг идёт из event loop: зависший loop его не пошлёт, и systemd перезапустит
    демон. is_healthy ловит то, чего loop не видит, — зависший инференс.
    """
    while True:
        await asyncio.sleep(period)
        if is_healthy():
            notify(WATCHDOG)
