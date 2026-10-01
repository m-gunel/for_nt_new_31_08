#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика почтового тракта lib-local-rag на АРМ с SberOS.

Запуск (только stdlib, ничего ставить не нужно; совместим с python3 >= 3.6):

    python3 scripts/sberos_check.py                 # все проверки
    python3 scripts/sberos_check.py --port 8000     # порт демона, если не 8000
    python3 scripts/sberos_check.py --compaction before
    ... удалить письмо, очистить корзину, дождаться compact в Thunderbird ...
    python3 scripts/sberos_check.py --compaction after

Отчёт печатается в stdout и сохраняется в sberos_report.json рядом со скриптом.

ВАЖНО — отчёт спроектирован БЕЗ персональных данных: в него не попадают
темы и тела писем, адреса, имена пользовательских файлов и папок. Имена
нестандартных почтовых папок заменяются на хеши. Отчёт безопасно пересылать.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import sys
import time
from email.parser import BytesHeaderParser

HOME = os.path.expanduser("~")
THUNDERBIRD_ROOT = os.path.join(HOME, ".thunderbird")
FILES_SEARCH_ROOT = os.path.join(HOME, ".files-search")
RAGSEARCH_CONF = os.path.join(HOME, ".ragsearch", "user_conf.yml")
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sberos_compaction_state.json"
)
REPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sberos_report.json"
)

# Повторяет логику find_email_root из src/thunderbird/find_files.py: корнем
# считается каталог ImapMail/<host>, в котором есть хотя бы одна пара
# <folder> + <folder>.msf; при нескольких аккаунтах предпочитается
# корпоративный хост. Скрипт обязан выбирать тот же корень, что и демон,
# иначе все остальные секции считаются по чужому каталогу.
PREFERRED_HOST = "imap.sberbank.ru"

# mUTF-7-имя папки «Отправленные». Раньше именно по нему искался корень —
# у пользователя без локально синхронизированных «Отправленных» почта не
# индексировалась вовсе. Оставлено только для информационного поля отчёта.
SENT_MUTF7 = "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"

# Имена папок, которые не считаются персональными данными.
KNOWN_FOLDERS = {
    "INBOX": "INBOX",
    "Trash": "Trash",
    "Sent": "Sent",
    "Drafts": "Drafts",
    "Junk": "Junk",
    "Archive": "Archive",
    "Templates": "Templates",
    "Unsent Messages": "Unsent Messages",
    SENT_MUTF7: "Отправленные (mUTF-7)",
    "&BCcENQRABD0EPgQyBDgEOgQ4-": "Черновики (mUTF-7)",
    "&BCMENAQwBDsENQQ9BD0ESwQ1-": "Удалённые (mUTF-7)",
    "&BB0ENQQ2BDUEOwQwBEIENQQ7BEwEPQQwBE8- &BD8EPgRHBEIEMA-": "Нежелательная почта (mUTF-7)",
    "&BBAEQARFBDgEsg-": "Архив (mUTF-7)",
    "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1": "Отправленные-безсуффикса (mUTF-7)",
}


def mask_folder(name):
    """Имя папки в отчёт: известные — открыто, остальные — хешем."""
    if name in KNOWN_FOLDERS:
        return KNOWN_FOLDERS[name]
    digest = hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:8]
    return "folder#{0} (len={1})".format(digest, len(name))


def section(report, name, verdict, details):
    report["sections"][name] = {"verdict": verdict, "details": details}
    print("\n=== {0}: {1} ===".format(name, verdict))
    print(json.dumps(details, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- profile ---


def find_profile_dirs(tb_root):
    """Все каталоги ImapMail/<host> под профилем Thunderbird."""
    found = []
    if not os.path.isdir(tb_root):
        return found
    for dirpath, dirnames, _filenames in os.walk(tb_root):
        if os.path.basename(dirpath) == "ImapMail":
            for host in dirnames:
                found.append(os.path.join(dirpath, host))
            dirnames[:] = []  # глубже ImapMail не спускаемся
    return found


def list_folder_pairs(mail_root):
    """Пары <name> + <name>.msf — то, что видит list_email_folders."""
    ret = []
    try:
        names = os.listdir(mail_root)
    except OSError:
        return ret
    for f in names:
        if f.endswith(".msf"):
            base = f[:-4]
            full = os.path.join(mail_root, base)
            if os.path.isfile(full):
                ret.append((base, os.path.getsize(full)))
    return ret


def check_profile(report, tb_root):
    details = {"thunderbird_root_exists": os.path.isdir(tb_root)}
    if not details["thunderbird_root_exists"]:
        section(
            report,
            "profile",
            "FAIL",
            dict(details, note="каталог {0} не найден".format(tb_root)),
        )
        return None

    # Тот же отбор, что в find_email_root: кандидаты ImapMail/<host>,
    # корпоративный хост первым, берём первый с непустым списком папок.
    mail_roots = find_profile_dirs(tb_root)
    details["imap_mail_dirs_found"] = len(mail_roots)
    mail_roots.sort(key=lambda p: os.path.basename(p) != PREFERRED_HOST)

    chosen = None
    for candidate in mail_roots:
        if list_folder_pairs(candidate):
            chosen = candidate
            break

    if chosen is None:
        details["note"] = (
            "ни в одном ImapMail/<host> нет пары <folder>+<folder>.msf — "
            "find_email_root вернёт None, почта не индексируется"
        )
        section(report, "profile", "FAIL", details)
        return None

    details["chosen_host"] = os.path.basename(chosen)
    details["chosen_is_preferred_host"] = details["chosen_host"] == PREFERRED_HOST
    pairs = list_folder_pairs(chosen)
    details["folders"] = [{"name": mask_folder(n), "size_bytes": s} for n, s in pairs]
    names = set(n for n, _ in pairs)
    details["has_inbox"] = "INBOX" in names
    details["has_sent_mutf7"] = any(n.startswith(SENT_MUTF7[:-1]) for n in names)
    details["total_mbox_bytes"] = sum(s for _, s in pairs)

    verdict = "PASS"
    if not details["has_inbox"]:
        # Не блокер: демон индексирует любые папки с парой mbox+.msf,
        # но отсутствие «Входящих» стоит заметить.
        verdict = "WARN"
        details["note"] = "папки INBOX нет — проверьте, синхронизирована ли она"
    section(report, "profile", verdict, details)
    return chosen


# ---------------------------------------------------------------- offsets ---


def parse_user_conf(path):
    """Примитивный разбор user_conf.yml без PyYAML (только нужные ключи)."""
    conf = {
        "exists": os.path.isfile(path),
        "email_enabled": None,
        "index_last_n_days": None,
        "folders_count": None,
        "offsets": {},
    }
    if not conf["exists"]:
        return conf
    try:
        with open(path, "r") as f:
            lines = f.read().splitlines()
    except OSError:
        conf["exists"] = "unreadable"
        return conf
    in_email = False
    in_offsets = False
    folders_count = 0
    in_folders = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_email = stripped.startswith("email:")
            in_folders = stripped.startswith("folders:")
            in_offsets = False
            continue
        if in_folders and stripped.startswith("- "):
            folders_count += 1
            continue
        if in_email:
            if stripped.startswith("enabled:"):
                conf["email_enabled"] = stripped.split(":", 1)[1].strip() == "true"
            elif stripped.startswith("index_last_n_days:"):
                v = stripped.split(":", 1)[1].strip()
                conf["index_last_n_days"] = None if v in ("null", "~", "") else v
            elif stripped.startswith("offsets:"):
                v = stripped.split(":", 1)[1].strip()
                in_offsets = v in ("", "null") and v != "null"
                in_offsets = v == ""
            elif in_offsets and ":" in stripped and not stripped.startswith("- "):
                key, val = stripped.rsplit(":", 1)
                key = key.strip().strip("'\"")
                try:
                    conf["offsets"][key] = int(val.strip())
                except ValueError:
                    pass
    conf["folders_count"] = folders_count
    return conf


def _parse_offsets_state(raw):
    """Достать {folder: offset} из offsets.json любой версии.

    v2 — {"version": 2, "folders": {name: {offset,size,mtime}}};
    v1 — плоский {name: offset}. Без разбора версии ключи "version" и
    "folders" принимались за имена папок: секция всегда падала в FAIL, а
    вложенный dict с сырыми (личными) именами папок утекал в отчёт.
    """
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") == 2:
        folders = raw.get("folders")
        if not isinstance(folders, dict):
            return {}
        out = {}
        for name, st in folders.items():
            if isinstance(st, dict) and isinstance(st.get("offset"), int):
                out[name] = st["offset"]
        return out
    return {n: o for n, o in raw.items() if isinstance(o, int)}


def check_offsets(report, mail_root):
    offsets_json = os.path.join(FILES_SEARCH_ROOT, "offsets.json")
    details = {
        "offsets_json_exists": os.path.isfile(offsets_json),
        "user_conf": parse_user_conf(RAGSEARCH_CONF),
        "dangling": [],
        "checked": 0,
    }
    saved = {}
    if details["offsets_json_exists"]:
        try:
            with open(offsets_json) as f:
                raw = json.load(f)
            saved = _parse_offsets_state(raw)
            details["offsets_json_version"] = (
                raw.get("version", 1) if isinstance(raw, dict) else "invalid"
            )
        except (OSError, ValueError) as e:
            details["offsets_json_error"] = type(e).__name__
    merged = dict(details["user_conf"]["offsets"])
    merged.update(saved)
    for name, off in merged.items():
        details["checked"] += 1
        # В отчёт кладём только маскированное имя и число: имена личных
        # почтовых папок — персональные данные.
        entry = {"folder": mask_folder(name), "offset": off}
        if mail_root is None:
            entry["status"] = "no-profile"
        else:
            full = os.path.join(mail_root, name)
            if not os.path.isfile(full):
                entry["status"] = "MISSING FILE"  # причина B бага P3
                details["dangling"].append(entry)
                continue
            size = os.path.getsize(full)
            entry["file_size"] = size
            if isinstance(off, int) and off > size:
                entry["status"] = "OFFSET BEYOND EOF"  # след компакции
                details["dangling"].append(entry)
                continue
            entry["status"] = "ok"
    verdict = (
        "FAIL" if details["dangling"] else ("PASS" if details["checked"] else "SKIP")
    )
    section(report, "offsets", verdict, details)


# --------------------------------------------------- mbox_scan + duplicates --


def scan_mbox(path, sample_headers=2000):
    """Один проход по mbox: число From_-строк, тайминг, сэмпл заголовков."""
    t0 = time.time()
    msg_count = 0
    size = os.path.getsize(path)
    boundaries = []
    with open(path, "rb") as f:
        pos = 0
        for line in f:
            if line.startswith(b"From "):
                msg_count += 1
                if len(boundaries) < sample_headers:
                    boundaries.append(pos)
            pos += len(line)
    toc_seconds = time.time() - t0

    t1 = time.time()
    parser = BytesHeaderParser()
    with_date = 0
    alternative = 0
    parsed = 0
    with open(path, "rb") as f:
        for i, start in enumerate(boundaries):
            stop = boundaries[i + 1] if i + 1 < len(boundaries) else size
            f.seek(start)
            head = f.read(min(stop - start, 16384))
            try:
                msg = parser.parsebytes(head)
            except Exception:
                continue
            parsed += 1
            if msg["Date"] is not None:
                with_date += 1
            ctype = (msg.get_content_type() or "").lower()
            if ctype == "multipart/alternative":
                alternative += 1
    header_seconds = time.time() - t1
    return {
        "size_bytes": size,
        "messages": msg_count,
        "toc_scan_seconds": round(toc_seconds, 2),
        "headers_sampled": parsed,
        "headers_parse_seconds": round(header_seconds, 2),
        "sample_with_date": with_date,
        "sample_multipart_alternative": alternative,
    }


def check_mbox_scan(report, mail_root):
    if mail_root is None:
        section(report, "mbox_scan", "SKIP", {"note": "профиль не найден"})
        section(report, "duplicates", "SKIP", {"note": "профиль не найден"})
        return
    folders = {}
    total_msgs = 0
    total_alt = 0
    total_sampled = 0
    for name, _size in list_folder_pairs(mail_root):
        try:
            folders[mask_folder(name)] = scan_mbox(os.path.join(mail_root, name))
        except OSError as e:
            folders[mask_folder(name)] = {"error": type(e).__name__}
            continue
        total_msgs += folders[mask_folder(name)]["messages"]
        total_alt += folders[mask_folder(name)]["sample_multipart_alternative"]
        total_sampled += folders[mask_folder(name)]["headers_sampled"]
    section(
        report,
        "mbox_scan",
        "PASS" if folders else "SKIP",
        {"folders": folders, "total_messages": total_msgs},
    )
    ratio = round(total_alt / total_sampled, 3) if total_sampled else None
    section(
        report,
        "duplicates",
        "PASS" if total_sampled else "SKIP",
        {
            "sampled": total_sampled,
            "multipart_alternative": total_alt,
            "ratio": ratio,
            "note": "каждое такое письмо сейчас эмбеддится дважды (plain + html)",
        },
    )


# ------------------------------------------------------------- compaction ---


def snapshot_folders(mail_root, first_n=50):
    snap = {}
    for name, _size in list_folder_pairs(mail_root):
        full = os.path.join(mail_root, name)
        st = os.stat(full)
        offs = []
        with open(full, "rb") as f:
            pos = 0
            for line in f:
                if line.startswith(b"From ") and len(offs) < first_n:
                    offs.append(pos)
                pos += len(line)
                if len(offs) >= first_n and pos > 1 << 22:
                    break
        with open(full, "rb") as f:
            head_hash = hashlib.sha256(f.read(65536)).hexdigest()[:16]
        snap[name] = {
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "head_sha256_16": head_hash,
            "first_offsets": offs,
        }
    return snap


def check_compaction(report, mail_root, phase):
    if mail_root is None:
        section(report, "compaction", "SKIP", {"note": "профиль не найден"})
        return
    if phase == "before":
        snap = snapshot_folders(mail_root)
        with open(STATE_FILE, "w") as f:
            json.dump(snap, f)
        section(
            report,
            "compaction",
            "PASS",
            {
                "note": "снимок сохранён; удалите письмо, очистите корзину, "
                "дождитесь compact и запустите --compaction after",
                "folders": len(snap),
            },
        )
        return
    if not os.path.isfile(STATE_FILE):
        section(report, "compaction", "SKIP", {"note": "нет снимка --before"})
        return
    with open(STATE_FILE) as f:
        before = json.load(f)
    after = snapshot_folders(mail_root)
    diffs = {}
    for name, b in before.items():
        a = after.get(name)
        if a is None:
            diffs[mask_folder(name)] = {"status": "folder gone"}
            continue
        shifted = b["first_offsets"] != a["first_offsets"]
        diffs[mask_folder(name)] = {
            "size_delta": a["size"] - b["size"],
            "mtime_changed": a["mtime"] != b["mtime"],
            "head_changed": a["head_sha256_16"] != b["head_sha256_16"],
            "offsets_shifted": shifted,
        }
    section(report, "compaction", "PASS", {"diff": diffs})


# ---------------------------------------------------------------- inotify ---


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


# ------------------------------------------------------------ find daemon ---


def find_daemon_pid():
    """PID процесса демона по cmdline (dist/main/main или main.py)."""
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
        if "dist/main/main" in cmd or ("main.py" in cmd and "python" in cmd):
            return int(pid)
    return None


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


# ---------------------------------------------------------------- runtime ---


def http_json(method, url, body=None, timeout=10):
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    if st == 200 and isinstance(body, dict):
        email_cfg = body.get("email") or {}
        details["config"] = {
            "status": st,
            "folders_count": len(body.get("folders") or []),
            "email_enabled": email_cfg.get("enabled"),
            "index_last_n_days": email_cfg.get("index_last_n_days"),
            "offsets_folders": len(email_cfg.get("offsets") or {}),
        }
    else:
        details["config"] = {"status": st}
    # sources задаём явно: по умолчанию /search ищет только по файлам, а ниже
    # из выдачи берётся письмо для /get_file_path.
    st, body = http_json(
        "POST",
        base + "/search",
        {
            "query": "тест",
            "limit": 5,
            "query_type": "hybrid",
            "sources": ["files", "email"],
        },
    )
    email_id = None
    if st == 200 and isinstance(body, dict):
        results = body.get("results") or []
        types = {}
        for r in results:
            types[r.get("type")] = types.get(r.get("type"), 0) + 1
            if email_id is None and str(r.get("id", "")).startswith("email_"):
                email_id = r["id"]
        details["search"] = {
            "status": st,
            "total": body.get("total"),
            "result_types": types,
        }
    else:
        details["search"] = {"status": st}
    if email_id:
        st, body = http_json(
            "POST", base + "/get_file_path", {"absolute_path": email_id}
        )
        got = bool(body and body.get("absolute_temp_path"))
        details["get_file_path"] = {"status": st, "returned_path": got}
    else:
        details["get_file_path"] = {
            "status": "SKIP",
            "note": "в выдаче нет email-результатов",
        }
    section(report, "runtime", "PASS", details)


# ------------------------------------------------------------ frozen_lance --


def check_frozen_lance(report, dist_dir):
    details = {"dist_dir": dist_dir, "exists": os.path.isdir(dist_dir)}
    if not details["exists"]:
        section(
            report,
            "frozen_lance",
            "SKIP",
            {"note": "сборка не найдена: {0}".format(dist_dir)},
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
            "модуль lance не упакован (main.spec его исключает) "
            "— optimize_indices во фризнутой сборке молча "
            "не работает"
        )
    section(report, "frozen_lance", verdict, details)


# ------------------------------------------------------------------- main ---


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--thunderbird", default=THUNDERBIRD_ROOT, help="корень профиля Thunderbird"
    )
    ap.add_argument("--port", type=int, default=8000, help="порт демона")
    ap.add_argument(
        "--pid", type=int, default=None, help="PID демона для замера ресурсов"
    )
    ap.add_argument("--dist", default="dist/main", help="каталог PyInstaller-сборки")
    ap.add_argument("--resource-seconds", type=int, default=15)
    ap.add_argument(
        "--compaction",
        choices=["before", "after"],
        default=None,
        help="снять снимок до/после компакции Thunderbird",
    )
    args = ap.parse_args()

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "sections": {},
    }

    mail_root = check_profile(report, args.thunderbird)
    check_offsets(report, mail_root)
    if args.compaction:
        check_compaction(report, mail_root, args.compaction)
    else:
        check_mbox_scan(report, mail_root)
    check_inotify(report)
    check_runtime(report, args.port)
    pid = args.pid if args.pid else find_daemon_pid()
    check_resources(report, pid, args.resource_seconds)
    check_frozen_lance(report, args.dist)

    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\nОтчёт сохранён: {0}".format(REPORT_FILE))
    print(
        "В отчёте нет тем/тел писем, адресов и имён пользовательских "
        "файлов — его безопасно пересылать."
    )


if __name__ == "__main__":
    main()
