#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регистрация MCP-моста LocalRAG в конфигах агентских приложений.

Агент запускает мост по абсолютному пути; скрипт вписывает его в конфиги агентов.
Зовут его пользовательский systemd-юнит (при входе в сессию и при появлении
конфига агента) и postinst.

Цели и схемы. Схемы строгие (additionalProperties: false), и один неверный ключ
отвергает весь конфиг, а не одну запись:
  - Workspace Computer, ~/.wsc/config/wsc.json — opencode: `mcp.<имя>` с полями
    type/command/timeout/environment (см. config-schema.json рядом с конфигом);
  - GigaCode, ~/.gigacode/settings.json — плоский `mcpServers`: command/args/env.
Легаси-каталог ~/.config/gigatool/ только вычищаем от невалидного `mcp.servers`.
Конфиг агента — JSONC: допустимы комментарии, висячие запятые и BOM. Пишем
обычный JSON, и комментарии при перезаписи теряются; прежний текст остаётся в
копии рядом (make_backup).

На SberOS у доменного пользователя два дома: pw_dir /home/DOMAIN@LOGIN (GUI,
конфиг WSC) и /home/work/LOGIN@DOMAIN — $HOME терминала. Цели обходятся в каждом
доме (discover_homes), пишем только туда, где агент установлен. От root скрипт
не пишет ничего (root_refusal).

Код возврата без --strict — 0: юнит дёргается .path-срабатываниями, и
failed-состояние упёрлось бы в StartLimit. С --strict (так зовёт postinst):
0 — ошибок нет и агент найден, 3 — агентов нет ни в одном доме, 1 — ошибка по
цели или не найден ни один дом. Отказ под root — 2 в любом режиме.

Только stdlib и конструкции python3.6: скрипт работает на системном
интерпретаторе, а нижняя граница python3 в debian/control снята сознательно.

    python3 register_mcp.py                  # прописать во все цели
    python3 register_mcp.py --dry-run        # показать, что получится
    python3 register_mcp.py --doctor         # диагностика, ничего не меняет
    python3 register_mcp.py --target wsc     # только одна цель
    python3 register_mcp.py --home DIR       # только этот дом (можно повторять)
    python3 register_mcp.py --strict         # код возврата по итогу (postinst)
    python3 register_mcp.py --remove         # снять при удалении ПО
"""

import argparse
import errno
import json
import os
import pwd
import shlex
import stat
import sys
import time

DEFAULT_NAME = "localrag"
LAUNCHER_NAME = "mcp_launcher.sh"

# Таймаут MCP-запроса у агента, мс. Дефолт агента 5000 меньше прогрева модели
# (40-45 с); две минуты покрывают и холодный поиск по большому индексу.
MCP_TIMEOUT_MS = 120000

# Таймаут моста (LQRAG_TIMEOUT, дефолт 30 с) — под холодный поиск по большому
# индексу. Прогрев сюда не входит: uvicorn начинает listen после lifespan, и до
# конца прогрева мост сразу отвечает «запускается или прогревается». Должен быть
# не больше MCP_TIMEOUT_MS, иначе агент оборвёт запрос раньше, чем мост вернёт
# осмысленную ошибку.
BRIDGE_TIMEOUT_S = "110"

# marker — каталог, по которому судим, что агент установлен. Это не каталог
# конфига: ~/.wsc/config может ещё не существовать при установленном агенте.
# «~» разворачивается относительно каждого дома из discover_homes.
TARGETS = {
    "wsc": {
        "title": "Workspace Computer",
        "marker": "~/.wsc",
        "path": "~/.wsc/config/wsc.json",
        "schema": "opencode",
    },
    "gigacode": {
        "title": "GigaCode",
        "marker": "~/.gigacode",
        "path": "~/.gigacode/settings.json",
        "schema": "mcp_servers",
    },
}

# Сюда больше не пишем, но вычищаем оставленный прежней версией ключ
# mcp.servers: с ним агент отвергает конфиг целиком.
CLEANUP_TARGETS = {
    "gigatool": {
        "title": "GigaTool (легаси-каталог)",
        "marker": "$CONFIG_HOME/gigatool",
        "path": "$CONFIG_HOME/gigatool/opencode.json",
        "schema": "opencode",
    },
}

# Корни, от которых по имени пользователя строятся SberOS-дома (_sberos_pair).
# Модульные константы, чтобы тест мог подменить их на временный каталог.
HOME_ROOT = "/home"
WORK_HOME_ROOT = "/home/work"

# Явный список домов (через os.pathsep). Вместе с --home ЗАМЕНЯЕТ обнаружение:
# тестам нельзя трогать настоящие ~/.wsc и ~/.gigacode разработчика, а
# нестандартной установке — гадать по passwd.
HOMES_ENV = "LQRAG_REGISTER_HOMES"

# Пользовательские юниты пакета. --doctor проверяет, что пользователь может их
# прочитать: при правах 0600 root systemd --user отвечает «could not be found»,
# хотя файлы на месте. Переменная — для тестов.
UNIT_DIR = "/usr/lib/systemd/user"
UNIT_DIR_ENV = "LQRAG_UNIT_DIR"
UNITS = (
    "lib-local-rag.service",
    "lib-local-rag-mcp-register.service",
    "lib-local-rag-mcp-register-wsc.path",
    "lib-local-rag-mcp-register-gigacode.path",
)


def config_home():
    """XDG_CONFIG_HOME, если задан (на АРМ его переопределяет политика), иначе ~/.config."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return xdg
    return os.path.join(os.path.expanduser("~"), ".config")


def resolve_path(template, home=None):
    """Развернуть шаблон пути. С home «~» означает этот дом, а не $HOME."""
    path = template.replace("$CONFIG_HOME", config_home())
    if home is not None and (path == "~" or path.startswith("~/")):
        path = home + path[1:]
    return os.path.abspath(os.path.expanduser(path))


def _passwd_entry():
    """(имя, pw_dir) текущего uid; (None, None), если NSS его не знает.

    Отдельной функцией, чтобы тест подменял passwd, не трогая модуль pwd.
    """
    try:
        record = pwd.getpwuid(os.getuid())
    except KeyError:
        return None, None
    return record.pw_name, record.pw_dir


def _sberos_pair(name):
    """SberOS-дома по имени: /home/work/<имя>, а для LOGIN@DOMAIN — ещё /home/DOMAIN@LOGIN.

    /home/work/<имя> — для любого имени: .path-юниты следят за /home/work/%u
    безусловно, а имя без «@» бывает и у локальной учётки, и у SSSD с
    use_fully_qualified_names=false. Чужой каталог с тем же именем отсекает
    check_home. Обход /home/* не делаем: писать можно только в свои каталоги,
    а на сотнях доменных пользователей с автомонтированием это дорого.
    """
    if not name or "/" in name or name in (".", ".."):
        return []
    result = [os.path.join(WORK_HOME_ROOT, name)]
    if name.count("@") == 1:
        login, domain = name.split("@")
        if login and domain:
            result.append(os.path.join(HOME_ROOT, domain + "@" + login))
    return result


def home_candidates():
    """Кандидаты в дома по приоритету, ещё без проверки: [(путь, откуда)].

    pw_dir первым: там конфиги агентов, которые запускаются из GUI, и туда же
    смотрят пользовательские юниты. $HOME — вторым: у терминала на SberOS он
    указывает в /home/work, и туда пишет запущенный из терминала GigaCode CLI.
    """
    name, pw_dir = _passwd_entry()
    result = []
    if pw_dir:
        result.append((pw_dir, "pw_dir"))
    env_home = os.environ.get("HOME")
    if env_home:
        result.append((env_home, "$HOME"))
    for path in _sberos_pair(name):
        result.append((path, "SberOS"))
    return result


def check_home(path, uid):
    """(stat, None), если каталог годится в дома, иначе (None, причина).

    Годится только СВОЙ существующий каталог: $HOME наследуется от
    вызывающего (sudo -E, runuser -m) и может указывать в /root или в чужой
    дом, а писать туда от имени пользователя всё равно нельзя.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, "нет каталога"
        return None, "недоступен (%s)" % (exc.strerror or exc)
    if not stat.S_ISDIR(st.st_mode):
        return None, "не каталог"
    if st.st_uid != uid:
        return None, "владелец uid %d, а не вы (uid %d)" % (st.st_uid, uid)
    return st, None


def scan_homes():
    """Все кандидаты с вердиктом: [(путь, откуда, причина отказа или None)].

    Дубли отсекаем по (st_dev, st_ino), а не по строке пути: $HOME бывает
    симлинком на pw_dir. Полное совпадение пути ($HOME == pw_dir) дописываем к
    первой строке: «[pw_dir, $HOME]».
    """
    uid = os.getuid()
    seen = {}
    rows = []
    for path, origin in home_candidates():
        path = os.path.abspath(os.path.expanduser(path))
        st, reason = check_home(path, uid)
        if st is not None:
            first = seen.get((st.st_dev, st.st_ino))
            if first is None:
                seen[(st.st_dev, st.st_ino)] = len(rows)
            elif rows[first][0] == path:
                rows[first][1] += ", " + origin
                continue
            else:
                reason = "тот же каталог, что %s" % rows[first][0]
        rows.append([path, origin, reason])
    return [tuple(row) for row in rows]


def explicit_homes(cli_homes):
    """Дома, заданные явно: (список, откуда) или (None, None). --home сильнее переменной."""
    if cli_homes:
        raw, source = cli_homes, "--home"
    else:
        value = os.environ.get(HOMES_ENV, "")
        raw, source = [p for p in value.split(os.pathsep) if p], HOMES_ENV
    if not raw:
        return None, None
    homes = []
    for path in raw:
        path = os.path.abspath(os.path.expanduser(path))
        if path not in homes:
            homes.append(path)
    return homes, source


def discover_homes(cli_homes=None):
    """Дома, в которых искать конфиги агентов; pw_dir — первым.

    Явные --home / LQRAG_REGISTER_HOMES заменяют обнаружение и владельцем не
    фильтруются: запись в чужой дом всё равно упрётся в «нет прав» (от root
    скрипт не пишет) и уйдёт в строку ОШИБКА по цели.
    """
    homes, _source = explicit_homes(cli_homes)
    if homes is not None:
        return homes
    return [path for path, _origin, reason in scan_homes() if reason is None]


def default_launcher():
    """Лаунчер рядом (раскладка поставки, mcp/) или этажом выше (репозиторий, scripts/)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, LAUNCHER_NAME),
        os.path.join(os.path.dirname(here), LAUNCHER_NAME),
    ):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(os.path.dirname(here), LAUNCHER_NAME)


def ensure_launcher(launcher, fix=True):
    """Проверить лаунчер и по возможности починить права.

    Бит +x теряется при распаковке distro.zip: Jenkins собирает архив
    Java-реализацией zip, которая unix-права не сохраняет. Без него у агента
    просто нет инструментов — без единой ошибки.
    """
    if not os.path.isfile(launcher):
        return ["файла нет — проверьте, что поставка распакована полностью"]
    if os.access(launcher, os.X_OK):
        return []
    if not fix:
        return ["нет бита +x (будет восстановлен при записи)"]
    try:
        os.chmod(launcher, os.stat(launcher).st_mode | 0o111)
    except OSError as exc:
        return ["нет бита +x, и chmod не удался (%s) — выполните вручную" % exc]
    return ["восстановлен бит +x (потерян при распаковке архива)"]


PACKAGED_LAUNCHER = "/usr/lib/lib-local-rag/mcp/mcp_launcher.sh"


def check_launcher_origin(launcher):
    """Предупредить, если в конфиг уедет путь из рабочей копии при установленном пакете.

    Такой путь молча пропадает при чистке рабочей копии, и агент теряет
    инструменты без единой ошибки.
    """
    if launcher == PACKAGED_LAUNCHER or not os.path.isfile(PACKAGED_LAUNCHER):
        return []
    return [
        "ВНИМАНИЕ: в конфиг пойдёт %s, хотя пакет поставил %s. "
        "Путь из рабочей копии не переживёт её удаление — если это не отладка, "
        "перезапустите с --launcher %s" % (launcher, PACKAGED_LAUNCHER, PACKAGED_LAUNCHER)
    ]


def _strip_comments(text):
    """Убрать // и /* */ снаружи строковых литералов.

    Посимвольно, а не регуляркой: «//» внутри значения (например, в URL или в
    пути) регуляркой не отличить от начала комментария, и конфиг бы портился.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text):
    """Убрать запятую перед } и ] — тоже с учётом строковых литералов."""
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch in "}]":
            j = len(out) - 1
            while j >= 0 and out[j] in " \t\r\n":
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
            out.append(ch)
            continue
        out.append(ch)
    return "".join(out)


def load_config(path):
    """Прочитать конфиг. Возвращает (данные, исходный текст).

    Нет файла — пустой словарь, это штатный первый запуск. Битый JSON или не
    UTF-8 — отказ, а не перезапись: в файле лежат чужие настройки, и затирать
    их из-за одной опечатки нельзя.
    """
    if not os.path.exists(path):
        return {}, None
    try:
        # utf-8-sig снимает BOM: json.loads на нём падает, а файл, сохранённый
        # «Блокнотом» или редактором с такой настройкой, в остальном валиден.
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = handle.read()
    except OSError as exc:
        raise SystemExit("не удалось прочитать %s: %s" % (path, exc))
    except UnicodeDecodeError as exc:
        # Не OSError и не SystemExit: без перевода исключение прошло бы мимо
        # process_target и оборвало бы регистрацию во всех остальных целях.
        raise SystemExit(
            "%s не в кодировке UTF-8 (%s). Пересохраните файл в UTF-8 — "
            "перезаписывать его я не буду, там могут быть ваши настройки." % (path, exc)
        )
    text = _strip_trailing_commas(_strip_comments(raw)).strip()
    if not text:
        return {}, raw
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SystemExit(
            "%s не разбирается как JSON (%s). Исправьте файл вручную — "
            "перезаписывать его я не буду, там могут быть ваши настройки." % (path, exc)
        )
    if not isinstance(data, dict):
        raise SystemExit("%s: ожидался объект JSON, получено %s" % (path, type(data).__name__))
    return data, raw


# На Linux O_NOFOLLOW есть и в python3.6; на платформе без него обходимся
# одним O_EXCL, который симлинк на месте имени тоже не пропускает.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _unlink_missing_ok(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _create_file(path, payload, mode):
    """Создать НОВЫЙ файл path с байтами payload и правами mode.

    O_EXCL|O_NOFOLLOW: существующее имя, в том числе симлинк (и висячий), даёт
    отказ, а не запись по ссылке — иначе подложенный в каталог конфига симлинк
    увёл бы запись и chmod в произвольный файл. Права — fchmod по дескриптору:
    mode в os.open урезается umask'ом, а chmod по имени пошёл бы по ссылке.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None  # дальше дескриптор закрывает handle
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd is not None:
            os.close(fd)
        _unlink_missing_ok(path)
        raise


def make_backup(path, raw):
    """Сохранить исходный конфиг рядом перед перезаписью.

    Первая копия (.localrag-backup) не перезаписывается: в ней состояние до
    нашей первой правки. Комментарии при записи теряются, поэтому файл с
    комментариями, когда первая копия уже есть, копируется ещё раз — с меткой
    времени в имени. lexists, а не exists: висячий симлинк на месте копии —
    тоже «есть», и создавать файл по нему нельзя (см. _create_file).
    """
    if raw is None:
        return None
    has_comments = _strip_comments(raw) != raw
    backup = path + ".localrag-backup"
    if os.path.lexists(backup):
        if not has_comments:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = "%s.localrag-backup-%s" % (path, stamp)
        n = 1
        while os.path.lexists(backup):
            n += 1
            backup = "%s.localrag-backup-%s-%d" % (path, stamp, n)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        _create_file(backup, raw.encode("utf-8"), mode)
    except OSError as exc:
        print("не удалось сохранить резервную копию %s: %s" % (backup, exc), file=sys.stderr)
        return None
    if has_comments:
        print(
            "%s: комментарии при записи не сохраняются, прежний текст — в %s" % (path, backup),
            file=sys.stderr,
        )
    return backup


def save_config(path, config):
    """Атомарная запись: агент не должен прочитать половину файла.

    Режим существующего файла сохраняется; новый — 0600: в конфиге агента
    бывают токены соседних MCP-серверов.

    Временный файл — .localrag.tmp, а не <имя>.tmp: за <имя> следит
    .path-юнит. Остаток прерванного прогона или подложенный симлинк удаляем
    (unlink и os.replace по ссылкам не ходят) и создаём заново.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, 0o700, exist_ok=True)
    mode = 0o600
    if os.path.exists(path):
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            pass
    tmp = path + ".localrag.tmp"
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    _unlink_missing_ok(tmp)
    _create_file(tmp, payload.encode("utf-8"), mode)
    os.replace(tmp, path)


def _opencode_entry(launcher, allow_write):
    environment = {"LQRAG_TIMEOUT": BRIDGE_TIMEOUT_S}
    if allow_write:
        # У моста это и так умолчание; явное значение показывает, что
        # управление индексацией разрешено сознательно.
        environment["LQRAG_ALLOW_WRITE"] = "1"
    # LQRAG_URL не задаём: без него мост берёт фактический порт из
    # ~/.files-search/port, а зашитый адрес сильнее порт-файла и сломался бы
    # при перезапуске демона на другом порту.
    return {
        "type": "local",
        "command": [launcher],
        "enabled": True,
        "timeout": MCP_TIMEOUT_MS,
        "environment": environment,
    }


def _merge_env(current, ours):
    """Окружение записи: чужие переменные из текущей, наши — поверх.

    Чужие переменные вписал пользователь или администратор, в том числе
    LQRAG_ALLOW_WRITE=0 закрытой установки. Регистрацию запускает каждая
    правка конфига агента, и сброс окружения к эталону откатывал бы opt-out.
    """
    if not isinstance(current, dict):
        return ours
    merged = dict(current)
    merged.update(ours)
    return merged


def _opencode_desired(current, launcher, allow_write):
    """Какой должна стать запись: наши поля — по эталону, поля пользователя — как были.

    Пользователю принадлежат enabled (выключатель сервера в агенте) и чужие
    переменные environment. Остальное — по эталону: схема строгая, и лишний
    ключ отверг бы весь конфиг.
    """
    entry = _opencode_entry(launcher, allow_write)
    if isinstance(current, dict):
        if isinstance(current.get("enabled"), bool):
            entry["enabled"] = current["enabled"]
        entry["environment"] = _merge_env(current.get("environment"), entry["environment"])
    return entry


def _opencode_cleanup(config):
    """Снять невалидный ключ mcp.servers, оставшийся от прежней версии скрипта.

    Только объект без поля type: сервер, названный «servers» и оформленный по
    схеме, — чужая рабочая запись.
    """
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return False
    broken = mcp.get("servers")
    if not isinstance(broken, dict) or "type" in broken:
        return False
    del mcp["servers"]
    if not mcp:
        del config["mcp"]
    return True


def _opencode_place(config, name, launcher, allow_write):
    changed = _opencode_cleanup(config)
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise SystemExit("в конфиге ключ 'mcp' не объект — не берусь его чинить")
    entry = _opencode_desired(mcp.get(name), launcher, allow_write)
    if mcp.get(name) != entry:
        mcp[name] = entry
        changed = True
    return changed


def _opencode_drop(config, name):
    changed = _opencode_cleanup(config)
    mcp = config.get("mcp")
    if isinstance(mcp, dict) and name in mcp:
        del mcp[name]
        if not mcp:
            del config["mcp"]
        changed = True
    return changed


def _opencode_has(config, name):
    mcp = config.get("mcp")
    return isinstance(mcp, dict) and name in mcp


def _mcp_servers_entry(launcher, allow_write):
    # В отличие от opencode: command — строка, аргументы — в args (пустой
    # пишем явно), окружение — env.
    # Поля транспорта нет. Прежняя версия писала type: "stdio" по образцу
    # VS Code, но в рабочем конфиге GigaCode stdio выводится из наличия command.
    env = {"LQRAG_TIMEOUT": BRIDGE_TIMEOUT_S}
    if allow_write:
        env["LQRAG_ALLOW_WRITE"] = "1"
    return {
        "command": launcher,
        "args": [],
        "env": env,
    }


def _mcp_servers_desired(current, launcher, allow_write):
    """Какой должна стать запись GigaCode: из текущей сохраняются чужие переменные env."""
    entry = _mcp_servers_entry(launcher, allow_write)
    if isinstance(current, dict):
        entry["env"] = _merge_env(current.get("env"), entry["env"])
    return entry


def _mcp_servers_place(config, name, launcher, allow_write):
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit("в конфиге ключ 'mcpServers' не объект — не берусь его чинить")
    entry = _mcp_servers_desired(servers.get(name), launcher, allow_write)
    if servers.get(name) == entry:
        return False
    servers[name] = entry
    return True


def _mcp_servers_drop(config, name):
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    # Пустой mcpServers НЕ удаляем: его создаёт сам GigaCode, и его отсутствие
    # может быть для плагина значимым.
    return True


def _mcp_servers_cleanup(config):
    return False


def _mcp_servers_has(config, name):
    servers = config.get("mcpServers")
    return isinstance(servers, dict) and name in servers


# section и entry нужны --doctor: сравнить запись с желаемой и найти дубли.
SCHEMAS = {
    "opencode": {
        "place": _opencode_place,
        "drop": _opencode_drop,
        "cleanup": _opencode_cleanup,
        "has": _opencode_has,
        "section": "mcp",
        "entry": _opencode_desired,
    },
    "mcp_servers": {
        "place": _mcp_servers_place,
        "drop": _mcp_servers_drop,
        "cleanup": _mcp_servers_cleanup,
        "has": _mcp_servers_has,
        "section": "mcpServers",
        "entry": _mcp_servers_desired,
    },
}


def launcher_duplicates(config, schema, name):
    """Чужие имена в секции, чьи command/args запускают наш лаунчер.

    Рукописная запись (localrag-dev, local-rag) рядом с нашей — два моста к
    одному демону: агент видит инструменты дважды, а при устаревшем пути одна
    копия молча не стартует.
    """
    section = config.get(schema["section"])
    if not isinstance(section, dict):
        return []
    found = []
    for other in sorted(section):
        entry = section[other]
        if other == name or not isinstance(entry, dict):
            continue
        parts = []
        for field in ("command", "args"):
            value = entry.get(field)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(v for v in value if isinstance(v, str))
        if any(LAUNCHER_NAME in part for part in parts):
            found.append(other)
    return found


# Итог обработки цели: по нему main() считает код возврата под --strict и
# решает, просить ли перезапустить агента.
WRITTEN = "записано"  # файл изменён (в том числе снята запись или вычищен мусор)
UNCHANGED = "без изменений"  # агент есть, запись уже такая, как надо
PLANNED = "получилось бы"  # --dry-run: агент есть, запись изменилась бы
SKIPPED = "пропуск"  # агента нет (или нечего снимать)
FAILED = "ошибка"

# Статусы «агент в доме найден» — для --strict это «было куда регистрировать».
AGENT_FOUND = (WRITTEN, UNCHANGED, PLANNED)


def process_target(key, target, launcher, args, cleanup_only, home=None):
    """Обработать одну цель и вернуть её статус. Отказ по цели НЕ прерывает остальные.

    Битый конфиг одной цели не должен мешать регистрации в других. Ошибка
    уходит в stderr (журнал юнита) и возвращается как FAILED для --strict.

    home — дом, относительно которого разворачивается «~» цели; None — $HOME
    (легаси-цель и явный --config).
    """
    try:
        return _process_target(key, target, launcher, args, cleanup_only, home)
    except SystemExit as exc:
        print("%-9s ОШИБКА: %s" % (key, exc), file=sys.stderr)
    except OSError as exc:
        print("%-9s ОШИБКА ввода-вывода: %s" % (key, exc), file=sys.stderr)
    except Exception as exc:
        # Непредвиденный сбой — тоже отказ одной цели, а не всего прогона.
        print("%-9s ОШИБКА: %s: %s" % (key, type(exc).__name__, exc), file=sys.stderr)
    return FAILED


def _process_target(key, target, launcher, args, cleanup_only, home):
    path = resolve_path(target["path"], home)
    marker = resolve_path(target["marker"], home)
    schema = SCHEMAS[target["schema"]]
    exists = os.path.exists(path)

    if not exists and (cleanup_only or args.remove):
        return SKIPPED
    if not exists and not os.path.isdir(marker):
        # Агент не установлен. Каталог НЕ создаём: пустой ~/.gigacode под
        # неустановленный плагин — мусор в домашнем каталоге и ложный сигнал.
        # Дом называем явно: при двух домах «не установлен» без него читается
        # как «не установлен вообще».
        where = " в %s" % home if home is not None else ""
        print("%-9s пропуск: %s не установлен%s" % (key, target["title"], where))
        return SKIPPED

    config, raw = load_config(path)

    if not exists and target["schema"] == "opencode":
        # Новый конфиг подписываем схемой первым ключом, как это делает сам
        # агент (подсветка и проверка в редакторе). В существующий не
        # добавляем: там подпись либо есть, либо её убрали сознательно.
        if os.path.exists(os.path.join(os.path.dirname(path), "config-schema.json")):
            config["$schema"] = "./config-schema.json"

    if args.remove:
        changed = schema["drop"](config, args.name)
    elif cleanup_only:
        changed = schema["cleanup"](config)
    else:
        changed = schema["place"](config, args.name, launcher, args.allow_write)

    if not changed:
        print("%-9s без изменений: %s" % (key, path))
        return UNCHANGED
    if args.dry_run:
        print("%-9s (--dry-run) получилось бы: %s" % (key, path))
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return PLANNED
    if exists:
        make_backup(path, raw)
    save_config(path, config)
    if args.remove:
        print("%-9s регистрация снята: %s" % (key, path))
    elif cleanup_only:
        print("%-9s вычищен невалидный mcp.servers: %s" % (key, path))
    else:
        print("%-9s записано: %s" % (key, path))
    return WRITTEN


def _script_path():
    return os.path.abspath(__file__)


def _access_problem(path, mode):
    """None, если путь доступен в режиме mode, иначе причина по-русски.

    os.access, а не разбор битов: он учитывает и права каталогов по пути.
    """
    if os.access(path, mode):
        return None
    try:
        os.stat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return "нет файла"
        return "нет доступа к пути (%s)" % (exc.strerror or exc)
    return "нет прав"


def _entry_state(config, schema, launcher, args):
    section = config.get(schema["section"])
    if not isinstance(section, dict) or args.name not in section:
        return "нет — запустите без --doctor"
    current = section[args.name]
    desired = schema["entry"](current, launcher, args.allow_write)
    if current == desired:
        if current.get("enabled") is False:
            # Выбор пользователя, а не дефект: регистрация его не откатывает.
            return "есть, но выключена в агенте (enabled: false)"
        return "есть"
    # Называем только поля, без значений: в environment/env рядом могут
    # оказаться чужие переменные, а вывод --doctor пересылают в чаты.
    if isinstance(current, dict):
        fields = sorted(k for k in set(current) | set(desired) if current.get(k) != desired.get(k))
        detail = "отличаются поля: %s" % ", ".join(fields)
    else:
        detail = "запись не объект"
    return "УСТАРЕЛА (%s) — запустите без --doctor" % detail


def _doctor_target(key, target, home, launcher, args, is_legacy):
    path = resolve_path(target["path"], home)
    marker = resolve_path(target["marker"], home)
    print("%s (%s)" % (key, target["title"]))
    print("  конфиг:    %s" % path)
    if is_legacy:
        print("  режим:     только очистка, новых записей не делаем")
    else:
        print("  агент:     %s" % ("установлен" if os.path.isdir(marker) else "не найден"))
    if not os.path.exists(path):
        print("  состояние: файла нет")
        print("")
        return
    try:
        config, _ = load_config(path)
    except SystemExit as exc:
        print("  состояние: НЕ ЧИТАЕТСЯ — %s" % exc)
        print("")
        return
    schema = SCHEMAS[target["schema"]]
    if not is_legacy:
        print("  запись:    %s" % _entry_state(config, schema, launcher, args))
        for other in launcher_duplicates(config, schema, args.name):
            print(
                "  ВНИМАНИЕ:  дубль — запись «%s» тоже запускает %s: агент поднимет "
                "мост дважды, лишнюю запись удалите вручную" % (other, LAUNCHER_NAME)
            )
    mcp = config.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        print("  ВНИМАНИЕ:  есть невалидный ключ mcp.servers — запустите без --doctor")
    print("")


def run_doctor(launcher, args):
    """Диагностика: только чтение, без systemctl и без сети.

    Вывод должен сам по себе объяснять, почему у агента нет инструментов:
    чей это запуск, какие дома найдены, может ли пользователь прочитать файлы
    пакета и юниты, что лежит в каждом конфиге. Значения env не печатаются.
    """
    name, pw_dir = _passwd_entry()
    print("пользователь: uid %d, %s" % (os.getuid(), name or "нет в passwd"))
    print("$HOME:        %s" % (os.environ.get("HOME") or "не задан"))
    print("pw_dir:       %s" % (pw_dir or "нет в passwd"))
    if os.geteuid() == 0:
        # root читает всё, поэтому проверки прав ниже от root ничего не докажут.
        print(
            "ВНИМАНИЕ: запуск от root — дома и права ниже показаны для root, а не для "
            "пользователя. Проверка за него: runuser -u <имя> -- python3 %s --doctor"
            % _script_path()
        )
    homes, source = explicit_homes(args.homes)
    if homes is not None:
        print("дома (заданы явно через %s):" % source)
        for home in homes:
            print("  %s%s" % (home, "" if os.path.isdir(home) else "  (нет каталога)"))
    else:
        homes = []
        print("дома (найдены автоматически):")
        for path, origin, reason in scan_homes():
            if reason is None:
                homes.append(path)
                print("  %s  [%s]" % (path, origin))
            else:
                print("  -- %s  [%s]: %s" % (path, origin, reason))
        if not homes:
            print("  НИ ОДНОГО подходящего — регистрировать некуда")
    print("")

    print("файлы пакета:")
    problem = _access_problem(launcher, os.R_OK | os.X_OK)
    print("  лаунчер:   %s" % launcher)
    if problem is None:
        print("             чтение и запуск — есть")
    else:
        hint = "агент не сможет запустить мост"
        if problem == "нет прав" and os.access(launcher, os.R_OK):
            hint = "нет бита +x, будет восстановлен при записи"
        print("             НЕ ДОСТУПЕН на чтение и запуск: %s; %s" % (problem, hint))
    script = _script_path()
    problem = _access_problem(script, os.R_OK)
    print("  скрипт:    %s" % script)
    print("             чтение — %s" % ("есть" if problem is None else "НЕТ: %s" % problem))
    print("")

    unit_dir = os.environ.get(UNIT_DIR_ENV) or UNIT_DIR
    print("юниты (%s):" % unit_dir)
    for unit in UNITS:
        problem = _access_problem(os.path.join(unit_dir, unit), os.R_OK)
        if problem is None:
            state = "читается"
        elif problem == "нет файла":
            state = "нет файла (пакет не установлен?)"
        else:
            state = (
                'НЕ ЧИТАЕТСЯ пользователем: пакет собран с неверными правами, systemd '
                'ответит "could not be found" (%s)' % problem
            )
        print("  %-42s %s" % (unit, state))
    print("")

    for home in homes:
        print("== дом %s" % home)
        for key, target in sorted(TARGETS.items()):
            _doctor_target(key, target, home, launcher, args, is_legacy=False)
    for key, target in sorted(CLEANUP_TARGETS.items()):
        _doctor_target(key, target, None, launcher, args, is_legacy=True)


def _suggest_user(args):
    """Кому предложить запуск в отказе root: имя или None.

    SUDO_USER — если запускали через sudo. Иначе (su -, runuser, скрипт от
    root) — владелец первого существующего явного дома или каталога --config:
    именно ему и адресована запись.
    """
    user = os.environ.get("SUDO_USER")
    if user and user != "root":
        return user
    paths = list(explicit_homes(args.homes)[0] or [])
    if args.config:
        paths.append(os.path.dirname(os.path.abspath(os.path.expanduser(args.config))))
    for path in paths:
        try:
            uid = os.stat(path).st_uid
            if uid != 0:
                return pwd.getpwuid(uid).pw_name
        except (OSError, KeyError):
            continue
    return None


def root_refusal(args, argv):
    """Текст отказа, если root собрался что-то записать; иначе None.

    От root не пишем ничего, в том числе по явным --home/LQRAG_REGISTER_HOMES
    и --config: файл root'а агент не перезапишет, а новый с 0600 и не
    прочитает. chown тоже нельзя: в каталоге пользователя root пошёл бы по
    подложенному симлинку. Maintainer-скрипты и юнит зовут скрипт от имени
    пользователя (runuser, systemd --user). Чтение (--doctor, --list,
    --dry-run) разрешено.
    """
    if os.geteuid() != 0:
        return None
    if args.doctor or args.list or args.dry_run:
        return None
    user = _suggest_user(args)
    user = shlex.quote(user) if user else "<пользователь>"
    command = " ".join(shlex.quote(part) for part in [_script_path()] + list(argv))
    return (
        "register_mcp.py запущен от root и ничего не записал.\n"
        "Запись от root сделает конфиги агентов собственностью root: агент перестанет "
        "их перезаписывать, а новый файл с правами 0600 не сможет и прочитать. "
        "К тому же от root находится дом root, а не пользователя.\n"
        "Запустите от имени пользователя:\n"
        "  runuser -u %s -- python3 %s" % (user, command)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        metavar="NAME",
        help="обработать только эту цель (можно повторять): %s" % ", ".join(sorted(TARGETS)),
    )
    parser.add_argument("--all", action="store_true", help="все цели (поведение по умолчанию)")
    parser.add_argument("--list", action="store_true", help="перечислить цели и выйти")
    parser.add_argument("--doctor", action="store_true", help="диагностика, ничего не менять")
    parser.add_argument(
        "--home",
        action="append",
        dest="homes",
        metavar="PATH",
        help="дом, в котором искать конфиги агентов (можно повторять). Вместе с %s "
        "ЗАМЕНЯЕТ обнаружение по passwd и $HOME" % HOMES_ENV,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="код возврата по итогу: 0 — ошибок нет и агент найден, 3 — агентов нет "
        "ни в одном доме, 1 — хотя бы одна ОШИБКА или не найден ни один дом "
        "(без флага — 0, чтобы юнит не упирался в StartLimit)",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="работать с одним указанным файлом вместо реестра целей "
        "(этим пользуется MCP_CONFIG в setup.sh и uninstall.sh)",
    )
    parser.add_argument(
        "--schema",
        default="opencode",
        choices=sorted(SCHEMAS),
        help="схема записи для --config (%(default)s)",
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="имя сервера в конфиге (%(default)s)")
    parser.add_argument("--launcher", default=None, help="путь к mcp_launcher.sh")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="вписать в запись агента явный LQRAG_ALLOW_WRITE=1 (write-инструменты и так "
        "включены у моста по умолчанию; флаг оставляет след в конфиге)",
    )
    parser.add_argument("--remove", action="store_true", help="снять регистрацию")
    parser.add_argument("--dry-run", action="store_true", help="показать результат, не записывая")
    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    if args.list:
        for home in discover_homes(args.homes):
            for key, target in sorted(TARGETS.items()):
                print("%-9s %-22s %s" % (key, target["title"], resolve_path(target["path"], home)))
        for key, target in sorted(CLEANUP_TARGETS.items()):
            print("%-9s %-22s %s  (только очистка)" % (key, target["title"], resolve_path(target["path"])))
        return 0

    launcher = os.path.abspath(os.path.expanduser(args.launcher or default_launcher()))

    if args.doctor:
        run_doctor(launcher, args)
        return 0

    # До любой записи, включая chmod лаунчера в ensure_launcher.
    refusal = root_refusal(args, argv)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    for note in check_launcher_origin(launcher):
        print("%s" % note, file=sys.stderr)

    for note in ensure_launcher(launcher, fix=not args.dry_run):
        # Не отказ: пакет может регистрировать мост до того, как файлы
        # разложены. Но без предупреждения агент молча останется без инструментов.
        print("%s: %s" % (launcher, note), file=sys.stderr)

    # Легаси-очистка считается отдельно: она не повод перезапускать агента и не
    # признак того, что агент найден. Ошибки считаются по всем.
    agent_statuses = []
    cleanup_statuses = []
    homes = None

    if args.config:
        # Явно указанный файл. Реестр в этом случае не трогаем вообще: вызывающий
        # знает, куда пишет, и подмешивать к его выбору стандартные цели значило
        # бы делать не то, о чём просили. Маркер агента — каталог самого файла:
        # нет каталога — цель пропускается, как неустановленный агент (под
        # --strict код 3). Каталог не создаём: опечатка в пути оставила бы мусор.
        ad_hoc = {
            "title": "указан через --config",
            "marker": os.path.dirname(args.config) or ".",
            "path": args.config,
            "schema": args.schema,
        }
        agent_statuses.append(process_target("--config", ad_hoc, launcher, args, cleanup_only=False))
    else:
        selected = args.targets
        if selected:
            unknown = [name for name in selected if name not in TARGETS and name not in CLEANUP_TARGETS]
            if unknown:
                raise SystemExit("неизвестные цели: %s (см. --list)" % ", ".join(unknown))

        homes = discover_homes(args.homes)
        if not homes:
            # Не отказ: без --strict код 0 (см. docstring модуля).
            print(
                "не найден ни один домашний каталог uid %d — регистрировать некуда "
                "(подробности: --doctor)" % os.getuid(),
                file=sys.stderr,
            )

        for key, target in sorted(TARGETS.items()):
            if selected and key not in selected:
                continue
            # Один и тот же файл, видимый из двух домов (например, .wsc в
            # /home/work — симлинк в pw_dir), обрабатываем один раз.
            seen = set()
            for home in homes:
                real = os.path.realpath(resolve_path(target["path"], home))
                if real in seen:
                    continue
                seen.add(real)
                agent_statuses.append(
                    process_target(key, target, launcher, args, cleanup_only=False, home=home)
                )

        for key, target in sorted(CLEANUP_TARGETS.items()):
            if selected and key not in selected:
                continue
            cleanup_statuses.append(
                process_target(key, target, launcher, args, cleanup_only=not args.remove)
            )

    # Просим перезапуск, только если конфиг агента действительно переписан.
    if not args.remove and WRITTEN in agent_statuses:
        print("")
        print("Перезапустите агента, чтобы он подхватил инструменты.")

    if not args.strict:
        return 0
    if homes == [] or FAILED in agent_statuses + cleanup_statuses:
        return 1
    if not any(status in AGENT_FOUND for status in agent_statuses):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
