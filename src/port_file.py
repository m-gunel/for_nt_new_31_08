"""Публикация рантайм-фактов о демоне: фактический порт и PID.

Порт. MCP-мост запускается агентом как подпроцесс, и адрес сайдкара приходит
ему переменной окружения, зафиксированной в конфиге агента. Пока порт
захардкожен, это работает; при динамическом порте (БТ §4.2) такой конфиг
протухает при первом же перезапуске демона, и приложению пришлось бы
переписывать чужой конфиг и перезапускать агента на каждый старт индексатора.
Поэтому демон кладёт фактический порт в файл рядом с индексами, а мост читает
его, когда LQRAG_URL не задан явно.

PID. Своего механизма остановки у демона нет: ни systemd-юнита, ни ручки API.
По БТ §7 удаление обязано «немедленно остановить» демон, а §5.2 требует
останова с сохранением индексов. Без опубликованного PID это сводится к
угадыванию процесса по имени, что на чужой машине небезопасно. Файл делает
остановку детерминированной.

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

# Историческое имя: до появления PID модуль публиковал только порт.
FILE_NAME = PORT_FILE_NAME


def port_file_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser() / PORT_FILE_NAME


def pid_file_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser() / PID_FILE_NAME


def _publish(path: Path, value: int, what: str) -> Path | None:
    """Атомарно записать одно число. None, если не вышло.

    Сбой записи не должен мешать демону работать: без файла клиенты вернутся к
    историческому 8000, а не останутся без индексатора.
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
    return _publish(port_file_path(root), port, "порт")


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


def remove_port(path: Path | None) -> None:
    """Совместимость с прежним именем."""
    remove_published(path)
