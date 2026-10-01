"""Охват обхода файловой системы: ссылки, границы устройства, слышимость.

Обход `iter_file_metadata` молча терял целые куски дерева:
* каталог за символьной ссылкой не индексировался вообще
  (`entry.is_dir(follow_symlinks=False)`), а пользователи держат за ссылками
  рабочие каталоги;
* включить ссылки нельзя было безопасно: петля `docs/loop -> root` крутилась
  бы вечно, а bind-mount/вторая ссылка удваивали бы индекс;
* обход уходил на примонтированную сетевую шару и вставал на её таймауте;
* о результатах прохода в лог не попадало ничего — пустой индекс невозможно
  было отличить от зависшего обхода.

Плюс `src/indexer/exclude.py`, который не был покрыт ни одним тестом: в нём
восемь альтернатив, дублировавших `\\..+`, заменены на реально нужные
`venv`/`site-packages`/`snap`.
"""

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

    iter_file_metadata читает настоящий /proc/mounts. Если TMPDIR окажется под
    autofs или NFS (корпоративный Linux с autofs-домами — обычное дело), обход
    не спустится в tmp_path и тесты про ссылки и счётчики упадут по причине,
    не имеющей отношения к проверяемому. Тесты самой network_mount_points
    зовут её по имени, импортированному в этот модуль, поэтому подмена
    атрибута в scan_dir их не задевает, а
    test_scan_does_not_descend_into_network_mount перекрывает подмену своей.
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


# ------------------------------------------------ ДЕФЕКТ 1: ссылки на каталоги


def test_directory_behind_symlink_is_scanned_only_when_asked(tmp_path):
    """Каталог за ссылкой раньше не индексировался вообще.

    `entry.is_dir(follow_symlinks=False)` для ссылки давал False, дальше её
    отвергала и проверка `is_file`, — то есть рабочий каталог, доступный
    пользователю через ссылку, в индекс не попадал ни одним файлом.
    Умолчание параметра осталось False намеренно: прямые вызовы (в том числе
    из старых тестов) не должны менять поведение молча.
    """
    outside = tmp_path / "outside"
    _mkfile(outside / "doc.txt")
    root = tmp_path / "scan"
    _mkfile(root / "own.txt")
    (root / "link").symlink_to(outside, target_is_directory=True)

    assert _scan(root, follow_symlinks=True) == ["link/doc.txt", "own.txt"]
    # Старое поведение сохранено и по умолчанию, и при явном False.
    assert _scan(root) == ["own.txt"]
    assert _scan(root, follow_symlinks=False) == ["own.txt"]


# ------------------------------------- ДЕФЕКТ 2: петли, дубли и настоящий путь


def test_symlink_loop_does_not_hang_the_scan(tmp_path):
    """Петля `docs/loop -> root` обязана завершаться.

    Без учёта каталогов по (st_dev, st_ino) включение ссылок означало бы
    бесконечный спуск scan/docs/loop/docs/loop/... — демон крутился бы на
    обходе вечно, а индексация не заканчивалась никогда.
    """
    root = tmp_path / "scan"
    _mkfile(root / "docs" / "doc.txt")
    (root / "docs" / "loop").symlink_to(root, target_is_directory=True)

    # islice — страховка от зависания самого теста: при потере visited_dirs
    # генератор бесконечен, и без ограничения тест не упал бы, а завис.
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

    Каталоги, найденные через ссылку, откладываются в deferred_links и
    обходятся после всех настоящих. Без этого выигрывал тот, кто попался
    первым: в индекс уходил путь scan/link/doc.txt, и снятие ссылки
    выкидывало из выдачи живой файл, который никуда не девался.
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
    # Прежнее поведение при выключенных ссылках сохранено.
    assert _scan(root) == ["target.txt"]


# --------------------------------------- ДЕФЕКТ 3: граница локального носителя

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
    """Подменить чтение /proc/mounts, чтобы тест шёл и на macOS.

    Атрибут `open` в модуле перекрывает встроенный: разработка идёт на macOS,
    где /proc/mounts нет, а проверять разбор надо именно там же, где пишут код.
    """

    def _open(path, *args, **kwargs):
        assert str(path) == "/proc/mounts", path
        return io.StringIO(content)

    monkeypatch.setattr(scan_dir, "open", _open, raising=False)


def test_network_mount_points_parses_proc_mounts(monkeypatch):
    """Сетевые точки монтирования вынимаются из /proc/mounts, локальные — нет.

    Раньше границы устройства не было вовсе: обход спускался в примонтированную
    шару и вставал на сетевом таймауте, останавливая весь проход по домашнему
    каталогу. autofs здесь тоже сетевой: спуск в него сам ТРИГГЕРИТ
    монтирование.
    """
    _fake_proc_mounts(monkeypatch, _PROC_MOUNTS)

    points = network_mount_points()

    assert points == (
        "/mnt/nfs",
        # \040 — экранированный пробел из /proc/mounts, его надо развернуть,
        # иначе точка монтирования с пробелом в имени не совпадёт ни с чем.
        "/mnt/win share",
        "/home/u/remote",
        "/net",
    )
    # ext4, tmpfs и proc — локальные, их в списке быть не должно.
    assert "/" not in points
    assert "/run" not in points
    assert "/proc" not in points


def test_network_mount_points_empty_without_proc_mounts(monkeypatch):
    """Без /proc/mounts (macOS, контейнер без procfs) ограничение просто не действует.

    Обход обязан вести себя как раньше, а не падать на отсутствующем файле.
    """

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

    network_mount_points подменён: настоящую сетевую шару в тесте не поднять,
    а проверять надо именно решение обхода — спускаться или нет.
    """
    root = tmp_path / "scan"
    _mkfile(root / "local" / "doc.txt")
    _mkfile(root / "share" / "remote.txt")
    monkeypatch.setattr(
        scan_dir, "network_mount_points", lambda: (str(root / "share"),)
    )

    assert _scan(root) == ["local/doc.txt"]


# ------------------------------------------------- ДЕФЕКТ 4: молчаливый обход


def test_scan_summary_lands_in_info_with_counters(tmp_path, caplog):
    """Итог обхода — одна строка INFO со счётчиками, без единого WARNING.

    Раньше обход не сообщал о себе ничего: пустой индекс было не отличить от
    зависшего прохода. Уровень принципиален — пропуск подкаталога по правам
    штатен, и WARNING делал бы красным каждый обход домашнего каталога
    (см. tests/test_scan_paths_diagnostics.py).
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

    Одна строка в самом конце на домашнем каталоге — это десятки минут
    тишины, по которым не отличить работу от зависания. Шаг занижен
    monkeypatch'ем: настоящий (_PROGRESS_EVERY_FILES) потребовал бы тысячи
    файлов на диске.
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


# ------------------------------------------- проводка флага до демона


def test_config_follows_symlinks_by_default(config):
    """Демон обязан идти по ссылкам без правки конфига.

    Умолчание самой iter_file_metadata осталось False ради прямых вызовов,
    поэтому единственное место, где ссылки включаются, — конфиг. Сбросится
    оно в False — и дефект 1 вернётся целиком, молча.
    """
    assert config.file_scan_cfg.follow_symlinks is True


@pytest.mark.asyncio
async def test_files_scanner_passes_follow_symlinks_from_config(
    tmp_path, monkeypatch, config, lsm_table
):
    """FilesScanner пробрасывает флаг из конфига, а не зашивает своё значение.

    Без проброса вся защита от петель и дублей оставалась бы мёртвым кодом:
    обход демона по-прежнему не заходил бы за ссылки.

    Сканер поднимается штатно, а не через `__new__`: иначе тест держался бы на
    том, что preindex трогает ровно `self.cfg`, и первая же строка про
    self.queue превратила бы его в AttributeError вместо осмысленного падения
    по существу проверки.
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


# ------------------------------------------------- ДЕФЕКТ 5: exclude.py


def test_virtualenv_and_snap_dirs_are_excluded():
    """venv/site-packages/snap уезжали в индекс тысячами файлов из состава пакетов.

    В шаблоне их не было: `\\..+` ловит только .venv, а окружение без точки и
    ~/snap обходились целиком — сотни тысяч .txt и .py из пакетов вытесняли
    из выдачи настоящие документы пользователя.
    """
    for name in ("venv", "site-packages", "snap"):
        assert exclude_pattern.match(name), name


def test_previously_known_garbage_stays_excluded():
    """Чистка восьми мёртвых альтернатив не должна была задеть живые.

    Тест ХАРАКТЕРИЗАЦИОННЫЙ: на старом шаблоне он тоже зелёный — все эти имена
    ловились и до правки. Сам дефект стерегут кейсы на venv/site-packages/snap
    (тест выше и последний тест файла), а здесь фиксируется, что рефакторинг,
    убравший восемь альтернатив, не задел живые группы. Файл не был покрыт ни одним
    тестом, и пропажа любой из них заметна иначе только по распухшему индексу.
    """
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

    Тоже характеризационный: группа `\\..+` была в шаблоне всегда, так что на
    старом коде тест зелёный. Он страхует ровно удаление восьми дублей —
    .venv/.git/.svn/.hg/.idea/.vscode/.DS_Store/.Trash*. Отдельно важны
    .files-search и .ragsearch: это собственные каталоги демона, и
    индексировать свой индекс нельзя — иначе обход кормит сам себя.
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
    """Якоря ^ и $ — единственное, что отделяет мусор от рабочих каталогов.

    Уберут `$` — и в мусор уедут envelope, venv2, snapshot, distribution:
    пользователь молча лишится своих каталогов в поиске и никак не узнает,
    почему документы не находятся.
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
    «readme.txt» и попадал в индекс, хотя лежит внутри исключённого каталога.
    Кейсы на venv/site-packages/snap стерегут именно дефект: на старом шаблоне
    этих трёх альтернатив не было, и все три пути возвращали True.
    """
    assert check_path(Path("/home/u/Документы/отчёт.docx")) is True
    assert check_path(Path("/home/u/venv/lib/site-packages/pkg/readme.txt")) is False
    # Новые альтернативы шаблона — на путях целиком, а не только по имени.
    assert check_path(Path("/home/u/snap/firefox/notes.txt")) is False
    assert check_path(Path("/home/u/proj/site-packages/pkg/readme.txt")) is False
    assert check_path(Path("/home/u/.cache/notes.txt")) is False
    assert check_path(Path("/home/u/docs/~$отчёт.docx")) is False
