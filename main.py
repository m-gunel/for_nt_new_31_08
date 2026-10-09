import sys

if sys.argv[1:2] == ["--ocr-worker"]:
    # Воркер OCR (src/ocr_worker.py): тот же бинарь, но без OpenVINO, конфига и
    # сервера — только pymupdf. Проверка до всех тяжёлых импортов ниже.
    from src.ocr_worker import main as _ocr_worker

    raise SystemExit(_ocr_worker())

import faulthandler
import logging
import os
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from src.malloc_tuning import apply_arena_limit, memory_state

# До импорта нативных расширений: при SIGABRT/SIGSEGV в LSM, Lance или OpenVINO
# в лог попадёт стек всех потоков, а не одна строка ассерта.
faulthandler.enable()

# До импорта src.config и src.server: они тянут OpenVINO, Lance и polars, чьи
# потоки иначе успеют получить собственные арены glibc, и лимит опоздает.
apply_arena_limit()

from src.config import protect_storage, reset_storage_if_requested
from src.port_file import publish_pid, publish_port, remove_published
from src.server import app
from src.utils import set_config

cfg = set_config()

# Общий формат для stderr и файлового хендлера.
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

# После настройки логирования, иначе строка о сбросе никуда не попадёт; до
# файлового лога — он лежит внутри сносимого lsm_root.
reset_storage_if_requested(cfg)
protect_storage(cfg)


def _attach_file_log() -> logging.Handler | None:
    """Продублировать лог в файл cfg.log_file — на случай, если journald его не хранит.

    Отказ файла демон не роняет: без лога лучше, чем без индексатора.
    """
    path = (cfg.log_file or "").strip()
    if not path:
        return None
    target = Path(path).expanduser()
    try:
        # В логе оседают пути к личным файлам пользователя: свой каталог
        # создаём закрытым, а существующий (дом, /tmp) не трогаем — он не наш.
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


# До первых сообщений: строки о памяти и порте нужны в файле для разбора старта.
_file_handler = _attach_file_log()


def _attach_file_log_to_uvicorn() -> None:
    """Добавить файловый сток логгерам uvicorn.

    У них propagate=False, и хендлер на root их строк не видит. Звать только
    после создания uvicorn.Config: его __init__ применяет dictConfig и затёр бы
    сделанное раньше.
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


# Здесь, а не в apply_arena_limit(): там логирование ещё не настроено.
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


_log_startup_context()

if __name__ == "__main__":
    # Внешний интерфейс — только явным RAG_HOST: у API нет аутентификации.
    # Пустое значение — тоже умолчание, а не «все интерфейсы».
    host = os.environ.get("RAG_HOST") or "127.0.0.1"
    # Сокет биндим сами: при RAG_PORT=0 (БТ §4.2) порт выбирает ядро, и номер
    # нужно опубликовать для MCP-моста до старта uvicorn.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(os.environ.get("RAG_PORT", "8000"))))
    bound_port = sock.getsockname()[1]

    published_port = publish_port(cfg.lsm_root, bound_port)
    published_pid = publish_pid(cfg.lsm_root)

    # С готовым сокетом uvicorn не печатает «Uvicorn running»: Server.startup()
    # пропускает _log_started_message. Адрес пишем сами.
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
