#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика окружения lib-local-rag на АРМ с SberOS.

Проверяет: лимиты inotify, ответы API демона (health, status, config,
search), CPU и память его процесса, наличие модуля lance в PyInstaller-сборке.

Запуск (только stdlib, ничего ставить не нужно; совместим с python3 >= 3.6):

    python3 scripts/sberos_check.py                 # все проверки
    python3 scripts/sberos_check.py --port 8000     # порт, если демон его не опубликовал
    python3 scripts/sberos_check.py --pid 1234      # процесс для замера ресурсов

Порт и pid берутся из дома демона, как у MCP-моста: $HOME или дом из passwd
(на SberOS это разные каталоги, служба systemd --user живёт во втором).

Отчёт печатается в stdout и сохраняется в sberos_report.json рядом со скриптом.

ВАЖНО — отчёт спроектирован БЕЗ персональных данных: в него не попадают
логин, имена и содержимое пользовательских файлов и папок, домашний каталог
в путях заменён на ~. Отчёт безопасно пересылать.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import time
import urllib.request

REPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sberos_report.json"
)

# Куда пакет ставит демон (debian/install); служба запускает PACKAGE_DIR/main.
PACKAGE_DIR = "/opt/lib-local-rag"
# Порт, если демон его не опубликовал, — как FALLBACK_URL моста.
DEFAULT_PORT = 8000

# Без прокси: штатный urlopen берёт http_proxy из окружения, а no_proxy
# сравнивает строкой («localhost» не покрывает 127.0.0.1): 502 прокси выглядел
# бы как «демон не отвечает». Как _OPENER в mcp_server.py.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def section(report, name, verdict, details):
    report["sections"][name] = {"verdict": verdict, "details": details}
    print("\n=== {0}: {1} ===".format(name, verdict))
    print(json.dumps(details, ensure_ascii=False, indent=2))


def home_candidates():
    """$HOME и дом из passwd без повторов — как _port_file_candidates моста.

    Служба systemd --user живёт в доме из passwd, а терминал SberOS открыт с
    HOME=/home/work/<LOGIN@DOMAIN>: по одному ~ данных демона не найти.
    """
    homes = [os.path.expanduser("~")]
    try:
        import pwd

        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        pass  # не POSIX или uid без записи в passwd
    found = []
    seen = set()
    for home in homes:
        if not home or not os.path.isabs(home):
            continue
        key = os.path.realpath(home)
        if key not in seen:
            seen.add(key)
            found.append(home)
    return found


def read_number(path):
    """Число из файла, опубликованного демоном (port, pid); None — нет или мусор."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_port(files_root):
    """Порт из <files_root>/port (src/port_file.py); None — не опубликован."""
    port = read_number(os.path.join(files_root, "port"))
    return port if port is not None and 1 <= port <= 65535 else None


def pid_alive(pid):
    """Жив ли процесс: True, False или None (неизвестно) — как _pid_alive моста."""
    if pid is None or pid <= 0 or os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # процесс есть, просто не наш
    except OSError:
        return None
    return True


# Ранг дома: живой pid, неизвестный, мёртвый; без порт-файла — последним.
_PID_RANK = {True: 0, None: 1, False: 2}
_NO_PORT_RANK = 3


def find_daemon_home():
    """Дом, где демон публикует port и pid.

    Порядок как у моста (mcp_server._port_file_routes): устаревший порт-файл
    в $HOME не заслоняет живую службу в доме из passwd; при равенстве — $HOME.
    Без порт-файлов — первый дом с .files-search, иначе $HOME.
    """
    best = None
    for home in home_candidates():
        root = os.path.join(home, ".files-search")
        if not os.path.isdir(root):
            continue
        if read_port(root) is None:
            rank = _NO_PORT_RANK
        else:
            rank = _PID_RANK[pid_alive(read_number(os.path.join(root, "pid")))]
        if best is None or rank < best[0]:
            best = (rank, home)
    return best[1] if best else os.path.expanduser("~")


def display_path(path):
    """Путь для отчёта: домашний каталог (в нём логин) заменён на ~."""
    for home in home_candidates():
        home = home.rstrip(os.sep)
        if home and (path == home or path.startswith(home + os.sep)):
            return "~" + path[len(home) :]
    return path


def check_inotify(report):
    details = {}
    for key in ("max_user_watches", "max_user_instances", "max_queued_events"):
        p = "/proc/sys/fs/inotify/" + key
        try:
            with open(p) as f:
                details[key] = int(f.read().strip())
        except (OSError, ValueError):
            details[key] = None
    verdict = "PASS" if details.get("max_user_watches") else "SKIP"
    if details.get("max_user_watches") and details["max_user_watches"] < 65536:
        verdict = "WARN"
        details["note"] = "лимит watch'ей низкий для рекурсивного наблюдения"
    section(report, "inotify", verdict, details)


def find_daemon_pid(files_root):
    """PID демона: из pid рядом с его порт-файлом, иначе по cmdline."""
    pid = read_number(os.path.join(files_root, "pid"))
    if pid_alive(pid):
        return pid
    if not os.path.isdir("/proc"):
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/{0}/cmdline".format(pid), "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if is_daemon_cmdline(cmd):
            return int(pid)
    return None


def is_daemon_cmdline(cmd):
    """cmdline демона: пакет, zip-поставка ($DIR/main/main), сборка или main.py."""
    argv = cmd.split("\0")
    if "--ocr-worker" in argv:
        return False  # дочерний OCR-воркер того же бинаря
    return (
        argv[0] in (os.path.join(PACKAGE_DIR, "main"), "main/main")
        or argv[0].endswith("/main/main")
        or ("main.py" in cmd and "python" in cmd)
    )


def check_resources(report, pid, seconds):
    if pid is None or not os.path.isdir("/proc"):
        section(
            report,
            "resources",
            "SKIP",
            {"note": "процесс демона не найден (или не Linux); укажите --pid"},
        )
        return
    try:
        clk = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk = 100

    def read_cpu():
        with open("/proc/{0}/stat".format(pid)) as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / float(clk)  # utime+stime

    def read_rss():
        with open("/proc/{0}/status".format(pid)) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # kB
        return None

    try:
        c0, t0 = read_cpu(), time.time()
        time.sleep(seconds)
        c1, t1 = read_cpu(), time.time()
        cpu_pct = round(100.0 * (c1 - c0) / (t1 - t0), 1)
        details = {
            "pid": pid,
            "sample_seconds": seconds,
            "cpu_percent_of_one_core": cpu_pct,
            "rss_kb": read_rss(),
        }
        section(report, "resources", "PASS", details)
    except OSError as e:
        section(
            report,
            "resources",
            "SKIP",
            {"note": "процесс исчез: {0}".format(type(e).__name__)},
        )


def http_json(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except Exception as e:
        code = getattr(e, "code", None)
        return code if code else type(e).__name__, None


def check_runtime(report, port):
    base = "http://127.0.0.1:{0}/api/v1".format(port)
    details = {}
    st, body = http_json("GET", base + "/health")
    details["health"] = {"status": st}
    if st != 200:
        section(
            report,
            "runtime",
            "SKIP",
            dict(details, note="демон не отвечает на порту {0}".format(port)),
        )
        return
    st, body = http_json("GET", base + "/status")
    details["status"] = {"status": st, "body": body}
    st, body = http_json("GET", base + "/config")
    details["config"] = {"status": st}
    if st == 200 and isinstance(body, dict):
        # Число папок, не сами пути: в путях логин и имена личных каталогов.
        details["config"]["folders_count"] = len(body.get("folders") or [])
    st, body = http_json(
        "POST",
        base + "/search",
        {"query": "тест", "limit": 5, "query_type": "hybrid"},
    )
    details["search"] = {"status": st}
    if st == 200 and isinstance(body, dict):
        types = {}
        for r in body.get("results") or []:
            types[r.get("type")] = types.get(r.get("type"), 0) + 1
        details["search"].update(total=body.get("total"), result_types=types)
    section(report, "runtime", "PASS", details)


def check_frozen_lance(report, dist_dir):
    shown = display_path(dist_dir)
    details = {"dist_dir": shown, "exists": os.path.isdir(dist_dir)}
    if not details["exists"]:
        section(
            report,
            "frozen_lance",
            "SKIP",
            {"note": "сборка не найдена: {0}".format(shown)},
        )
        return
    hits = []
    for dirpath, dirnames, filenames in os.walk(dist_dir):
        for n in dirnames + filenames:
            low = n.lower()
            if low == "lance" or low.startswith("lance."):
                hits.append(os.path.relpath(os.path.join(dirpath, n), dist_dir))
        if len(hits) > 20:
            break
    details["lance_artifacts"] = hits[:20]
    verdict = "PASS" if hits else "FAIL"
    if not hits:
        details["note"] = (
            "модуль lance не упакован — optimize_indices не дописывает "
            "новые строки в индексы до компакции (в журнале демона "
            "WARNING «нет модуля lance»)"
        )
    section(report, "frozen_lance", verdict, details)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--port",
        type=int,
        default=None,
        help="порт демона (по умолчанию из ~/.files-search/port, иначе 8000)",
    )
    ap.add_argument(
        "--pid", type=int, default=None, help="PID демона для замера ресурсов"
    )
    ap.add_argument(
        "--dist",
        default=None,
        help="каталог PyInstaller-сборки (по умолчанию {0}, если он есть, "
        "иначе dist/main)".format(PACKAGE_DIR),
    )
    ap.add_argument("--resource-seconds", type=int, default=15)
    args = ap.parse_args()

    home = find_daemon_home()
    files_root = os.path.join(home, ".files-search")

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        # Чей дом описывает отчёт; сам путь не пишем — в нём логин.
        "daemon_home": "HOME" if home == os.path.expanduser("~") else "passwd",
        "sections": {},
    }

    check_inotify(report)
    check_runtime(report, args.port or read_port(files_root) or DEFAULT_PORT)
    pid = args.pid if args.pid else find_daemon_pid(files_root)
    check_resources(report, pid, args.resource_seconds)
    dist = args.dist or (PACKAGE_DIR if os.path.isdir(PACKAGE_DIR) else "dist/main")
    check_frozen_lance(report, dist)

    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\nОтчёт сохранён: {0}".format(REPORT_FILE))
    print(
        "В отчёте нет логина, имён и содержимого пользовательских файлов — "
        "его безопасно пересылать."
    )


if __name__ == "__main__":
    main()
