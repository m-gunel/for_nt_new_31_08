"""Проверка системного простоя для ленивой индексации.

Стратегия: считать "шумом" CPU-потребление root-агентов безопасности/телеметрии
(SberOS: /opt/sberemp, KES, BI.Zone EDR, fapolicyd) и считать систему idle
по USER-CPU. На SberOS с вечно активными агентами это даёт нормальную работу
индексатора — root-агенты дают ~85% CPU (system/irq), а user-space всего 10-15%.

Переменные окружения:
    IDLE_MAX_LOADAVG        — максимальный loadavg по USER-процессам (float).
                              По умолчанию 1.0 * cores (на 12 cores = 12).
                              Установите 0 чтобы отключить проверку loadavg.
    IDLE_MAX_USER_CPU       — максимальный % CPU от USER-процессов (UID>=1000)
                              за последние ~50мс. По умолчанию 60.
                              Установите 0 чтобы отключить.
    IDLE_MIN_CPU_IDLE       — минимальный TOTAL % idle CPU (0..100).
                              По умолчанию 0 (отключено — на SberOS всегда ~0%).
                              Если хотите дополнительно проверять — поставьте 20.
    IDLE_CHECK_INTERVAL     — интервал между проверками (сек). По умолчанию 15.
    IDLE_DISABLED           — если "Y"/"1"/"true" — модуль возвращает True сразу.
    IDLE_IGNORE_ROOT_AGENTS — если "Y" — не вычитать root-агентов из loadavg.
                              По умолчанию "N" — вычитать.
    IDLE_ROOT_PREFIXES      — пути root-агентов через ";". По умолчанию:
                              /opt/sberemp;/opt/kaspersky;/opt/BI.Zone;
                              /usr/sbin/fapolicyd;...
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from src.utils import get_logger

lg = get_logger("app.idle")

# Пути root-агентов по умолчанию. Используются для матчинга по /proc/PID/exe.
# Если путь в /proc/PID/exe недоступен (часто для root-агентов из-под non-root
# пользователя), мы также матчим по comm + UID=0 (см. _read_root_agent_cpu_ticks).
_DEFAULT_ROOT_PREFIXES = (
    "/opt/sberemp",
    "/opt/kaspersky",
    "/opt/BI.Zone",
    "/usr/sbin/fapolicyd",
    "/usr/lib/systemd/systemd-journald",
    "/usr/lib/systemd/systemd-udevd",
    "/usr/sbin/auditd",
)

# Имена процессов root-агентов для матчинга по /proc/PID/comm (когда /proc/PID/exe
# недоступен). Это дамные SberOS (Sigma 1.7+).
_DEFAULT_ROOT_COMMS = (
    "kauditd",          # Kaspersky audit daemon
    "kesl",             # Kaspersky Endpoint Security
    "kesl-gui",
    "bzsenedrcore",     # BI.Zone EDR
    "fapolicyd",        # file access policy daemon
    "systemd-journald",
    "systemd-udevd",
    "systemd-logind",
    "auditd",
    "systemd",          # systemd (PID 1, может быть idle)
    "Agent.User",       # sberemp
    "Agent",            # sberemp launcher
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        lg.warning("%s=%r is not a float, using default %s", name, raw, default)
        return default


def _env_bool(name: str) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return v in ("y", "yes", "1", "true", "on")


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


def _cpu_count() -> int:
    """Число логических CPU. fallback на 4."""
    try:
        return os.sysconf("SC_NPROCESSORS_ONLN") or 4
    except (ValueError, OSError, AttributeError):
        return 4


def _root_prefixes() -> tuple[str, ...]:
    raw = _env_str("IDLE_ROOT_PREFIXES", ";".join(_DEFAULT_ROOT_PREFIXES))
    return tuple(p.strip() for p in raw.split(";") if p.strip())


def _root_comms() -> tuple[str, ...]:
    return _DEFAULT_ROOT_COMMS


def _read_proc_uid(pid: str) -> int | None:
    """Возвращает UID процесса или None."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return None
    return None


def _read_proc_comm(pid: str) -> str | None:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except (FileNotFoundError, ProcessLookupError):
        return None


def _is_root_agent(pid: str) -> bool:
    """Возвращает True если процесс — root-агент безопасности/телеметрии.

    Стратегия:
      1. По /proc/PID/exe — если доступен и матчит IDLE_ROOT_PREFIXES.
      2. По UID=0 + comm в IDLE_ROOT_COMMS (для агентов с закрытым exe).
    """
    # 1. exe-путь
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        if any(p in exe for p in _root_prefixes()):
            return True
    except OSError:
        pass
    # 2. UID + comm
    uid = _read_proc_uid(pid)
    if uid != 0:
        return False
    comm = _read_proc_comm(pid)
    if not comm:
        return False
    return any(c == comm or c in comm for c in _root_comms())


def _read_total_cpu_ticks() -> int:
    """Сумма utime+stime всех процессов в системе (тики jiffies)."""
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                parts = f.read().split()
            # utime=14, stime=15 (1-based -> 14, 15)
            total += int(parts[13]) + int(parts[14])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
    return total


def _read_root_agent_cpu_ticks() -> int:
    """Сумма utime+stime root-агентов безопасности (тики jiffies)."""
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if not _is_root_agent(pid):
                continue
            with open(f"/proc/{pid}/stat", "r") as f:
                parts = f.read().split()
            # utime=14, stime=15 (1-based -> 14, 15; 0-based -> 13, 14)
            total += int(parts[13]) + int(parts[14])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
    return total


def _read_loadavg() -> float:
    """1-минутный loadavg. /proc/loadavg -> 0.0 на ошибке."""
    try:
        with open("/proc/loadavg", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _read_non_root_loadavg() -> float:
    """Приблизительная оценка loadavg без root-агентов.
    Берём долю CPU root-агентов от общей системы за последнюю секунду
    и пропорционально уменьшаем loadavg.

    Точная формула: loadavg * (1 - root_cpu_share).
    """
    sample_delay = 0.5
    t1_all = _read_total_cpu_ticks()
    t1_root = _read_root_agent_cpu_ticks()
    time.sleep(sample_delay)
    t2_all = _read_total_cpu_ticks()
    t2_root = _read_root_agent_cpu_ticks()

    d_all = t2_all - t1_all
    d_root = t2_root - t1_root
    if d_all <= 0:
        return _read_loadavg()  # fallback на raw loadavg

    root_share = max(0.0, min(1.0, d_root / d_all))
    raw_load = _read_loadavg()
    return raw_load * (1.0 - root_share)


def _read_cpu_user_percent(sample_delay: float = 0.05) -> float:
    """Читает /proc/stat дважды, возвращает % CPU занятый USER (не root).

    Поля /proc/stat: user, nice, system, idle, iowait, irq, softirq, steal.
    user+nice — это UID >= 1000 (т.е. обычные пользователи). system/irq/softirq —
    это ядро + root. idle+iowait — простой.

    На SberOS root-агенты (KES, EDR, sberemp) едят много CPU, но это всё попадает
    в system/irq (UID=0). Поэтому USER% показывает реальную нагрузку от
    пользовательских приложений.

    Возвращает 100 * (user_diff / total_diff). 0..100.
    """
    try:
        def _read():
            with open("/proc/stat", "r") as f:
                line = f.readline()  # "cpu ..."
                parts = line.split()[1:]
                vals = [int(x) for x in parts]
                user = vals[0] + vals[1]   # user + nice
                total = sum(vals)
                return user, total
        user1, total1 = _read()
        time.sleep(sample_delay)
        user2, total2 = _read()
        dt = total2 - total1
        if dt <= 0:
            return 0.0
        du = user2 - user1
        return max(0.0, min(100.0, 100.0 * du / dt))
    except Exception:
        return 0.0


def _read_cpu_idle_percent(sample_delay: float = 0.05) -> float:
    """Классический idle% из /proc/stat (idle + iowait)."""
    try:
        def _read():
            with open("/proc/stat", "r") as f:
                line = f.readline()
                parts = line.split()[1:]
                vals = [int(x) for x in parts]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                total = sum(vals)
                return idle, total
        idle1, total1 = _read()
        time.sleep(sample_delay)
        idle2, total2 = _read()
        dt = total2 - total1
        if dt <= 0:
            return 100.0
        di = idle2 - idle1
        return max(0.0, min(100.0, 100.0 * di / dt))
    except Exception:
        return 100.0


async def is_system_idle() -> bool:
    """Возвращает True если система считается простаивающей по USER-CPU.

    Стратегия: считаем "шумом" CPU root-агентов (KES/EDR/sberemp/systemd) и
    принимаем решение по USER-CPU. На SberOS с вечно активными root-агентами
    total CPU ~100%, но user CPU ~5-15% — и тогда индексация работает.

    Главная метрика: cpu_user% (% CPU от USER-процессов UID>=1000 за ~50мс).
    По умолчанию порог IDLE_MAX_USER_CPU=60 — индексируем когда user CPU < 60%.

    Опциональная дополнительная проверка — loadavg без root-агентов
    (IDLE_MAX_LOADAVG, default 1.0*cores, отключена по умолчанию через =0,
    потому что loadavg это инерционная метрика и плохо реагирует на текущую
    загрузку).
    """
    if _env_bool("IDLE_DISABLED"):
        return True

    cores = _cpu_count()
    subtract_root = not _env_bool("IDLE_IGNORE_ROOT_AGENTS")
    # По умолчанию loadavg-проверку ОТКЛЮЧАЕМ (она инерционная).
    # Если хотите — установите IDLE_MAX_LOADAVG явно (например 2*cores).
    max_load = _env_float("IDLE_MAX_LOADAVG", 0.0)
    max_user_cpu = _env_float("IDLE_MAX_USER_CPU", 60.0)
    # Обратная совместимость.
    min_cpu_idle = _env_float("IDLE_MIN_CPU_IDLE", 0.0)

    if max_load <= 0 and max_user_cpu <= 0 and min_cpu_idle <= 0:
        return True

    if subtract_root:
        load = await asyncio.to_thread(_read_non_root_loadavg)
    else:
        load = _read_loadavg()
    user_cpu = await asyncio.to_thread(_read_cpu_user_percent)
    cpu_idle = await asyncio.to_thread(_read_cpu_idle_percent) if min_cpu_idle > 0 else 100.0

    load_ok = (max_load <= 0) or (load < max_load)
    user_ok = (max_user_cpu <= 0) or (user_cpu < max_user_cpu)
    idle_ok = (min_cpu_idle <= 0) or (cpu_idle >= min_cpu_idle)

    if not (load_ok and user_ok and idle_ok):
        lg.debug(
            "system NOT idle: loadavg=%.2f (max %.2f), user_cpu=%.1f%% (max %.1f%%), "
            "cpu_idle=%.1f%% (min %.1f%%), root_subtracted=%s",
            load, max_load, user_cpu, max_user_cpu, cpu_idle, min_cpu_idle, subtract_root,
        )
        return False
    return True


async def wait_until_idle(
    check_interval: float | None = None,
    max_wait: float | None = None,
) -> bool:
    """Ждёт пока система не станет idle.

    check_interval: секунд между проверками (по умолчанию IDLE_CHECK_INTERVAL=15).
    max_wait: максимум ждать (None — бесконечно).

    Возвращает True если система стала idle, False если истёк max_wait.
    """
    interval = check_interval if check_interval is not None else _env_float("IDLE_CHECK_INTERVAL", 15.0)
    start = time.monotonic()
    waited_total = 0.0
    while True:
        if await is_system_idle():
            if waited_total > 0:
                lg.info("system became idle after %.1fs of waiting", waited_total)
            return True
        if max_wait is not None and (time.monotonic() - start) >= max_wait:
            lg.warning("wait_until_idle: max_wait=%.1fs reached, giving up", max_wait)
            return False
        await asyncio.sleep(interval)
        waited_total += interval
