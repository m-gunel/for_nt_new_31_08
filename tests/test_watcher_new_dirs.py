"""Появление и перемещение каталогов внутри наблюдаемого дерева.

Перенос каталога — один rename: ядро шлёт единственный `IN_MOVED_TO` по
самому каталогу, событий по вложенным файлам нет. Поэтому появившийся каталог
надо подписать (DirCreatedEvent), обойти и поставить на него watch вручную, а
переименование — обработать как снятие старого пути и обход нового.

Живой inotify на macOS невоспроизводим (FSEvents склеивает события иначе),
поэтому обработчики зовутся напрямую, как в tests/test_indexing_live.py.
"""

import errno
import os

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)
from watchfiles import Change

from src.common import FileMetadata
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.indexer.watcher.watchdog_adapter import (
    MyEventHandler,
    WatchDogAdapter,
    WatchEvent,
)
from tests.test_watcher_max_depth import watcher_config  # noqa: F401


class _RecordingWatcher(AbstractFilesWatcher):
    """Watcher, который вместо индексации записывает, что до него дошло."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.deleted_dirs: list[str] = []
        self.upserted: list[str] = []

    async def on_delete(self, file: FileMetadata) -> None:
        self.deleted.append(file.path)

    async def on_delete_dir(self, path: str) -> None:
        # Как боевой Watcher.on_delete_dir: delete_prefix сносит всё поддерево,
        # со слэшем на конце, чтобы снятие /a/b не задело /a/bc. Без этого
        # порядок обработчиков в run() ни на что не влиял бы.
        self.deleted_dirs.append(path)
        prefix = path.rstrip(os.sep) + os.sep
        self.upserted = [p for p in self.upserted if not p.startswith(prefix)]

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        self.upserted.extend(f.path for f in files)


class _RecordingAdapter:
    """Заглушка адаптера: помнит, на какие каталоги ставили наблюдение."""

    def __init__(self, error: Exception | None = None) -> None:
        self.watched: list[str] = []
        self._error = error

    def watch_dir(self, path: str) -> bool:
        self.watched.append(str(path))
        if self._error is not None:
            raise self._error
        return True


class _KwargsObserver:
    """Observer, сохраняющий ВСЕ аргументы schedule — включая event_filter.

    Отдельный от `_RecordingObserver` в test_watcher_max_depth: тот kwargs
    отбрасывает, а здесь проверяются именно они.
    """

    def __init__(self) -> None:
        self.schedules: list[dict] = []
        self.fail: OSError | None = None
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, recursive=False, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.schedules.append({"path": str(path), "recursive": recursive, **kwargs})
        return object()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _adapter(monkeypatch, *paths, **kwargs) -> tuple[WatchDogAdapter, _KwargsObserver]:
    """WatchDogAdapter с подменённым Observer: настоящий inotify не поднимаем.

    Подменяется символ в модуле, а не поле после конструктора: иначе
    `Observer()` всё-таки создаётся (на Linux — поток InotifyObserver, который
    никто не останавливает).
    """
    import src.indexer.watcher.watchdog_adapter as mod

    observer = _KwargsObserver()
    monkeypatch.setattr(mod, "Observer", lambda: observer)
    return WatchDogAdapter(*paths, **kwargs), observer


@pytest.fixture
def pinned_scan_cfg(config, monkeypatch):
    """Закрепить правила обхода, которые _collect_dir_files читает из конфига.

    Конфиг один на сессию, а `serve_indexer` (src/indexer/utils.py) в тестах,
    идущих раньше, присваивает `filter_files` и `filter_meta` без monkeypatch:
    без фиксации сравнение списков зависело бы от порядка прогона.
    """
    monkeypatch.setattr(config.file_scan_cfg, "filter_files", None)
    monkeypatch.setattr(config.file_scan_cfg, "filter_meta", None)
    monkeypatch.setattr(config.file_scan_cfg, "follow_symlinks", True)
    return config


def _make_dir_with_files(root) -> tuple[str, list[str]]:
    """Каталог с файлом на верхнем уровне и файлом во вложенном подкаталоге."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "nested").mkdir()
    top = root / "report.txt"
    top.write_text("годовой отчёт", encoding="utf-8")
    deep = root / "nested" / "estimate.txt"
    deep.write_text("смета работ", encoding="utf-8")
    return str(root), sorted([str(top), str(deep)])


def test_dir_created_event_is_subscribed():
    """DirCreatedEvent подписан.

    Иначе каталог, перенесённый внутрь дерева, не порождает ни одного события,
    прошедшего фильтр.
    """
    assert DirCreatedEvent in WatchDogAdapter.EVENT_FILTER, (
        "появление каталога не подписано — событие не дойдёт до адаптера"
    )
    # Остальные типы событий на месте.
    for event_type in (
        FileModifiedEvent,
        FileMovedEvent,
        FileDeletedEvent,
        FileCreatedEvent,
        DirDeletedEvent,
        DirMovedEvent,
    ):
        assert event_type in WatchDogAdapter.EVENT_FILTER, event_type


def test_event_filter_with_dir_created_reaches_schedule(tmp_path, monkeypatch):
    """Список типов уходит в observer.schedule — и на старте, и в watch_dir.

    Фильтр, разошедшийся между этими местами, оставил бы перенесённые каталоги
    незамеченными избирательно.
    """
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)

    adapter, observer = _adapter(monkeypatch, root, max_depth=1)
    adapter._start_observer()

    # И для каталога, появившегося после старта.
    late = tmp_path / "docs" / "late"
    late.mkdir()
    assert adapter.watch_dir(str(late)) is True

    assert len(observer.schedules) == 3, observer.schedules
    for call in observer.schedules:
        passed = call["event_filter"]
        assert DirCreatedEvent in passed, f"нет DirCreatedEvent в {call['path']}"
        assert set(passed) == set(WatchDogAdapter.EVENT_FILTER), (
            "фильтр разошёлся с WatchDogAdapter.EVENT_FILTER"
        )


@pytest.mark.asyncio
async def test_moved_in_dir_is_indexed_without_file_events(tmp_path, pinned_scan_cfg):
    """Появившийся каталог обходится сам — событий по его файлам не будет.

    Несуществующий путь в той же пачке — забота _handle_missing_dirs, здесь он
    пропускается.
    """
    moved, expected = _make_dir_with_files(tmp_path / "moved_in")
    ghost = str(tmp_path / "gone_forever")

    watcher = _RecordingWatcher()
    await watcher._handle_new_dirs({moved, ghost})

    assert sorted(watcher.upserted) == expected, (
        "содержимое появившегося каталога не ушло в индекс"
    )
    assert watcher.deleted == []
    assert watcher.deleted_dirs == [], "удалением занимается _handle_missing_dirs"


@pytest.mark.asyncio
async def test_new_dir_scan_uses_same_filters_as_full_pass(
    tmp_path, config, monkeypatch
):
    """Обход появившегося каталога идёт по конфигу, как полный проход.

    Проверяется проброс правил в iter_file_metadata, а не её умолчания (они —
    в tests/test_scan_dir_coverage.py):

    * `filter_files` — отбор по расширению из serve_indexer;
    * `filter_meta` — `max_size`, иначе образ диска ушёл бы прямо в парсер;
    * `follow_symlinks` — в конфиге True, а в iter_file_metadata по умолчанию
      False.
    """
    root = tmp_path / "project"
    root.mkdir()
    # Цель ссылки вне каталога: внутри файл нашёлся бы и по настоящему пути.
    outside = tmp_path / "снаружи"
    outside.mkdir()
    (outside / "по-ссылке.txt").write_text("за ссылкой", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    good = root / "readme.txt"
    good.write_text("описание проекта", encoding="utf-8")
    secret = root / "секрет.txt"
    secret.write_text("пароли", encoding="utf-8")
    huge = root / "образ.txt"
    huge.write_bytes(b"x" * 4096)

    monkeypatch.setattr(
        config.file_scan_cfg, "filter_files", lambda path: "секрет" not in path
    )
    monkeypatch.setattr(
        config.file_scan_cfg, "filter_meta", lambda meta: meta.size <= 1024
    )
    monkeypatch.setattr(config.file_scan_cfg, "follow_symlinks", True)

    watcher = _RecordingWatcher()
    await watcher._handle_new_dirs({str(root)})

    through_link = str(root / "link" / "по-ссылке.txt")
    assert str(secret) not in watcher.upserted, (
        "filter_files из конфига не прокинут — отбор файлов разошёлся с полным проходом"
    )
    assert str(huge) not in watcher.upserted, (
        "filter_meta из конфига не прокинут — max_size в новом каталоге не действует"
    )
    assert through_link in watcher.upserted, (
        "follow_symlinks взят не из конфига (в iter_file_metadata умолчание — "
        "False): перенесённый каталог перестал ходить по ссылкам"
    )
    assert sorted(watcher.upserted) == sorted([str(good), through_link]), (
        f"в индекс ушло не то, что отбирает конфиг: {watcher.upserted}"
    )


@pytest.mark.asyncio
async def test_moved_in_dir_gets_watch(tmp_path, pinned_scan_cfg):
    """На появившийся каталог наблюдение ставится вручную.

    У IN_MOVED_TO нет бита IN_CREATE: watchdog ставит watch на каталог из
    mkdir, но не на перемещённый внутрь, и файлы, положенные туда после
    переноса, не замечались бы.
    """
    moved, expected = _make_dir_with_files(tmp_path / "moved_in")
    ghost = str(tmp_path / "gone_forever")

    watcher = _RecordingWatcher()
    adapter = _RecordingAdapter()
    watcher._adapter = adapter  # так его выставляет run()

    await watcher._handle_new_dirs({moved, ghost})

    assert adapter.watched == [moved], "наблюдение на перенесённый каталог не встало"
    assert sorted(watcher.upserted) == expected


def test_watch_dir_is_idempotent(tmp_path, monkeypatch):
    """Повторный watch_dir по тому же пути не плодит второе наблюдение.

    watch_dir зовётся на каждый появившийся каталог в каждой пачке, и без учёта
    поставленных путей каталог получал бы по эмиттеру на событие — дубли
    событий и лишний расход fs.inotify.max_user_watches.
    """
    root = tmp_path / "docs"
    root.mkdir()
    new_dir = root / "appeared"
    new_dir.mkdir()

    adapter, observer = _adapter(monkeypatch, root, max_depth=1)

    assert adapter.watch_dir(str(new_dir)) is True
    assert adapter.watch_dir(str(new_dir)) is False, "повтор должен отдать False"
    assert [c["path"] for c in observer.schedules] == [str(new_dir)]


def test_watch_dir_returns_false_on_oserror(tmp_path, monkeypatch):
    """Отказ inotify — это False, а не исключение наружу.

    Упор в fs.inotify.max_user_watches (ENOSPC) и каталог без прав (EACCES)
    штатны на домашнем дереве; исключение оборвало бы обход всей пачки
    появившихся каталогов.
    """
    root = tmp_path / "docs"
    root.mkdir()
    adapter, observer = _adapter(monkeypatch, root, max_depth=1)
    observer.fail = OSError(errno.ENOSPC, "No space left on device")

    assert adapter.watch_dir(str(root)) is False

    observer.fail = OSError(errno.EACCES, "Permission denied")
    assert adapter.watch_dir(str(root / "secret")) is False

    # Отказавший путь не помечен наблюдаемым: повтор после освобождения лимита
    # ещё может удаться.
    observer.fail = None
    assert adapter.watch_dir(str(root)) is True, (
        "после снятия отказа наблюдение обязано встать: иначе путь потерян "
        "до перезапуска демона"
    )
    assert [c["path"] for c in observer.schedules] == [str(root)]


@pytest.mark.asyncio
async def test_watch_failure_does_not_stop_indexing(tmp_path, pinned_scan_cfg):
    """Сорванное наблюдение не отменяет индексацию каталога: это разные задачи."""
    moved, expected = _make_dir_with_files(tmp_path / "moved_in")

    watcher = _RecordingWatcher()
    watcher._adapter = _RecordingAdapter(
        error=OSError(errno.ENOSPC, "No space left on device")
    )

    await watcher._handle_new_dirs({moved})

    assert sorted(watcher.upserted) == expected


def _emitted(event) -> list[tuple[Change, str, bool]]:
    """Что MyEventHandler кладёт в очередь по одному событию перемещения."""
    events: list[tuple[Change, str, bool]] = []
    handler = MyEventHandler(
        lambda e: events.append((e.change, e.path, e.is_directory))
    )
    handler.on_moved(event)
    return events


def test_dir_rename_arrives_as_two_directory_events(tmp_path):
    """Переименование каталога — deleted по старому пути и added по новому.

    Оба приходят одной пачкой с is_directory=True: без признака каталога они
    ушли бы в _handle_files, где нет ни снятия поддерева, ни обхода нового пути.
    """
    old = tmp_path / "отчёты"
    new = tmp_path / "отчёты-2024"

    assert _emitted(DirMovedEvent(src_path=str(old), dest_path=str(new))) == [
        (Change.deleted, str(old), True),
        (Change.added, str(new), True),
    ]


@pytest.mark.asyncio
async def test_renamed_dir_returns_under_new_path(tmp_path, pinned_scan_cfg):
    """Переименование каталога — снятие старого пути и обход нового.

    При max_depth != None наблюдение нерекурсивное, и watchdog отдаёт один
    DirMovedEvent без под-событий: delete_prefix снимает поддерево, и вернуть
    его под новым именем больше некому.
    """
    old = str(tmp_path / "old_name")  # переименован — на диске его уже нет
    new, expected = _make_dir_with_files(tmp_path / "new_name")
    batch = {old, new}

    watcher = _RecordingWatcher()
    # Тот же порядок, что и в run(): сначала исчезнувшие, затем появившиеся.
    await watcher._handle_missing_dirs(batch)
    await watcher._handle_new_dirs(batch)

    assert watcher.deleted_dirs == [old], "старый путь не снят с индекса"
    assert sorted(watcher.upserted) == expected, (
        "поддерево не вернулось в индекс под новым именем"
    )


@pytest.mark.asyncio
async def test_run_handles_new_dirs_right_after_missing(
    watcher_config,  # noqa: F811
    pinned_scan_cfg,
    monkeypatch,
):
    """run() зовёт _handle_new_dirs, и строго после _handle_missing_dirs.

    При обратном порядке переименование каталога внутри дерева (старый и новый
    путь в одной пачке) кончилось бы тем, что delete_prefix снёс бы только что
    проиндексированное поддерево.
    """
    from src.indexer.watcher import base_watcher

    _cfg, live = watcher_config
    appeared, expected = _make_dir_with_files(live / "appeared")

    class _OneBatchAdapter:
        """Адаптер, отдающий ровно одну пачку с событием по каталогу."""

        last: "_OneBatchAdapter | None" = None

        def __init__(self, *paths, **kwargs):
            self.watched: list[str] = []
            type(self).last = self

        async def start_async(self):
            return self

        def close(self):
            return None

        def watch_dir(self, path):
            self.watched.append(str(path))
            return True

        async def __aiter__(self):
            yield [WatchEvent(Change.added, appeared, True)]

    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _OneBatchAdapter)

    calls: list[str] = []

    class _OrderWatcher(_RecordingWatcher):
        async def _handle_missing_dirs(self, dir_paths):
            calls.append("missing")
            await super()._handle_missing_dirs(dir_paths)

        async def _handle_new_dirs(self, dir_paths):
            calls.append("new")
            await super()._handle_new_dirs(dir_paths)

        async def _handle_files(self, file_paths):
            calls.append("files")
            await super()._handle_files(file_paths)

    watcher = _OrderWatcher()
    await watcher.run()

    assert calls == ["missing", "new", "files"], calls
    assert sorted(watcher.upserted) == expected
    assert _OneBatchAdapter.last is not None
    assert _OneBatchAdapter.last.watched == [os.path.abspath(appeared)], (
        "run() не отдал адаптер обработчику — наблюдение поставить нечем"
    )


@pytest.mark.asyncio
async def test_run_keeps_events_under_root_named_like_excluded_dir(
    watcher_config,  # noqa: F811
    monkeypatch,
):
    """Корень с «мусорным» именем (~/Build) не глушит события под собой.

    Полный проход сверяет исключения только ниже корня и такой корень
    индексирует; watcher обязан так же, иначе изменения и удаления под ним
    доходили бы до поиска только с часовым полным проходом.
    """
    from src.indexer.watcher import base_watcher

    cfg, live = watcher_config
    root = live.parent / "Build"
    root.mkdir()
    monkeypatch.setattr(cfg.file_scan_cfg, "paths", [str(root)])
    report = root / "report.txt"
    report.write_text("отчёт", encoding="utf-8")
    junk = root / "venv" / "lib.txt"
    junk.parent.mkdir()
    junk.write_text("пакет", encoding="utf-8")
    gone = root / "old.txt"  # удалён: на диске его уже нет

    class _OneBatchAdapter:
        """Адаптер, отдающий одну пачку событий по файлам под корнем."""

        def __init__(self, *paths, **kwargs):
            pass

        async def start_async(self):
            return self

        def close(self):
            return None

        def watch_dir(self, path):
            return True

        async def __aiter__(self):
            yield [
                WatchEvent(Change.added, str(report), False),
                WatchEvent(Change.deleted, str(gone), False),
                WatchEvent(Change.added, str(junk), False),
            ]

    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _OneBatchAdapter)

    watcher = _RecordingWatcher()
    await watcher.run()

    assert watcher.upserted == [str(report)], (
        "событие под корнем Build потеряно — или прошёл мусор ниже корня"
    )
    assert watcher.deleted == [str(gone)], "удаление под корнем Build потеряно"
