"""starter.sh: ожидание Istio и SecMan включается только значением TRUE."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "starter.sh"

# starter.sh в контекст docker-сборки не копируется, а тесты там гоняются.
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="нет starter.sh")


def _run(shell, istio, secman):
    if shutil.which(shell) is None:
        pytest.skip(f"нет {shell}")
    env = {
        "PATH": os.environ["PATH"],
        "ISTIO": istio,
        "WAIT_ENVOY": istio,
        "SECMAN": secman,
        "COMMAND": "echo started",
    }
    result = subprocess.run(
        [shell, str(SCRIPT)],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode == 0
    return result.stdout.splitlines()


@pytest.mark.parametrize("shell", ["sh", "bash"])
@pytest.mark.parametrize("value", ["FALSE", ""])
def test_no_wait_unless_true(shell, value):
    assert _run(shell, value, value) == ["started"]


@pytest.mark.skipif(
    os.path.exists("/opt/secman-wait-config/secman.txt"),
    reason="на машине настоящий SecMan: ожидание займёт до минуты",
)
@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_secman_wait_runs_when_true(shell):
    assert _run(shell, "FALSE", "TRUE") == ["Ожидание закончилось.", "started"]
