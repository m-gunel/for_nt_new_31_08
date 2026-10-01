"""Появление и перемещение каталогов внутри наблюдаемого дерева.

Перенос каталога — это ОДИН rename: ядро шлёт единственный `IN_MOVED_TO` по
самому каталогу, событий по вложенным файлам не приходит вовсе. До доработки
на этом терялось сразу три вещи:

* `DirCreatedEvent` не был подписан в `event_filter`, поэтому факт «в дереве
  возник каталог» до адаптера не доходил в принципе;
* даже дойдя, событие никем не обрабатывалось — содержимое перенесённого
  каталога оставалось непроиндексированным до перезапуска демона;
* у `IN_MOVED_TO` нет бита `IN_CREATE`, поэтому watchdog не ставит watch на
  ПЕРЕМЕЩЁННЫЙ каталог (на созданный обычным `mkdir` — ставит), и файлы,
  появившиеся в нём уже после переноса, тоже не замечались;
* переименование каталога при `max_depth != None` приходило одним
  `DirMovedEvent` — старый путь сносился `delete_prefix`, а под новым именем
  поддерево никто не возвращал, то есть переименование папки просто удаляло
  её из поиска.

Живой inotify здесь не нужен и на macOS невоспроизводим (там FSEvents с
другой склейкой событий), поэтому обработчики зовутся напрямую — так же, как
в tests/test_indexing_live.py.
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
        # Как боевой Watcher.on_delete_dir: delete_prefix сносит ВСЁ поддерево,
        # и со слэшем на конце — иначе снятие /a/b задело бы и /a/bc. Без этого
        # перестановка обработчиков в run() не имела бы никаких последствий,
        # кроме журнала вызовов.
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
    `Observer()` всё-таки создаётся (на Linux это InotifyObserver — поток,
    который никто не стартует и не останавливает). Тот же приём в
    tests/test_watcher_max_depth.py и tests/test_watcher_resilience.py.
    """
    import src.indexer.watcher.watchdog_adapter as mod

    observer = _KwargsObserver()
    monkeypatch.setattr(mod, "Observer", lambda: observer)
    return WatchDogAdapter(*paths, **kwargs), observer


@pytest.fixture
def pinned_scan_cfg(config, monkeypatch):
    """Закрепить правила обхода, которые _collect_dir_files читает из конфига.

    Конфиг в тестах один на всю сессию, а `serve_indexer`
    (src/indexer/utils.py) присваивает `file_scan_cfg.filter_files` и
    `filter_meta` НАВСЕГДА, без monkeypatch, — и поднимается он в других
    файлах тестов, которые по алфавиту идут раньше этого. Без явной фиксации
    проверки на точное равенство списков зависели бы от порядка прогона.
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


# ---------------------------------------------------- дефект 1: подписка ----


def test_dir_created_event_is_subscribed():
    """Без DirCreatedEvent появление каталога не доходило до адаптера вообще.

    Раньше список типов событий писался прямо в вызове observer.schedule и
    содержал только файловые события плюс DirDeleted/DirMoved. Каталог,
    перенесённый снаружи внутрь наблюдаемого дерева, не порождал ни одного
    события, которое прошло бы фильтр, — индексировать было нечего.
    """
    assert DirCreatedEvent in WatchDogAdapter.EVENT_FILTER, (
        "появление каталога не подписано — событие не дойдёт до адаптера"
    )
    # Заодно: из прежнего списка ничего не потеряно.
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
    """Список типов должен реально уходить в observer.schedule, а не лежать рядом.

    Атрибут класса появился как раз затем, чтобы фильтр не разошёлся между
    стартовой постановкой наблюдения и watch_dir: подписка на DirCreatedEvent,
    забытая в одном из двух мест, снова оставила бы перенесённые каталоги
    незамеченными — но уже избирательно, что заметить ещё труднее.
    """
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)

    adapter, observer = _adapter(monkeypatch, root, max_depth=1)
    adapter._start_observer()

    # ...и то же самое для каталога, появившегося уже после старта.
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


# ------------------------------- дефект 2: содержимое нового каталога -------


@pytest.mark.asyncio
async def test_moved_in_dir_is_indexed_without_file_events(tmp_path, pinned_scan_cfg):
    """Появившийся каталог обходится сам — событий по его файлам не будет.

    Раньше обработчика _handle_new_dirs не существовало: единственный
    IN_MOVED_TO по каталогу ничего не индексировал, и перенесённая снаружи
    папка со всем содержимым отсутствовала в поиске до перезапуска демона.
    Несуществующий путь в той же пачке — забота _handle_missing_dirs, здесь он
    должен быть просто пропущен.
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
    """Обход появившегося каталога обязан идти ПО КОНФИГУ, как полный проход.

    Стережём именно проброс правил в iter_file_metadata, а не её собственные
    умолчания (исключения по имени и отсутствие парсера покрыты в
    tests/test_scan_dir_coverage.py и остались бы зелёными даже при голом
    `iter_file_metadata(path)`):

    * `filter_files` — им serve_indexer отбирает файлы по расширению;
    * `filter_meta` — им же применяется `max_size`, иначе перенос каталога с
      образом диска внутри отправил бы его прямо в парсер;
    * `follow_symlinks` — в конфиге True, а в самой iter_file_metadata по
      умолчанию False: потеряв его, перенесённый каталог тихо перестал бы
      ходить по ссылкам, в отличие от полного прохода.
    """
    root = tmp_path / "project"
    root.mkdir()
    # Цель ссылки лежит ВНЕ обходимого каталога намеренно: внутри файл нашёлся
    # бы и по настоящему пути, и follow_symlinks ничего не решал бы.
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


# ---------------------------- дефект 3: наблюдение за перенесённой папкой ---


@pytest.mark.asyncio
async def test_moved_in_dir_gets_watch(tmp_path, pinned_scan_cfg):
    """На появившийся каталог ставится наблюдение вручную.

    У IN_MOVED_TO нет бита IN_CREATE, поэтому watchdog добавляет watch на
    каталог, созданный mkdir, но НЕ на перемещённый внутрь. Расхождение
    незаметное: файлы, положенные в такую папку уже после переноса, не
    индексировались до перезапуска демона. На несуществующий путь наблюдение
    ставить не за что.
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

    watch_dir зовётся на КАЖДЫЙ появившийся каталог в каждой пачке событий, и
    без учёта уже поставленных путей один и тот же каталог получал бы по
    эмиттеру на событие — то есть дубли событий и лишний расход
    fs.inotify.max_user_watches.
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
    штатны на домашнем дереве. Пробросив OSError, watch_dir оборвал бы обход
    всей пачки появившихся каталогов — из-за одной недоступной папки не
    проиндексировались бы все остальные.
    """
    root = tmp_path / "docs"
    root.mkdir()
    adapter, observer = _adapter(monkeypatch, root, max_depth=1)
    observer.fail = OSError(errno.ENOSPC, "No space left on device")

    assert adapter.watch_dir(str(root)) is False

    observer.fail = OSError(errno.EACCES, "Permission denied")
    assert adapter.watch_dir(str(root / "secret")) is False

    # Отказавший путь не помечен наблюдаемым, поэтому повтор после того, как
    # лимит освободился, ещё может удаться. Само состояние `_watched` проверено
    # в tests/test_watcher_resilience.py, здесь важен результат повтора.
    observer.fail = None
    assert adapter.watch_dir(str(root)) is True, (
        "после снятия отказа наблюдение обязано встать: иначе путь потерян "
        "до перезапуска демона"
    )
    assert [c["path"] for c in observer.schedules] == [str(root)]


@pytest.mark.asyncio
async def test_watch_failure_does_not_stop_indexing(tmp_path, pinned_scan_cfg):
    """Сорванное наблюдение не должно отменять индексацию каталога.

    Наблюдение и обход — разные задачи: даже если watch поставить не удалось,
    содержимое появившегося каталога обязано попасть в индекс. Раньше на этом
    месте не было ни того, ни другого.
    """
    moved, expected = _make_dir_with_files(tmp_path / "moved_in")

    watcher = _RecordingWatcher()
    watcher._adapter = _RecordingAdapter(
        error=OSError(errno.ENOSPC, "No space left on device")
    )

    await watcher._handle_new_dirs({moved})

    assert sorted(watcher.upserted) == expected


# ------------------------------------ дефект 4: переименование каталога -----


def _emitted(event) -> list[tuple[Change, str, bool]]:
    """Что MyEventHandler кладёт в очередь по одному событию перемещения."""
    events: list[tuple[Change, str, bool]] = []
    handler = MyEventHandler(
        lambda e: events.append((e.change, e.path, e.is_directory))
    )
    handler.on_moved(event)
    return events


def test_dir_rename_arrives_as_two_directory_events(tmp_path):
    """Переименование каталога — это deleted по старому пути и added по новому.

    На этом стоит вся пара _handle_missing_dirs/_handle_new_dirs: оба пути
    приходят ОДНОЙ пачкой и оба с is_directory=True. Потеряв признак каталога,
    они ушли бы в файловую ветку (_handle_files), где нет ни снятия
    поддерева, ни обхода нового пути. Для файлов это покрыто
    tests/test_watcher_resilience.py, для каталогов — не было, а теряли
    поддерево именно они.
    """
    old = tmp_path / "отчёты"
    new = tmp_path / "отчёты-2024"

    assert _emitted(DirMovedEvent(src_path=str(old), dest_path=str(new))) == [
        (Change.deleted, str(old), True),
        (Change.added, str(new), True),
    ]


@pytest.mark.asyncio
async def test_renamed_dir_returns_under_new_path(tmp_path, pinned_scan_cfg):
    """Переименование каталога — снятие старого пути И обход нового.

    При max_depth != None наблюдение нерекурсивное, и watchdog отдаёт один
    DirMovedEvent без под-событий. Старый путь уходил в on_delete_dir и
    delete_prefix сносил всё поддерево, а вернуть его под новым именем было
    некому: папка просто исчезала из поиска до следующего полного прохода.
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
    """run() обязан звать _handle_new_dirs, иначе обработчик мёртв.

    Порядок важен: сначала снимаем исчезнувшие пути, потом обходим
    появившиеся. При обратном порядке переименование каталога внутри дерева
    (старый и новый путь приходят одной пачкой) закончилось бы тем, что
    delete_prefix снёс бы только что проиндексированное поддерево.
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
