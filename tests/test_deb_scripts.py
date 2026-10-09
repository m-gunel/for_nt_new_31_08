"""Скрипты сборки .deb и юнит демона.

Собрать пакет в тестах нельзя (нужны dpkg и debhelper), поэтому скрипты идут в
копии проекта во временном каталоге с подменёнными dh, dpkg-buildpackage и
make: проверяется, какой командой они собирают, а не сама сборка. Окружение
задаётся целиком — настоящие инструменты сборки тест не зовёт.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.utils.openvino_encoder import INFER_STALL_LIMIT_S

ROOT = Path(__file__).resolve().parents[1]

# Прогрев модели до падения на старте: 40-45 c, на медленной машине до полутора
# минут (так же рассчитан mcp_server.STUCK_AFTER_S).
WARMUP_S = 90


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_nosudo(tmp_path: Path, admindir: Path) -> list[str]:
    """build_deb_nosudo.sh, когда debhelper и dpkg-dev уже в системе.

    Возвращает аргументы, с которыми скрипт вызвал make.
    """
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "build_deb_nosudo.sh", proj / "scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _tool(bin_dir, "dh", "exit 0\n")
    _tool(bin_dir, "dpkg-buildpackage", "exit 0\n")
    _tool(bin_dir, "make", 'printf "%s\\n" "$@" > "$MAKE_ARGS"\n')
    args_file = tmp_path / "make-args"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MAKE_ARGS": str(args_file),
        "DPKG_ADMINDIR": str(admindir),
    }
    proc = subprocess.run(
        ["sh", str(proj / "scripts" / "build_deb_nosudo.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return args_file.read_text(encoding="utf-8").splitlines()


def test_nosudo_closed_dpkg_db_builds_via_rules(tmp_path):
    # Базу dpkg прочитать нельзя — как на SberOS («Operation not permitted»):
    # dpkg-buildpackage упадёт, кто бы ни ставил debhelper.
    args = _run_nosudo(tmp_path, tmp_path / "no-admindir")
    assert args == ["deb", "DEB_BUILD_CMD=sh scripts/deb_rules_build.sh"]


def test_nosudo_readable_dpkg_db_keeps_plain_make_deb(tmp_path):
    admindir = tmp_path / "admindir"
    admindir.mkdir()
    (admindir / "status").write_text("Package: dpkg\n", encoding="utf-8")
    assert _run_nosudo(tmp_path, admindir) == ["deb"]


def test_rules_build_restores_lost_exec_bit(tmp_path):
    """Перенос проекта архивом снимает +x с debian/rules — сборка не падает."""
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "debian").mkdir()
    shutil.copy(ROOT / "scripts" / "deb_rules_build.sh", proj / "scripts")
    (proj / "debian" / "control").write_text(
        "Source: lib-local-rag\nRules-Requires-Root: no\n", encoding="utf-8"
    )
    rules = proj / "debian" / "rules"
    rules.write_text('#!/bin/sh\necho "$1" >> "$RULES_LOG"\n', encoding="utf-8")
    rules.chmod(0o644)
    log = tmp_path / "rules.log"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RULES_LOG": str(log),
        # Иначе дату спросят у dpkg-parsechangelog, которого может не быть.
        "SOURCE_DATE_EPOCH": "1700000000",
    }
    proc = subprocess.run(
        ["sh", str(proj / "scripts" / "deb_rules_build.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert log.read_text(encoding="utf-8").split() == ["clean", "build", "binary"]
    assert os.access(rules, os.X_OK)


def _unit_sections(path: Path) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return sections


@pytest.mark.skipif(
    not (ROOT / "debian").is_dir(),
    reason="debian/ в контекст docker-сборки не копируется",
)
def test_daemon_unit_restart_loop_reaches_start_limit():
    unit = _unit_sections(ROOT / "debian" / "lib-local-rag.user.service")
    assert unit["Service"]["Restart"] == "on-failure"
    restart_sec = int(unit["Service"]["RestartSec"])
    # В [Unit], как в lib-local-rag-mcp-register.service.
    interval = int(unit["Unit"]["StartLimitIntervalSec"])
    burst = int(unit["Unit"]["StartLimitBurst"])
    # Запуски с прогревом должны превысить burst внутри окна, иначе юнит не
    # уйдёт в failed никогда (умолчание 5 за 10 c при RestartSec=5 недостижимо).
    assert burst * (WARMUP_S + restart_sec) < interval


@pytest.mark.skipif(
    not (ROOT / "debian").is_dir(),
    reason="debian/ в контекст docker-сборки не копируется",
)
def test_daemon_unit_waits_for_ready_and_has_watchdog():
    service = _unit_sections(ROOT / "debian" / "lib-local-rag.user.service")["Service"]

    assert service["Type"] == "notify"
    assert int(service["WatchdogSec"]) > 0
    assert int(service["TimeoutStartSec"]) > WARMUP_S, "до READY=1 идёт прогрев"


@pytest.mark.skipif(
    not (ROOT / "debian").is_dir(),
    reason="debian/ в контекст docker-сборки не копируется",
)
def test_daemon_unit_starts_after_registration_without_depending_on_it():
    """Токены раскладывает регистрация, но её сбой не должен держать демон."""
    unit = _unit_sections(ROOT / "debian" / "lib-local-rag.user.service")["Unit"]

    assert unit["After"] == "lib-local-rag-mcp-register.service"
    assert not {"Requires", "Requisite", "BindsTo"} & unit.keys()
    # Зависшая регистрация держала бы старт демона: у oneshot таймаута нет.
    register = _unit_sections(
        ROOT / "debian" / "units" / "lib-local-rag-mcp-register.service"
    )["Service"]
    assert 0 < int(register["TimeoutStartSec"]) < 300


@pytest.mark.skipif(
    not (ROOT / "debian").is_dir(),
    reason="debian/ в контекст docker-сборки не копируется",
)
def test_watchdog_restart_loop_reaches_start_limit():
    """При устойчивом зависании юнит должен уйти в failed, а не крутиться вечно."""
    unit = _unit_sections(ROOT / "debian" / "lib-local-rag.user.service")
    service = unit["Service"]
    # TimeoutStopSec — сколько systemd ждёт процесс после SIGABRT сторожа.
    cycle = (
        WARMUP_S
        + INFER_STALL_LIMIT_S
        + int(service["WatchdogSec"])
        + int(service["TimeoutStopSec"])
        + int(service["RestartSec"])
    )

    assert int(unit["Unit"]["StartLimitBurst"]) * cycle < int(
        unit["Unit"]["StartLimitIntervalSec"]
    )
