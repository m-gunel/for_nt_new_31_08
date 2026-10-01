import faulthandler
import logging
import os
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from src.malloc_tuning import apply_arena_limit, memory_state

# До импорта нативных расширений: abort() внутри C-библиотеки (LSM, Lance,
# OpenVINO) иначе оставляет в логе одну строку ассерта и ни следа Python —
# по такому логу нельзя понять, какой вызов упал. С faulthandler на SIGABRT
# и SIGSEGV печатается стек всех потоков.
faulthandler.enable()

# До импорта src.config и src.server: они тянут OpenVINO, Lance и polars, чьи
# потоки иначе успеют получить собственные арены glibc, и лимит опоздает.
apply_arena_limit()

from src.config import reset_storage_if_requested
from src.port_file import publish_pid, publish_port, remove_published
from src.server import app
from src.utils import set_config

reset_storage_if_requested()

cfg = set_config()

# Формат держим в константах: тот же самый нужен файловому хендлеру, а
# разъехавшиеся форматы в двух стоках одного лога читать невозможно.
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=cfg.logging_level,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
)
logging.getLogger("watchfiles.main").setLevel(logging.INFO)
logging.getLogger("watchdog").setLevel(logging.INFO)
logging.getLogger("app").setLevel(cfg.app_logging_level)

lg = logging.getLogger("app")


def _attach_file_log() -> logging.Handler | None:
    """Продублировать лог в файл, независимо от политики journald.

    Демон пишет в stderr, и при исправном journald этого хватает. Но настройки
    журнала задаёт образ ОС: при Storage=none или SplitMode=none
    пользовательского журнала нет вовсе, а MaxLevelStore=warning выбрасывает
    всё, что мы пишем на INFO. Со стороны это неотличимо — «сервис работает,
    логов нет», — и диагностировать нечем.

    Отказ файла демон НЕ роняет: лог — вспомогательная вещь, и остаться без
    индексатора из-за неудачного mkdir было бы хуже, чем без файла лога.
    """
    path = (cfg.log_file or "").strip()
    if not path:
        return None
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # В логе оседают пути к личным файлам пользователя — каталог закрываем.
        os.chmod(target.parent, 0o700)
        handler = RotatingFileHandler(
            target,
            maxBytes=cfg.log_max_bytes,
            backupCount=cfg.log_backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        lg.warning("файловый лог отключён, пишу только в stderr: %s (%s)", target, exc)
        return None
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(handler)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return handler


# Вешаем ДО первых сообщений: иначе «настройка памяти» и публикация порта
# в файл не попадут, а это ровно те строки, по которым разбирают старт.
_file_handler = _attach_file_log()


def _attach_file_log_to_uvicorn() -> None:
    """Добавить файловый сток логгерам uvicorn.

    uvicorn в своём LOGGING_CONFIG ставит им propagate=False и собственные
    хендлеры, поэтому до корневого логгера его строки не доходят — и хендлер,
    навешенный только на root, их не увидит. Звать эту функцию можно ТОЛЬКО
    после конструирования uvicorn.Config: именно его __init__ применяет
    dictConfig, и сделанное раньше он бы затёр.
    """
    if _file_handler is None:
        return
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(_file_handler)


# Набор уровней у pydantic-конфига шире, чем понимает uvicorn: WARN, FATAL и
# NOTSET он не знает и падает на них при разборе своего dictConfig.
_UVICORN_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})
_UVICORN_ALIASES = {"warn": "warning", "fatal": "critical", "notset": "debug"}


def _uvicorn_log_level(value: str) -> str:
    level = (value or "info").lower()
    level = _UVICORN_ALIASES.get(level, level)
    return level if level in _UVICORN_LEVELS else "info"


# Не в момент применения ручек: там логирование ещё не настроено.
lg.info("настройка памяти: %s", memory_state())


def _log_startup_context() -> None:
    """Откуда взялись настройки и что именно будет обходиться."""
    conf = os.environ.get("CONFIG_PATH")
    home = os.path.expanduser("~")
    lg.info(
        "CONFIG_PATH=%s | HOME=%s | user_conf.yml=%s",
        conf or "(не задан — YAML не читается, действуют умолчания)",
        home,
        cfg.user_conf_path if cfg.user_conf_path.exists() else "(нет)",
    )
    if _file_handler is not None:
        lg.info("лог дублируется в файл: %s", cfg.log_file)
    roots = [str(Path(path).expanduser()) for path in cfg.scan_paths]
    if roots:
        lg.info("каталоги индексации: %s", ", ".join(roots))
    else:
        lg.warning(
            "список каталогов индексации ПУСТ — обходить нечего. Проверьте "
            "file_scan_cfg.paths в %s и folders в %s",
            conf or "конфиге",
            cfg.user_conf_path,
        )
    # Под sudo HOME становится /root: тогда ~ раскроется не туда, индекс уедет
    # в /root/.files-search, а домашний каталог пользователя не обойдётся.
    if os.geteuid() == 0 or home == "/root":
        lg.warning(
            "демон работает от root (HOME=%s). Он должен запускаться ОТ "
            "ПОЛЬЗОВАТЕЛЯ: иначе ~ указывает не на его каталог, а индексы "
            "лягут в /root",
            home,
        )


# Вызов на уровне модуля, а НЕ внутри самой функции. Раньше он стоял с отступом
# в её теле: до него не доходило исполнение, и вся стартовая диагностика была
# мёртвым кодом — ни CONFIG_PATH, ни списка каталогов, ни предупреждения о
# запуске от root в логе не появлялось никогда.
_log_startup_context()

if __name__ == "__main__":
    host = os.environ.get("RAG_HOST", "0.0.0.0")
    # RAG_PORT=0 отдаёт выбор порта ядру — этого требует БТ §4.2. Сокет биндим
    # сами, а не силами uvicorn: иначе фактический номер известен только внутри
    # сервера, и опубликовать его для MCP-моста уже неоткуда.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(os.environ.get("RAG_PORT", "8000"))))
    bound_port = sock.getsockname()[1]

    published_port = publish_port(cfg.lsm_root, bound_port)
    published_pid = publish_pid(cfg.lsm_root)

    # Своя строка о старте, потому что uvicorn свою НЕ напечатает: при
    # переданном готовом сокете Server.startup() пропускает
    # _log_started_message, считая, что адрес уже сообщил bind_socket(), —
    # а его мы не звали. Без этой строки в логе нет ни одного упоминания
    # фактического адреса, и документация годами обещала «Uvicorn running»,
    # которого не бывает.
    lg.info(
        "слушаю http://%s:%d (Swagger: http://127.0.0.1:%d/docs). Идёт прогрев "
        "модели OpenVINO, 40-45 c — до его конца API не отвечает",
        host,
        bound_port,
        bound_port,
    )

    try:
        server_config = uvicorn.Config(
            app,
            host=host,
            port=bound_port,
            log_level=_uvicorn_log_level(cfg.logging_level),
            reload=False,
            timeout_graceful_shutdown=5,
        )
        _attach_file_log_to_uvicorn()
        uvicorn.Server(server_config).run(sockets=[sock])
    finally:
        # Снимаем публикацию: следующий клиент не должен идти на порт, где уже
        # никто не слушает, а uninstall — искать давно мёртвый процесс.
        remove_published(published_port, published_pid)
