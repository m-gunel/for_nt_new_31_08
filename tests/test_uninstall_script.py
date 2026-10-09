"""scripts/uninstall.sh: пакетная установка и каталоги из переменных RAG_*.

Скрипт копируется во временную раскладку пакета (uninstall.sh и
mcp/register_mcp.py рядом, как в /usr/lib/lib-local-rag/); systemctl и python3
подменены и только пишут вызовы в журнал. Окружение задаётся целиком: настоящий
systemctl --user остановил бы службу разработчика, а настоящий HOME — его данные.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$CALLS"
case "$*" in
    *"show -p LoadState --value lib-local-rag.service") echo "$LOAD_STATE" ;;
esac
"""
FAKE_PYTHON3 = """#!/bin/sh
echo "python3 $*" >> "$CALLS"
"""


@pytest.fixture
def base(tmp_path):
    lib = tmp_path / "lib"
    (lib / "mcp").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "uninstall.sh", lib / "uninstall.sh")
    (lib / "mcp" / "register_mcp.py").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("python3", FAKE_PYTHON3)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    (home / ".files-search" / "lance").mkdir(parents=True)
    (home / ".ragsearch").mkdir()
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _run(base: Path, load_state: str, **env_extra: str) -> tuple[str, list[str]]:
    """uninstall.sh --yes; LoadState юнита демона отдаёт подменённый systemctl."""
    env = {
        "PATH": f"{base / 'bin'}:/usr/bin:/bin",
        "HOME": str(base / "home"),
        "TMPDIR": str(base / "tmp"),
        "CALLS": str(base / "calls.log"),
        "LOAD_STATE": load_state,
        **env_extra,
    }
    proc = subprocess.run(
        ["sh", str(base / "lib" / "uninstall.sh"), "--yes"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    log = base / "calls.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.stdout, calls


def _first(calls: list[str], *words: str) -> int:
    """Номер первого вызова, в котором есть все слова."""
    for i, line in enumerate(calls):
        if set(words) <= set(line.split()):
            return i
    raise AssertionError(f"нет вызова {words} среди {calls}")


def test_package_install_stops_units_before_removing_registration(base):
    out, calls = _run(base, "loaded")
    unregister = _first(calls, "--remove")
    # Иначе .path-юнит вернёт снятую запись прогоном register_mcp.py --all.
    for unit in (
        "lib-local-rag-mcp-register-wsc.path",
        "lib-local-rag-mcp-register-gigacode.path",
        "lib-local-rag-mcp-register.service",
    ):
        assert _first(calls, "stop", unit) < unregister
    # Маска раньше останова: мост на отказе соединения сам зовёт systemctl start.
    mask = _first(calls, "mask", "--runtime", "lib-local-rag.service")
    assert mask < _first(calls, "stop", "lib-local-rag.service") < unregister
    # Пакет остаётся установленным — итог должен это сказать.
    assert "sudo apt purge lib-local-rag" in out
    assert not (base / "home" / ".files-search").exists()
    assert not (base / "home" / ".ragsearch").exists()


def test_archive_install_leaves_systemd_alone(base):
    out, calls = _run(base, "not-found")
    assert not [c for c in calls if {"stop", "mask"} & set(c.split())]
    assert "apt purge" not in out
    _first(calls, "--remove")
    assert not (base / "home" / ".files-search").exists()
    assert not (base / "home" / ".ragsearch").exists()


def test_rag_lsm_root_also_removes_index_in_home(base):
    alt = base / "alt-state"
    (alt / "files").mkdir(parents=True)
    lance = base / "lance-elsewhere"
    lance.mkdir()
    _run(base, "not-found", RAG_LSM_ROOT=str(alt), RAG_LD_DB_PATH=str(lance))
    assert not alt.exists()
    # LanceDB и лог демон держит в ~/.files-search и при заданном RAG_LSM_ROOT.
    assert not (base / "home" / ".files-search").exists()
    assert not lance.exists()


def test_default_layout_removes_state_dir_once(base):
    out, _ = _run(base, "not-found")
    state = str(base / "home" / ".files-search")
    assert out.count(f"удалено: {state}") == 1
    assert f"уже отсутствует: {state}" not in out
