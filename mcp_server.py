#!/usr/bin/env python3
"""LocalRAG → MCP (stdio) мост.

Отдаёт агенту то, чего у него нет своими силами: семантический поиск по
локальному индексу LocalRAG и состояние этого индекса. Разговаривает с
сайдкаром по HTTP через уже существующие ручки (/health, /status, /search,
/config, /read_file) — собственного API у моста нет.

Только stdlib: в поставке мост запускается на системном python3 без venv.
Офисные форматы и pdf читает демон (POST /api/v1/read_file: pymupdf и
конвертер Р7 для .doc/.xls). Без демона OOXML (.docx/.pptx/.xlsx) мост
разбирает сам, pdf и легаси-форматы — отказ. Текстовые файлы читаются всегда.

Протокол: MCP stdio (JSON-RPC построчно через stdin/stdout).

Переменные окружения:
  LQRAG_URL          — базовый URL сайдкара. Не задан — порт берётся из файла,
                       опубликованного демоном, иначе http://127.0.0.1:8000
  LQRAG_PORT_FILE    — явный путь к файлу с портом; задан — ищем только в нём.
                       Не задан — $HOME/.files-search/port, затем тот же файл
                       в домашнем каталоге из passwd; файл, чей демон жив по
                       соседнему pid, берётся раньше устаревшего
  LQRAG_AGENT        — несекретный ярлык агента (gigacode, wsc); по нему мост
                       берёт токен из tokens/<агент>.token рядом с порт-файлом
  LQRAG_TOKEN_FILE   — явный путь к файлу токена; задан — берём только его.
                       Токен уходит лишь нашему демону: по порт-файлу с живым
                       pid этой учётки или на loopback-адрес LQRAG_URL, а на
                       Linux — если порт слушает процесс этой учётки
  LQRAG_TIMEOUT      — таймаут HTTP, сек (по умолчанию 30)
  LQRAG_AUTOSTART    — 0 запрещает мосту поднимать lib-local-rag.service, когда
                       демон недоступен (по умолчанию поднимает, не чаще раза
                       в 3 минуты)
  LQRAG_SYSTEMCTL    — какой systemctl звать для автозапуска (по умолчанию из PATH)
  LQRAG_ALLOW_WRITE  — 0 прячет от агента инструменты управления индексацией;
                       по умолчанию они включены (агент — единственный пульт)
  LQRAG_MAX_OUTPUT   — бюджет символов на ответ поиска (по умолчанию 12000)
"""

import datetime
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

# Адрес сайдкара: LQRAG_URL, иначе порт из файла, который публикует демон
# (порт динамический, БТ §4.2), иначе 8000.
# Заданный LQRAG_PORT_FILE исключает поиск по домашним каталогам: иначе тест
# или нестандартная установка подхватили бы порт чужого демона.
_PORT_FILE_ENV = os.environ.get("LQRAG_PORT_FILE")
LQRAG_PORT_FILE = os.path.expanduser(_PORT_FILE_ENV) if _PORT_FILE_ENV else None
PORT_FILE_RELATIVE = os.path.join(".files-search", "port")
# pid демона рядом с порт-файлом (src/port_file.py), см. _pid_alive.
PID_FILE_NAME = "pid"
FALLBACK_URL = "http://127.0.0.1:8000"
LQRAG_TIMEOUT = float(os.environ.get("LQRAG_TIMEOUT", "30"))

# Токен агента (VAS-6850): в конфиге агента только несекретный ярлык
# LQRAG_AGENT, секрет — в tokens/<агент>.token рядом с порт-файлом. Явный
# LQRAG_TOKEN_FILE заменяет поиск, как LQRAG_PORT_FILE.
_AGENT_ENV = os.environ.get("LQRAG_AGENT", "").strip()
# Имя агента — часть пути к токену: «../x» увёл бы путь за пределы tokens/.
_AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
AGENT_NAME = _AGENT_ENV if _AGENT_NAME_RE.fullmatch(_AGENT_ENV) else ""
_TOKEN_FILE_ENV = os.environ.get("LQRAG_TOKEN_FILE")
LQRAG_TOKEN_FILE = os.path.expanduser(_TOKEN_FILE_ENV) if _TOKEN_FILE_ENV else None
TOKENS_DIR_NAME = "tokens"
TOKEN_SUFFIX = ".token"
# Алфавит secrets.token_urlsafe. Перевод строки внутри файла http.client
# вернул бы в ValueError вместе с токеном, а текст ошибки уходит агенту.
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}")
# Владельца слушающего порта Linux показывает в этих таблицах без root.
PROC_NET_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
TCP_LISTEN_STATE = "0A"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект отдаётся как ответ: urllib перенёс бы Authorization на новый адрес."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Без прокси: штатный urlopen берёт http_proxy из окружения агента, а no_proxy
# сравнивает строкой («localhost» не покрывает 127.0.0.1). 502 прокси выглядит
# как ответ живого демона, и автозапуск не срабатывает. Все запросы к сайдкару —
# только через этот opener.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

# Автозапуск: если служба не поднята (остановлена, сессия не перезапущена после
# установки), мост сам зовёт systemctl — просить об этом пользователя нельзя.
SERVICE_UNIT = "lib-local-rag.service"
AUTOSTART_TIMEOUT_S = 10
# Ожидание systemctl внутри вызова: таймаут MCP-запроса у агента (WSC) 5 с,
# ответ «прогревается» должен успеть раньше.
AUTOSTART_WAIT_S = 3.0
# Проба на initialize: только отличить «слушает» от «отказ».
PROBE_TIMEOUT_S = 1.0
# Прогрев 40-45 с, на медленной машине до полутора минут; отказы дольше — уже
# не прогрев, а «не поднимается».
STUCK_AFTER_S = 180.0
# Если systemd службу не поднял, повтор start на каждом вызове ничего не даст,
# только забьёт лог.
AUTOSTART_RETRY_S = 180.0
# Маску postinst снимает сам, и «masked» устаревает быстро.
MASKED_RETRY_S = 30.0
WARMING_TEXT = (
    "Индексатор LocalRAG запускается или прогревается (обычно около 45 секунд). "
    "Повторите запрос через минуту."
)
UNAUTHORIZED_TEXT = (
    "Индексатор LocalRAG отклонил запрос: нет действующего токена агента "
    "(401). Токен выдаётся при регистрации моста; после установки или "
    "обновления пакета перезапустите агент, а если не помогло — диагностика: "
    "python3 /usr/lib/lib-local-rag/mcp/register_mcp.py --doctor"
)
BROKEN_TOKEN_TEXT = (
    "Файл токена агента %s повреждён (%s), запрос не отправлен. Перевыпустите "
    "токен: python3 /usr/lib/lib-local-rag/mcp/register_mcp.py --rotate"
)
FOREIGN_LISTENER_TEXT = (
    "порт %d занят чужим процессом (не этого пользователя), запрос и токен "
    "агента туда не отправлены"
)
STUCK_TEXT = "Индексатор LocalRAG не поднимается (%s)."
SERVICE_MISSING_TEXT = (
    "Служба lib-local-rag.service недоступна для пользователя (не найдена). "
    "Проверьте: python3 /usr/lib/lib-local-rag/mcp/register_mcp.py --doctor"
)
SERVICE_MASKED_TEXT = (
    "Служба LocalRAG временно остановлена: идёт установка или обновление пакета. "
    "Повторите запрос через минуту."
)
# Ответ systemctl на юнит, которого он не видит (в том числе нечитаемый файл).
_UNIT_MISSING_MARKERS = ("not found", "could not be found", "not be found")
# Юнит под маской: prerm маскирует службу (--runtime) на время замены файлов
# пакета, чтобы мост не поднял демон из полузаменённого бандла.
_UNIT_MASKED_MARKERS = ("masked",)
# Что показать агенту из `systemctl --user show`, когда служба не поднимается.
_SERVICE_PROPERTIES = ("ActiveState", "SubState", "NRestarts")
# Агент — единственный интерфейс управления индексатором, поэтому write-
# инструменты видны ему по умолчанию. Закрытая установка отключает их явно:
# LQRAG_ALLOW_WRITE=0. Песочница local_read_file от флага не зависит, но её
# граница — список папок, а его с write-инструментами меняет сам агент, в том
# числе по инструкции из проиндексированного документа. Корень ФС, системные
# и скрытые каталоги демон в список не пускает (_validate_folders).
ALLOW_WRITE = os.environ.get("LQRAG_ALLOW_WRITE", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MAX_OUTPUT_CHARS = int(os.environ.get("LQRAG_MAX_OUTPUT", "12000"))

SERVER_NAME = "localrag-mcp"
SERVER_VERSION = "0.4.0"

# Версии протокола, которые мост реально отрабатывает. structuredContent —
# из 2025-06-18, поэтому он же и предпочитаемый.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = "2025-06-18"

# Сколько текста отдаём на документ. Без этих границ limit=50 × чанки × 600
# символов уходит в контекст модели целиком.
MAX_CHUNKS_PER_DOC = 3
MAX_CHUNK_CHARS = 600
# Короче фрагмент уже не несёт смысла: при нехватке места фрагменты получают
# верхние документы, остальные остаются с заголовком и путём.
MIN_CHUNK_CHARS = 120
# Строка о документе и список копий — служебные: длинное название из свойств
# файла или десяток копий не должны съедать место других документов.
MAX_ABOUT_CHARS = 300
MAX_COPIES_SHOWN = 5
# Верхняя граница чтения файла: больше в контекст всё равно не влезет.
MAX_FILE_CHARS = 40000
# Строк на лист книги Excel при локальном чтении (без демона): больше в ответ на
# MAX_FILE_CHARS всё равно не влезет. У индексатора свой лимит — ExcelParserConfig.max_rows.
MAX_XLSX_ROWS = 1000
# Потолок локального разбора по размеру файла — как _READ_FILE_MAX_BYTES демона
# (его ответ 413). Это сжатый размер; объём после распаковки — ниже.
MAX_LOCAL_READ_BYTES = 64 * 1024 * 1024
# Части OOXML мост разбирает в дерево целиком, а deflate сжимает до ~1000:1:
# без своих пределов zip-бомба в сотни КиБ занимает гигабайты (как в
# src/parsers/ooxml_limits.py демона).
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
# Живой XML жмётся в 5–20 раз; сотни к одному — признак бомбы.
MAX_RATIO = 100


def _log(*args):
    """Лог в stderr: в stdout идёт JSON-RPC, туда писать нельзя."""
    print("[localrag-mcp]", *args, file=sys.stderr, flush=True)


class InvalidParams(Exception):
    """Невалидные аргументы вызова — ошибка протокола -32602.

    Молча не исправляем (зажать limit, подменить query_type): агент должен
    увидеть свою ошибку.
    """


class ToolError(Exception):
    """Инструмент не смог отработать — уходит агенту как isError, не как сбой."""


class ApiError(ToolError):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__("LocalRAG API вернул %s: %s" % (code, detail))


class TransportError(ToolError):
    """Сайдкар недоступен, молчит или ответил не-JSON."""


class ReplyTimeout(TransportError):
    """Сайдкар принял запрос, но не ответил за LQRAG_TIMEOUT: жив, но занят."""


class ForeignListener(Exception):
    """Порт маршрута слушает процесс другого пользователя: туда не шлём ничего."""


def _port_file_candidates():
    """Где искать порт-файл без LQRAG_PORT_FILE: в $HOME и в доме из passwd.

    Служба systemd --user публикует порт в pw_dir, а терминал SberOS
    открывается с HOME=/home/work/<LOGIN@DOMAIN>: по одному ~ мост файла не
    найдёт. $HOME первым — туда пишет демон, запущенный вручную из терминала
    (живость по pid важнее порядка, см. _port_file_routes). Один каталог под
    двумя именами (симлинк) проверяется один раз.
    """
    homes = [os.path.expanduser("~")]
    try:
        import pwd

        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        # Не POSIX или uid без записи в passwd — остаётся один $HOME.
        pass
    candidates = []
    seen = set()
    for home in homes:
        if not home or not os.path.isabs(home):
            continue
        path = os.path.join(home, PORT_FILE_RELATIVE)
        key = os.path.realpath(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates


def _read_port(path):
    """Порт из файла; None, если файла нет или порт в нём невалиден."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            port = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _read_pid(port_file):
    """pid демона из файла рядом с порт-файлом; None — нет, битый или не POSIX."""
    # os.kill(pid, 0) на Windows не проверка, а TerminateProcess.
    if not port_file or os.name != "posix":
        return None
    path = os.path.join(os.path.dirname(port_file), PID_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    # 0 и отрицательные — группа процессов, а не процесс: такой ответ ни о чём.
    return pid if pid > 0 else None


def _pid_alive(port_file):
    """Жив ли демон, опубликовавший порт: True, False или None (неизвестно).

    После SIGKILL или выключения питания порт-файл и pid остаются, и по одному
    порту живой демон от мёртвого не отличить.
    """
    pid = _read_pid(port_file)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но сигналить ему нам не положено — он всё равно жив.
        return True
    except OSError:
        return None
    return True


def pid_is_ours(port_file):
    """Порт опубликовал живой процесс этого пользователя.

    PermissionError здесь — «чужой»: pid из устаревшего файла мог достаться
    процессу другой учётки, а ей токен не положен.
    """
    pid = _read_pid(port_file)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# Кого пробовать первым: живой pid, затем неизвестный, мёртвый — последним.
_PID_RANK = {True: 0, None: 1, False: 2}


def _port_file_routes():
    """(routes, checked): опубликованные порты, живые демоны — первыми.

    routes — (url, port_file, alive) по каждому читаемому порт-файлу; checked —
    все просмотренные порт-файлы, для лога и ошибок. При равной живости —
    порядок кандидатов. Без сортировки по pid устаревший файл в $HOME заслонял
    бы живой демон службы в pw_dir. Мёртвый кандидат остаётся: другого может не
    быть, а путь к файлу нужен для текста ошибки.
    """
    checked = [LQRAG_PORT_FILE] if LQRAG_PORT_FILE else _port_file_candidates()
    found = []
    for index, path in enumerate(checked):
        port = _read_port(path)
        if port is None:
            continue
        alive = _pid_alive(path)
        found.append((_PID_RANK[alive], index, path, port, alive))
    found.sort()
    routes = [
        ("http://127.0.0.1:%d" % port, path, alive) for _, _, path, port, alive in found
    ]
    return routes, checked


def _token_path(source, port_file):
    """Файл токена для маршрута или None, если токен по нему не положен.

    Токен — из того же корня, что и порт: при разошедшихся домах иначе ушёл бы
    токен одного дома демону другого. Запасному адресу токен не положен: на
    8000 может слушать кто угодно.
    """
    if source == "fallback":
        return None
    if LQRAG_TOKEN_FILE:
        return LQRAG_TOKEN_FILE
    if not AGENT_NAME or not port_file:
        return None
    return os.path.join(
        os.path.dirname(port_file), TOKENS_DIR_NAME, AGENT_NAME + TOKEN_SUFFIX
    )


def _read_token(path):
    """Токен из файла; None — файла нет. Битый файл — ToolError без содержимого.

    Читается на каждом запросе: ротация и отзыв подменяют файл, и перезапуск
    агента для них не нужен.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise ToolError(BROKEN_TOKEN_TEXT % (path, "не текст UTF-8")) from None
    except OSError as exc:
        raise ToolError(BROKEN_TOKEN_TEXT % (path, exc.strerror or exc)) from None
    if not TOKEN_RE.fullmatch(token):
        raise ToolError(BROKEN_TOKEN_TEXT % (path, "содержимое не похоже на токен"))
    return token


def _loopback_targets(base):
    """(адреса, порт) маршрута на эту машину; None — адрес не loopback."""
    parts = urllib.parse.urlsplit(base)
    try:
        port = parts.port
        target = ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return None
    if port is None or not target.is_loopback:
        return None
    return [target], port


def _pin_localhost(url):
    """localhost в URL — 127.0.0.1, где и слушает демон (сокет AF_INET).

    Иначе urllib разрешил бы имя и в ::1, а владельца там мост не проверял.
    """
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() != "localhost":
        return url
    try:
        port = parts.port
    except ValueError:
        return url
    netloc = "127.0.0.1" if port is None else "127.0.0.1:%d" % port
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def _proc_address(hex_address):
    """Адрес из /proc/net/tcp*: ядро печатает его 32-битными словами хоста."""
    words = [hex_address[i : i + 8] for i in range(0, len(hex_address), 8)]
    packed = b"".join(struct.pack("=I", int(word, 16)) for word in words)
    return ipaddress.ip_address(packed)


def _table_listeners(table, port):
    """[(адрес, uid)] сокетов в LISTEN на port из одной таблицы /proc/net."""
    with open(table, "r", encoding="ascii") as handle:
        rows = handle.read().splitlines()[1:]
    found = []
    for row in rows:
        # sl local_address rem_address st tx:rx tr:when retrnsmt uid ...
        fields = row.split()
        if len(fields) < 8 or fields[3] != TCP_LISTEN_STATE:
            continue
        address, _, hex_port = fields[1].partition(":")
        if int(hex_port, 16) == port:
            found.append((_proc_address(address), int(fields[7])))
    return found


def _proc_listeners(port):
    """[(адрес, uid)] сокетов в LISTEN на port; None — /proc нет (не Linux)."""
    if not os.path.exists(PROC_NET_TABLES[0]):
        return None
    listeners = []
    for table in PROC_NET_TABLES:
        # tcp6 нет, если IPv6 выключен в ядре.
        if os.path.exists(table):
            listeners.extend(_table_listeners(table, port))
    return listeners


def _accepting_uids(listeners, target):
    """uid сокетов, которые примут соединение на target.

    Точный адрес ядро предпочитает подстановочному: иначе чужой [::]:порт с
    IPV6_V6ONLY отрезал бы мост от нашего демона на 127.0.0.1.
    """
    exact = [
        uid
        for address, uid in listeners
        if address == target or getattr(address, "ipv4_mapped", None) == target
    ]
    # IPv6-подстановочный сокет (dual-stack) принимает и IPv4.
    wildcard = [
        uid
        for address, uid in listeners
        if address.is_unspecified and address.version >= target.version
    ]
    return exact or wildcard


def _listener_owner(targets, port):
    """Кто примет соединение: "ours", "foreign", "absent"; None — нечем проверить."""
    listeners = _proc_listeners(port)
    if listeners is None:
        return None
    uids = set()
    for target in targets:
        uids.update(_accepting_uids(listeners, target))
    if not uids:
        return "absent"
    return "ours" if uids == {os.getuid()} else "foreign"


def check_listener(base):
    """Кто слушает порт маршрута: "ours", "absent", "remote" (не loopback), None.

    None — проверить нечем (нет /proc). ForeignListener — порт слушает процесс
    другой учётки. Туда не шлём ничего, даже без токена: её ответы — это выдача
    поиска и папки, которыми мост ограничивает local_read_file.
    """
    route = _loopback_targets(base)
    if route is None:
        return "remote"
    owner = _listener_owner(*route)
    if owner == "foreign":
        raise ForeignListener(FOREIGN_LISTENER_TEXT % route[1])
    return owner


def _route_auth(source, base, port_file):
    """Заголовок Authorization для маршрута; {} — токен по нему не положен.

    Токен уходит только нашему демону: на loopback, по порт-файлу — с живым pid
    этой учётки, а на Linux порт должен слушать процесс нашего uid. Иначе
    перехватчик порта из устаревшего порт-файла получил бы токен, а с ним и
    чтение файлов. ForeignListener — порт слушает чужой процесс.
    """
    owner = check_listener(base)
    path = _token_path(source, port_file)
    if not path or owner in ("absent", "remote"):
        return {}
    if source == "port-file" and not pid_is_ours(port_file):
        return {}
    token = _read_token(path)
    return {"Authorization": "Bearer " + token} if token else {}


def _resolve_routes():
    """(routes, source, checked): куда стучаться, по порядку предпочтения.

    routes — непустой список (url, port_file, alive); первый в нём — выбранный
    адрес, остальные — запасные порт-файлы на случай отказа соединения.
    """
    explicit = os.environ.get("LQRAG_URL")
    if explicit:
        return [(_pin_localhost(explicit.rstrip("/")), None, None)], "env", []
    routes, checked = _port_file_routes()
    if routes:
        return routes, "port-file", checked
    return [(FALLBACK_URL, None, None)], "fallback", checked


def _resolve_address():
    """(url, source, port_file, checked): адрес сайдкара со всей диагностикой.

    port_file — файл, из которого взят порт (None, если не из файла);
    checked — какие порт-файлы просмотрены, для стартового лога и ошибок.
    """
    routes, source, checked = _resolve_routes()
    url, port_file, _ = routes[0]
    return url, source, port_file, checked


def _resolve_base_url():
    """(url, source) сайдкара на момент вызова.

    source: "env" (LQRAG_URL), "port-file" (порт опубликован демоном) или
    "fallback" (http://127.0.0.1:8000 — обычно демон не запущен).

    Адрес не кэшируется: демон могут перезапустить на другом порту, пока мост
    жив, а чтение файла в пять байт дешевле самого HTTP-запроса.
    """
    url, source, _, _ = _resolve_address()
    return url, source


def _attempt_order(routes):
    """Выбранный адрес, затем запасные с живым или неизвестным pid.

    Мёртвый pid в запасных не пробуем: по такому порту если кто и слушает, то
    уже не наш демон.
    """
    return [routes[0]] + [route for route in routes[1:] if route[2] is not False]


def _is_refused(exc):
    """URLError от отказа соединения: по этому адресу сейчас никто не слушает.

    Запрос до сервера не дошёл, поэтому повторить его по другому адресу
    безопасно даже для POST.
    """
    return isinstance(getattr(exc, "reason", None), ConnectionRefusedError)


def _autostart_state():
    """Начальное состояние автозапуска: модулю на старте, тестам — на каждый тест."""
    return {
        # Поток systemctl — последний запущенный; пока он жив, новый не стартует.
        "thread": None,
        # (state, note) последнего start; None — попытки не было или systemctl
        # ещё не ответил.
        "result": None,
        # time.monotonic() последнего start — для ограничения частоты.
        "attempt_at": None,
        # Сводка `systemctl --user show` для текста «не поднимается».
        "service": None,
        # time.monotonic() первого и последнего отказа в текущей серии отказов.
        "down_since": None,
        "last_refused": None,
    }


# Общее состояние основного цикла и фоновых потоков (проба на initialize,
# вызов systemctl): читается и меняется только под _autostart_lock.
_autostart = _autostart_state()
_autostart_lock = threading.Lock()


def _autostart_enabled():
    value = os.environ.get("LQRAG_AUTOSTART", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _note_reachable():
    """Сайдкар ответил (любым HTTP-кодом или хотя бы принял соединение)."""
    with _autostart_lock:
        _autostart["down_since"] = None
        _autostart["last_refused"] = None


def _note_refused():
    """Отметить отказ соединения; вернуть, сколько секунд отказы идут подряд.

    Пауза дольше STUCK_AFTER_S начинает новую серию: демон мог за это время
    подняться и упасть, и старый отсчёт дал бы «не поднимается» на первом же
    отказе.
    """
    now = time.monotonic()
    with _autostart_lock:
        state = _autostart
        last = state["last_refused"]
        if state["down_since"] is None or last is None or now - last > STUCK_AFTER_S:
            state["down_since"] = now
        state["last_refused"] = now
        return now - state["down_since"]


def _run_systemctl(systemctl, args):
    """`systemctl --user <args>` с таймаутом; TimeoutExpired и OSError — наружу.

    stdin и stdout моста — канал JSON-RPC, и чужие байты в нём рвут сессию:
    у systemctl stdin закрыт, stdout — в трубу. Вывод читаем байтами: на
    python3.6 в локали C декодирование падает на первом не-ASCII символе.
    """
    env = dict(os.environ)
    # Агент может запустить мост без XDG_RUNTIME_DIR (не из графической сессии),
    # и тогда systemctl --user не находит сокет пользовательского менеджера.
    if not env.get("XDG_RUNTIME_DIR") and hasattr(os, "getuid"):
        env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
    return subprocess.run(
        [systemctl, "--user"] + list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=AUTOSTART_TIMEOUT_S,
    )


def _systemctl_path():
    return os.environ.get("LQRAG_SYSTEMCTL") or shutil.which("systemctl")


def _start_service():
    """Одна попытка `systemctl --user start --no-block`. Возвращает (state, note).

    state: "started" (systemd принял задание), "not-found" (юнит не виден
    пользовательскому менеджеру), "masked" (служба замаскирована на время
    установки пакета), "failed", "disabled", "no-systemctl".
    """
    if not _autostart_enabled():
        return "disabled", "отключён (LQRAG_AUTOSTART=0)"
    systemctl = _systemctl_path()
    if not systemctl:
        return "no-systemctl", "systemctl не найден, поднять службу нечем"
    # --no-block: прогрев модели идёт 40-45 с, ждать его здесь нельзя.
    try:
        proc = _run_systemctl(systemctl, ["start", "--no-block", SERVICE_UNIT])
    except subprocess.TimeoutExpired:
        return "failed", "systemctl не ответил за %d с" % AUTOSTART_TIMEOUT_S
    except OSError as exc:
        return "failed", "не удалось вызвать %s: %s" % (systemctl, exc)
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        return "started", "systemctl --user start --no-block %s принят" % SERVICE_UNIT
    lowered = err.lower()
    if any(marker in lowered for marker in _UNIT_MASKED_MARKERS):
        return "masked", err
    if any(marker in lowered for marker in _UNIT_MISSING_MARKERS):
        return "not-found", err
    return "failed", "systemctl завершился с кодом %d: %s" % (
        proc.returncode,
        err or "(stderr пуст)",
    )


def _service_summary():
    """Краткое состояние службы из `systemctl --user show` — строкой для агента."""
    if not _autostart_enabled():
        return "не опрашивалась (LQRAG_AUTOSTART=0)"
    systemctl = _systemctl_path()
    if not systemctl:
        return "systemctl не найден"
    args = ["show"]
    for prop in _SERVICE_PROPERTIES:
        args += ["-p", prop]
    args.append(SERVICE_UNIT)
    try:
        proc = _run_systemctl(systemctl, args)
    except subprocess.TimeoutExpired:
        return "systemctl show не ответил за %d с" % AUTOSTART_TIMEOUT_S
    except OSError as exc:
        return "не удалось вызвать %s: %s" % (systemctl, exc)
    values = {}
    for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in _SERVICE_PROPERTIES:
            values[key] = value
    if not values:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return "состояние не получено: %s" % (err or "код %d" % proc.returncode)
    return ", ".join(
        "%s=%s" % (key, values[key]) for key in _SERVICE_PROPERTIES if key in values
    )


def _systemctl_worker(state, start, show):
    """Тело фонового потока: снимок состояния службы и/или попытка start.

    state передаётся явно: поток может пережить тест, подменивший _autostart.
    Снимок — до start, иначе ActiveState=failed сменится на activating и
    причина сбоя пропадёт из текста.
    """
    if show:
        try:
            summary = _service_summary()
        except Exception as exc:  # без сводки текст всё равно нужен
            summary = "непредвиденная ошибка: %r" % (exc,)
        with _autostart_lock:
            state["service"] = summary
    if start:
        try:
            result = _start_service()
        except Exception as exc:  # без результата каждый вызов врал бы «запускается»
            result = ("failed", "непредвиденная ошибка: %r" % (exc,))
        with _autostart_lock:
            state["result"] = result
        _log("автозапуск %s: %s" % (SERVICE_UNIT, result[1]))


def _start_allowed(state, now):
    """Можно ли сейчас просить systemd о start. Звать под _autostart_lock."""
    if state["attempt_at"] is None:
        return True
    last = state["result"]
    interval = MASKED_RETRY_S if last and last[0] == "masked" else AUTOSTART_RETRY_S
    return now - state["attempt_at"] >= interval


def _request_systemd(wait=0.0, start=True, show=False):
    """Попросить systemd поднять демон и/или сообщить его состояние.

    systemctl зовётся в фоновом потоке: вызов инструмента ждёт его не дольше
    wait секунд и отвечает агенту в срок, даже если менеджер завис. start — не
    чаще раза в AUTOSTART_RETRY_S (после «masked» — в MASKED_RETRY_S); пока
    прежний поток systemctl жив, новый не запускается.

    Возвращает (result, service): (state, note) последнего start или None,
    сводку `systemctl --user show` или None.
    """
    state = _autostart
    now = time.monotonic()
    with _autostart_lock:
        thread = state["thread"]
        if thread is None or not thread.is_alive():
            start = start and _start_allowed(state, now)
            if start or show:
                if start:
                    state["attempt_at"] = now
                    state["result"] = None
                if show:
                    state["service"] = None
                thread = threading.Thread(
                    target=_systemctl_worker,
                    args=(state, start, show),
                    name="localrag-systemctl",
                )
                thread.daemon = True
                state["thread"] = thread
                thread.start()
    if wait and thread is not None:
        thread.join(wait)
    with _autostart_lock:
        return state["result"], state["service"]


def _probe_and_autostart():
    """Проба на initialize: демон не слушает — просим systemd поднять его сразу.

    Агент подключается задолго до первого поиска, и прогрев обычно успевает
    закончиться. Недоступность — только сбой соединения: HTTP-ошибка или
    таймаут чтения значат, что демон жив. Живой pid у выбранного порт-файла —
    демон греет модель, второй экземпляр на том же индексе не заказываем.
    """
    routes, source, _ = _resolve_routes()
    if source == "env":
        return
    failure = None
    for url, _, _ in _attempt_order(routes):
        try:
            check_listener(url)
        except ForeignListener as exc:
            # Чужой ответ не значит, что наш демон жив: идём дальше к автозапуску.
            if failure is None:
                failure = (url, str(exc))
            continue
        req = urllib.request.Request(
            url + "/api/v1/health", headers={"Accept": "application/json"}
        )
        try:
            with _OPENER.open(req, timeout=PROBE_TIMEOUT_S) as resp:
                resp.read()
        except urllib.error.HTTPError:
            pass
        except urllib.error.URLError as exc:
            if failure is None:
                failure = (url, exc.reason)
            if _is_refused(exc):
                continue
            break
        except Exception:  # фоновый поток: трейсбек в stderr агенту ничем не поможет
            return
        _note_reachable()
        return
    _log("проба %s/api/v1/health: %s" % failure)
    _note_refused()
    if source == "port-file" and routes[0][2]:
        return
    _request_systemd()


def _start_probe():
    thread = threading.Thread(target=_probe_and_autostart, name="localrag-probe")
    thread.daemon = True
    thread.start()


def _stuck_text(down_for, service):
    parts = ["соединение отвергается уже %d с" % down_for]
    parts.append("служба: %s" % (service or "состояние ещё не получено"))
    return STUCK_TEXT % "; ".join(parts)


def _unreachable(base, source, port_file, checked, reason):
    """Ошибка для агента, когда до сайдкара не достучаться.

    Отказ на 127.0.0.1 мгновенный, и до таймаута агента мост успевает выяснить
    причину (прогрев, маска, нет юнита). Технический текст уходит в
    «Подробности» — по нему разбирают сбой.
    """
    detail = (
        "сайдкар LocalRAG недоступен по %s (%s). Проверьте, что он запущен "
        "и что %s/api/v1/health отвечает OK." % (base, reason, base)
    )
    if source == "port-file":
        detail += " Порт взят из %s." % port_file
    elif source == "fallback":
        detail += (
            " Файла с фактическим портом демона (%s) нет, а LQRAG_URL не "
            "задан — вероятно, сервис LocalRAG не запущен. Для поставки .deb: "
            "systemctl --user status lib-local-rag.service "
            "(при необходимости systemctl --user start)." % ", ".join(checked)
        )
    if source == "env":
        # Адрес задан явно — демон по нему не наша служба (тест, ручной запуск),
        # поднимать её бессмысленно, а «прогревается» было бы догадкой.
        return TransportError(detail)
    down_for = _note_refused()
    stuck = down_for >= STUCK_AFTER_S
    if source == "port-file" and _pid_alive(port_file):
        # Процесс жив, а соединение отвергнуто — демон греет модель: uvicorn
        # слушает только после прогрева. start не зовём: второй экземпляр на
        # том же индексе хуже ожидания.
        if not stuck:
            return TransportError("%s Подробности: %s" % (WARMING_TEXT, detail))
        _, service = _request_systemd(AUTOSTART_WAIT_S, start=False, show=True)
        return TransportError(
            "%s Подробности: %s" % (_stuck_text(down_for, service), detail)
        )
    # Живого демона нет: pid мёртв, pid-файла нет или порт-файла нет вовсе.
    result, service = _request_systemd(AUTOSTART_WAIT_S, start=True, show=stuck)
    state, note = result if result is not None else (None, "systemctl ещё не ответил")
    if state == "masked":
        return TransportError("%s Подробности: %s" % (SERVICE_MASKED_TEXT, note))
    if state == "not-found":
        return TransportError("%s Подробности: %s" % (SERVICE_MISSING_TEXT, note))
    if stuck:
        return TransportError(
            "%s Автозапуск: %s. Подробности: %s"
            % (_stuck_text(down_for, service), note, detail)
        )
    # systemd принял задание (или ещё думает) — служба как раз поднимается.
    if state in (None, "started"):
        return TransportError("%s Подробности: %s" % (WARMING_TEXT, detail))
    # Поднять службу не вышло (отказ, автозапуск выключен, нет systemctl):
    # «запускается» было бы неправдой.
    return TransportError("%s Автозапуск: %s." % (detail, note))


def _api_error(exc):
    """ApiError из HTTPError сайдкара: detail из JSON-тела, если оно есть."""
    raw = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    detail = raw
    try:
        detail = json.loads(raw).get("detail", raw)
    except Exception:
        pass
    # Серверный detail называет состояние, а агенту нужен рецепт починки.
    if exc.code == 401:
        return ApiError(401, UNAUTHORIZED_TEXT)
    return ApiError(exc.code, detail)


def _http_json(method, path, payload=None):
    """Запрос к сайдкару. Возвращает разобранный JSON ({} на пустое тело)."""
    routes, source, checked = _resolve_routes()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    refused = None
    body = None
    for base, port_file, _ in _attempt_order(routes):
        try:
            auth = _route_auth(source, base, port_file)
        except ForeignListener as exc:
            # Запрос не ушёл, как и при отказе соединения: пробуем другой адрес.
            if refused is None:
                refused = (base, port_file, str(exc))
            continue
        req = urllib.request.Request(
            base + path, data=data, headers=dict(headers, **auth), method=method
        )
        try:
            with _OPENER.open(req, timeout=LQRAG_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            _note_reachable()
            raise _api_error(exc) from None
        except urllib.error.URLError as exc:
            # Сбой соединения (отказ или таймаут подключения). Таймаут чтения
            # urllib в URLError не заворачивает — он попадёт в OSError ниже.
            if _is_refused(exc):
                # Устаревший порт-файл мог оказаться первым, а живой демон —
                # в другом доме.
                if refused is None:
                    refused = (base, port_file, exc.reason)
                continue
            raise _unreachable(base, source, port_file, checked, exc.reason) from None
        except OSError as exc:
            # Соединение было, ответа нет — демон жив, но не уложился.
            _note_reachable()
            error = ReplyTimeout if isinstance(exc, socket.timeout) else TransportError
            raise error("сбой связи с сайдкаром %s: %s" % (base, exc)) from None
        break
    else:
        base, port_file, reason = refused
        raise _unreachable(base, source, port_file, checked, reason)
    _note_reachable()
    # POST /config отвечает пустым телом — это успех, а не поломка.
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError as exc:
        raise TransportError(
            "сайдкар ответил не-JSON на %s %s: %s" % (method, path, exc)
        ) from None


def _reject_unknown(args, allowed):
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise InvalidParams(
            "неизвестные параметры: %s. Допустимые: %s"
            % (", ".join(extra), ", ".join(sorted(allowed)) or "нет")
        )


def _require_str(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(
            "параметр '%s' обязателен и должен быть непустой строкой" % name
        )
    return value.strip()


def _optional_int(args, name, default, minimum, maximum):
    if args.get(name) is None:
        return default
    value = args[name]
    # bool — подкласс int, а limit=true осмысленным значением не является.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParams("параметр '%s' должен быть целым числом" % name)
    if not minimum <= value <= maximum:
        raise InvalidParams(
            "параметр '%s'=%s вне допустимого диапазона %s..%s"
            % (name, value, minimum, maximum)
        )
    return value


def _optional_enum(args, name, default, allowed):
    if args.get(name) is None:
        return default
    value = args[name]
    if isinstance(value, str):
        value = value.strip().lower()  # «Hybrid» = «hybrid»: регистр не важен
    if value not in allowed:
        raise InvalidParams(
            "параметр '%s'=%r недопустим. Возможные значения: %s"
            % (name, value, ", ".join(allowed))
        )
    return value


_EXT_RE = re.compile(r"[0-9a-z]+")
# ASCII: strptime в _optional_date, в отличие от date.fromisoformat, принимает
# и не-ASCII цифры («２０２４»), отсечь их может только шаблон.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _optional_text(args, name):
    """Необязательная непустая строка; None — параметра нет."""
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams("параметр '%s' должен быть непустой строкой" % name)
    return value.strip()


def _optional_ext(args):
    """ext: «docx» или [«docx», «.PDF»] → [«docx», «pdf»]."""
    value = args.get("ext")
    if value is None:
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        raise InvalidParams(
            "параметр 'ext' должен быть строкой или непустым списком строк"
        )
    exts = []
    for item in items:
        ext = item.strip().lstrip(".").lower() if isinstance(item, str) else None
        # «docx,pdf» одной строкой — не расширение: молча искать по нему
        # значило бы вернуть пустую выдачу вместо ошибки вызова.
        if not ext or not _EXT_RE.fullmatch(ext):
            raise InvalidParams(
                "недопустимое расширение %r в 'ext': нужны буквы и цифры без "
                "точки, несколько расширений — списком" % (item,)
            )
        if ext not in exts:
            exts.append(ext)
    return exts


def _optional_folder(args):
    folder = _optional_text(args, "folder")
    if folder is None:
        return None
    folder = os.path.expanduser(folder)
    # Относительный путь демон считал бы от своего каталога, а не от того,
    # что имел в виду агент.
    if not os.path.isabs(folder):
        raise InvalidParams(
            "параметр 'folder' должен быть абсолютным путём, получено %r" % folder
        )
    return os.path.normpath(folder)


def _optional_date(args, name):
    value = _optional_text(args, name)
    if value is None:
        return None
    try:
        if not _DATE_RE.fullmatch(value):
            raise ValueError
        # strptime, а не date.fromisoformat (он с 3.7): нижней границы
        # python3 в debian/control нет.
        day = datetime.datetime.strptime(value, "%Y-%m-%d").date()
        # Диапазон демона (SearchFilters в src/server/model.py): за ним
        # timestamp индекса переполняется.
        if not 1900 <= day.year <= 2200:
            raise ValueError
        return day.isoformat()
    except ValueError:
        raise InvalidParams(
            "параметр '%s'=%r должен быть датой ГГГГ-ММ-ДД между 1900 и 2200 годами"
            % (name, value)
        ) from None


def _search_filters(args):
    """Фильтры local_search → поле filters запроса /search (пусто — None).

    Имена параметров — как их скажет агент («после», «до»), имена полей —
    как в SearchFilters демона (src/server/model.py). Обе даты включительно.
    """
    after = _optional_date(args, "modified_after")
    before = _optional_date(args, "modified_before")
    if after and before and after > before:
        raise InvalidParams(
            "modified_after (%s) позже modified_before (%s)" % (after, before)
        )
    filters = {
        "ext": _optional_ext(args),
        "folder": _optional_folder(args),
        "modified_from": after,
        "modified_to": before,
        "author": _optional_text(args, "author"),
    }
    return {k: v for k, v in filters.items() if v is not None} or None


def _filters_line(filters):
    """«Фильтры: ext=docx, pdf; автор: Иванов» — чем сужена выдача."""
    labels = (
        ("ext", "ext="),
        ("folder", "папка: "),
        ("modified_from", "изменён с "),
        ("modified_to", "изменён по "),
        ("author", "автор: "),
    )
    parts = []
    for key, label in labels:
        value = filters.get(key)
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            parts.append(label + value)
    return "Фильтры: " + "; ".join(parts)


def _safe_status():
    """Статус индексации, но без права уронить вызов, ради которого спрошен."""
    try:
        return _http_json("GET", "/api/v1/status")
    except ToolError:
        return None


def _inactive(status):
    """Индексация выключена (/deactivate, /reset, autostart_indexing: false).

    is_indexing=false бывает и в простое, отличает только поле active. У
    демонов без этого поля индексация считается включённой.
    """
    return isinstance(status, dict) and status.get("active") is False


# Инструмента для /activate у моста нет: агенту называем ручку, чтобы он
# передал её пользователю.
INACTIVE_TEXT = (
    "Индексация выключена (включается POST /api/v1/activate): новые и "
    "изменённые файлы не индексируются, индекс может быть пуст или устарел."
)
SAVED_INACTIVE_TEXT = (
    "Индексация выключена: список сохранён, обход начнётся после её включения "
    "(POST /api/v1/activate)."
)


def _format_score(value):
    return ", score=%.3f" % value if isinstance(value, (int, float)) else ""


# Единица «страницы» фрагмента по формату: у pptx это слайд, у таблиц — лист.
_PAGE_UNIT = {".pptx": "слайд", ".xlsx": "лист", ".xls": "лист"}
# Поля места фрагмента, которые демон отдаёт в /search (src/server/model.py).
_CHUNK_PLACE = (
    "kind",
    "page_start",
    "page_end",
    "line_start",
    "line_end",
    "section",
    "table_id",
    "row_start",
    "row_end",
)


def _span(start, end):
    return "%s" % start if end in (None, start) else "%s–%s" % (start, end)


def _chunk_place(chunk, ext):
    """«стр. 3–4 · Глава 2 › 2.1» — где в документе фрагмент; пусто у старого демона."""
    parts = []
    if isinstance(chunk.get("page_start"), int):
        unit = _PAGE_UNIT.get(ext, "стр.")
        parts.append(
            "%s %s" % (unit, _span(chunk["page_start"], chunk.get("page_end")))
        )
    if isinstance(chunk.get("line_start"), int):
        parts.append("строки %s" % _span(chunk["line_start"], chunk.get("line_end")))
    if chunk.get("section"):
        parts.append(str(chunk["section"]))
    if chunk.get("kind") == "ocr":
        # Скан: текст распознан, в цифрах и редких словах возможны ошибки.
        parts.append("распознано OCR")
    return " · ".join(parts)


def _document_line(doc):
    """Строка о документе: название, автор, редактор, дата изменения, объём."""
    if not isinstance(doc, dict):
        return ""
    parts = []
    if doc.get("title"):
        parts.append("«%s»" % doc["title"])
    if doc.get("author"):
        parts.append("автор: %s" % doc["author"])
    editor = doc.get("last_modified_by")
    if editor and editor != doc.get("author"):
        parts.append("изменил: %s" % editor)
    changed = doc.get("doc_modified") or doc.get("date_modified")
    if changed:
        parts.append("изменён: %s" % str(changed)[:10])
    if isinstance(doc.get("page_count"), int):
        parts.append("объём: %s" % doc["page_count"])
    return "; ".join(parts)


_SEARCH_FILTER_ARGS = ("ext", "folder", "modified_after", "modified_before", "author")

# Чем документ совпал, кроме фрагментов (поле match демона, src/server/model.py).
# Без отметки документ, найденный одной карточкой, приходит без фрагментов, и
# агент не понимает, почему он в выдаче.
_MATCH_LABELS = (
    ("name_code", "код из имени файла"),
    # Карточка — имя файла и все свойства, не только название: с меткой «по
    # названию» документ, найденный по автору, агент пересказал бы неверно.
    ("card", "слова запроса есть в имени или свойствах файла (название, автор, тема…)"),
)


def _clip(text, size):
    """Текст не длиннее size символов; обрезанный кончается «…»."""
    if len(text) <= size:
        return text
    return text[: max(size - 1, 0)].rstrip() + "…"


def _share(wants, budget):
    """Поделить budget поровну с переливом: кому нужно меньше доли — берёт своё,
    остаток делят остальные."""
    sizes = [0] * len(wants)
    left = budget
    order = sorted(range(len(wants)), key=wants.__getitem__)
    for pos, i in enumerate(order):
        sizes[i] = min(wants[i], left // (len(order) - pos))
        left -= sizes[i]
    return sizes


def _first_fragment_sizes(wants, overheads, budget):
    """Сколько символов текста дать первому фрагменту каждого документа.

    wants — длина полного фрагмента, overheads — строка вокруг него (отступ,
    место в документе, перевод строки). Сначала пробуем дать фрагмент всем,
    укорачивая поровну. Если на каждого выходит меньше MIN_CHUNK_CHARS,
    фрагменты получают только верхние документы — сколько влезет без
    обрывков в пару слов; заголовок с путём остаётся у всех.
    """
    for count in range(len(wants), 0, -1):
        sizes = _share(wants[:count], budget - sum(overheads[:count]))
        if all(size >= min(want, MIN_CHUNK_CHARS) for size, want in zip(sizes, wants)):
            return sizes + [0] * (len(wants) - count)
    return [0] * len(wants)


def _search_entry(item, number):
    """Документ выдачи до раскладки: обязательные строки, фрагменты, structured."""
    path = item.get("absolute_path") or item.get("id") or "?"
    item_type = item.get("type", "file")
    title = item.get("title") or path
    lines = [
        "%d. %s (тип=%s, путь=%s%s)"
        % (number, title, item_type, path, _format_score(item.get("score")))
    ]
    match = [m for m in (item.get("match") or []) if isinstance(m, str)]
    marks = [label for key, label in _MATCH_LABELS if key in match]
    if marks:
        lines.append("   " + "; ".join(marks))
    about = _document_line(item.get("document"))
    if about:
        lines.append("   " + _clip(about, MAX_ABOUT_CHARS))
    copies = [p for p in (item.get("duplicates") or []) if isinstance(p, str)]
    if copies:
        listed = "; ".join(copies[:MAX_COPIES_SHOWN])
        if len(copies) > MAX_COPIES_SHOWN:
            listed += " и ещё %d" % (len(copies) - MAX_COPIES_SHOWN)
        lines.append("   копии того же файла: " + listed)

    ext = os.path.splitext(path)[1].lower()
    fragments = []
    for chunk in (item.get("chunks") or [])[:MAX_CHUNKS_PER_DOC]:
        content = (chunk.get("content") or "").strip()
        if not content:
            continue
        place = _chunk_place(chunk, ext)
        fragments.append(
            {
                "prefix": "   · " + ("[%s] " % place if place else ""),
                "content": _clip(content, MAX_CHUNK_CHARS),
                "chunk": chunk,
            }
        )

    result = {
        "path": path,
        "title": title,
        "type": item_type,
        "score": item.get("score"),
        "match": match,
        "chunks": [],
    }
    if isinstance(item.get("document"), dict):
        result["document"] = {
            k: v for k, v in item["document"].items() if v is not None
        }
    if copies:
        result["duplicates"] = copies
    return {"lines": lines, "fragments": fragments, "result": result}


def _add_fragment(entry, fragment, size):
    """Дописать фрагмент документа, укоротив текст до size; вернуть длину строки."""
    text = _clip(fragment["content"], size)
    line = fragment["prefix"] + text
    entry["lines"].append(line)
    chunk = fragment["chunk"]
    record = {"text": text, "score": chunk.get("score")}
    record.update((k, chunk[k]) for k in _CHUNK_PLACE if chunk.get(k) is not None)
    entry["result"]["chunks"].append(record)
    return len(line) + 1


def _layout_search(results):
    """Разложить выдачу в бюджет MAX_OUTPUT_CHARS.

    Порядок важнее полноты фрагментов: сначала каждому документу —
    заголовок с путём, отметки и строка о документе, затем по первому
    фрагменту (при нехватке места — укороченному), и только потом добавочные
    фрагменты верхним документам — иначе длинные фрагменты первых документов
    вытесняют из ответа остальные найденные файлы. Документ отбрасывается,
    только если не влезает даже его заголовок.
    """
    entries = []
    used = 0
    for item in results:
        entry = _search_entry(item, len(entries) + 1)
        cost = sum(len(line) + 1 for line in entry["lines"])
        if used + cost > MAX_OUTPUT_CHARS and entries:
            break
        entries.append(entry)
        used += cost

    firsts = [entry for entry in entries if entry["fragments"]]
    wants = [len(entry["fragments"][0]["content"]) for entry in firsts]
    overheads = [len(entry["fragments"][0]["prefix"]) + 1 for entry in firsts]
    sizes = _first_fragment_sizes(wants, overheads, MAX_OUTPUT_CHARS - used)
    for entry, size in zip(firsts, sizes):
        if size > 0:
            used += _add_fragment(entry, entry["fragments"][0], size)

    for entry in entries:
        if len(entry["result"]["chunks"]) != 1:
            continue  # первый фрагмент не влез — добавочным тем более не место
        for fragment in entry["fragments"][1:]:
            want = len(fragment["content"])
            room = MAX_OUTPUT_CHARS - used - len(fragment["prefix"]) - 1
            if room < min(want, MIN_CHUNK_CHARS):
                break
            used += _add_fragment(entry, fragment, min(want, room))
    return entries


def _tool_search(args):
    _reject_unknown(args, ("query", "limit", "query_type") + _SEARCH_FILTER_ARGS)
    query = _require_str(args, "query")
    limit = _optional_int(args, "limit", 10, 1, 50)
    query_type = _optional_enum(
        args, "query_type", "hybrid", ("hybrid", "vector", "text")
    )
    filters = _search_filters(args)

    payload = {
        "query": query,
        "limit": limit,
        "query_type": query_type,
    }
    # Без фильтров поля нет вовсе: старые демоны и контрактные тесты ждут тело
    # без него.
    if filters:
        payload["filters"] = filters
    data = _http_json("POST", "/api/v1/search", payload)
    results = data.get("results") or []
    # Не поле total демона: у старых демонов оно считает строки, а не документы.
    total = len(results)

    if not results:
        # Пустая выдача во время первой индексации неотличима от «такого нет»,
        # и агент уверенно отвечает пользователю «не найдено». Различаем явно.
        status = _safe_status()
        indexing = bool(status and status.get("is_indexing"))
        if indexing:
            text = (
                "Ничего не найдено, но индексация ещё идёт: в очереди %s "
                "элемент(ов), фаза «%s». Это НЕ значит, что документа нет — "
                "повторите поиск позже или проверьте local_index_status."
                % (status.get("files_to_index", "?"), status.get("type", "?"))
            )
        elif status is None:
            # Без статуса «индекс актуален» утверждать нельзя — это то же
            # ложное «не найдено».
            text = (
                "Ничего не найдено. Состояние индексации проверить не удалось — "
                "возможно, индекс ещё строится."
            )
        elif _inactive(status):
            text = "Ничего не найдено. %s Это НЕ значит, что документа нет." % (
                INACTIVE_TEXT
            )
        else:
            text = "Ничего не найдено. Индексация не идёт, индекс актуален."
        if filters:
            # Пусто может быть из-за фильтров, а не из-за запроса: агенту
            # стоит знать, что ослабить.
            text += "\n%s — без них выдача может быть шире." % _filters_line(filters)
        return text, {"total": 0, "shown": 0, "indexing": indexing, "results": []}

    entries = _layout_search(results)
    lines = [line for entry in entries for line in entry["lines"]]
    structured = [entry["result"] for entry in entries]

    shown = len(structured)
    head = "Найдено документов: %s" % total
    if shown < total:
        head += " (показано %s — остальное не поместилось в ответ)" % shown
    # Статус и при непустой выдаче: во время индексации агент иначе счёл бы
    # список полным. Это один GET по localhost, счётчики демон берёт из памяти.
    status = _safe_status()
    indexing = bool(status and status.get("is_indexing"))
    if filters:
        head += "\n" + _filters_line(filters)
    text = head + "\n" + "\n".join(lines) + "\n"
    if indexing:
        text += (
            "ВНИМАНИЕ: индексация ещё идёт (в очереди %s, фаза «%s») — "
            "список может быть неполным, повторите поиск позже.\n"
            % (status.get("files_to_index", "?"), status.get("type", "?"))
        )
    elif _inactive(status):
        text += (
            "ВНИМАНИЕ: индексация выключена (включается POST /api/v1/activate) — "
            "новые и изменённые файлы не индексируются, список может быть неполным.\n"
        )
    return text, {
        "total": total,
        "shown": shown,
        "indexing": indexing,
        "results": structured,
    }


def _tool_index_status(args):
    _reject_unknown(args, ())
    status = _http_json("GET", "/api/v1/status")
    is_indexing = bool(status.get("is_indexing"))
    inactive = _inactive(status)
    if inactive:
        state = "выключена"
    else:
        state = "идёт" if is_indexing else "не идёт"
    text = (
        "Индексация: %s\nФаза: %s\nВ очереди файлов: %s\n"
        "Собрано на этапе preindex: %s"
        % (
            state,
            status.get("type", "?"),
            status.get("files_to_index", "?"),
            status.get("total_files", "?"),
        )
    )
    if inactive:
        text += "\n%s Пустая выдача поиска не означает отсутствия документа." % (
            INACTIVE_TEXT
        )
    elif is_indexing:
        text += "\nПустая выдача поиска сейчас не означает отсутствия документа."
    return text, status


# .env, .ini, .conf, .properties, .toml исключены намеренно: в таких файлах
# лежат токены, а индекс может покрывать весь домашний каталог.
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".log",
    ".json",
    ".yml",
    ".yaml",
    ".xml",
    ".html",
    ".htm",
    ".py",
    ".sh",
    ".sql",
    ".java",
    ".kt",
    ".ipynb",
}
# Легаси .doc/.xls читает только демон (конвертер Р7); без него — отказ с
# подсказкой.
OFFICE_EXTENSIONS = {".doc", ".docx", ".pdf", ".pptx", ".xls", ".xlsx"}

_FOLDERS_TTL = 5.0
_folders_cache = {"ts": 0.0, "roots": None}


def _indexed_roots():
    """Корни, внутри которых чтение разрешено, — из GET /config.

    Кэш на несколько секунд: подряд идущие чтения не должны платить за
    HTTP-запрос каждый раз, но и залипать на устаревшем списке не должны.
    """
    now = time.monotonic()
    if (
        _folders_cache["roots"] is not None
        and now - _folders_cache["ts"] < _FOLDERS_TTL
    ):
        return _folders_cache["roots"]
    data = _http_json("GET", "/api/v1/config")
    roots = []
    for folder in data.get("folders") or []:
        if not isinstance(folder, str):
            continue
        roots.append(os.path.abspath(os.path.expanduser(folder)))
    _folders_cache["ts"] = now
    _folders_cache["roots"] = roots
    return roots


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# Имена, которые обход индексатора пропускает, — копия exclude_pattern из
# src/indexer/exclude.py: мост на stdlib и импортировать src/ не может.
# Расхождение ловит tests/test_read_sandbox.py.
_EXCLUDED_NAME = re.compile(
    r"^(?:\..+|node_modules|__pycache__|venv|env|site-packages|snap|dist|build"
    r"|Thumbs\.db|~.*)$",
    re.IGNORECASE,
)


def _sandbox_refusal(path, roots):
    """Почему файл не отдаётся: None — отдаётся, иначе "outside" или "excluded".

    Правило обхода индексатора: файл внутри корня, и ни одно имя ниже корня не
    скрыто и не служебное. path сверяется как есть (ссылки не раскрыты — так
    индексатор хранит файлы за ссылками) и после realpath — тот же файл по
    настоящему пути. Демон проверяет так же: read_refusal в src/indexer/exclude.py.
    """
    real = os.path.realpath(path)
    inside = False
    for root in roots:
        for candidate, base in ((path, root), (real, os.path.realpath(root))):
            if not _inside(candidate, base):
                continue
            inside = True
            below = candidate[len(base.rstrip(os.sep)) :].split(os.sep)
            if not any(_EXCLUDED_NAME.match(part) for part in below):
                return None
    return "excluded" if inside else "outside"


def _read_part(bundle, name, used):
    """Часть пакета целиком — только в пределах распаковки (zip-бомба).

    Размер — из центрального каталога: zipfile больше заявленного не отдаёт.
    used — [сколько байт этого файла уже распаковано].
    """
    info = bundle.getinfo(name)
    size = info.file_size
    # Большая часть сама по себе не бомба (общие строки выгрузки, длинный docx):
    # отказ — при сжатии больше MAX_RATIO:1 или сверх общего потолка.
    bomb = size > MAX_XML_BYTES and size > MAX_RATIO * max(info.compress_size, 1)
    if bomb or used[0] + size > MAX_UNPACKED_BYTES:
        raise ValueError("%s: %d Б после распаковки — больше лимита" % (name, size))
    used[0] += size
    return bundle.read(name)


def _extract_docx(path):
    # Без docx2txt: на системном python3 поставки её нет, а тесты в venv с ней
    # не поймали бы ImportError.
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as bundle:
        root = ET.fromstring(_read_part(bundle, "word/document.xml", [0]))
    # w:p бывают вложенными (текстбоксы): paragraph.iter() внешнего уже
    # захватывает узлы внутреннего, и без seen текстбокс попал бы в текст дважды.
    seen = set()
    paragraphs = []
    for paragraph in root.iter(W + "p"):
        runs = []
        for node in paragraph.iter():
            if node.tag == W + "t":
                if id(node) not in seen:
                    seen.add(id(node))
                    runs.append(node.text or "")
            elif node.tag == W + "tab":
                if id(node) not in seen:
                    seen.add(id(node))
                    runs.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                if id(node) not in seen:
                    seen.add(id(node))
                    runs.append("\n")
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


def _extract_pdf(path):
    import pymupdf

    parts = []
    with pymupdf.open(path) as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)


def _extract_pptx(path):
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    slides = []
    used = [0]
    with zipfile.ZipFile(path) as bundle:
        parts = []
        for name in bundle.namelist():
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
            if match:
                parts.append((int(match.group(1)), name))
        # По номеру, а не по имени файла: лексикографически slide10 идёт
        # раньше slide2, и порядок слайдов переворачивался бы с десятого.
        for number, name in sorted(parts):
            root = ET.fromstring(_read_part(bundle, name, used))
            lines = []
            for paragraph in root.iter(A + "p"):
                line = "".join(t.text or "" for t in paragraph.iter(A + "t"))
                if line.strip():
                    lines.append(line)
            if lines:
                slides.append("--- слайд %d ---\n%s" % (number, "\n".join(lines)))
    return "\n\n".join(slides)


def _extract_xlsx(path):
    # Значения берутся те же, что видит Excel: кэш вычисленных формул лежит
    # в самих листах, пересчитывать нечего.
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    RD = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    blocks = []
    used = [0]
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            items = ET.fromstring(_read_part(bundle, "xl/sharedStrings.xml", used))
            for item in items.iter(NS + "si"):
                shared.append("".join(t.text or "" for t in item.iter(NS + "t")))
        titles = {}
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            targets = {}
            rels = ET.fromstring(_read_part(bundle, "xl/_rels/workbook.xml.rels", used))
            for rel in rels.iter(R + "Relationship"):
                targets[rel.get("Id")] = "xl/" + (rel.get("Target") or "").lstrip("/")
            workbook = ET.fromstring(_read_part(bundle, "xl/workbook.xml", used))
            for sheet in workbook.iter(NS + "sheet"):
                titles[targets.get(sheet.get(RD + "id"), "")] = sheet.get("name")
        parts = []
        for name in names:
            match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", name)
            if match:
                parts.append((int(match.group(1)), name))
        for number, name in sorted(parts):
            rows = []
            sheet = ET.fromstring(_read_part(bundle, name, used))
            for row in sheet.iter(NS + "row"):
                cells = []
                for cell in row.findall(NS + "c"):
                    kind = cell.get("t")
                    value = cell.find(NS + "v")
                    inline = cell.find(NS + "is")
                    if kind == "s" and value is not None:
                        text = shared[int(value.text)]
                    elif kind == "inlineStr" and inline is not None:
                        text = "".join(t.text or "" for t in inline.iter(NS + "t"))
                    elif value is not None:
                        text = value.text or ""
                    else:
                        continue
                    cells.append(text)
                if cells:
                    rows.append("\t".join(cells))
                # Без границы книга на сотни тысяч строк уезжала бы в контекст;
                # лимит свой, не индексатора (см. MAX_XLSX_ROWS).
                if len(rows) >= MAX_XLSX_ROWS:
                    rows.append("… лист обрезан на %d строках" % MAX_XLSX_ROWS)
                    break
            if rows:
                title = titles.get(name) or "sheet%d" % number
                blocks.append("--- лист %s ---\n%s" % (title, "\n".join(rows)))
    return "\n\n".join(blocks)


# (функция, пакет): пакет нужен только pdf, остальное — zip с XML.
_OFFICE_EXTRACTORS = {
    ".docx": (_extract_docx, None),
    ".pdf": (_extract_pdf, "pymupdf"),
    ".pptx": (_extract_pptx, None),
    ".xlsx": (_extract_xlsx, None),
}

_SIDECAR_ONLY_EXTENSIONS = {".doc", ".xls"}
_SIDECAR_READ_EXTENSIONS = set(_OFFICE_EXTRACTORS) | _SIDECAR_ONLY_EXTENSIONS


def _read_text(path, ext):
    if ext in _OFFICE_EXTRACTORS:
        extractor, package = _OFFICE_EXTRACTORS[ext]
        try:
            return extractor(path)
        except ImportError:
            raise ToolError(
                "формат %s требует библиотеки %s, которой нет в окружении моста. "
                "Текстовые форматы читаются без дополнительных библиотек; "
                "фрагменты этого документа доступны через local_search" % (ext, package)
            ) from None
        except Exception as exc:
            raise ToolError("не удалось разобрать %s: %s" % (ext, exc)) from None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FILE_CHARS + 1)
    except OSError as exc:
        raise ToolError("не удалось прочитать файл: %s" % exc) from None


def _tool_read_file(args):
    _reject_unknown(args, ("absolute_path",))
    raw_path = _require_str(args, "absolute_path")
    # Путь, как его хранит индекс: ~ раскрыт, «..» снят, ссылки не раскрыты.
    # Он же уходит демону: свои HOME и cwd раскрыли бы raw_path иначе.
    path = os.path.abspath(os.path.expanduser(raw_path))

    # Без списка папок от сайдкара границы песочницы неизвестны — отказ лучше
    # доступа ко всей файловой системе.
    try:
        roots = _indexed_roots()
    except ToolError as exc:
        raise ToolError(
            "не удалось получить список индексируемых папок (%s), поэтому чтение "
            "запрещено: без него нельзя проверить, что файл внутри разрешённых "
            "каталогов" % exc
        ) from None
    if not roots:
        raise ToolError("у LocalRAG не настроено ни одной индексируемой папки")
    refusal = _sandbox_refusal(path, roots)
    if refusal == "outside":
        raise ToolError(
            "путь %s вне индексируемых папок, чтение запрещено. Разрешены: %s"
            % (path, ", ".join(roots))
        )
    if refusal == "excluded":
        raise ToolError(
            "скрытые файлы и содержимое скрытых и служебных каталогов не "
            "читаются: %s" % path
        )

    if not os.path.isfile(path):
        raise ToolError("файл не найден или не является обычным файлом: %s" % path)

    # Расширение — у самого файла: ссылка «заметки.txt» на «prod.env» не должна
    # проводить .env мимо списка.
    ext = os.path.splitext(os.path.realpath(path))[1].lower()
    if ext not in TEXT_EXTENSIONS and ext not in OFFICE_EXTENSIONS:
        raise ToolError(
            "расширение '%s' не поддерживается. Доступны: %s"
            % (ext or "(нет)", ", ".join(sorted(TEXT_EXTENSIONS | OFFICE_EXTENSIONS)))
        )

    if ext in _SIDECAR_READ_EXTENSIONS:
        # Сначала демон: pymupdf и конвертер Р7 есть только у него. Локальный
        # разбор — лишь когда демона нет или нет ручки (404 "Not Found" от
        # FastAPI); таймаут живого демона, 404 «файл не найден», 413 и 422 — его
        # вердикт по файлу, подменять его нельзя.
        try:
            data = _http_json("POST", "/api/v1/read_file", {"absolute_path": path})
            remote = data.get("text")
            if isinstance(remote, str):
                truncated = len(remote) > MAX_FILE_CHARS or bool(data.get("truncated"))
                content = remote[:MAX_FILE_CHARS]
                header = "Путь: %s\nРазмер файла: %s байт\n" % (
                    path,
                    os.path.getsize(path),
                )
                if truncated:
                    header += "(текст обрезан до %d символов)\n" % MAX_FILE_CHARS
                text = (
                    header
                    + "---- начало содержимого ----\n"
                    + content
                    + "\n---- конец содержимого ----"
                )
                return text, {
                    "path": path,
                    "truncated": truncated,
                    "chars": len(content),
                }
        except ReplyTimeout:
            raise
        except TransportError:
            pass
        except ApiError as exc:
            if exc.code != 404 or exc.detail != "Not Found":
                raise

    if ext in _SIDECAR_ONLY_EXTENSIONS:
        raise ToolError(
            "легаси-формат %s читает только демон LocalRAG, а он недоступен "
            "или старой версии — обновите lib-local-rag. Фрагменты документа "
            "доступны через local_search" % ext
        )

    if os.path.getsize(path) > MAX_LOCAL_READ_BYTES:
        raise ToolError("файл больше лимита чтения")
    content = _read_text(path, ext)
    truncated = len(content) > MAX_FILE_CHARS
    if truncated:
        content = content[:MAX_FILE_CHARS]
    header = "Путь: %s\nРазмер файла: %s байт\n" % (path, os.path.getsize(path))
    if truncated:
        header += "(текст обрезан до %d символов)\n" % MAX_FILE_CHARS
    text = (
        header
        + "---- начало содержимого ----\n"
        + content
        + "\n---- конец содержимого ----"
    )
    return text, {"path": path, "truncated": truncated, "chars": len(content)}


def _get_folders():
    return list(_http_json("GET", "/api/v1/config").get("folders") or [])


def _post_folders(folders):
    """Записать список папок.

    POST /config с непустым folders сам перезапускает индексацию (stop_watcher
    → stop_index → удаление снятых префиксов → start_reindex →
    restart_watcher), отдельная переиндексация не нужна.
    """
    _http_json("POST", "/api/v1/config", {"folders": folders})
    _folders_cache["roots"] = None


def _normalize(path):
    return os.path.abspath(os.path.expanduser(path))


def _covering_root(path, folders):
    """Папка из folders, внутри которой лежит существующий каталог path, или None.

    По realpath, а не по _normalize: папка-ссылка внутри корня ведёт в другое
    место, и при follow_symlinks: false корень её содержимое не покрывает.
    """
    if not os.path.isdir(path):
        return None
    real = os.path.realpath(path)
    for folder in folders:
        root = _normalize(folder)
        if _inside(real, os.path.realpath(root)):
            return root
    return None


def _tool_list_paths(args):
    _reject_unknown(args, ())
    folders = _get_folders()
    lines = ["Индексируемых папок: %d" % len(folders)]
    items = []
    for number, folder in enumerate(folders, 1):
        path = _normalize(folder)
        if os.path.isdir(path):
            mark, state = "✓", "ok"
        elif os.path.exists(path):
            mark, state = "⚠", "not_a_directory"
        else:
            mark, state = "✗", "missing"
        lines.append("  %d. %s %s" % (number, mark, path))
        items.append({"path": path, "state": state})
    return "\n".join(lines), {"folders": items}


def _tool_add_path(args):
    _reject_unknown(args, ("path",))
    path = _normalize(_require_str(args, "path"))
    folders = _get_folders()
    if any(_normalize(folder) == path for folder in folders):
        return "Папка уже индексируется: %s" % path, {"path": path, "added": False}
    # Вложенную папку не отвергаем: последнюю папку удалить нельзя, и без
    # отдельной записи список не сузить с корня до папки внутри него.
    root = _covering_root(path, folders)
    # Существование пути проверяет сам сайдкар (400, конфиг не меняется):
    # сохранённый несуществующий путь роняет наблюдение за файловой системой.
    _post_folders(folders + [path])
    text = "Папка добавлена: %s." % path
    if root is not None:
        text += (
            " Она внутри индексируемой папки %s и уже входила в индексацию в её "
            "составе." % root
        )
    if _inactive(_safe_status()):
        text += " " + SAVED_INACTIVE_TEXT
    else:
        text += (
            " Индексация перезапущена автоматически — следите за local_index_status."
        )
    return text, {"path": path, "added": True}


def _tool_remove_path(args):
    _reject_unknown(args, ("path",))
    path = _normalize(_require_str(args, "path"))
    folders = _get_folders()
    remaining = [folder for folder in folders if _normalize(folder) != path]
    if len(remaining) == len(folders):
        root = _covering_root(path, folders)
        if root is not None:
            raise ToolError(
                "папка %s не числится отдельно: она внутри индексируемой папки %s "
                "и входит в индексацию в её составе. Убрать её из индекса можно "
                "только вместе с %s" % (path, root, root)
            )
        raise ToolError("папка %s не числится среди индексируемых" % path)
    if not remaining:
        # Пустой список демон отвергает (400): Config.scan_paths понял бы его
        # как «настройки нет» и обходил бы пути из YAML, а не выключил бы
        # индексацию.
        raise ToolError(
            "нельзя удалить последнюю папку: пустой список демон не принимает. "
            "Сначала добавьте другую папку"
        )
    _post_folders(remaining)
    root = _covering_root(path, remaining)
    inactive = _inactive(_safe_status())
    if root is not None:
        # Демон снимет документы папки, но обход root вернёт их обратно.
        text = (
            "Папка убрана из списка: %s. Она внутри индексируемой папки %s, "
            "поэтому её документы снова попадут в индекс при обходе этой папки — "
            "исключить её отдельно нельзя." % (path, root)
        )
        if inactive:
            text += " " + SAVED_INACTIVE_TEXT
    elif inactive:
        text = "Папка удалена: %s. Её документы сняты с индекса. %s" % (
            path,
            SAVED_INACTIVE_TEXT,
        )
    else:
        text = (
            "Папка удалена: %s. Её документы сняты с индекса, индексация "
            "перезапущена." % path
        )
    return text, {"path": path, "removed": True}


def _tool_trigger_rescan(args):
    _reject_unknown(args, ())
    status = _http_json("GET", "/api/v1/status")
    if _inactive(status):
        # На выключенной индексации демон только сохранил бы тот же список.
        raise ToolError(
            "индексация выключена — переиндексация не запущена. Полный обход "
            "начнётся после её включения (POST /api/v1/activate)"
        )
    if status.get("is_indexing"):
        # POST /config во время индексации ронял демон.
        raise ToolError(
            "индексация уже идёт (фаза «%s», в очереди %s) — переиндексация не "
            "запущена. Дождитесь окончания и повторите"
            % (status.get("type", "?"), status.get("files_to_index", "?"))
        )
    folders = _get_folders()
    if not folders:
        raise ToolError("список индексируемых папок пуст — переиндексировать нечего")
    # Тот же список: folders_changed=True запускает полный переобход, а
    # снятых префиксов нет, поэтому уже собранный индекс не теряется.
    _post_folders(folders)
    return (
        "Переиндексация запущена для %d папок" % len(folders),
        {"folders": len(folders)},
    )


_SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "total": {
            "type": "integer",
            "description": "Сколько документов вернул поиск (не больше limit на источник)",
        },
        "shown": {"type": "integer", "description": "Сколько вошло в ответ"},
        "indexing": {
            "type": "boolean",
            "description": "Идёт ли индексация — при true пустая выдача не окончательна",
        },
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string"},
                    "score": {
                        "type": ["number", "null"],
                        "description": (
                            "Оценка поиска, между query_type и источниками не "
                            "сравнима. Порядок ответа: сначала документы с "
                            "match name_code, затем по оценке. null — документа "
                            "не было в выдаче поиска, его нашёл только код из "
                            "имени файла"
                        ),
                    },
                    "match": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Чем ещё документ совпал, кроме фрагментов: name_code — "
                            "код из запроса есть в имени файла (такие идут первыми), "
                            "card — слова запроса есть в имени файла или его "
                            "свойствах (название, автор, редактор, тема, ключевые "
                            "слова, описание); в query_type=vector не ставится"
                        ),
                    },
                    "chunks": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Фрагменты: text, score и, если демон знает, место — "
                            "page_start/page_end (страница, слайд или лист), "
                            "line_start/line_end, section, kind, table_id"
                        ),
                    },
                    "document": {
                        "type": "object",
                        "description": (
                            "Свойства документа, если демон их отдаёт: title, author, "
                            "last_modified_by, date_created, date_modified, page_count, "
                            "size, mime, sha256, source_uri"
                        ),
                    },
                },
                "required": ["path", "title", "type"],
            },
        },
    },
    "required": ["total", "shown", "indexing", "results"],
}

READ_TOOLS = [
    {
        "name": "local_search",
        "description": (
            "Семантический поиск по локальному индексу файлов пользователя: "
            "документы, презентации, таблицы, PDF, текстовые файлы на этом "
            "устройстве. Ищет по смыслу, дословное совпадение не требуется. "
            "Возвращает документы (limit — их число) с лучшими фрагментами "
            "текста и абсолютными путями — путь из absolute_path можно передать "
            "в local_read_file. Ищет и по имени и свойствам файла: код из имени "
            "(«найди файл ЦБ_21») поднимает этот файл первым. "
            "Если пользователь назвал формат, папку, период или автора "
            "(«docx от Иванова за 2024»), передайте их фильтрами ext, folder, "
            "modified_after/modified_before, author, а в query оставьте "
            "только смысл. "
            "Если ничего не нашлось, проверьте local_index_status: во время "
            "индексации пустая выдача не означает, что документа нет."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Запрос на естественном языке, по смыслу",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "Сколько документов вернуть",
                },
                "query_type": {
                    "type": "string",
                    "enum": ["hybrid", "vector", "text"],
                    "default": "hybrid",
                    "description": (
                        "hybrid — смысл плюс точные слова (по умолчанию); "
                        "vector — только смысловая близость; "
                        "text — только точные слова"
                    ),
                },
                "ext": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": (
                        'Фильтр по расширению файла, без точки: "docx" или '
                        '["docx", "pdf"]. Задавайте, если пользователь назвал '
                        "формат («docx», «презентации» — pptx, «таблицы» — xlsx, xls)"
                    ),
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "Фильтр по папке: абсолютный путь, ищется во всём её поддереве"
                    ),
                },
                "modified_after": {
                    "type": "string",
                    "format": "date",
                    "description": (
                        "Изменён не раньше этой даты, ГГГГ-ММ-ДД, включительно. "
                        "Дата — та же, что «изменён:» в ответе: из свойств "
                        "документа, а без них — файла на диске. «За 2024» — "
                        "modified_after 2024-01-01 и modified_before 2024-12-31"
                    ),
                },
                "modified_before": {
                    "type": "string",
                    "format": "date",
                    "description": (
                        "Изменён не позже этой даты, ГГГГ-ММ-ДД, включительно "
                        "(дата — как у modified_after)"
                    ),
                },
                "author": {
                    "type": "string",
                    "description": (
                        "Фильтр по автору или последнему редактору документа: "
                        "часть имени, регистр и ё/е не важны («Иванов»)"
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "outputSchema": _SEARCH_OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "local_index_status",
        "description": (
            "Состояние локального индекса: идёт ли индексация, сколько элементов "
            "в очереди, какая фаза. Вызывайте, когда local_search вернул пустую "
            "выдачу, чтобы отличить «в индексе такого нет» от «индекс ещё строится»."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "local_read_file",
        "description": (
            "Читает полный текст файла по абсолютному пути из результата "
            "local_search. Офисные форматы и pdf читаются парсерами демона "
            "LocalRAG (в его бинаре pymupdf и конвертер легаси .doc/.xls); "
            "если демон недоступен, docx/pptx/xlsx разбирает сам мост без "
            "библиотек, а pdf и легаси-форматы возвращают подсказку. "
            "Доступны только файлы внутри индексируемых папок; возвращает "
            "до 40 000 символов. Нужен, когда фрагментов из поиска не "
            "хватает и требуется документ целиком."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "absolute_path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Значение absolute_path из результата local_search",
                }
            },
            "required": ["absolute_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]

WRITE_TOOLS = [
    {
        "name": "local_list_paths",
        "description": "Список папок, по которым LocalRAG строит индекс, с отметкой доступности.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "local_add_path",
        "description": (
            "Добавляет папку в индексацию. Путь должен существовать; ~ раскрывается. "
            "Индексация перезапускается автоматически, отдельная переиндексация не нужна."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
    {
        "name": "local_remove_path",
        "description": (
            "Убирает папку из индексации и снимает её документы с индекса. "
            "Последнюю папку удалить нельзя."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
        },
    },
    {
        "name": "local_trigger_rescan",
        "description": (
            "Форсирует переобход всех индексируемых папок без потери индекса. "
            "Отказывает, если индексация уже идёт."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
]

_HANDLERS = {
    "local_search": _tool_search,
    "local_index_status": _tool_index_status,
    "local_read_file": _tool_read_file,
    "local_list_paths": _tool_list_paths,
    "local_add_path": _tool_add_path,
    "local_remove_path": _tool_remove_path,
    "local_trigger_rescan": _tool_trigger_rescan,
}
_WRITE_TOOL_NAMES = {tool["name"] for tool in WRITE_TOOLS}


def visible_tools():
    return READ_TOOLS + (WRITE_TOOLS if ALLOW_WRITE else [])


def _result(msg_id, payload):
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


# Коды ошибок JSON-RPC 2.0.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(msg_id, text, structured=None, is_error=False):
    payload = {"content": [{"type": "text", "text": text}], "isError": is_error}
    # structuredContent только на успехе: outputSchema описывает удачный
    # результат, ошибку по нему валидировать нечем.
    if structured is not None and not is_error:
        payload["structuredContent"] = structured
    return _result(msg_id, payload)


def _handle_tools_call(msg_id, params):
    name = params.get("name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(msg_id, INVALID_PARAMS, "поле 'arguments' должно быть объектом")

    # Метод tools/call есть, неверен его параметр name: -32602, как в примере
    # спецификации MCP (Tools → Error Handling). Не строка — тоже неизвестный.
    if not isinstance(name, str) or name not in _HANDLERS:
        return _error(msg_id, INVALID_PARAMS, "неизвестный инструмент: %s" % (name,))
    if name in _WRITE_TOOL_NAMES and not ALLOW_WRITE:
        return _error(
            msg_id,
            INVALID_PARAMS,
            "инструмент %s скрыт в этой установке (LQRAG_ALLOW_WRITE=0): "
            "управление индексацией из агента отключено" % name,
        )

    try:
        text, structured = _HANDLERS[name](args)
    except InvalidParams as exc:
        # Ошибка вызова, а не ошибка выполнения: агент должен увидеть её как
        # неверные параметры и исправить вызов.
        return _error(msg_id, INVALID_PARAMS, str(exc))
    except ToolError as exc:
        _log("%s: %s" % (name, exc))
        return _tool_result(msg_id, "Ошибка %s: %s" % (name, exc), is_error=True)
    except Exception as exc:  # неожиданное — мост всё равно не падает
        _log("%s: непредвиденная ошибка: %r" % (name, exc))
        return _tool_result(
            msg_id, "Непредвиденная ошибка %s: %s" % (name, exc), is_error=True
        )
    return _tool_result(msg_id, text, structured)


def _handle(msg):
    """Обработать одно сообщение. None означает «ответа быть не должно»."""
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    params = msg.get("params") or {}
    # Нотификация — сообщение без "id". Отвечать на неё нельзя: ответ с
    # id=null клиент вправе счесть нарушением протокола и оборвать сессию.
    is_notification = "id" not in msg
    msg_id = msg.get("id")

    if method is None:
        # Ответ на наш запрос: мы сервер, своих запросов не шлём.
        return None
    if is_notification:
        return None
    # JSON-RPC допускает params-массив, а эти методы читают его через .get():
    # иначе AttributeError ронял бы мост.
    if method in ("initialize", "tools/call") and not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "поле 'params' должно быть объектом")

    if method == "initialize":
        # Проба и автозапуск — в фоне: рукопожатие не ждёт ни сети, ни systemd.
        _start_probe()
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                # listChanged не заявляем: состав инструментов фиксирован на
                # старте переменной LQRAG_ALLOW_WRITE и в работе не меняется.
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": visible_tools()})
    if method == "tools/call":
        return _handle_tools_call(msg_id, params)

    _log("метод не поддерживается:", method)
    return _error(msg_id, METHOD_NOT_FOUND, "метод не найден: %s" % method)


def _safe_handle(msg):
    """_handle без права уронить мост: сбой на одном сообщении — -32603 на него.

    Упавший процесс оставил бы агента без инструментов до перезапуска сессии.
    """
    try:
        return _handle(msg)
    except Exception as exc:
        _log("сбой обработки сообщения: %r" % (exc,))
        if isinstance(msg, dict) and "id" in msg:
            return _error(
                msg.get("id"), INTERNAL_ERROR, "внутренняя ошибка моста: %s" % exc
            )
        return None


def main():
    url, source, port_file, checked = _resolve_address()
    # Путь порт-файла и живость pid — в лог: при двух домах ($HOME терминала и
    # pw_dir службы) иначе не видно, что мост смотрит не туда.
    origin = {
        "env": "из LQRAG_URL",
        "port-file": "порт из %s, pid: %s"
        % (
            port_file,
            {True: "жив", False: "мёртв", None: "неизвестен"}[_pid_alive(port_file)],
        ),
        "fallback": "запасной адрес",
    }[source]
    _log(
        "старт: LocalRAG=%s (%s), инструментов=%d, запись=%s, автозапуск=%s, "
        "агент=%s"
        % (
            url,
            origin,
            len(visible_tools()),
            "разрешена" if ALLOW_WRITE else "запрещена",
            "да" if _autostart_enabled() and source != "env" else "нет",
            AGENT_NAME or "-",
        )
    )
    if _AGENT_ENV and not AGENT_NAME:
        # Значение не печатаем: по ошибке туда могли вставить сам токен.
        _log(
            "ПРЕДУПРЕЖДЕНИЕ: LQRAG_AGENT не имя агента ([a-z0-9][a-z0-9_-], до "
            "32 символов) — токен по нему не ищется"
        )
    if source == "fallback":
        # Без порт-файла демон, скорее всего, не поднят, а запасной адрес это
        # маскирует — предупреждаем сразу.
        if _autostart_enabled():
            consequence = "мост попросит systemd поднять его при первом обращении"
        else:
            consequence = "поиск будет падать с отказом соединения"
        _log(
            "ПРЕДУПРЕЖДЕНИЕ: фактический порт демона не обнаружен — порт-файла "
            "нет (искал: %s), а LQRAG_URL не задан, поэтому мост адресует "
            "сайдкар на запасной %s. Если сервис LocalRAG не запущен, %s. "
            "Проверьте: systemctl --user status lib-local-rag.service, затем "
            "%s/api/v1/health." % (", ".join(checked) or "-", url, consequence, url)
        )
    # JSON-RPC — всегда UTF-8: на python3.6 в локали C текстовые stdin/stdout
    # ASCII, и первый же русский ответ роняет мост UnicodeEncodeError.
    stdin = getattr(sys.stdin, "buffer", None)
    stdout = getattr(sys.stdout, "buffer", None)
    while True:
        line = sys.stdin.readline() if stdin is None else stdin.readline()
        if not line:
            _log("stdin закрыт, выходим")
            break
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except ValueError as exc:
            # Битую строку пропускаем: ответить нечем (id неизвестен), падать
            # из-за одного мусорного сообщения нельзя.
            _log("не разобрал JSON:", exc)
            continue

        if isinstance(message, list):
            responses = [_safe_handle(item) for item in message]
        else:
            responses = [_safe_handle(message)]
        for response in responses:
            if response is None:
                continue
            text = json.dumps(response, ensure_ascii=False) + "\n"
            if stdout is None:
                sys.stdout.write(text)
                sys.stdout.flush()
            else:
                stdout.write(text.encode("utf-8"))
                stdout.flush()


if __name__ == "__main__":
    main()
