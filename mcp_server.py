#!/usr/bin/env python3
"""LocalRAG → MCP (stdio) мост.

Отдаёт агенту то, чего у него нет своими силами: семантический поиск по
локальному индексу LocalRAG и состояние этого индекса. Разговаривает с
сайдкаром по HTTP через уже существующие ручки (/search, /status, /config) —
собственного API у моста нет.

Зависимостей при импорте нет вообще: только stdlib. Это условие поставки —
мост уезжает в дистрибутив отдельными файлами и должен запускаться на голом
системном python3, без venv репозитория. Чтение файлов: офисные форматы и pdf
мост запрашивает у демона (POST /api/v1/read_file) — в его бинаре вшиты
pymupdf и конвертер Р7 для легаси .doc/.xls; если демон недоступен или
старой версии, OOXML (.docx/.pptx/.xlsx) мост разбирает сам через zipfile +
ElementTree, а pdf и легаси-форматы честно отказывают. Текстовые файлы
читаются всегда и без демона.

Протокол: MCP stdio (JSON-RPC построчно через stdin/stdout).

Переменные окружения:
  LQRAG_URL          — базовый URL сайдкара. Не задан — порт берётся из файла,
                       опубликованного демоном, иначе http://127.0.0.1:8000
  LQRAG_PORT_FILE    — явный путь к файлу с портом; задан — ищем только в нём.
                       Не задан — $HOME/.files-search/port, затем тот же файл
                       в домашнем каталоге из passwd; файл, чей демон жив по
                       соседнему pid, берётся раньше устаревшего
  LQRAG_TIMEOUT      — таймаут HTTP, сек (по умолчанию 30)
  LQRAG_AUTOSTART    — 0 запрещает мосту поднимать lib-local-rag.service, когда
                       демон недоступен (по умолчанию поднимает, не чаще раза
                       в 3 минуты)
  LQRAG_SYSTEMCTL    — какой systemctl звать для автозапуска (по умолчанию из PATH)
  LQRAG_ALLOW_WRITE  — 0 прячет от агента инструменты управления индексацией;
                       по умолчанию они включены (агент — единственный пульт)
  LQRAG_MAX_OUTPUT   — бюджет символов на ответ поиска (по умолчанию 12000)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

# Адрес сайдкара. LQRAG_URL, если задан, сильнее всего остального: им
# пользуются тесты и нестандартные установки. Иначе порт берётся из файла,
# который демон публикует при старте, — без этого конфиг агента с зашитым
# адресом протухал бы при каждом перезапуске индексатора на другом порту
# (БТ §4.2 требует динамического назначения). Файла нет — исторический 8000.
#
# LQRAG_PORT_FILE — явный путь, и он эксклюзивен: заданный файл не дополняется
# поиском по домашним каталогам, иначе тест или нестандартная установка
# молча подхватили бы порт чужого демона. None — перебираем кандидатов
# из _port_file_candidates().
_PORT_FILE_ENV = os.environ.get("LQRAG_PORT_FILE")
LQRAG_PORT_FILE = os.path.expanduser(_PORT_FILE_ENV) if _PORT_FILE_ENV else None
PORT_FILE_RELATIVE = os.path.join(".files-search", "port")
# Демон кладёт свой pid рядом с портом (src/port_file.py): по нему мост
# отличает опубликованный порт живого демона от файла, оставшегося после
# SIGKILL, закрытого терминала или выключения питания.
PID_FILE_NAME = "pid"
FALLBACK_URL = "http://127.0.0.1:8000"
LQRAG_TIMEOUT = float(os.environ.get("LQRAG_TIMEOUT", "30"))

# Сайдкар всегда на этой же машине, и прокси из окружения агента ему не нужен.
# Штатный urlopen берёт http_proxy/HTTP_PROXY, а no_proxy сравнивает как
# строку: «localhost» не покрывает 127.0.0.1, CIDR не поддерживается. Запрос
# уходил на корпоративный прокси, его 502 выглядел как ответ живого демона, и
# проба, автозапуск и «прогревается» не срабатывали. Все запросы к сайдкару —
# только через этот opener.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Автозапуск демона. Мост — первое, что видит агент, и если индексатор не
# поднят (служба остановлена, вход без перезапуска сессии после установки),
# без этого агент получал бы отказ соединения до ручного вмешательства, а
# просить пользователя набирать systemctl в поставке нельзя.
SERVICE_UNIT = "lib-local-rag.service"
AUTOSTART_TIMEOUT_S = 10
# Сколько вызов инструмента ждёт ответа systemctl. У агента таймаут MCP-запроса
# по умолчанию 5 с (WSC), и ответ «прогревается» обязан успеть раньше него.
# Обычно systemctl --no-block отвечает за доли секунды.
AUTOSTART_WAIT_S = 3.0
# Проба на initialize: только отличить «слушает» от «отказ», не больше.
PROBE_TIMEOUT_S = 1.0
# Прогрев идёт 40-45 с, на медленной машине до полутора минут. Отказы подряд
# дольше этого порога — уже не прогрев: мост говорит «не поднимается».
STUCK_AFTER_S = 180.0
# start не чаще раза за этот интервал: если systemd службу не поднял, повтор
# на каждом вызове ничего не изменит, а лог забьётся одинаковыми строками.
AUTOSTART_RETRY_S = 180.0
# Маску на время установки пакета postinst снимает сам, и ответ «masked»
# устаревает быстро: ждать полный интервал до новой попытки незачем.
MASKED_RETRY_S = 30.0
WARMING_TEXT = (
    "Индексатор LocalRAG запускается или прогревается (обычно около 45 секунд). "
    "Повторите запрос через минуту."
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
# Так systemctl сообщает о юните, которого он не видит. Для 1.0.6 это ровно
# случай нечитаемого юнит-файла: systemd --user не смог его открыть.
_UNIT_MISSING_MARKERS = ("not found", "could not be found", "not be found")
# А так — о юните под маской: prerm маскирует службу (--runtime) на время
# замены файлов пакета, чтобы мост не поднял демон из полузаменённого бандла.
_UNIT_MASKED_MARKERS = ("masked",)
# Что показать агенту из `systemctl --user show`, когда служба не поднимается.
_SERVICE_PROPERTIES = ("ActiveState", "SubState", "NRestarts")
# Агент — единственный интерфейс управления индексатором, поэтому write-
# инструменты видны ему по умолчанию. Закрытая установка отключает их явно:
# LQRAG_ALLOW_WRITE=0. Песочница local_read_file от флага не зависит —
# чтение всегда ограничено индексируемыми папками, скрытые файлы и конфиги
# не выдаются, что бы ни было написано в проиндексированном документе.
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
# Верхняя граница чтения файла: больше в контекст всё равно не влезет.
MAX_FILE_CHARS = 40000
# Строк на лист книги Excel при локальном чтении (без демона): больше в ответ на
# MAX_FILE_CHARS всё равно не влезет. У индексатора свой лимит — ExcelParserConfig.max_rows.
MAX_XLSX_ROWS = 1000
# Лимит размера файла при локальном (без демона) разборе. Зеркалит
# _READ_FILE_MAX_BYTES демона: у его /read_file тот же потолок, и мост
# не должен разбирать локально то, что демон честно отверг бы кодом 413.
MAX_LOCAL_READ_BYTES = 64 * 1024 * 1024


def _log(*args):
    """Лог в stderr: в stdout идёт JSON-RPC, туда писать нельзя."""
    print("[localrag-mcp]", *args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------
class InvalidParams(Exception):
    """Аргументы вызова не проходят валидацию — это ошибка протокола (-32602).

    Не «поправить молча»: тихая коррекция (зажать limit, подменить query_type)
    прячет ошибку агента вместо того, чтобы вернуть её ему.
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


# ---------------------------------------------------------------------------
# HTTP к сайдкару
# ---------------------------------------------------------------------------
def _port_file_candidates():
    """Где искать порт-файл, когда LQRAG_PORT_FILE не задан.

    Домов два, и нужны оба. Служба systemd --user живёт с HOME из passwd
    (pw_dir) и публикует порт туда. А терминал на SberOS открывается с
    HOME=/home/work/<LOGIN@DOMAIN>, хотя пользователь тот же: мост, запущенный
    агентом из такого терминала, по одному ~ порт-файла не находил и уходил на
    8000, где никого нет. $HOME первым — демон, поднятый вручную из того же
    терминала, публикует порт именно туда; живость демона по pid важнее этого
    порядка (см. _port_file_routes). Один и тот же каталог под двумя именами
    (симлинк) проверяется один раз.
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
    """Порт из файла или None.

    Нет файла, пустой, мусор внутри, число вне диапазона — всё это означает
    одно: опубликованного порта здесь нет. Гадать нечего.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            port = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _pid_alive(port_file):
    """Жив ли демон, опубликовавший порт: True, False или None — неизвестно.

    pid читается из соседнего с порт-файлом файла. Демон снимает оба при
    штатной остановке, а после SIGKILL, закрытого терминала или выключения
    питания они остаются, и по одному порту живой демон от мёртвого не
    отличить. Нет файла, мусор внутри, не POSIX — неизвестно: гадать нечего.
    """
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
    if pid <= 0:
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


# Кого пробовать первым: живой pid, затем неизвестный, мёртвый — последним.
_PID_RANK = {True: 0, None: 1, False: 2}


def _port_file_routes():
    """(routes, checked): опубликованные порты, живые демоны — первыми.

    routes — (url, port_file, alive) по каждому читаемому порт-файлу, alive —
    ответ _pid_alive(). checked — какие порт-файлы просмотрены вообще, для
    стартового лога и ошибок. При равной живости порядок прежний: $HOME, затем
    pw_dir. Без учёта pid устаревший файл в $HOME заслонял живой демон службы в
    pw_dir, и мост до перезапуска агента отвечал «прогревается». Мёртвый
    кандидат не выбрасывается: другого может не быть, а адрес с путём к файлу
    нужен хотя бы для текста ошибки.
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


def _resolve_routes():
    """(routes, source, checked): куда стучаться, по порядку предпочтения.

    routes — непустой список (url, port_file, alive); первый в нём — выбранный
    адрес, остальные — запасные порт-файлы на случай отказа соединения.
    """
    explicit = os.environ.get("LQRAG_URL")
    if explicit:
        return [(explicit.rstrip("/"), None, None)], "env", []
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
    """Базовый URL сайдкара на момент вызова и то, откуда он взят.

    Возвращает (url, source), где source — одна из трёх строк:
      "env"       объект адресован через LQRAG_URL явно;
      "port-file" порт взят из файла, опубликованного демоном: явный
                  LQRAG_PORT_FILE или ~/.files-search/port в $HOME либо в
                  домашнем каталоге из passwd;
      "fallback"  ни того, ни другого нет — мост идёт на историческое
                  http://127.0.0.1:8000, и это, скорее всего, признак того, что
                  демон не запущен.

    Читается каждый раз, а не запоминается на старте: демон могли перезапустить
    на другом порту, пока мост жив, и залипший адрес выглядел бы как «сайдкар
    недоступен» до перезапуска агента. Чтение локального файла на пять байт
    несопоставимо дешевле самого HTTP-запроса, ради которого оно делается.
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


# ---------------------------------------------------------------------------
# Автозапуск демона
# ---------------------------------------------------------------------------
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
    """Отказ соединения. Возвращает, сколько секунд отказы идут подряд.

    Серия — это отказы без долгих пауз между ними. Если к мосту не обращались
    дольше STUCK_AFTER_S, непрерывности никто не видел: демон мог за это время
    подняться и упасть снова, и старый отсчёт выдал бы «не поднимается» на
    первом же отказе новой серии.
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

    stdin закрыт, stdout уходит в трубу, а не в наш: stdin моста — поток
    JSON-RPC от агента, stdout — ответы ему, и чужие байты в них рвут сессию.
    Вывод читаем байтами: на python3.6 в локали C декодирование по локали
    падает на первом же не-ASCII символе.
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

    state передаётся явно, а не берётся из модуля в момент записи: поток может
    пережить тест, который подменил _autostart, и писать должен в своё.
    Снимок — до start: после него ActiveState=failed сменился бы на activating,
    и причина, по которой служба не поднимается, из текста пропала бы.
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

    Агент подключается к MCP задолго до первого поиска, и прогрев, начатый
    здесь, к этому поиску обычно успевает закончиться. Недоступность — только
    сбой соединения: ответ с ошибкой или таймаут чтения значат, что демон жив,
    и поднимать его незачем. Живой pid у выбранного порт-файла — демон греет
    модель (служба или запущенный вручную), и второй экземпляр на том же
    индексе systemd не заказываем.
    """
    routes, source, _ = _resolve_routes()
    if source == "env":
        return
    failure = None
    for url, _, _ in _attempt_order(routes):
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

    Отказ соединения на 127.0.0.1 приходит мгновенно, и за оставшееся до
    таймаута агента время мост успевает понять, что происходит, и ответить
    по-человечески. Прежний текст про недоступный сайдкар остаётся в
    подробностях — по нему разбирают сбой.
    """
    detail = (
        "сайдкар LocalRAG недоступен по %s (%s). Проверьте, что он запущен "
        "и что %s/api/v1/health отвечает OK." % (base, reason, base)
    )
    if source == "port-file":
        detail += " Порт взят из %s." % port_file
    elif source == "fallback":
        # При source=="fallback" сайдкар, скорее всего, просто не запущен, и
        # это самая частая причина «агент слышит мост, но инструменты падают
        # с отказом соединения».
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
        # Процесс, опубликовавший порт, жив, а соединение отвергнуто — демон
        # греет модель: uvicorn начинает слушать только после прогрева.
        # systemctl start не зовём: служба либо уже поднята, либо демон
        # запущен вручную, и второй экземпляр на том же индексе хуже ожидания.
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
        req = urllib.request.Request(
            base + path, data=data, headers=headers, method=method
        )
        try:
            with _OPENER.open(req, timeout=LQRAG_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            _note_reachable()
            raise _api_error(exc) from None
        except urllib.error.URLError as exc:
            # URLError без HTTP-кода — сбой на этапе соединения (отказ или таймаут
            # подключения): таймаут чтения urllib в URLError не заворачивает, он
            # уходит в ветку OSError ниже с прежним текстом.
            if _is_refused(exc):
                # Отказ по одному порт-файлу — ещё не приговор: устаревший файл
                # мог оказаться первым, а живой демон — в другом доме.
                if refused is None:
                    refused = (base, port_file, exc.reason)
                continue
            raise _unreachable(base, source, port_file, checked, exc.reason) from None
        except OSError as exc:
            # Соединение было, ответа нет — демон жив, но не уложился.
            _note_reachable()
            raise TransportError(
                "сбой связи с сайдкаром %s: %s" % (base, exc)
            ) from None
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


# ---------------------------------------------------------------------------
# Валидация аргументов
# ---------------------------------------------------------------------------
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
        raise InvalidParams("параметр '%s' обязателен и должен быть непустой строкой" % name)
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
    if value not in allowed:
        raise InvalidParams(
            "параметр '%s'=%r недопустим. Возможные значения: %s"
            % (name, value, ", ".join(allowed))
        )
    return value


def _require_bool(args, name):
    value = args.get(name)
    if not isinstance(value, bool):
        raise InvalidParams("параметр '%s' обязателен и должен быть true или false" % name)
    return value


def _optional_sources(args):
    value = args.get("sources")
    if value is None:
        return ["files"]
    if not isinstance(value, list) or not value:
        raise InvalidParams("параметр 'sources' должен быть непустым списком")
    allowed = ("files", "email")
    for item in value:
        if item not in allowed:
            raise InvalidParams(
                "недопустимый источник %r в 'sources'. Возможные: %s"
                % (item, ", ".join(allowed))
            )
    if len(set(value)) != len(value):
        # Повтор дал бы двойной top_k из одной таблицы — сайдкар это отвергает.
        raise InvalidParams("источники в 'sources' не должны повторяться")
    return list(value)


# ---------------------------------------------------------------------------
# Инструмент: поиск
# ---------------------------------------------------------------------------
def _safe_status():
    """Статус индексации, но без права уронить вызов, ради которого спрошен."""
    try:
        return _http_json("GET", "/api/v1/status")
    except ToolError:
        return None


def _format_score(value):
    return ", score=%.3f" % value if isinstance(value, (int, float)) else ""


# Единица «страницы» фрагмента по формату: у pptx это слайд, у таблиц — лист.
_PAGE_UNIT = {".pptx": "слайд", ".xlsx": "лист", ".xls": "лист"}
# Поля места фрагмента, которые демон отдаёт в /search (src/server/model.py).
_CHUNK_PLACE = ("kind", "page_start", "page_end", "line_start", "line_end", "section",
                "table_id", "row_start", "row_end")


def _span(start, end):
    return "%s" % start if end in (None, start) else "%s–%s" % (start, end)


def _chunk_place(chunk, ext):
    """«стр. 3–4 · Глава 2 › 2.1» — где в документе фрагмент; пусто у старого демона."""
    parts = []
    if isinstance(chunk.get("page_start"), int):
        unit = _PAGE_UNIT.get(ext, "стр.")
        parts.append("%s %s" % (unit, _span(chunk["page_start"], chunk.get("page_end"))))
    if isinstance(chunk.get("line_start"), int):
        parts.append("строки %s" % _span(chunk["line_start"], chunk.get("line_end")))
    if chunk.get("section"):
        parts.append(str(chunk["section"]))
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


def _tool_search(args):
    _reject_unknown(args, ("query", "limit", "query_type", "sources"))
    query = _require_str(args, "query")
    limit = _optional_int(args, "limit", 10, 1, 50)
    query_type = _optional_enum(args, "query_type", "hybrid", ("hybrid", "vector", "text"))
    sources = _optional_sources(args)

    data = _http_json(
        "POST",
        "/api/v1/search",
        {
            "query": query,
            "limit": limit,
            "query_type": query_type,
            "sources": sources,
        },
    )
    results = data.get("results") or []
    total = data.get("total", len(results))

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
            # Утверждать «индекс актуален», не сумев спросить статус, нельзя:
            # это ровно то ложное «не найдено», ради которого статус и заведён.
            text = (
                "Ничего не найдено. Состояние индексации проверить не удалось — "
                "возможно, индекс ещё строится."
            )
        else:
            text = "Ничего не найдено. Индексация не идёт, индекс актуален."
        return text, {"total": 0, "shown": 0, "indexing": indexing, "results": []}

    lines = []
    structured = []
    used = 0
    for item in results:
        path = item.get("absolute_path") or item.get("id") or "?"
        item_type = item.get("type", "file")
        title = item.get("title") or path
        note = "" if item_type == "file" else "  ← не файл на диске, читать нечем"
        header = "%d. %s (тип=%s, путь=%s%s)%s" % (
            len(structured) + 1,
            title,
            item_type,
            path,
            _format_score(item.get("score")),
            note,
        )
        # Документ целиком либо влезает в бюджет, либо не начинается: обрезок
        # заголовка без фрагментов агенту бесполезен.
        if used + len(header) > MAX_OUTPUT_CHARS and structured:
            break
        block = [header]
        used += len(header)
        about = _document_line(item.get("document"))
        if about and used + len(about) + 5 <= MAX_OUTPUT_CHARS:
            block.append("   " + about)
            used += len(about) + 3
        ext = os.path.splitext(path)[1].lower()
        chunks = []
        for chunk in (item.get("chunks") or [])[:MAX_CHUNKS_PER_DOC]:
            content = (chunk.get("content") or "").strip()
            if not content:
                continue
            if len(content) > MAX_CHUNK_CHARS:
                content = content[:MAX_CHUNK_CHARS] + "…"
            place = _chunk_place(chunk, ext)
            line = "   · " + ("[%s] " % place if place else "") + content
            if used + len(line) > MAX_OUTPUT_CHARS:
                break
            block.append(line)
            used += len(line)
            entry = {"text": content, "score": chunk.get("score")}
            entry.update((k, chunk[k]) for k in _CHUNK_PLACE if chunk.get(k) is not None)
            chunks.append(entry)
        lines.extend(block)
        result = {
            "path": path,
            "title": title,
            "type": item_type,
            "score": item.get("score"),
            "chunks": chunks,
        }
        if isinstance(item.get("document"), dict):
            result["document"] = {k: v for k, v in item["document"].items() if v is not None}
        structured.append(result)

    shown = len(structured)
    head = "Найдено документов: %s" % total
    if shown < len(results):
        head += " (показано %s — остальное не поместилось в ответ)" % shown
    # Раньше здесь стояло "indexing": False жёстко. Агент, получивший три
    # документа во время первой индексации, считал список полным и отвечал
    # пользователю, что больше ничего нет, — то же ложное «не найдено», от
    # которого защищена пустая ветка выше. Статус стоит одного GET по
    # localhost: демон отдаёт счётчики из памяти, без обращения к диску.
    status = _safe_status()
    indexing = bool(status and status.get("is_indexing"))
    text = head + "\n" + "\n".join(lines) + "\n"
    if indexing:
        text += (
            "ВНИМАНИЕ: индексация ещё идёт (в очереди %s, фаза «%s») — "
            "список может быть неполным, повторите поиск позже.\n"
            % (status.get("files_to_index", "?"), status.get("type", "?"))
        )
    return text, {
        "total": total,
        "shown": shown,
        "indexing": indexing,
        "results": structured,
    }


# ---------------------------------------------------------------------------
# Инструмент: состояние индекса
# ---------------------------------------------------------------------------
def _tool_index_status(args):
    _reject_unknown(args, ())
    status = _http_json("GET", "/api/v1/status")
    is_indexing = bool(status.get("is_indexing"))
    text = (
        "Индексация: %s\nФаза: %s\nВ очереди всего: %s (файлов: %s, писем: %s)\n"
        "Собрано на этапе preindex: %s"
        % (
            "идёт" if is_indexing else "не идёт",
            status.get("type", "?"),
            status.get("files_to_index", "?"),
            status.get("queued_files", "?"),
            status.get("queued_emails", "?"),
            status.get("total_files", "?"),
        )
    )
    if is_indexing:
        text += "\nПустая выдача поиска сейчас не означает отсутствия документа."
    return text, status


# ---------------------------------------------------------------------------
# Инструмент: чтение файла
# ---------------------------------------------------------------------------
# Форматы, которые мост извлекает сам. Секретоёмкие расширения (.env, .ini,
# .conf, .properties, .toml) выкинуты намеренно: индекс стоит на домашнем
# каталоге, а в таких файлах лежат токены — в этом же репозитории индекс
# пакетов в pyproject.toml прописан вместе с плейсхолдером токена.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log",
    ".json", ".yml", ".yaml", ".xml", ".html", ".htm",
    ".py", ".sh", ".sql", ".java", ".kt", ".ipynb",
}
# Легаси .doc/.xls включены в белый список: их читает демон через конвертер
# Р7, а без демона мост отказывает с подсказкой.
OFFICE_EXTENSIONS = {".doc", ".docx", ".pdf", ".pptx", ".xls", ".xlsx"}

_FOLDERS_TTL = 5.0
_folders_cache = {"ts": 0.0, "roots": None}


def _indexed_roots():
    """Корни, внутри которых чтение разрешено, — из GET /config.

    Кэш на несколько секунд: подряд идущие чтения не должны платить за
    HTTP-запрос каждый раз, но и залипать на устаревшем списке не должны.
    """
    now = time.monotonic()
    if _folders_cache["roots"] is not None and now - _folders_cache["ts"] < _FOLDERS_TTL:
        return _folders_cache["roots"]
    data = _http_json("GET", "/api/v1/config")
    roots = []
    for folder in data.get("folders") or []:
        if not isinstance(folder, str):
            continue
        roots.append(os.path.realpath(os.path.expanduser(folder)))
    _folders_cache["ts"] = now
    _folders_cache["roots"] = roots
    return roots


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _extract_docx(path):
    # OOXML разбирается сам, без библиотек: в поставке мост живёт на системном
    # python3, где нет docx2txt, а тесты моста гоняют его подпроцессом из
    # venv, где она есть. Один код вместо двух путей — расхождение
    # «у разработчика работает, у пользователя ImportError» исключено.
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as bundle:
        root = ET.fromstring(bundle.read("word/document.xml"))
    # Параграфы бывают вложенными (текстбоксы внутри w:p): root.iter(W+"p")
    # обходит и внешние, и внутренние, а paragraph.iter() внешнего параграфа
    # уже захватывает узлы внутреннего — без seen-set текстбокс попал бы в
    # выдачу дважды. Узел элемента уникален как объект, id() надёжен.
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
    with zipfile.ZipFile(path) as bundle:
        parts = []
        for name in bundle.namelist():
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
            if match:
                parts.append((int(match.group(1)), name))
        # По номеру, а не по имени файла: лексикографически slide10 идёт
        # раньше slide2, и порядок слайдов переворачивался бы с десятого.
        for number, name in sorted(parts):
            root = ET.fromstring(bundle.read(name))
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
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            items = ET.fromstring(bundle.read("xl/sharedStrings.xml"))
            for item in items.iter(NS + "si"):
                shared.append("".join(t.text or "" for t in item.iter(NS + "t")))
        titles = {}
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            targets = {}
            rels = ET.fromstring(bundle.read("xl/_rels/workbook.xml.rels"))
            for rel in rels.iter(R + "Relationship"):
                targets[rel.get("Id")] = "xl/" + (rel.get("Target") or "").lstrip("/")
            workbook = ET.fromstring(bundle.read("xl/workbook.xml"))
            for sheet in workbook.iter(NS + "sheet"):
                titles[targets.get(sheet.get(RD + "id"), "")] = sheet.get("name")
        parts = []
        for name in names:
            match = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", name)
            if match:
                parts.append((int(match.group(1)), name))
        for number, name in sorted(parts):
            rows = []
            sheet = ET.fromstring(bundle.read(name))
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
                # Строк на лист — как в ExcelParserConfig.max_rows индексатора:
                # без границы книга на сотни тысяч строк уезжала бы в контекст.
                if len(rows) >= MAX_XLSX_ROWS:
                    rows.append("… лист обрезан на %d строках" % MAX_XLSX_ROWS)
                    break
            if rows:
                title = titles.get(name) or "sheet%d" % number
                blocks.append("--- лист %s ---\n%s" % (title, "\n".join(rows)))
    return "\n\n".join(blocks)


# Пакет нужен только pdf: у stdlib нет декодера потока PDF. Остальные
# офисные форматы — zip с XML внутри, их разбирает сам мост.
_OFFICE_EXTRACTORS = {
    ".docx": (_extract_docx, None),
    ".pdf": (_extract_pdf, "pymupdf"),
    ".pptx": (_extract_pptx, None),
    ".xlsx": (_extract_xlsx, None),
}

# Форматы, которые мост читает ТОЛЬКО через демона: pdf зашит в его бинарь
# (pymupdf), легаси .doc/.xls разбирает конвертер Р7-Офиса.
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
                "фрагменты этого документа доступны через local_search"
                % (ext, package)
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
    if raw_path.startswith("email_"):
        raise ToolError(
            "это ключ письма из выдачи поиска, а не путь на диске — прочитать нечем"
        )
    # realpath, а не abspath: иначе симлинк из индексируемой папки наружу
    # проводит чтение мимо песочницы.
    path = os.path.realpath(os.path.expanduser(raw_path))

    # Список папок берём у сайдкара. Если он недоступен — границы песочницы
    # неизвестны, и чтение запрещено: открытый доступ ко всей файловой системе
    # хуже отказа.
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
    if not any(_inside(path, root) for root in roots):
        raise ToolError(
            "путь %s вне индексируемых папок, чтение запрещено. Разрешены: %s"
            % (path, ", ".join(roots))
        )

    if not os.path.isfile(path):
        raise ToolError("файл не найден или не является обычным файлом: %s" % path)
    if os.path.basename(path).startswith("."):
        raise ToolError("скрытые файлы не читаются: %s" % path)

    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXTENSIONS and ext not in OFFICE_EXTENSIONS:
        raise ToolError(
            "расширение '%s' не поддерживается. Доступны: %s"
            % (ext or "(нет)", ", ".join(sorted(TEXT_EXTENSIONS | OFFICE_EXTENSIONS)))
        )

    if ext in _SIDECAR_READ_EXTENSIONS:
        # Сначала спрашиваем демона: у него в бинаре pymupdf для pdf и
        # конвертер Р7 для легаси .doc/.xls — этого у моста на системном
        # python3 нет и не будет. На локальный разбор ниже проваливаемся
        # только когда демона нет или ручки нет (404): прочие ответы демона
        # (413 больше лимита, 422 пустой текст) — это его честный вердикт
        # по файлу, и подменять его своей локальной попыткой нельзя.
        try:
            data = _http_json("POST", "/api/v1/read_file", {"absolute_path": raw_path})
            remote = data.get("text")
            if isinstance(remote, str):
                truncated = len(remote) > MAX_FILE_CHARS or bool(data.get("truncated"))
                content = remote[:MAX_FILE_CHARS]
                header = "Путь: %s\nРазмер файла: %s байт\n" % (path, os.path.getsize(path))
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
        except TransportError:
            pass
        except ApiError as exc:
            if exc.code != 404:
                raise

    if ext in _SIDECAR_ONLY_EXTENSIONS:
        raise ToolError(
            "легаси-формат %s читает только демон LocalRAG, а он недоступен "
            "или старой версии — обновите lib-local-rag. Фрагменты документа "
            "доступны через local_search" % ext
        )

    # Демон отклоняет чтение больше _READ_FILE_MAX_BYTES кодом 413; если его
    # нет, мост обязан отвергнуть такой же файл сам, а не втягивать распакованный
    # XML неограниченного объёма в память системного python3.
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


# ---------------------------------------------------------------------------
# Инструменты управления индексацией (скрыты при LQRAG_ALLOW_WRITE=0)
# ---------------------------------------------------------------------------
def _get_folders():
    return list(_http_json("GET", "/api/v1/config").get("folders") or [])


def _post_folders(folders):
    """Записать список папок.

    POST /config с непустым folders сам делает stop_watcher → stop_index →
    удаление снятых префиксов → start_reindex → restart_watcher, поэтому
    отдельная команда переиндексации после него не нужна.
    """
    _http_json("POST", "/api/v1/config", {"folders": folders})
    _folders_cache["roots"] = None


def _normalize(path):
    return os.path.abspath(os.path.expanduser(path))


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
    # Существование пути проверяет сам сайдкар (400, конфиг не меняется):
    # сохранённый несуществующий путь роняет наблюдение за файловой системой.
    _post_folders(folders + [path])
    return (
        "Папка добавлена: %s. Индексация перезапущена автоматически — "
        "следите за local_index_status." % path,
        {"path": path, "added": True},
    )


def _tool_remove_path(args):
    _reject_unknown(args, ("path",))
    path = _normalize(_require_str(args, "path"))
    folders = _get_folders()
    remaining = [folder for folder in folders if _normalize(folder) != path]
    if len(remaining) == len(folders):
        raise ToolError("папка %s не числится среди индексируемых" % path)
    if not remaining:
        # Пустой список не «выключает индексацию», а тихо откатывает конфиг к
        # путям из YAML: Config.scan_paths считает пустой список отсутствием
        # пользовательской настройки.
        raise ToolError(
            "нельзя удалить последнюю папку: пустой список молча вернёт пути из "
            "конфига демона. Сначала добавьте другую папку"
        )
    _post_folders(remaining)
    return (
        "Папка удалена: %s. Её документы сняты с индекса, индексация перезапущена."
        % path,
        {"path": path, "removed": True},
    )


def _tool_set_email(args):
    _reject_unknown(args, ("enabled",))
    enabled = _require_bool(args, "enabled")
    # Частичный апдейт поддержан сайдкаром: список папок этим запросом не
    # затрагивается. Рестарт демона не нужен — сканер поднимается лениво.
    _http_json("POST", "/api/v1/config", {"email": {"enabled": enabled}})
    actual = _http_json("GET", "/api/v1/config").get("email", {}).get("enabled")
    return (
        "Почтовая индексация %s (email.enabled=%s)"
        % ("включена" if enabled else "выключена", actual),
        {"email_enabled": actual},
    )


def _tool_trigger_rescan(args):
    _reject_unknown(args, ())
    status = _http_json("GET", "/api/v1/status")
    if status.get("is_indexing"):
        # POST /config во время активной индексации — то, на чём hunt_crash.py
        # ловил падения демона. Отказываем, а не рискуем.
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


# ---------------------------------------------------------------------------
# Контракт инструментов
# ---------------------------------------------------------------------------
_SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "total": {"type": "integer", "description": "Сколько документов нашлось"},
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
                    "score": {"type": ["number", "null"]},
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
            "Возвращает документы с фрагментами текста и абсолютными путями — "
            "путь из absolute_path можно передать в local_read_file. "
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
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["files", "email"]},
                    "default": ["files"],
                    "description": (
                        "files — файлы на диске (по умолчанию); email — письма, "
                        "работает только если включена почтовая индексация"
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
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "local_set_email",
        "description": (
            "Включает или выключает индексацию почты. Выключение снимает письма "
            "с индекса. Рестарт демона не нужен."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
    {
        "name": "local_trigger_rescan",
        "description": (
            "Форсирует переобход всех индексируемых папок без потери индекса. "
            "Отказывает, если индексация уже идёт."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
    "local_set_email": _tool_set_email,
    "local_trigger_rescan": _tool_trigger_rescan,
}
_WRITE_TOOL_NAMES = {tool["name"] for tool in WRITE_TOOLS}


def visible_tools():
    return READ_TOOLS + (WRITE_TOOLS if ALLOW_WRITE else [])


# ---------------------------------------------------------------------------
# JSON-RPC диспетчер
# ---------------------------------------------------------------------------
def _result(msg_id, payload):
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


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
        return _error(msg_id, -32602, "поле 'arguments' должно быть объектом")

    if name not in _HANDLERS:
        return _error(msg_id, -32601, "неизвестный инструмент: %s" % name)
    if name in _WRITE_TOOL_NAMES and not ALLOW_WRITE:
        return _error(
            msg_id,
            -32601,
            "инструмент %s скрыт в этой установке (LQRAG_ALLOW_WRITE=0): "
            "управление индексацией из агента отключено" % name,
        )

    try:
        text, structured = _HANDLERS[name](args)
    except InvalidParams as exc:
        # Ошибка вызова, а не ошибка выполнения: агент должен увидеть её как
        # неверные параметры и исправить вызов.
        return _error(msg_id, -32602, str(exc))
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
    return _error(msg_id, -32601, "метод не найден: %s" % method)


def main():
    url, source, port_file, checked = _resolve_address()
    # Путь порт-файла — в лог обязательно: при двух домах ($HOME терминала и
    # pw_dir службы) «мост смотрит не туда» иначе ничем не выдаёт себя. Там же
    # живость pid: устаревший файл после аварийной остановки виден сразу.
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
        "старт: LocalRAG=%s (%s), инструментов=%d, запись=%s, автозапуск=%s"
        % (
            url,
            origin,
            len(visible_tools()),
            "разрешена" if ALLOW_WRITE else "запрещена",
            "да" if _autostart_enabled() and source != "env" else "нет",
        )
    )
    if source == "fallback":
        # Наиболее частая жалоба пользователя: «инструменты есть, а поиск
        # падает с отказом соединения». Причина почти всегда в том, что демон
        # не поднят, а fallback-адрес молча маскирует её. Говорим сразу.
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
    # Поток JSON-RPC — всегда UTF-8, независимо от локали. На python3.6 в
    # локали C текстовые stdin/stdout — ASCII: кириллица запроса приходила
    # суррогатами, а первый же русский ответ ронял мост UnicodeEncodeError.
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
            responses = [_handle(item) for item in message]
        else:
            responses = [_handle(message)]
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
