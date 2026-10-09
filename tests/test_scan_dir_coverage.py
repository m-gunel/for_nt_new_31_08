"""Охват обхода ФС: символьные ссылки, сетевые ФС, отчёт обхода, exclude.py."""

import io
import logging
from itertools import islice
from pathlib import Path

import pytest

from src.config import Config
from src.indexer import scan_dir
from src.indexer.exclude import check_path, exclude_pattern
from src.indexer.scan_dir import _is_under, iter_file_metadata, network_mount_points
from src.indexer.scanners import files_scanner
from src.utils.lsm_adapter import LSMTable, _decode_file_meta, _encode_file_meta


@pytest.fixture(autouse=True)
def _no_network_mounts(monkeypatch):
    """Обход не должен зависеть от таблицы монтирования прогонной машины.

    iter_file_metadata читает настоящий /proc/mounts, и TMPDIR под autofs или
    NFS не дал бы спуститься в tmp_path. Тесты самой network_mount_points зовут
    её по импортированному имени, и подмена атрибута scan_dir их не задевает.
    """
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: ())


@pytest.fixture
def lsm_table(tmp_path):
    """Хранилище для штатной сборки FilesScanner (как в test_scan_paths_diagnostics)."""
    with LSMTable(
        tmp_path / "lsm.sqlite",
        encoder=_encode_file_meta,
        decoder=_decode_file_meta,
    ) as table:
        yield table


def _mkfile(path: Path, text: str = "текст") -> Path:
    """Создать файл вместе с недостающими каталогами."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _scan(root: Path, **kwargs) -> list[str]:
    """Найденные файлы как отсортированные пути относительно root."""
    metas = list(iter_file_metadata(str(root), **kwargs))
    return sorted(Path(m.path).relative_to(root).as_posix() for m in metas)


def test_directory_behind_symlink_is_scanned_only_when_asked(tmp_path):
    """Каталог за ссылкой обходится только при follow_symlinks=True.

    Умолчание параметра — False, чтобы прямые вызовы не меняли поведение
    молча; демон включает ссылки через конфиг.
    """
    outside = tmp_path / "outside"
    _mkfile(outside / "doc.txt")
    root = tmp_path / "scan"
    _mkfile(root / "own.txt")
    (root / "link").symlink_to(outside, target_is_directory=True)

    assert _scan(root, follow_symlinks=True) == ["link/doc.txt", "own.txt"]
    # По умолчанию и при явном False ссылки не обходятся.
    assert _scan(root) == ["own.txt"]
    assert _scan(root, follow_symlinks=False) == ["own.txt"]


def test_symlink_loop_does_not_hang_the_scan(tmp_path):
    """Петля `docs/loop -> root` обязана завершаться.

    Без учёта каталогов по (st_dev, st_ino) обход спускался бы по петле
    бесконечно.
    """
    root = tmp_path / "scan"
    _mkfile(root / "docs" / "doc.txt")
    (root / "docs" / "loop").symlink_to(root, target_is_directory=True)

    # islice — чтобы при петле тест упал, а не завис.
    metas = list(islice(iter_file_metadata(str(root), follow_symlinks=True), 20))
    found = sorted(Path(m.path).relative_to(root).as_posix() for m in metas)

    assert found == ["docs/doc.txt"]


def test_two_symlinks_to_one_directory_do_not_duplicate_files(tmp_path):
    """Одно дерево под двумя ссылками — один комплект файлов, не два.

    Каталог отмечается по (st_dev, st_ino) ПРИ ВХОДЕ, поэтому вторая ссылка
    на уже обойдённый каталог отсеивается как повтор. Иначе каждый файл
    оказался бы в индексе столько раз, сколько на каталог ведёт ссылок.
    """
    outside = tmp_path / "outside"
    _mkfile(outside / "doc.txt")
    root = tmp_path / "scan"
    root.mkdir()
    (root / "l1").symlink_to(outside, target_is_directory=True)
    (root / "l2").symlink_to(outside, target_is_directory=True)

    found = _scan(root, follow_symlinks=True)

    # Какая из двух ссылок попадётся первой — дело порядка scandir, но файл
    # обязан быть ровно один.
    assert found in (["l1/doc.txt"], ["l2/doc.txt"]), found


def test_real_path_wins_over_path_through_symlink(tmp_path):
    """Каталог, доступный и напрямую, и через ссылку, индексируется по настоящему пути.

    Каталоги за ссылками откладываются в deferred_links и обходятся после
    настоящих: иначе в индекс мог уйти путь через ссылку, и её снятие выкинуло
    бы из выдачи живой файл.
    """
    root = tmp_path / "scan"
    _mkfile(root / "real" / "doc.txt")
    (root / "link").symlink_to(root / "real", target_is_directory=True)

    assert _scan(root, follow_symlinks=True) == ["real/doc.txt"]


def test_symlinked_file_is_indexed_by_its_own_path(tmp_path):
    """Ссылка на ОТДЕЛЬНЫЙ файл индексируется, файлы по inode не схлопываются.

    Дедупликация каталогов не должна распространяться на файлы: ключ индекса —
    путь, и файл, доступный по двум путям, обязан находиться по обоим. Иначе
    удаление одной из ссылок выкидывало бы из индекса живой файл.
    """
    root = tmp_path / "scan"
    _mkfile(root / "target.txt", "содержимое")
    (root / "alias.txt").symlink_to(root / "target.txt")

    metas = {
        Path(m.path).name: m
        for m in iter_file_metadata(str(root), follow_symlinks=True)
    }

    assert sorted(metas) == ["alias.txt", "target.txt"]
    # stat идёт по ссылке: размер — у цели, а не у самой ссылки.
    assert metas["target.txt"].size > 0
    assert metas["alias.txt"].size == metas["target.txt"].size
    # При выключенных ссылках алиас не берётся.
    assert _scan(root) == ["target.txt"]


_PROC_MOUNTS = r"""proc /proc proc rw,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
server:/export /mnt/nfs nfs4 rw,relatime 0 0
//srv/share /mnt/win\040share cifs rw,relatime 0 0
user@host:/ /home/u/remote fuse.sshfs rw,nosuid 0 0
systemd-1 /net autofs rw,relatime 0 0
огрызок строки
"""


def _fake_proc_mounts(monkeypatch, content: str) -> None:
    """Подменить /proc/mounts через атрибут `open` модуля — тест идёт и на macOS."""

    def _open(path, *args, **kwargs):
        assert str(path) == "/proc/mounts", path
        return io.StringIO(content)

    monkeypatch.setattr(scan_dir, "open", _open, raising=False)


def test_network_mount_points_parses_proc_mounts(monkeypatch):
    """Сетевые точки монтирования вынимаются из /proc/mounts, локальные — нет.

    Спуск в сетевую шару вставал бы на её таймауте и останавливал весь проход.
    autofs тоже считается сетевым: спуск в него сам запускает монтирование.
    """
    _fake_proc_mounts(monkeypatch, _PROC_MOUNTS)

    points = network_mount_points()

    assert points == (
        "/mnt/nfs",
        # \040 — экранированный пробел из /proc/mounts, разворачивается.
        "/mnt/win share",
        "/home/u/remote",
        "/net",
    )
    # ext4, tmpfs и proc — локальные.
    assert "/" not in points
    assert "/run" not in points
    assert "/proc" not in points


def test_network_mount_points_empty_without_proc_mounts(monkeypatch):
    """Без /proc/mounts (macOS, контейнер без procfs) ограничение не действует."""

    def _no_file(*args, **kwargs):
        raise OSError(2, "No such file or directory", "/proc/mounts")

    monkeypatch.setattr(scan_dir, "open", _no_file, raising=False)

    assert network_mount_points() == ()


def test_is_under_matches_whole_path_components():
    """Сосед по имени не считается лежащим под точкой монтирования.

    Сравнение голым startswith отправило бы /mnt/nfs2 в «сетевые» вслед за
    /mnt/nfs — и локальный каталог целиком выпал бы из индекса.
    """
    roots = ("/mnt/nfs", "/home/u/remote/")

    assert _is_under("/mnt/nfs", roots)
    assert _is_under("/mnt/nfs/dir/file.txt", roots)
    assert _is_under("/home/u/remote/docs", roots)
    assert not _is_under("/mnt/nfs2/docs", roots)
    assert not _is_under("/home/u/remote2", roots)
    assert not _is_under("/mnt", roots)


def test_scan_does_not_descend_into_network_mount(tmp_path, monkeypatch):
    """Каталог под точкой монтирования сетевой ФС в обход не берётся.

    network_mount_points подменён: настоящую сетевую шару в тесте не поднять.
    """
    root = tmp_path / "scan"
    _mkfile(root / "local" / "doc.txt")
    _mkfile(root / "share" / "remote.txt")
    monkeypatch.setattr(
        scan_dir, "network_mount_points", lambda: (str(root / "share"),)
    )

    assert _scan(root) == ["local/doc.txt"]


def test_scan_summary_lands_in_info_with_counters(tmp_path, caplog):
    """Итог обхода — одна строка INFO со счётчиками, без единого WARNING.

    Пропуск подкаталога по правам штатен, и WARNING делал бы красным каждый
    обход домашнего каталога (см. tests/test_scan_paths_diagnostics.py).
    """
    root = tmp_path / "scan"
    _mkfile(root / "doc1.txt")
    _mkfile(root / "sub" / "doc2.txt")
    _mkfile(root / "__pycache__" / "junk.txt")
    _mkfile(root / "readme.md")  # парсера нет — в найденные не попадает

    with caplog.at_level(logging.INFO, logger="app.scanner"):
        found = _scan(root)

    assert found == ["doc1.txt", "sub/doc2.txt"]
    records = [r for r in caplog.records if r.name == "app.scanner"]
    summary = [r for r in records if "завершён" in r.getMessage()]
    assert len(summary) == 1, "итоговая сводка обхода должна быть ровно одна"
    assert summary[0].levelno == logging.INFO
    msg = summary[0].getMessage()
    assert str(root) in msg
    for part in (
        "файлов 2",
        "каталогов 2",
        "по правам 0",
        "по исключениям 1",  # __pycache__
        "на сетевых ФС 0",
        "повторных 0",
    ):
        assert part in msg, msg
    assert not [r for r in records if r.levelno >= logging.WARNING], (
        "успешный обход обязан молчать на WARNING"
    )


def test_progress_is_logged_during_long_scan(tmp_path, caplog, monkeypatch):
    """Долгий обход отчитывается по ходу, а не только в конце.

    Иначе десятки минут обхода домашнего каталога не отличить от зависания.
    Шаг занижен: настоящий _PROGRESS_EVERY_FILES потребовал бы тысяч файлов.
    """
    monkeypatch.setattr(scan_dir, "_PROGRESS_EVERY_FILES", 2)
    root = tmp_path / "scan"
    for i in range(5):
        _mkfile(root / f"doc{i}.txt")

    with caplog.at_level(logging.INFO, logger="app.scanner"):
        found = _scan(root)

    assert len(found) == 5
    records = [r for r in caplog.records if r.name == "app.scanner"]
    progress = [r for r in records if "найдено файлов" in r.getMessage()]
    assert len(progress) == 2, [r.getMessage() for r in records]
    assert all(r.levelno == logging.INFO for r in progress)
    assert not [r for r in records if r.levelno >= logging.WARNING]


def test_config_follows_symlinks_by_default(config):
    """Демон идёт по ссылкам без правки конфига.

    Умолчание iter_file_metadata — False ради прямых вызовов, поэтому ссылки
    включаются только конфигом.
    """
    assert config.file_scan_cfg.follow_symlinks is True


@pytest.mark.asyncio
async def test_files_scanner_passes_follow_symlinks_from_config(
    tmp_path, monkeypatch, config, lsm_table
):
    """FilesScanner пробрасывает флаг из конфига, а не зашивает своё значение.

    Сканер поднимается штатно, а не через `__new__`: иначе тест зависел бы от
    того, какие атрибуты трогает preindex.
    """
    root = tmp_path / "scan"
    root.mkdir()
    calls: list[dict] = []

    def _recording(path, **kwargs):
        calls.append(kwargs)
        return iter(())

    monkeypatch.setattr(files_scanner, "iter_file_metadata", _recording)
    monkeypatch.setattr(Config, "scan_paths", property(lambda self: [str(root)]))

    async with files_scanner.FilesScanner(
        lsm_table, queue_path=tmp_path / "queue", reset=True
    ) as scanner:
        monkeypatch.setattr(config.file_scan_cfg, "follow_symlinks", True)
        assert [m async for m in scanner.preindex()] == []
        monkeypatch.setattr(config.file_scan_cfg, "follow_symlinks", False)
        assert [m async for m in scanner.preindex()] == []

    assert [c["follow_symlinks"] for c in calls] == [True, False]


def test_virtualenv_and_snap_dirs_are_excluded():
    """venv, site-packages и snap исключены.

    `\\..+` ловит только .venv, а сотни тысяч .txt и .py из пакетов вытесняли
    бы из выдачи документы пользователя.
    """
    for name in ("venv", "site-packages", "snap"):
        assert exclude_pattern.match(name), name


def test_previously_known_garbage_stays_excluded():
    """Остальные группы шаблона на месте: пропажа любой заметна иначе только
    по распухшему индексу."""
    for name in (
        "node_modules",
        "__pycache__",
        "env",
        "dist",
        "build",
        "Thumbs.db",
        "~$отчёт.docx",
        "~WRL0001.tmp",
    ):
        assert exclude_pattern.match(name), name


def test_hidden_names_are_covered_by_single_group():
    """Скрытые имена держатся на одной группе `\\..+` — она обязана покрывать всё.

    Отдельно важны .files-search и .ragsearch: это каталоги самого демона, и
    индексировать свой индекс нельзя.
    """
    for name in (
        ".venv",
        ".git",
        ".svn",
        ".hg",
        ".idea",
        ".vscode",
        ".DS_Store",
        ".Trash-1000",
        ".files-search",
        ".ragsearch",
        ".cache",
    ):
        assert exclude_pattern.match(name), name


def test_pattern_stays_anchored_at_both_ends():
    """Якоря ^ и $ отделяют мусор от рабочих каталогов.

    Без `$` в исключения уехали бы envelope, venv2, snapshot, distribution, и
    документы пользователя молча пропали бы из поиска.
    """
    for name in (
        "envelope",
        "venv2",
        "snapshot",
        "buildings",
        "distribution",
        "site-packages-old",
        "myenv",
        "ThumbsXdb",
        "отчёт~",
        "отчёт.docx",
        "Документы",
    ):
        assert exclude_pattern.match(name) is None, name


def test_check_path_rejects_anything_under_excluded_dir():
    """check_path смотрит и на предков, а не только на имя самого файла.

    Иначе venv/lib/site-packages/pkg/readme.txt проходил бы проверку по имени
    «readme.txt», хотя лежит внутри исключённого каталога.
    """
    assert check_path(Path("/home/u/Документы/отчёт.docx")) is True
    assert check_path(Path("/home/u/venv/lib/site-packages/pkg/readme.txt")) is False
    # venv/site-packages/snap — на путях целиком, а не только по имени.
    assert check_path(Path("/home/u/snap/firefox/notes.txt")) is False
    assert check_path(Path("/home/u/proj/site-packages/pkg/readme.txt")) is False
    assert check_path(Path("/home/u/.cache/notes.txt")) is False
    assert check_path(Path("/home/u/docs/~$отчёт.docx")) is False


def _network_home_tree(tmp_path: Path) -> tuple[Path, Path]:
    """/home с домом u (файл в корне дома и два подкаталога) и чужим домом v."""
    home = tmp_path / "home"
    u = home / "u"
    _mkfile(u / "top.txt")
    _mkfile(u / "Documents" / "report.txt")
    _mkfile(u / "Desktop" / "plan.txt")
    _mkfile(home / "v" / "other.txt")
    return home, u


_HOME_U = ["u/Desktop/plan.txt", "u/Documents/report.txt", "u/top.txt"]


def test_network_home_under_home_root_is_scanned(tmp_path, monkeypatch):
    """Дом пользователя на сетевой ФС обходится целиком из корня /home.

    Штатный корень пакета — /home. Сетевой дом бывает и под autofs-/home, и
    отдельным NFS-монтированием в локальном /home; раньше в обоих случаях
    отбрасывался каждый подкаталог и индекс оставался пустым. Чужой сетевой
    дом по-прежнему не обходится.
    """
    home, u = _network_home_tree(tmp_path)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (str(u),), raising=False)

    # /home — точка autofs, дома под ним — NFS.
    monkeypatch.setattr(
        scan_dir,
        "network_mount_points",
        lambda: (str(home), str(u), str(home / "v")),
    )
    assert _scan(home, follow_symlinks=True) == _HOME_U

    # /home локальный, дом — отдельное NFS-монтирование.
    monkeypatch.setattr(
        scan_dir, "network_mount_points", lambda: (str(u), str(home / "v"))
    )
    assert _scan(home, follow_symlinks=True) == _HOME_U


def test_root_on_network_fs_is_scanned_whole(tmp_path, monkeypatch):
    """Корень, лежащий на сетевой ФС, обходится целиком, а не только верхний уровень.

    Путь выбран пользователем (`~` на NFS или папка на шаре): отбрасывать его
    подкаталоги как «сетевые» значит молча индексировать только файлы в корне.
    """
    home, u = _network_home_tree(tmp_path)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (), raising=False)

    # `~` = /home/u, и это само NFS-монтирование.
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: (str(u),))
    assert _scan(u, follow_symlinks=True) == [
        "Desktop/plan.txt",
        "Documents/report.txt",
        "top.txt",
    ]

    # Папка на шаре, примонтированной выше корня.
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: (str(home),))
    assert _scan(u, follow_symlinks=True) == [
        "Desktop/plan.txt",
        "Documents/report.txt",
        "top.txt",
    ]


def test_share_mounted_inside_network_home_stays_excluded(tmp_path, monkeypatch):
    """Исключение сетевых шар внутри дерева не ослабло: дом разрешён, шара в нём — нет."""
    home, u = _network_home_tree(tmp_path)
    _mkfile(u / "remote" / "share.txt")
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (str(u),), raising=False)
    monkeypatch.setattr(
        scan_dir, "network_mount_points", lambda: (str(u), str(u / "remote"))
    )

    assert _scan(u, follow_symlinks=True) == [
        "Desktop/plan.txt",
        "Documents/report.txt",
        "top.txt",
    ]


def test_user_homes_cover_both_sberos_forms(monkeypatch):
    """Оба дома доменного пользователя SberOS считаются его домами.

    pw_dir — /home/ДОМЕН@ЛОГИН, HOME терминала — /home/work/ЛОГИН@ДОМЕН; любой из
    них может оказаться сетевым монтированием под корнем /home.
    """
    import pwd
    from types import SimpleNamespace

    monkeypatch.setenv("HOME", "/home/work/ivanov@sigma.sbrf.ru")
    monkeypatch.setattr(
        pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(
            pw_name="ivanov@sigma.sbrf.ru", pw_dir="/home/sigma.sbrf.ru@ivanov"
        ),
    )

    homes = scan_dir._user_homes()

    assert "/home/sigma.sbrf.ru@ivanov" in homes
    assert "/home/work/ivanov@sigma.sbrf.ru" in homes


def test_symlink_to_network_mount_is_not_followed(tmp_path, monkeypatch):
    """Ссылка на сетевую шару не уводит обход на шару.

    Сверяется цель ссылки, а не её путь: иначе `~/Сетевой диск -> /mnt/nfs`
    проходил проверку и шара обходилась целиком при штатном follow_symlinks.
    """
    mnt = tmp_path / "mnt_nfs"
    _mkfile(mnt / "docs" / "remote.txt")
    _mkfile(mnt / "file.txt")
    root = tmp_path / "lhome"
    _mkfile(root / "local.txt")
    (root / "Сетевой диск").symlink_to(mnt, target_is_directory=True)
    (root / "docs-link").symlink_to(mnt / "docs", target_is_directory=True)
    (root / "rel-link").symlink_to(Path("..") / "mnt_nfs", target_is_directory=True)
    (root / "remote.txt").symlink_to(mnt / "file.txt")
    # Цепочка: ссылка на локальную ссылку, ведущую на шару.
    (tmp_path / "hop").symlink_to(mnt, target_is_directory=True)
    (root / "chain").symlink_to(tmp_path / "hop", target_is_directory=True)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (), raising=False)
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: (str(mnt),))

    assert _scan(root, follow_symlinks=True) == ["local.txt"]


def test_symlink_to_network_is_rejected_before_stat_of_target(
    tmp_path, monkeypatch, caplog
):
    """Ссылка на шару отсекается до stat цели.

    Недоступная цель (на живой машине — зависший сервер NFS) не мешает
    признать ссылку сетевой: она учитывается в счётчике «на сетевых ФС», а не
    проваливается в stat, который на hard-mount висел бы.
    """
    mnt = tmp_path / "mnt_nfs"  # не создаётся: цель недоступна
    root = tmp_path / "lhome"
    _mkfile(root / "local.txt")
    (root / "Сетевой диск").symlink_to(mnt / "docs", target_is_directory=True)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (), raising=False)
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: (str(mnt),))

    with caplog.at_level(logging.INFO, logger="app.scanner"):
        assert _scan(root, follow_symlinks=True) == ["local.txt"]

    summary = [r.getMessage() for r in caplog.records if "завершён" in r.getMessage()]
    assert len(summary) == 1
    assert "на сетевых ФС 1" in summary[0], summary[0]


def test_check_path_with_roots_checks_only_below_root():
    """С корнями check_path сверяет только компоненты ниже корня.

    Полный проход сам корень и его предков не проверяет и корни вида ~/Build,
    ~/snap/<app>/common/Документы обходит; watcher обязан пропускать их события.
    Без корней правило прежнее — все компоненты вплоть до «/».
    """
    build = Path("/home/u/Build")
    assert check_path(build / "report.docx", [build]) is True
    assert check_path(build, [build]) is True
    snap_docs = Path("/home/u/snap/app/common/Документы")
    assert check_path(snap_docs / "a.docx", [str(snap_docs)]) is True
    wine_docs = Path("/home/u/.wine/drive_c/docs")
    assert check_path(wine_docs / "a.docx", [wine_docs]) is True
    # Ниже корня исключения действуют как прежде.
    assert check_path(build / "venv" / "lib.txt", [build]) is False
    assert check_path(build / "~$отчёт.docx", [build]) is False
    # Под несколькими корнями достаточно одного, который путь пропускает.
    assert check_path(build / "report.docx", ["/home", build]) is True
    assert check_path(build / "report.docx", ["/home"]) is False
    # Без корней и вне корней — все компоненты.
    assert check_path(build / "report.docx") is False
    assert check_path(build / "report.docx", ["/srv"]) is False


def test_home_symlinked_to_network_dir_is_still_scanned(tmp_path, monkeypatch):
    """Дом-ссылка на сетевой каталог (/home/u -> /net/srv/home/u) обходится, как раньше.

    Ссылки на сетевые шары отсекаются по цели, и без сверки настоящего пути дома
    отсёкся бы и сам дом.
    """
    net_home = tmp_path / "net" / "srv" / "u"
    _mkfile(net_home / "top.txt")
    _mkfile(net_home / "Documents" / "report.txt")
    home = tmp_path / "home"
    home.mkdir()
    u = home / "u"
    u.symlink_to(net_home, target_is_directory=True)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (str(u),), raising=False)
    monkeypatch.setattr(
        scan_dir,
        "network_mount_points",
        lambda: (str(tmp_path / "net"), str(net_home)),
    )

    assert _scan(home, follow_symlinks=True) == ["u/Documents/report.txt", "u/top.txt"]


def test_links_inside_root_on_share_are_followed(tmp_path, monkeypatch):
    """Корень — ссылка на папку шары: ссылки внутри той же шары обходятся, как раньше.

    Пользователь сам выбрал этот путь, и точка монтирования под ним разрешена
    по настоящему пути корня.
    """
    mnt = tmp_path / "mnt"
    _mkfile(mnt / "projects" / "plan.txt")
    _mkfile(mnt / "other" / "x.txt")
    (mnt / "projects" / "other-link").symlink_to(
        mnt / "other", target_is_directory=True
    )
    root = tmp_path / "Проекты"
    root.symlink_to(mnt / "projects", target_is_directory=True)
    monkeypatch.setattr(scan_dir, "_user_homes", lambda: (), raising=False)
    monkeypatch.setattr(scan_dir, "network_mount_points", lambda: (str(mnt),))

    assert _scan(root, follow_symlinks=True) == ["other-link/x.txt", "plan.txt"]


@pytest.mark.parametrize("root_spec", ["home", "~"])
def test_local_scan_unchanged_when_network_mounts_exist_elsewhere(
    tmp_path, monkeypatch, caplog, root_spec
):
    """Обычный локальный обход не меняется от сетевых монтирований вне дерева.

    На АРМ в /proc/mounts почти всегда есть сетевые точки (/net, шары в /mnt).
    Локальный /home или `~` с вложенными папками и ссылками на локальные
    каталоги и файлы обязан давать ту же выдачу, что без них, а события
    watcher'а по найденным файлам — проходить check_path. Дома пользователя
    берутся настоящие, без подмены. Проходит и до правки обхода.
    """
    home = tmp_path / "home"
    u = home / "u"
    _mkfile(u / "top.txt")
    _mkfile(u / "Documents" / "report.txt")
    _mkfile(u / "Documents" / "sub" / "deep" / "x.txt")
    _mkfile(u / "venv" / "lib.txt")  # мусор исключается, как прежде
    _mkfile(tmp_path / "data" / "projects" / "plan.txt")
    _mkfile(tmp_path / "data" / "rel_target" / "r.txt")
    (u / "Проекты").symlink_to(tmp_path / "data" / "projects", target_is_directory=True)
    (u / "rel").symlink_to(
        Path("..") / ".." / "data" / "rel_target", target_is_directory=True
    )
    (u / "alias.txt").symlink_to(u / "Documents" / "report.txt")
    (u / "Documents" / "loop").symlink_to(u, target_is_directory=True)
    mnt = tmp_path / "mnt_nfs"
    _mkfile(mnt / "remote.txt")
    monkeypatch.setenv("HOME", str(u))
    root = u if root_spec == "~" else home
    raw = "~" if root_spec == "~" else str(home)

    def scan() -> list[str]:
        metas = list(iter_file_metadata(raw, follow_symlinks=True))
        return sorted(Path(m.path).relative_to(root).as_posix() for m in metas)

    baseline = scan()
    monkeypatch.setattr(
        scan_dir,
        "network_mount_points",
        lambda: (str(mnt), "/net/srv", "/mnt/share"),
    )
    with caplog.at_level(logging.INFO, logger="app.scanner"):
        found = scan()

    prefix = "u/" if root_spec == "home" else ""
    expected = sorted(
        prefix + p
        for p in (
            "top.txt",
            "alias.txt",
            "Documents/report.txt",
            "Documents/sub/deep/x.txt",
            "Проекты/plan.txt",
            "rel/r.txt",
        )
    )
    assert baseline == expected
    assert found == expected
    summary = [r.getMessage() for r in caplog.records if "завершён" in r.getMessage()]
    assert len(summary) == 1 and "на сетевых ФС 0" in summary[0], summary
    for rel in found:
        assert check_path(root / rel) is True, rel
