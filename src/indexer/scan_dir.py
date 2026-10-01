import os
from collections.abc import Callable, Generator
from pathlib import Path
from re import Pattern
from typing import Any

from src.common import FileMetadata
from src.parsers import check_has_parser
from src.utils.logging import get_logger

from .exclude import exclude_pattern

lg = get_logger("app.scanner")

# Типы файловых систем, которые считаем сетевыми. Индексировать сетевые
# ресурсы не нужно (решение по объёму работ), и это же снимает риск, что обход
# повиснет на таймауте недоступного сервера: домашний каталог с примонтированной
# сетевой шарой иначе останавливал бы весь проход. autofs здесь тоже: спуск в
# него ТРИГГЕРИТ монтирование, то есть обход сам создаёт себе сетевой ввод-вывод.
NETWORK_FS_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "afs",
        "ceph",
        "9p",
        "autofs",
        "fuse.sshfs",
        "fuse.davfs",
        "davfs",
    }
)

# Как часто сообщать о ходе обхода. Единственная строка в конце (см.
# FilesScanner.preindex) на домашнем каталоге означает десятки минут тишины,
# по которым не отличить работу от зависания.
_PROGRESS_EVERY_FILES = 1000


def network_mount_points() -> tuple[str, ...]:
    """Точки монтирования сетевых ФС по данным /proc/mounts.

    Вне Linux файла нет — тогда ограничение просто не действует и обход
    ведёт себя как раньше.
    """
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ()
    points: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fs_type = parts[1], parts[2]
        if fs_type in NETWORK_FS_TYPES:
            # В /proc/mounts пробелы и табуляции экранированы восьмеричными
            # последовательностями.
            points.append(mount_point.replace("\\040", " ").replace("\\011", "\t"))
    return tuple(points)


def _is_under(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def iter_file_metadata(
    root: str,
    exclude_patt: Pattern | None = exclude_pattern,
    check_parsers: bool = True,
    filter_func: Callable[[str], bool] | None = None,
    filter_meta: Callable[[FileMetadata], bool] | None = None,
    follow_symlinks: bool = False,
) -> Generator[FileMetadata, Any, None]:
    """Обойти дерево и отдать метаданные подходящих файлов.

    `follow_symlinks` — идти ли по символьным ссылкам. При включении обход
    защищён от петель и от повторного прохода одного дерева под разными
    путями: каждый каталог учитывается по паре (st_dev, st_ino), поэтому
    `a/b -> a` завершается, а bind-mount не удваивает индекс. Файлы при этом
    намеренно НЕ дедуплицируются по inode: ключ индекса — путь, и файл,
    доступный по двум путям, должен находиться по обоим. Снятие первого из
    них иначе выкидывало бы из индекса живой файл.
    """
    root_path = Path(root).expanduser()
    try:
        os.scandir(root_path).close()
    except OSError as exc:
        # Недоступный корень — не то же, что недоступный подкаталог: пропуск
        # подкаталога по правам норма, а корень означает, что весь путь не
        # индексируется. Молча это выглядит как «поиск ничего не находит».
        lg.warning(
            "путь сканирования %s недоступен (errno=%s %s) — пропущен",
            root_path,
            exc.errno,
            exc.strerror,
        )
        return

    # Счётчики, а не строка в лог на каждый пропуск: подкаталог без прав —
    # штатное явление, и предупреждение на каждый превратило бы лог в шум
    # (см. tests/test_scan_paths_diagnostics.py). Итог печатается один раз.
    found = 0
    dirs = 0
    denied = 0
    excluded = 0
    on_network = 0
    revisited = 0

    network_roots = network_mount_points()
    # Каталоги, уже побывавшие в обходе, по (устройство, inode).
    visited_dirs: set[tuple[int, int]] = set()

    # Use a stack to avoid recursion overhead; scandir is efficient per entry
    stack = [root_path]
    # Каталоги, найденные ЧЕРЕЗ символьную ссылку, откладываются до конца
    # обхода настоящих. Иначе выигрывает тот, кто попался первым, и в индекс
    # уходит путь через ссылку: снимут ссылку — и живой файл пропадёт из
    # выдачи, хотя сам он никуда не делся.
    deferred_links: list[Path] = []

    try:
        while stack or deferred_links:
            if not stack:
                stack.append(deferred_links.pop(0))
            current = stack.pop()
            if follow_symlinks:
                # Проверяем на входе в каталог, а не при постановке в стек:
                # к моменту, когда до отложенной ссылки дойдёт очередь,
                # настоящий каталог уже обойдён и помечен, и ссылка на него
                # отсеется как повтор. Петля `a/b -> a` отсекается тем же.
                try:
                    st_dir = current.stat()
                except OSError:
                    denied += 1
                    continue
                key = (st_dir.st_dev, st_dir.st_ino)
                if key in visited_dirs:
                    revisited += 1
                    continue
                visited_dirs.add(key)
            dirs += 1
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        # Push subdirectories for traversal
                        try:
                            is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                        except OSError:
                            # Permission or race condition; skip
                            denied += 1
                            continue
                        if is_dir:
                            # исключаем мусор
                            if exclude_patt is not None and exclude_patt.match(
                                Path(entry.path).name
                            ):
                                excluded += 1
                                continue
                            # Не уходим на сетевые ресурсы: они вне задачи и
                            # способны подвесить обход на сетевом таймауте.
                            if network_roots and _is_under(entry.path, network_roots):
                                on_network += 1
                                continue
                            if follow_symlinks and entry.is_symlink():
                                # Откладываем: сначала настоящие пути.
                                deferred_links.append(Path(entry.path))
                                continue
                            stack.append(Path(entry.path))
                            continue

                        try:
                            if not entry.is_file(follow_symlinks=follow_symlinks):
                                continue
                        except OSError:
                            denied += 1
                            continue
                        if exclude_patt is not None and exclude_patt.match(
                            Path(entry.path).name
                        ):
                            excluded += 1
                            continue
                        # Build absolute path once; DirEntry.path is already absolute if given absolute root
                        abs_path = os.path.abspath(entry.path)

                        # Apply custom filter if specified (before expensive stat call)
                        if filter_func and not filter_func(abs_path):
                            continue
                        if check_parsers and not check_has_parser(Path(abs_path)):
                            continue
                        # Single stat call per entry; efficient with scandir
                        try:
                            st = entry.stat(follow_symlinks=follow_symlinks)
                        except OSError:
                            denied += 1
                            continue

                        mtime_seconds = int(st.st_mtime)

                        meta = FileMetadata(
                            path=abs_path,
                            size=st.st_size,
                            mtime_seconds=mtime_seconds,
                        )
                        if filter_meta is not None:
                            if not filter_meta(meta):
                                continue
                        found += 1
                        if found % _PROGRESS_EVERY_FILES == 0:
                            lg.info(
                                "обход %s: найдено файлов %s, каталогов пройдено %s",
                                root_path,
                                found,
                                dirs,
                            )
                        yield meta
            except OSError:
                # Skip directories that can't be opened
                denied += 1
                continue
            except Exception:
                lg.exception("iter_file_metadata")
                raise
    finally:
        # На уровне INFO: пропуск по правам — норма, и WARNING здесь сделал бы
        # красным каждый обход домашнего каталога.
        lg.info(
            "обход %s завершён: файлов %s, каталогов %s; пропущено — "
            "по правам %s, по исключениям %s, на сетевых ФС %s, повторных %s",
            root_path,
            found,
            dirs,
            denied,
            excluded,
            on_network,
            revisited,
        )
