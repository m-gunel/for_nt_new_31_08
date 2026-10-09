"""main.py: адрес по умолчанию и каталог файлового лога.

main.py при импорте поднимает конфиг и логирование, а в __main__ биндит сокет
и запускает uvicorn, поэтому выполняется в подпроцессе: uvicorn.Server подменён
заглушкой, которая только сообщает адрес уже забинденного сокета. Модель не
грузится, сервер не стартует; хранилище (DEV=1) и HOME — внутри tmp_path.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_BOOT = r"""
import json, runpy, sys
import uvicorn

class _FakeServer:
    def __init__(self, config):
        self.config = config

    def run(self, sockets=None):
        print("BOUND=" + json.dumps(sockets[0].getsockname()[0]), flush=True)

uvicorn.Server = _FakeServer
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _run_main(tmp_path: Path, log_file: Path, **extra_env: str) -> str:
    """Выполнить main.py как __main__ и вернуть адрес, на котором он слушал бы."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # RAG_* из окружения разработчика (RAG_HOST, RAG_LSM_ROOT…) исказили бы проверку.
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAG_")}
    env.update(
        PYTHONPATH=str(ROOT),
        CONFIG_PATH=str(ROOT / "cfg" / "dev.yml"),
        DEV="1",
        RESET="0",
        HOME=str(home),
        RAG_PORT="0",
        RAG_LOG_FILE=str(log_file),
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update(extra_env)
    done = subprocess.run(
        [sys.executable, "-c", _BOOT, str(ROOT / "main.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    bound = [line for line in done.stdout.splitlines() if line.startswith("BOUND=")]
    assert bound, done.stdout + done.stderr[-3000:]
    return json.loads(bound[0][len("BOUND=") :])


@pytest.fixture(scope="module")
def started(tmp_path_factory):
    """Один прогон main.py: лог — в уже существующем каталоге с правами 0755."""
    tmp_path = tmp_path_factory.mktemp("main")
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    log_file = shared / "rag.log"
    return _run_main(tmp_path, log_file), shared, log_file


def test_default_bind_is_loopback(started):
    host, _, _ = started
    assert host == "127.0.0.1", "без RAG_HOST API открыт сети"


def test_existing_log_dir_keeps_its_mode(started):
    """Каталог лога не наш (дом, /tmp): права не трогаем, лог всё равно пишем."""
    _, shared, log_file = started
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755, "права чужого каталога"
    assert log_file.is_file() and log_file.stat().st_size > 0, "файловый лог отключён"


def test_empty_host_is_loopback_and_new_log_dir_private(tmp_path):
    """RAG_HOST= (пусто) — тоже умолчание; свой новый каталог лога — 0700."""
    log_file = tmp_path / "fresh" / "logs" / "rag.log"

    host = _run_main(tmp_path, log_file, RAG_HOST="")

    assert host == "127.0.0.1", "пустой RAG_HOST открыл API сети"
    assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700
    assert log_file.is_file()
