"""Подготовка хранилища индекса при старте — до того, как его откроет Index.

Здесь ещё ничего не открыто (LSM, очереди, таблицы LanceDB), поэтому
хранилище можно править на уровне файлов, не трогая живые хендлы.
"""

import shutil
import typing
from pathlib import Path

from lsm import LSM

from src.utils import get_logger
from src.utils.lsm_adapter import lsm_file

if typing.TYPE_CHECKING:
    from src.config import Config

lg = get_logger(__name__)

# Таблица состояния файлов — атрибут LSMCollection.indexed.
STATE_TABLE = "indexed"

# Почты в индексаторе больше нет. Её данные от прежних версий удаляются при
# старте: в них тексты писем.
_EMAIL_TABLE = "files_email"
_EMAIL_QUEUE = "emails"
_EMAIL_OFFSETS = "offsets.json"
# Ключи писем в LSM — `email_<папка>:<начало>:<конец>`; диапазон
# [`email_`, `email\``) — ровно они: «`» следует в ASCII за «_».
_EMAIL_KEYS = (b"email_", b"email`")


def remove_email_leftovers(cfg: "Config") -> None:
    """Удалить данные почты прежних версий: таблицу, очередь, позиции, ключи LSM."""
    leftovers = [path for path in _email_paths(cfg) if path.exists()]
    for path in leftovers:
        _remove(path)
    names = [path.name for path in leftovers]
    state = lsm_file(cfg.lsm_root, STATE_TABLE)
    if state.exists() and _has_email_keys(state):
        _delete_email_keys(state)
        names.append("ключи писем в LSM")
    if names:
        lg.info("удалены данные почты прежних версий: %s", ", ".join(names))


def _email_paths(cfg: "Config") -> list[Path]:
    root = Path(cfg.lsm_root)
    return [
        Path(cfg.ld_db_path) / f"{_EMAIL_TABLE}.lance",
        root / _EMAIL_QUEUE,
        *sorted(root.glob(f"{_EMAIL_QUEUE}.retired.*")),
        root / _EMAIL_OFFSETS,
        root / f"{_EMAIL_OFFSETS}.tmp",
    ]


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _has_email_keys(path: Path) -> bool:
    # Выборка читается целиком: открытый курсор вместе с записью на одном
    # хендле lsm не допускает.
    with LSM(str(path)) as db:
        return bool(list(db[_EMAIL_KEYS[0] : _EMAIL_KEYS[1]]))


def _delete_email_keys(path: Path) -> None:
    with LSM(str(path)) as db:
        db.delete_range(*_EMAIL_KEYS)
