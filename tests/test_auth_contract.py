"""Сквозной контракт токена агента: регистратор → мост → демон, без сети.

Имена каталогов и файлов, шаблоны имени агента и токена, имя переменной
ярлыка и хэш повторены в трёх местах: stdlib-скрипты (scripts/register_mcp.py,
mcp_server.py) не импортируют src. Расхождение любого из них агент увидит
только как 401 на всё, поэтому оно должно ронять этот тест.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import mcp_server
from src.port_file import publish_pid, publish_port
from src.server import auth

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "register_mcp_contract", ROOT / "scripts" / "register_mcp.py"
)
reg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reg)

AGENT = "wsc"
DAEMON_PORT = 43123


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Дом пользователя в tmp; мост ищет порт-файл только в нём."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcp_server, "LQRAG_PORT_FILE", None)
    monkeypatch.setattr(mcp_server, "LQRAG_TOKEN_FILE", None)
    monkeypatch.setattr(mcp_server, "AGENT_NAME", AGENT)
    monkeypatch.setattr(
        mcp_server,
        "_port_file_candidates",
        lambda: [os.path.join(str(home), mcp_server.PORT_FILE_RELATIVE)],
    )
    # Владельца порта по настоящему /proc здесь не проверить: порт не слушают.
    missing = tmp_path / "нет-proc"
    monkeypatch.setattr(
        mcp_server, "PROC_NET_TABLES", (str(missing / "tcp"), str(missing / "tcp6"))
    )
    monkeypatch.delenv("LQRAG_URL", raising=False)
    return home


def _daemon_request(authorization: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/search",
            "query_string": b"",
            "headers": [(b"authorization", authorization.encode("ascii"))],
        }
    )


def test_registered_token_reaches_daemon_through_bridge(home: Path) -> None:
    # Демон в проде публикует порт и pid в lsm_root = ~/.files-search, там же
    # ищет auth.json; регистратор пишет в <дом>/.files-search.
    lsm_root = home / reg.FILES_SEARCH_DIR
    assert reg.ensure_agent_tokens([str(home)], [AGENT]) == []
    publish_port(lsm_root, DAEMON_PORT)
    publish_pid(lsm_root)

    routes, source, _ = mcp_server._resolve_routes()
    url, port_file, _ = routes[0]
    headers = mcp_server._route_auth(source, url, port_file)
    request = _daemon_request(headers.get("Authorization", ""))
    auth.verify_token(request, SimpleNamespace(lsm_root=str(lsm_root)))

    assert (source, url) == ("port-file", "http://127.0.0.1:%d" % DAEMON_PORT)
    assert request.state.agent == AGENT


def test_bridge_reads_agent_label_written_by_registrar() -> None:
    entry = reg._mcp_servers_entry("/opt/x/mcp_launcher.sh", False, AGENT)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LQRAG_")}
    env.update(entry["env"])

    label = subprocess.run(
        [sys.executable, "-c", "import mcp_server; print(mcp_server.AGENT_NAME)"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert label == AGENT


def test_name_and_token_patterns_agree() -> None:
    assert (
        auth.AGENT_NAME_PATTERN.pattern
        == reg.AGENT_NAME_PATTERN
        == mcp_server._AGENT_NAME_RE.pattern
    )
    assert reg.TOKEN_PATTERN == mcp_server.TOKEN_RE.pattern
