"""Публикация рантайм-фактов о демоне: фактический порт и PID.

Порт. В поставке RAG_PORT=0 (БТ §4.2): номер выбирает ядро, и при каждом
старте он новый, поэтому прописать его в конфиг агента нельзя. Демон кладёт
фактический порт в файл рядом с индексами, а MCP-мост читает его, когда
LQRAG_URL не задан явно.

PID. Пакетный демон останавливает systemd (юнит, prerm), индексацию на сессию —
POST /api/v1/deactivate. PID читают uninstall.sh (останавливает по нему демон
без systemd, БТ §7, не угадывая процесс по имени), мост (отличает
прогревающийся демон от мёртвого) и scripts/sberos_check.py (находит процесс
и домашний каталог демона).

Оба файла создаются с правами 0600 и снимаются при штатной остановке.
Устаревший файл после падения демона не опаснее отсутствующего: клиент получит
отказ соединения и честное «сайдкар недоступен», а по мёртвому PID никого не
найдёт.
"""

import logging
import os
from pathlib import Path

lg = logging.getLogger("app")

PORT_FILE_NAME = "port"
PID_FILE_NAME = "pid"
# Порт, на который мост идёт без порт-файла (FALLBACK_URL в mcp_server.py).
BRIDGE_FALLBACK_PORT = 8000


def port_file_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser() / PORT_FILE_NAME


def pid_file_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser() / PID_FILE_NAME


def _publish(path: Path, value: int, what: str) -> Path | None:
    """Атомарно записать одно число. None, если не вышло.

    Сбой записи демон не роняет: индексация и HTTP API продолжают работать.
    Но мост без порт-файла найдёт демон, только если тот слушает 8000 (см.
    publish_port).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Через временный файл: читатель не должен увидеть пустое или
        # наполовину записанное значение.
        tmp = path.with_suffix(".tmp")
        tmp.write_text("%d\n" % value, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        lg.warning("не удалось опубликовать %s в %s: %s", what, path, exc)
        return None
    lg.info("%s %d опубликован в %s", what, value, path)
    return path


def publish_port(root: str | os.PathLike[str], port: int) -> Path | None:
    path = _publish(port_file_path(root), port, "порт")
    if path is None and port != BRIDGE_FALLBACK_PORT:
        # Демон жив, но агент до него не дотянется: это отказ функции, и в
        # журнале он должен читаться как ошибка, а не как предупреждение.
        lg.error(
            "мост не найдёт демон: порт %d не опубликован, а без порт-файла мост "
            "идёт на %d. Поиск агенту недоступен до перезапуска службы",
            port,
            BRIDGE_FALLBACK_PORT,
        )
    return path


def publish_pid(root: str | os.PathLike[str], pid: int | None = None) -> Path | None:
    return _publish(pid_file_path(root), os.getpid() if pid is None else pid, "pid")


def remove_published(*paths: Path | None) -> None:
    """Снять публикацию при остановке. Отсутствие файла — не ошибка."""
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            lg.warning("не удалось удалить %s: %s", path, exc)
