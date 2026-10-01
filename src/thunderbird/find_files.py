import os
from pathlib import Path

from src.utils import get_logger

lg = get_logger("app.email")

# Корпоративный хост — предпочитаем его, если в профиле несколько аккаунтов.
_PREFERRED_HOST = "imap.sberbank.ru"


def find_email_root(home: Path | None = None) -> Path | None:
    """Каталог с mbox-файлами почты в профиле Thunderbird.

    Ищем каталоги ``ImapMail/<host>`` и берём тот, где есть хотя бы одна
    пара ``<folder>`` + ``<folder>.msf`` (предпочитая корпоративный хост).

    Раньше корень определялся по файлу папки «Отправленные» (её имя в
    modified-UTF-7 было зашито в регулярку): у пользователя без локально
    синхронизированных «Отправленных» корень не находился, и почта молча
    не индексировалась, даже когда INBOX лежал на месте.
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

    candidates.sort(key=lambda p: p.name != _PREFERRED_HOST)
    for root in candidates:
        if list_email_folders(root):
            return root
    lg.warning(
        "no ImapMail folder with mbox files found under %s (checked %s candidates)",
        tb_root,
        len(candidates),
    )
    return None


def list_email_folders(root: Path) -> list[str]:
    ret = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return ret
    for f in entries:
        if f.name.endswith(".msf"):
            name = f.name[:-4]
            if (root / name).is_file():
                ret.append(name)
    return ret
