"""Подготовка хранилища индекса при старте — до того, как его откроет Index.

Здесь ещё ничего не открыто (LSM, очереди, таблицы LanceDB), поэтому
хранилище можно править на уровне файлов, не трогая живые хендлы.
"""

import dataclasses
import json
import os
import shutil
import time
import typing
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import lancedb
from lsm import LSM

from src.common import chunk_with_meta_schema
from src.utils import get_logger
from src.utils.lsm_adapter import lsm_file
from src.utils.openvino_encoder import model_fingerprint
from src.version import __version__

if typing.TYPE_CHECKING:
    from src.config import Config

lg = get_logger(__name__)

# Таблица состояния файлов — атрибут LSMCollection.indexed.
STATE_TABLE = "indexed"

# Поднимать, когда индекс прежней версии нельзя использовать как есть при той же
# схеме (другие нарезка или нормализация текста): демон сам соберёт его заново.
INDEX_FORMAT = 1
STAMP_FILE = "index_version.json"
# Повреждённое хранилище переносится сюда; хранится только последняя копия.
CORRUPT_DIR = "corrupt"
# Колонки, без которых индекс не годен: схема чанка и вектор.
_REQUIRED_COLUMNS = frozenset({*chunk_with_meta_schema.names, "vector"})
# Строки писем в таблице файлов — от версий до разделения таблиц.
_LEGACY_EMAIL_ROWS = "starts_with(path, 'email_')"

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


@dataclass(frozen=True)
class IndexStamp:
    """С чем собран индекс — содержимое файла версии."""

    format: int
    model: str
    model_fingerprint: str
    embed_dim: int

    def changes_since(self, stored: "IndexStamp") -> list[str]:
        """Что изменилось с stored из того, от чего зависят векторы и нарезка.

        Имя каталога модели само по себе не в счёт: переименование без замены
        файлов векторы не меняет.
        """
        changes = []
        if stored.format != self.format:
            changes.append(f"формат индекса {stored.format} → {self.format}")
        if stored.model_fingerprint != self.model_fingerprint:
            changes.append(
                f"модель {stored.model}@{stored.model_fingerprint} → "
                f"{self.model}@{self.model_fingerprint}"
            )
        if stored.embed_dim != self.embed_dim:
            changes.append(f"размерность {stored.embed_dim} → {self.embed_dim}")
        return changes


@dataclass(frozen=True)
class IndexProblem:
    """Почему индекс надо собрать заново."""

    reason: str
    # Повреждённые файлы: уходят в CORRUPT_DIR, а не удаляются.
    damaged: tuple[Path, ...] = ()


def ensure_index_usable(cfg: "Config") -> None:
    """Несовместимый или повреждённый индекс — собрать заново; настройки остаются."""
    root = Path(cfg.lsm_root)
    expected = expected_stamp(cfg)
    problem = _find_index_problem(cfg, expected)
    if problem is not None:
        lg.warning("индекс будет собран заново: %s", problem.reason)
        if problem.damaged:
            target = quarantine(problem.damaged, root / CORRUPT_DIR)
            lg.warning("повреждённые файлы индекса перенесены в %s", target)
        _wipe_index(cfg)
    write_stamp(root, expected)


def expected_stamp(cfg: "Config") -> IndexStamp:
    model_dir = Path(cfg.embed_model_cfg.model_path)
    return IndexStamp(
        format=INDEX_FORMAT,
        model=model_dir.name,
        model_fingerprint=model_fingerprint(model_dir),
        embed_dim=cfg.embed_dim,
    )


def _find_index_problem(cfg: "Config", expected: IndexStamp) -> IndexProblem | None:
    """Первая причина собрать индекс заново; None — индекс годен как есть."""
    return (
        _stamp_problem(Path(cfg.lsm_root), expected)
        or _table_problem(cfg, expected.embed_dim)
        or _state_problem(cfg)
    )


def _stamp_problem(root: Path, expected: IndexStamp) -> IndexProblem | None:
    stored = read_stamp(root)
    if stored is None or not (changes := expected.changes_since(stored)):
        return None
    return IndexProblem("сменились " + ", ".join(changes))


def _table_problem(cfg: "Config", embed_dim: int) -> IndexProblem | None:
    path = _table_dir(cfg)
    if not path.exists():
        return None
    try:
        table = lancedb.connect(cfg.ld_db_path).open_table(cfg.ld_files_table)
        schema = table.schema
        # Порча файлов данных видна только при чтении строк.
        table.head(1)
    except Exception as exc:  # любой сбой открытия и чтения — повреждение
        return IndexProblem(
            f"таблица {cfg.ld_files_table} не читается ({exc})", (path,)
        )
    if missing := sorted(_REQUIRED_COLUMNS - set(schema.names)):
        return IndexProblem(f"в индексе нет колонок {missing} — он старой версии")
    if (dim := schema.field("vector").type.list_size) != embed_dim:
        return IndexProblem(
            f"размерность векторов в индексе {dim}, у модели {embed_dim}"
        )
    if table.count_rows(_LEGACY_EMAIL_ROWS):
        return IndexProblem("в индексе остались письма версий до разделения таблиц")
    return None


def _state_problem(cfg: "Config") -> IndexProblem | None:
    path = lsm_file(cfg.lsm_root, STATE_TABLE)
    if not path.exists():
        return None
    try:
        with LSM(str(path)) as db:
            remembers_files = next(iter(db.keys()), None) is not None
    except Exception as exc:  # любой сбой чтения состояния — повреждение
        return IndexProblem(f"состояние LSM не читается ({exc})", _state_files(cfg))
    # Таблицу создаёт первый же старт; нет её — удалили вместе с индексом, и без
    # сброса файлы числились бы проиндексированными и не вернулись бы в него.
    # Пустая таблица — не признак: файлы без текста LSM помнит, а строк не дают.
    if remembers_files and not _table_dir(cfg).exists():
        return IndexProblem("LSM помнит файлы, а таблицы файлов нет")
    return None


def _table_dir(cfg: "Config") -> Path:
    return Path(cfg.ld_db_path) / f"{cfg.ld_files_table}.lance"


def _state_files(cfg: "Config") -> tuple[Path, ...]:
    # Вместе с журналами lsm (-log, -shm).
    state = lsm_file(cfg.lsm_root, STATE_TABLE)
    return tuple(sorted(state.parent.glob(f"{state.name}*")))


def _wipe_index(cfg: "Config") -> None:
    """Сбросить индекс на уровне файлов: таблицу, состояние LSM, хвосты очереди.

    Саму очередь файлов Index пересоздаёт на старте. Кэш OCR остаётся:
    распознанный текст годен при любой модели, а распознавание — самая долгая
    часть разбора.
    """
    retired = sorted(Path(cfg.lsm_root).glob("files.retired.*"))
    for path in (_table_dir(cfg), *_state_files(cfg), *retired):
        if path.exists():
            _remove(path)


def quarantine(paths: Sequence[Path], root: Path) -> Path:
    """Перенести повреждённое в root/<время>/; прежняя копия удаляется."""
    if root.exists():
        shutil.rmtree(root)
    target = root / time.strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True)
    for path in paths:
        shutil.move(path, target / path.name)
    return target


def read_stamp(root: Path) -> IndexStamp | None:
    """Файл версии; None — его нет или он не читается."""
    path = root / STAMP_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return IndexStamp(
            **{f.name: data[f.name] for f in dataclasses.fields(IndexStamp)}
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        lg.warning(
            "файл версии индекса %s не читается (%s) — индекс проверяется заново",
            path,
            exc,
        )
        return None


def write_stamp(root: Path, stamp: IndexStamp) -> None:
    """Записать файл версии атомарно: обрыв не оставит обрезанный файл."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / STAMP_FILE
    tmp = path.with_name(f"{STAMP_FILE}.tmp")
    # app_version — справка для сопровождения: какой версией собран индекс.
    data = {**dataclasses.asdict(stamp), "app_version": __version__}
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
