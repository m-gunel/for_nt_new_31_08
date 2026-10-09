import configparser
import os
from collections import deque
from pathlib import Path

from src.utils import get_logger

lg = get_logger("app.email")

# Корпоративный хост — предпочитаем его, если в профиле несколько аккаунтов.
_PREFERRED_HOST = "imap.sberbank.ru"

# Корень, о выборе которого уже сказали в лог: find_email_root зовут на каждом
# поллинге, а сообщать о выборе между профилями достаточно при его смене.
_reported_root: Path | None = None


def _profile_ranks(tb_root: Path) -> dict[str, int]:
    """Ранг профиля по profiles.ini и installs.ini: 0 — профиль установки, 1 — Default=1.

    Профиль установки (`[Install…]`, секции installs.ini) Thunderbird и
    открывает; `Default=1` после обновления может остаться у прежнего профиля.
    Ключ — realpath каталога профиля; профилей без отметки в словаре нет.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(
            [tb_root / "profiles.ini", tb_root / "installs.ini"], encoding="utf-8"
        )
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    ranks: dict[str, int] = {}
    for name in parser.sections():
        section = parser[name]
        if name.startswith("Profile"):
            if section.get("Default") == "1" and section.get("Path"):
                ranks.setdefault(os.path.realpath(tb_root / section["Path"]), 1)
        elif section.get("Default"):
            # Абсолютный путь (IsRelative=0) pathlib при склейке оставит как есть.
            ranks[os.path.realpath(tb_root / section["Default"])] = 0
    return ranks


def _report_choice(root: Path, candidates: list[Path]) -> None:
    global _reported_root
    profiles = {c.parent.parent for c in candidates}
    if len(profiles) > 1 and root != _reported_root:
        _reported_root = root
        lg.info(
            "профилей Thunderbird с ImapMail несколько (%s), почта берётся из %s",
            len(profiles),
            root,
        )


def find_email_root(home: Path | None = None) -> Path | None:
    """Каталог с mbox-файлами почты в профиле Thunderbird.

    Ищем каталоги ``ImapMail/<host>`` и берём тот, где есть хотя бы одна
    пара ``<folder>`` + ``<folder>.msf`` (предпочитая корпоративный хост).
    Не по конкретной папке: «Отправленных», например, локально может не быть.
    """
    home = home or Path.home()
    tb_root = home / ".thunderbird"
    if not tb_root.is_dir():
        lg.warning("thunderbird profile not found at %s", tb_root)
        return None

    candidates: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(tb_root):
        if Path(dirpath).name == "ImapMail":
            candidates.extend(Path(dirpath) / host for host in sorted(dirnames))
            # глубже ImapMail спускаться незачем
            dirnames.clear()

    ranks = _profile_ranks(tb_root)
    # Между профилями — сначала тот, что открывает Thunderbird, затем по имени
    # каталога: порядок обхода каталогов не определён, и прежний профиль с тем
    # же хостом мог перехватить выбор. Внутри профиля порядок хостов прежний.
    candidates.sort(
        key=lambda p: (
            p.name != _PREFERRED_HOST,
            ranks.get(os.path.realpath(p.parent.parent), 2),
            str(p.parent.parent),
        )
    )
    for root in candidates:
        if list_email_folders(root):
            _report_choice(root, candidates)
            return root
    lg.warning(
        "no ImapMail folder with mbox files found under %s (checked %s candidates)",
        tb_root,
        len(candidates),
    )
    return None


def list_email_folders(root: Path) -> list[str]:
    """Почтовые папки под корнем: пары ``<name>`` + ``<name>.msf``.

    Подпапки Thunderbird хранит в каталоге ``<родитель>.sbd/``; их имена —
    пути от корня (``INBOX.sbd/Проекты``): ключ письма
    ``email_<папка>:<start>:<stop>`` и ``root / name`` такие имена допускают.
    """
    ret: list[str] = []
    # Уже обойдённые каталоги (realpath): .sbd-симлинк на предка иначе
    # зациклил бы обход.
    seen: set[str] = set()
    pending: deque[tuple[Path, str]] = deque([(root, "")])
    while pending:
        directory, rel = pending.popleft()
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = list(directory.iterdir())
            for f in entries:
                if f.name.endswith(".msf"):
                    name = f.name[:-4]
                    if (directory / name).is_file():
                        ret.append(rel + name)
            for f in entries:
                if f.name.endswith(".sbd") and f.is_dir():
                    pending.append((f, f"{rel}{f.name}/"))
        except OSError:
            continue
    return ret
