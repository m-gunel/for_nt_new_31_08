#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регистрация MCP-моста LocalRAG в конфигах агентских приложений.

Зачем. Агент запускает мост как локальный процесс по АБСОЛЮТНОМУ пути, и до сих
пор этот путь вписывал человек руками. Скрипт закрывает этот шаг: его зовёт
пользовательский systemd-юнит при входе в сессию и при появлении конфига агента.

ЧТО ИЗМЕНИЛОСЬ ПО СРАВНЕНИЮ С ПРЕЖНЕЙ ВЕРСИЕЙ — и почему это было обязательно.

1. Ключ записи. Раньше скрипт клал сервер в `mcp.servers.<имя>`. Это неверно.
   По config-schema.json (лежит рядом с конфигом Workspace Computer) ключи
   внутри `mcp` — это САМИ ИМЕНА СЕРВЕРОВ, то есть правильно `mcp.<имя>`.
   Прежняя запись читалась агентом как сервер с именем "servers", у которого
   нет обязательных `type` и `command`; схема строгая (additionalProperties:
   false на всех уровнях), поэтому отвергался ВЕСЬ конфиг, а не одна запись.
   Снаружи это выглядело как «файл правильный, а в интерфейсе MCP не настроены».

2. Поле окружения. В схеме оно называется `environment`, а не `env`.
   Прежний `env` тоже не проходил валидацию.

3. Таймаут. У McpLocalConfig есть `timeout`, и по умолчанию он 5000 мс. Прогрев
   модели OpenVINO — 40-45 секунд, значит первый же local_search после старта
   гарантированно упирался в дефолт. Выставляем явно.

4. Цели. Их две, и схемы у них РАЗНЫЕ:
     - Workspace Computer читает ~/.wsc/config/wsc.json (opencode-схема);
     - GigaCode читает ~/.gigacode/settings.json (плоский mcpServers,
       запись из command/args/env — без поля type, как в рабочем конфиге).
   Каталог ~/.config/gigatool/ — легаси, новых записей туда не пишем, но
   вычищаем оставленный там прежней версией невалидный ключ.

5. JSONC. Конфиг агента допускает комментарии и висячие запятые (в схеме стоят
   allowComments и allowTrailingCommas). json.loads такого не умеет, и прежний
   скрипт на живом пользовательском конфиге просто отказывался работать.
   BOM в начале файла (его оставляют редакторы Windows) тоже снимаем.

6. Дома. «~» в целях — это не только $HOME. На SberOS у доменного пользователя
   два домашних каталога в одной ФС: pw_dir /home/DOMAIN@LOGIN (там GUI,
   «Загрузки» и конфиг WSC) и «папка разработчика» /home/work/LOGIN@DOMAIN,
   которую терминал отдаёт как $HOME. Прежняя версия смотрела только в $HOME,
   и ручной запуск из терминала писал не туда, где конфиг читает агент.
   Теперь цели обходятся в каждом доме (см. discover_homes), а пишем только
   туда, где агент уже установлен.

7. root. От root скрипт не пишет НИЧЕГО (см. root_refusal) — ни в найденные
   дома, ни по явным --home/LQRAG_REGISTER_HOMES, ни по --config. Конфиг,
   записанный root'ом, агент больше не перезапишет, а в чужом каталоге root
   шёл бы по симлинкам, которые туда заранее может положить владелец.
   postinst, prerm и юнит зовут скрипт от имени пользователя (runuser),
   запрет их не касается. Читать от root можно: --doctor, --list, --dry-run.

8. Код возврата. Без --strict он 0 почти всегда: юнит дёргается
   .path-срабатываниями, и failed-состояние упёрлось бы в StartLimit. С
   --strict (так зовёт postinst) по итогу: 0 — ошибок нет и хотя бы один
   агент найден, 3 — ошибок нет, но агентов нет ни в одном доме, 1 — хотя бы
   одна цель дала ОШИБКУ или не найден ни один дом. 2 — отказ под root.

Только stdlib и только конструкции уровня python3.6: мост и этот скрипт живут на
системном интерпретаторе, а нижняя граница python3 в debian/control снята
сознательно — на базе уровня Debian 12 это python3.11.

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

DEFAULT_NAME = "localrag"
LAUNCHER_NAME = "mcp_launcher.sh"

# Таймаут MCP-запроса в миллисекундах. Дефолт агента — 5000, а прогрев модели
# занимает 40-45 секунд: без явного значения первый поиск после старта демона
# обречён. Две минуты закрывают и прогрев, и холодный поиск по большому индексу.
MCP_TIMEOUT_MS = 120000

# Внутренний таймаут самого моста (LQRAG_TIMEOUT, см. mcp_server.py), его
# дефолт — 30 секунд. Прогрев здесь ни при чём: main.py только биндит сокет, а
# listen uvicorn делает после lifespan, то есть после прогрева. Все 40-45
# секунд прогрева соединение сразу отвергается, и мост без всякого ожидания
# отвечает «запускается или прогревается». 110 секунд нужны холодному поиску
# по большому индексу — первый запрос после старта демона в 30 секунд
# укладывается не всегда. Держим оба таймаута согласованными: агент ждёт
# MCP_TIMEOUT_MS, мост внутри — BRIDGE_TIMEOUT_S, и внутренний обязан быть НЕ
# БОЛЬШЕ внешнего, иначе агент оборвёт запрос раньше, чем мост успеет вернуть
# осмысленную ошибку.
BRIDGE_TIMEOUT_S = "110"

# marker — каталог, по наличию которого судим, что агент вообще установлен.
# Он НЕ совпадает с каталогом конфига: у WC конфиг лежит на уровень глубже
# (~/.wsc/config), и этого подкаталога может ещё не быть, хотя сам агент есть.
# «~» здесь разворачивается относительно КАЖДОГО дома из discover_homes.
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

# Цели, куда мы больше НЕ пишем, но откуда обязаны убрать за собой мусор.
# Прежняя версия скрипта оставила здесь ключ mcp.servers, из-за которого конфиг
# не проходит валидацию целиком — то есть мы своими руками сломали агента тем,
# кто уже тестировал сборку.
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

# Где пакет кладёт пользовательские юниты. --doctor проверяет, что пользователь
# может их прочитать: 1.0.6 положил их с правами 0600 root, и systemd --user
# отвечал «could not be found», хотя файлы на месте. Переменная — для тестов.
UNIT_DIR = "/usr/lib/systemd/user"
UNIT_DIR_ENV = "LQRAG_UNIT_DIR"
UNITS = (
    "lib-local-rag.service",
    "lib-local-rag-mcp-register.service",
    "lib-local-rag-mcp-register-wsc.path",
    "lib-local-rag-mcp-register-gigacode.path",
)


# --- пути -------------------------------------------------------------------


def config_home():
    """XDG_CONFIG_HOME, если задан, иначе ~/.config.

    Прежняя версия разворачивала только «~» и переменную игнорировала. На АРМ,
    где XDG_CONFIG_HOME переопределён политикой, скрипт писал не туда.
    """
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


# --- дома -------------------------------------------------------------------


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

    /home/work/<имя> — для ЛЮБОГО имени: .path-юниты следят за
    /home/work/%u безусловно, а без «@» бывают и локальная учётка, и SSSD с
    use_fully_qualified_names=false. Иначе срабатывание .path перезапускало
    бы регистрацию, а она этот дом не видела. От чужого каталога с таким же
    именем защищает проверка владельца в check_home. «@» нужен только второй
    половине пары: перевёрнутое DOMAIN@LOGIN без домена не построить.

    Строим по имени, а не обходом /home/*: сканирование чужих каталогов
    ничего не даст (писать можно только в свои) и на сотнях доменных
    пользователей с автомонтированием дорого.
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
    симлинком на pw_dir, и одна и та же запись иначе обрабатывалась бы дважды.
    Совпадение и по строке (обычный случай $HOME == pw_dir) отдельной строкой
    не показываем, а дописываем к первой: «[pw_dir, $HOME]».
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
    """Дома, заданные явно: (список, откуда) или (None, None).

    --home сильнее переменной окружения: аргумент командной строки задан
    конкретно для этого запуска.
    """
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

    Явные --home / LQRAG_REGISTER_HOMES заменяют обнаружение целиком и
    владельцем не фильтруются: их задал вызывающий. Запись в чужой дом всё
    равно не состоится — скрипт работает от имени пользователя (root
    отказывается, см. root_refusal), и ядро ответит «нет прав», что уйдёт в
    строку ОШИБКА по цели.
    """
    homes, _source = explicit_homes(cli_homes)
    if homes is not None:
        return homes
    return [path for path, _origin, reason in scan_homes() if reason is None]


def default_launcher():
    """Найти лаунчер в обеих раскладках, в которых живёт этот скрипт.

    В репозитории он лежит в scripts/, а лаунчер — этажом выше. В поставке оба
    файла оказываются рядом, в каталоге mcp/. Проверяем сначала соседний путь:
    если файлы разложены поставкой, брать надо именно его.
    """
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
    Java-реализацией zip, которая unix-права не сохраняет. На стороне
    пользователя это выглядит как «у агента просто нет инструментов» — без
    единой ошибки и без подсказки, куда смотреть.
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
    """Предупредить, если в конфиг уедет путь из чекаута, а не из поставки.

    На машине тестировщика в трёх конфигах оказалось три разных рукописных
    пути — из dist/, из корня репозитория и из домашнего каталога. Такой путь
    переживает переустановку пакета и молча перестаёт существовать при чистке
    рабочей копии: агент теряет инструменты без единой ошибки. Если рядом
    лежит поставленный пакетом лаунчер, а взят не он — об этом надо сказать.
    """
    if launcher == PACKAGED_LAUNCHER or not os.path.isfile(PACKAGED_LAUNCHER):
        return []
    return [
        "ВНИМАНИЕ: в конфиг пойдёт %s, хотя пакет поставил %s. "
        "Путь из рабочей копии не переживёт её удаление — если это не отладка, "
        "перезапустите с --launcher %s" % (launcher, PACKAGED_LAUNCHER, PACKAGED_LAUNCHER)
    ]


# --- чтение и запись JSONC --------------------------------------------------


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

    Нет файла — пустой словарь, это штатный первый запуск. Битый JSON — отказ,
    а не перезапись: в файле лежат чужие настройки, и затирать их из-за одной
    опечатки нельзя.
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
    """os.unlink, которому отсутствие файла — не ошибка."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _create_file(path, payload, mode):
    """Создать НОВЫЙ файл path с байтами payload и правами mode.

    Только через os.open с O_CREAT|O_EXCL|O_NOFOLLOW: уже существующее имя,
    в том числе симлинк (и висячий), даёт отказ, а не запись по ссылке.
    Каталог конфига принадлежит пользователю, и заранее положенный туда
    симлинк на месте временного файла или копии иначе увёл бы запись — а с
    ней и chmod — в произвольный файл. Права ставим через fchmod по
    дескриптору: mode в os.open урезается umask'ом, а chmod по имени снова
    пошёл бы по ссылке. Не вышло — недописанный файл убираем.
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
    """Один раз сохранить исходный конфиг рядом.

    Делается только при первой правке: если копия уже есть, она ценнее свежей —
    в ней состояние до нашего первого вмешательства. Наличие проверяем через
    lexists: висячий симлинк на месте копии — тоже «есть», и создавать файл по
    нему мы не должны (см. _create_file).
    """
    if raw is None:
        return None
    backup = path + ".localrag-backup"
    if os.path.lexists(backup):
        return None
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        _create_file(backup, raw.encode("utf-8"), mode)
    except OSError as exc:
        print("не удалось сохранить резервную копию %s: %s" % (backup, exc), file=sys.stderr)
        return None
    return backup


def save_config(path, config):
    """Атомарная запись: агент не должен прочитать половину файла.

    Режим существующего файла СОХРАНЯЕТСЯ. Прежняя версия безусловно ставила
    0600 и тем самым понижала права чужого конфига на каждой регистрации.
    Новый файл создаём с 0600: в конфиге агента могут оказаться токены
    соседних MCP-серверов.

    Временный файл называется .localrag.tmp, а не <имя>.tmp: за <имя> следит
    systemd .path-юнит, и лишнее имя рядом не должно его дёргать. Остаток от
    прерванного прогона (или подложенный симлинк) удаляем — unlink убирает
    саму ссылку, не трогая то, на что она указывает, — и создаём заново
    через _create_file. os.replace тоже не идёт по ссылкам: он заменяет имя.
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


# --- схема opencode: mcp.<имя> ---------------------------------------------


def _opencode_entry(launcher, allow_write):
    environment = {"LQRAG_TIMEOUT": BRIDGE_TIMEOUT_S}
    if allow_write:
        # Явный LQRAG_ALLOW_WRITE=1 в записи: у моста оно и так умолчание,
        # но так видно, что установка сознательно разрешает агенту
        # управлять индексацией.
        environment["LQRAG_ALLOW_WRITE"] = "1"
    # LQRAG_URL здесь НЕ задаётся намеренно: без него мост читает фактический
    # порт из ~/.files-search/port, и запись переживает перезапуск демона на
    # другом порту. Зашитый адрес отключил бы этот механизм — он сильнее
    # порт-файла.
    return {
        "type": "local",
        "command": [launcher],
        "enabled": True,
        "timeout": MCP_TIMEOUT_MS,
        "environment": environment,
    }


def _opencode_cleanup(config):
    """Снять невалидный ключ mcp.servers, оставшийся от прежней версии скрипта.

    Удаляем осторожно: только если под этим именем лежит объект БЕЗ поля type,
    то есть заведомо не описание сервера. Если кто-то и правда назвал свой
    MCP-сервер «servers» и оформил его по схеме — это чужая рабочая запись,
    и трогать её нельзя.
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
    entry = _opencode_entry(launcher, allow_write)
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


# --- схема mcpServers (GigaCode) -------------------------------------------


def _mcp_servers_entry(launcher, allow_write):
    # Отличия от opencode-схемы, и каждое обязательно соблюсти:
    #   - command — СТРОКА, а не массив;
    #   - аргументы отдельным массивом args. Он необязателен, но пишем пустым
    #     явно: так видно, что аргументов нет намеренно, а не потерялись;
    #   - окружение называется env, а не environment.
    # Поля транспорта нет. Прежняя версия писала type: "stdio" по образцу
    # VS Code, но в рабочем конфиге GigaCode запись состоит из command, и
    # stdio выводится из его наличия. Держим запись такой же, как проверенная
    # руками; старая запись с type не совпадёт с этой и перезапишется сама.
    env = {"LQRAG_TIMEOUT": BRIDGE_TIMEOUT_S}
    if allow_write:
        env["LQRAG_ALLOW_WRITE"] = "1"
    return {
        "command": launcher,
        "args": [],
        "env": env,
    }


def _mcp_servers_place(config, name, launcher, allow_write):
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit("в конфиге ключ 'mcpServers' не объект — не берусь его чинить")
    entry = _mcp_servers_entry(launcher, allow_write)
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
        "entry": _opencode_entry,
    },
    "mcp_servers": {
        "place": _mcp_servers_place,
        "drop": _mcp_servers_drop,
        "cleanup": _mcp_servers_cleanup,
        "has": _mcp_servers_has,
        "section": "mcpServers",
        "entry": _mcp_servers_entry,
    },
}


def launcher_duplicates(config, schema, name):
    """Чужие имена в секции, чьи command/args запускают наш лаунчер.

    Рукописная запись от прежних инструкций (localrag-dev, local-rag) плюс наша
    — это два моста к одному демону: агент видит инструменты дважды, а при
    устаревшем пути одна из копий молча не стартует.
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


# --- обработка целей --------------------------------------------------------


# Итог обработки одной цели (process_target). По нему main() считает код
# возврата под --strict и решает, звать ли перезапустить агента: фраза про
# перезапуск при пропуске или ошибке была бы неправдой.
WRITTEN = "записано"  # файл изменён (в том числе снята запись или вычищен мусор)
UNCHANGED = "без изменений"  # агент есть, запись уже такая, как надо
PLANNED = "получилось бы"  # --dry-run: агент есть, запись изменилась бы
SKIPPED = "пропуск"  # агента нет (или нечего снимать)
FAILED = "ошибка"

# Статусы, при которых агент в доме найден — для --strict это «было куда
# регистрировать».
AGENT_FOUND = (WRITTEN, UNCHANGED, PLANNED)


def process_target(key, target, launcher, args, cleanup_only, home=None):
    """Обработать одну цель и вернуть её статус. Отказ по цели НЕ прерывает остальные.

    Это не вежливость, а требование к юниту, который дёргается автоматически.
    Если у пользователя битый JSON в конфиге легаси-каталога, регистрация в
    Workspace Computer и GigaCode всё равно обязана состояться, а сам юнит —
    завершиться успешно: иначе .path-юнит будет упираться в StartLimit и
    в какой-то момент отвалится вместе с полезными срабатываниями.
    Проблемы уходят в stderr, то есть в журнал, и видны в --doctor. Статус
    FAILED при этом возвращается — по нему --strict даёт ненулевой код.

    home — дом, относительно которого разворачивается «~» цели. None значит
    $HOME: так обрабатываются легаси-цель и явный --config.
    """
    try:
        return _process_target(key, target, launcher, args, cleanup_only, home)
    except SystemExit as exc:
        print("%-9s ОШИБКА: %s" % (key, exc), file=sys.stderr)
    except OSError as exc:
        print("%-9s ОШИБКА ввода-вывода: %s" % (key, exc), file=sys.stderr)
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
        # Свежесозданный конфиг подписываем схемой, как это делает сам агент:
        # config-schema.json лежит в том же каталоге, ссылка на него включает
        # подсветку и проверку в редакторе. Ставим ПЕРВЫМ ключом, чтобы файл
        # выглядел так же, как написанный руками. Существующий конфиг не
        # трогаем: там подпись либо уже есть, либо её сознательно нет.
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


# --- диагностика ------------------------------------------------------------


def _script_path():
    return os.path.abspath(__file__)


def _access_problem(path, mode):
    """None, если путь доступен в режиме mode, иначе причина по-русски.

    os.access, а не разбор битов: он учитывает и права каталогов по пути.
    У 1.0.6 файлы были целы, но лежали в каталоге 0700 root.
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
    desired = schema["entry"](launcher, args.allow_write)
    if current == desired:
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

    От root не пишем НИЧЕГО, в том числе по явным --home/LQRAG_REGISTER_HOMES
    и --config. Файл, записанный root'ом, достаётся root: агент перестаёт его
    перезаписывать, а новый файл с 0600 не может и прочитать. Возвращать
    владельца chown'ом тоже нельзя: каталог конфига принадлежит пользователю,
    и root, создающий там файлы и меняющий им права по имени, пойдёт по
    подложенному симлинку куда угодно. Писать должен сам пользователь —
    maintainer-скрипты и юнит так и делают (runuser, systemd --user). Чтение
    (--doctor, --list, --dry-run) не запрещаем.
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
        # Не отказ: пакет может регистрировать мост до того, как файлы разложены.
        # Но молчать нельзя — именно так и получается агент без инструментов,
        # диагностики которому взять неоткуда.
        print("%s: %s" % (launcher, note), file=sys.stderr)

    # Статусы целей агентов (реестр TARGETS или --config) и легаси-очистки
    # отдельно: вычищенный мусор в легаси-каталоге — не повод перезапускать
    # агента и не признак того, что агент найден. Ошибки считаются по всем.
    agent_statuses = []
    cleanup_statuses = []
    homes = None

    if args.config:
        # Явно указанный файл. Реестр в этом случае не трогаем вообще: вызывающий
        # знает, куда пишет, и подмешивать к его выбору стандартные цели значило
        # бы делать не то, о чём просили. Каталог считаем существующим по факту
        # указания пути — проверка marker'а здесь только мешала бы.
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
            # Без --strict — не отказ с ненулевым кодом: юнит дёргается
            # .path-срабатываниями, и failed-состояние заглушило бы и
            # следующие, полезные.
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

    # Только если конфиг агента действительно переписан: после «без изменений»
    # или «пропуск» призыв перезапустить агента вводил бы в заблуждение.
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
