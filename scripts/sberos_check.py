#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика почтового тракта lib-local-rag на АРМ с SberOS.

Запуск (только stdlib, ничего ставить не нужно; совместим с python3 >= 3.6):

    python3 scripts/sberos_check.py                 # все проверки
    python3 scripts/sberos_check.py --port 8000     # порт, если демон его не опубликовал
    python3 scripts/sberos_check.py --compaction before
    ... удалить письмо, очистить корзину, дождаться compact в Thunderbird ...
    python3 scripts/sberos_check.py --compaction after

Порт, pid, offsets.json, user_conf.yml и профиль Thunderbird берутся из дома
демона, как у MCP-моста: $HOME или дом из passwd (на SberOS это разные
каталоги, служба systemd --user живёт во втором).

Отчёт печатается в stdout и сохраняется в sberos_report.json рядом со скриптом.

ВАЖНО — отчёт спроектирован БЕЗ персональных данных: в него не попадают
темы и тела писем, адреса, имена пользовательских файлов и папок. Имена
нестандартных почтовых папок заменяются на хеши. Отчёт безопасно пересылать.
"""

from __future__ import print_function

import argparse
import configparser
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from email.parser import BytesHeaderParser

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sberos_compaction_state.json"
)
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

# Корень почты выбирается как Config.email_root (src/config.py): явный
# email.path_to_email_folder, иначе как find_email_root
# (src/thunderbird/find_files.py) — ImapMail/<host> хотя бы с одной парой
# <folder> + <folder>.msf, корпоративный хост первым, между профилями —
# профиль установки по installs.ini/profiles.ini, остальные по алфавиту.
# Корень должен совпадать с демоновым, иначе остальные секции считаются по
# чужому каталогу.
PREFERRED_HOST = "imap.sberbank.ru"

# mUTF-7-имя папки «Отправленные» — только для информационного поля отчёта.
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
    "&BBAEQARFBDgEMg-": "Архив (mUTF-7)",
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
    """Дом, где лежат данные демона: port, pid, offsets.json, user_conf.yml.

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


def expand_home(path, home):
    """~ — дом демона: так путь из его конфига раскрывает Path.expanduser()."""
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    return os.path.expanduser(path)


def find_profile_dirs(tb_root):
    """Все каталоги ImapMail/<host> под профилем Thunderbird.

    Хосты по алфавиту, как в find_email_root: при нескольких аккаунтах без
    корпоративного демон берёт первый с папками.
    """
    found = []
    if not os.path.isdir(tb_root):
        return found
    for dirpath, dirnames, _filenames in os.walk(tb_root):
        if os.path.basename(dirpath) == "ImapMail":
            for host in sorted(dirnames):
                found.append(os.path.join(dirpath, host))
            dirnames[:] = []  # глубже ImapMail не спускаемся
    return found


def profile_ranks(tb_root):
    """Ранг профиля, как в find_email_root: 0 — профиль установки, 1 — Default=1."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(
            [
                os.path.join(tb_root, "profiles.ini"),
                os.path.join(tb_root, "installs.ini"),
            ],
            encoding="utf-8",
        )
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    ranks = {}
    for name in parser.sections():
        sect = parser[name]
        if name.startswith("Profile"):
            if sect.get("Default") == "1" and sect.get("Path"):
                path = os.path.realpath(os.path.join(tb_root, sect["Path"]))
                ranks.setdefault(path, 1)
        elif sect.get("Default"):
            ranks[os.path.realpath(os.path.join(tb_root, sect["Default"]))] = 0
    return ranks


def list_folder_pairs(mail_root):
    """Пары <name> + <name>.msf — то, что видит list_email_folders.

    Подпапки из <родитель>.sbd/ — с путём от корня: INBOX.sbd/Проекты.
    """
    ret = []
    seen = set()
    pending = [(mail_root, "")]
    while pending:
        directory, rel = pending.pop(0)
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for f in names:
            if f.endswith(".msf"):
                base = f[:-4]
                full = os.path.join(directory, base)
                if os.path.isfile(full):
                    ret.append((rel + base, os.path.getsize(full)))
        for f in names:
            full = os.path.join(directory, f)
            if f.endswith(".sbd") and os.path.isdir(full):
                pending.append((full, rel + f + "/"))
    return ret


def check_profile(report, tb_root, email_folder=None):
    if email_folder:
        # Как Config.email_root: явный путь главнее автопоиска, пар .msf демон
        # от него не требует. Сам путь в отчёт не идёт — в нём логин.
        details = {"email_root_source": "path_to_email_folder"}
        if not os.path.isdir(email_folder):
            details["note"] = (
                "email.path_to_email_folder задан, но каталога нет — "
                "почта не индексируется"
            )
            section(report, "profile", "FAIL", details)
            return None
        return describe_mail_root(report, details, email_folder)

    details = {"thunderbird_root_exists": os.path.isdir(tb_root)}
    if not details["thunderbird_root_exists"]:
        section(
            report,
            "profile",
            "FAIL",
            dict(details, note="каталог {0} не найден".format(display_path(tb_root))),
        )
        return None

    # Тот же отбор, что в find_email_root: кандидаты ImapMail/<host>,
    # корпоративный хост первым, между профилями — профиль установки, затем
    # по имени каталога; берём первый с непустым списком папок.
    mail_roots = find_profile_dirs(tb_root)
    details["imap_mail_dirs_found"] = len(mail_roots)
    ranks = profile_ranks(tb_root)
    mail_roots.sort(
        key=lambda p: (
            os.path.basename(p) != PREFERRED_HOST,
            ranks.get(os.path.realpath(os.path.dirname(os.path.dirname(p))), 2),
            os.path.dirname(os.path.dirname(p)),
        )
    )

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
    return describe_mail_root(report, details, chosen)


def describe_mail_root(report, details, chosen):
    """Папки выбранного корня почты — в секцию profile (имена через mask_folder)."""
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


# Однобуквенные escape-последовательности YAML в "..." (yaml/emitter.py).
_YAML_ESCAPES = dict(zip("0abtnvfreN_LP", "\0\a\b\t\n\v\f\r\x1b\x85\xa0\u2028\u2029"))
_YAML_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)")


def _yaml_unescape(match):
    code = match.group(1)
    if len(code) > 1:
        return chr(int(code[1:], 16))
    return _YAML_ESCAPES.get(code, code)


def yaml_str(parts):
    """Строковое значение user_conf.yml в том виде, как его пишет yaml.dump.

    parts — текст после «ключ:» и строки переноса без отступа. Не-ASCII
    yaml.dump пишет в двойных кавычках escape-последовательностями, длинное
    значение переносит: в двойных кавычках перенос после обратной косой черты
    склеивается без пробела, в остальных стилях — через пробел. null — пустая
    строка, как умолчание EmailConfig.
    """
    text = parts[0]
    for part in parts[1:]:
        if text.startswith('"') and (len(text) - len(text.rstrip("\\"))) % 2:
            text = text[:-1] + part
        else:
            text = text + " " + part
    if text in ("", "~", "null"):
        return ""
    if len(text) > 1 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) > 1 and text[0] == text[-1] == '"':
        return _YAML_ESCAPE_RE.sub(_yaml_unescape, text[1:-1])
    return text


def parse_user_conf(path):
    """Примитивный разбор user_conf.yml без PyYAML (только нужные ключи)."""
    conf = {
        "exists": os.path.isfile(path),
        "email_enabled": None,
        "index_last_n_days": None,
        "folders_count": None,
        "offsets": {},
        "path_to_email_folder": "",
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
    path_parts = []
    path_indent = None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if path_indent is not None:
            if stripped and indent > path_indent:
                # Длинное значение yaml.dump переносит на строки глубже ключа.
                path_parts.append(stripped)
                continue
            path_indent = None
        # До проверки отступа: yaml.dump пишет элементы списка без него.
        if in_folders and stripped.startswith("- "):
            folders_count += 1
            continue
        if indent == 0:
            in_email = stripped.startswith("email:")
            in_folders = stripped.startswith("folders:")
            in_offsets = False
            continue
        if in_email:
            if stripped.startswith("enabled:"):
                conf["email_enabled"] = stripped.split(":", 1)[1].strip() == "true"
            elif stripped.startswith("index_last_n_days:"):
                v = stripped.split(":", 1)[1].strip()
                conf["index_last_n_days"] = None if v in ("null", "~", "") else v
            elif stripped.startswith("offsets:"):
                v = stripped.split(":", 1)[1].strip()
                in_offsets = v == ""
            elif stripped.startswith("path_to_email_folder:"):
                in_offsets = False
                path_parts = [stripped.split(":", 1)[1].strip()]
                path_indent = indent
            elif in_offsets and ":" in stripped and not stripped.startswith("- "):
                key, val = stripped.rsplit(":", 1)
                key = key.strip().strip("'\"")
                try:
                    conf["offsets"][key] = int(val.strip())
                except ValueError:
                    pass
    conf["folders_count"] = folders_count
    if path_parts:
        conf["path_to_email_folder"] = yaml_str(path_parts)
    return conf


def _parse_offsets_state(raw):
    """Достать {folder: offset} из offsets.json любой версии.

    v2 — {"version": 2, "folders": {name: {offset,size,mtime}}};
    v1 — плоский {name: offset}. Без разбора версии ключи "version" и
    "folders" сошли бы за имена папок, а сырые имена папок попали бы в отчёт.
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


def user_conf_for_report(conf):
    """user_conf без личного: имена папок из offsets — хешем, путь к почте — фактом.

    Отчёт пересылают, а в путях логин и имена папок.
    """
    safe = dict(conf)
    safe["offsets"] = {mask_folder(k): v for k, v in conf["offsets"].items()}
    safe["path_to_email_folder_set"] = bool(safe.pop("path_to_email_folder"))
    return safe


def check_offsets(report, mail_root, files_root, user_conf):
    offsets_json = os.path.join(files_root, "offsets.json")
    details = {
        "offsets_json_exists": os.path.isfile(offsets_json),
        "user_conf": user_conf_for_report(user_conf),
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
    merged = dict(user_conf["offsets"])
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
                entry["status"] = "MISSING FILE"
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
            # Как _iter_parts в src/thunderbird/get_body.py.
            "note": "из таких писем индексируется одна версия: text/plain, иначе html",
        },
    )


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
            "модуль lance не упакован — optimize_indices не дописывает "
            "новые строки в индексы до компакции (в журнале демона "
            "WARNING «нет модуля lance»)"
        )
    section(report, "frozen_lance", verdict, details)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--thunderbird",
        default=None,
        help="корень профиля Thunderbird (по умолчанию ~/.thunderbird дома демона)",
    )
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
    ap.add_argument(
        "--compaction",
        choices=["before", "after"],
        default=None,
        help="снять снимок до/после компакции Thunderbird",
    )
    args = ap.parse_args()

    home = find_daemon_home()
    files_root = os.path.join(home, ".files-search")
    user_conf = parse_user_conf(os.path.join(home, ".ragsearch", "user_conf.yml"))
    email_folder = user_conf["path_to_email_folder"]

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        # Чей дом описывает отчёт; сам путь не пишем — в нём логин.
        "daemon_home": "HOME" if home == os.path.expanduser("~") else "passwd",
        "sections": {},
    }

    if args.thunderbird:
        mail_root = check_profile(report, args.thunderbird)
    else:
        mail_root = check_profile(
            report,
            os.path.join(home, ".thunderbird"),
            expand_home(email_folder, home) if email_folder else None,
        )
    check_offsets(report, mail_root, files_root, user_conf)
    if args.compaction:
        check_compaction(report, mail_root, args.compaction)
    else:
        check_mbox_scan(report, mail_root)
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
        "В отчёте нет тем/тел писем, адресов и имён пользовательских "
        "файлов — его безопасно пересылать."
    )


if __name__ == "__main__":
    main()
