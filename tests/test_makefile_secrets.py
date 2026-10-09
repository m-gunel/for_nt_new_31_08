"""Makefile: токен SberOSC не печатается в выводе make и не расходится по
окружению рецептов, а цели с uv работают с одним окружением (.venv проекта).

Гоняется копия Makefile во временном каталоге с поддельным .secret и
заглушкой docker в PATH: настоящий docker и сеть не нужны.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE = shutil.which("make")

pytestmark = pytest.mark.skipif(
    MAKE is None or not (ROOT / "Makefile").is_file(), reason="нужны make и Makefile"
)

# Собирается в рантайме: литерал в исходнике незачем держать рядом с проверками.
FAKE_TOKEN = "fake" + "0123456789" * 3

# Записывает аргументы и OSC_TOKEN из своего окружения — как их увидел бы docker.
DOCKER_STUB = """#!/bin/sh
{ printf 'ARGV'; for a in "$@"; do printf ' %s' "$a"; done; printf '\\n'
  printf 'ENV_OSC_TOKEN=%s\\n' "${OSC_TOKEN-<unset>}"; } >> "$DOCKER_LOG"
"""

PROBE = """
__probe_env:
\t@env | grep -E '^(OSC_TOKEN|OTHER_SECRET|UV_INDEX_URL)=' | cut -d= -f1 | sort
"""


@pytest.fixture
def work(tmp_path):
    makefile = tmp_path / "Makefile"
    shutil.copy(ROOT / "Makefile", makefile)
    with open(makefile, "a", encoding="utf-8") as f:
        f.write(PROBE)
    (tmp_path / ".secret").write_text(
        f"OSC_TOKEN={FAKE_TOKEN}\nOTHER_SECRET=zzz\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(0o755)
    return tmp_path


def _make(work, *args):
    drop = {
        "OSC_TOKEN",
        "UV_INDEX_URL",
        "MAKEFLAGS",
        "MFLAGS",
        "MAKELEVEL",
        "VIRTUAL_ENV",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PATH"] = f"{work / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_LOG"] = str(work / "docker.log")
    return subprocess.run(
        [MAKE, *args], cwd=work, env=env, capture_output=True, text=True, timeout=60
    )


def test_secret_vars_not_exported_to_recipes(work):
    out = _make(work, "-s", "__probe_env")
    assert out.returncode == 0, out.stderr
    # UV_INDEX_URL нужен uv и экспортируется явно; остальное из .secret — нет.
    assert out.stdout.split() == ["UV_INDEX_URL"]


def test_docker_build_passes_token_as_buildkit_secret_without_echo(work):
    # MAKE=true: вложенный $(MAKE) dist_mcp dist_extras к проверке не относится.
    out = _make(work, "docker_build", "MAKE=true")
    assert out.returncode == 0, out.stderr
    assert FAKE_TOKEN not in out.stdout + out.stderr
    log = (work / "docker.log").read_text(encoding="utf-8")
    build = next(line for line in log.splitlines() if line.startswith("ARGV build "))
    assert "--secret id=osc_token,env=OSC_TOKEN" in build
    assert "--build-arg" not in build
    assert FAKE_TOKEN not in build
    # Секрет env=OSC_TOKEN docker читает из своего окружения.
    assert f"ENV_OSC_TOKEN={FAKE_TOKEN}" in log


@pytest.mark.parametrize("target", ["build", "spec", "openvino"])
def test_uv_calls_in_target_use_project_env(work, target):
    out = _make(work, "-n", target)
    assert out.returncode == 0, out.stderr
    uv_lines = [ln for ln in out.stdout.splitlines() if ln.lstrip().startswith("uv ")]
    assert uv_lines
    assert [ln for ln in uv_lines if "--active" in ln] == []
