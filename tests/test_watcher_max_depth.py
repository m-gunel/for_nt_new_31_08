"""Ограничение глубины рекурсивного наблюдения.

На большом домашнем каталоге recursive=True ставит inotify-наблюдение на
каждый подкаталог и упирается в fs.inotify.max_user_watches (см. лог
«inotify watch limit reached»). max_depth заставляет обходить дерево самим и
наблюдать только каталоги до заданной вложенности — по одному watch'у на
каждый, без рекурсии watchdog'а.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from src.common import FileMetadata
from src.indexer.watcher.base_watcher import AbstractFilesWatcher
from src.indexer.watcher.watchdog_adapter import (
    WatchDogAdapter,
    _dirs_for_watch,
)


def _make_tree(root: Path, max_sub: int = 2, depth: int = 4) -> None:
    """Полное ветвление до depth: root/d0/{d0,d1,...}, root/d1/..."""
    if depth == 0:
        return
    for i in range(max_sub):
        sub = root / f"d{i}"
        sub.mkdir()
        _make_tree(sub, max_sub=max_sub, depth=depth - 1)


def _rel(paths: list[Path], root: Path) -> list[str]:
    out = []
    for p in paths:
        rel = p.relative_to(root).as_posix()
        out.append("" if rel == "." else rel)
    return sorted(out)


class _NoopWatcher(AbstractFilesWatcher):
    """Реализация абстрактных методов — сами события не проверяем."""

    async def on_delete(self, file: FileMetadata) -> None:
        return None

    async def on_delete_dir(self, path: str) -> None:
        return None

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        return None


class _FakeAdapter:
    """Подмена WatchDogAdapter: запоминает пути и kwargs, сразу закрывается."""

    instances: ClassVar[list["_FakeAdapter"]] = []

    def __init__(self, *paths, **kwargs):
        self.paths = [str(p) for p in paths]
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def start_async(self):
        return self

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    async def __aiter__(self):
        if False:  # pragma: no cover
            yield []


@pytest.fixture
def watcher_config(config, tmp_path, monkeypatch):
    """Конфиг с одним живым каталогом и заданным max_depth."""
    live = tmp_path / "docs"
    live.mkdir()
    # data_directory во временный каталог: иначе scan_paths прочитает
    # настоящий ~/.ragsearch/user_conf.yml разработчика.
    monkeypatch.setattr(config, "data_directory", str(tmp_path / "ragsearch"))
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)
    monkeypatch.setattr(config.file_scan_cfg, "paths", [str(live)])
    yield config, live
    config.__dict__.pop("user_conf_path", None)
    config.__dict__.pop("data_directory_path", None)


class _RecordingObserver:
    """Observer, записывающий вызовы schedule — без реального inotify."""

    def __init__(self):
        self.schedules: list[tuple[str, bool]] = []
        self.started = False
        self.stopped = False

    def schedule(self, handler, path: str, recursive: bool = False, **kwargs):
        self.schedules.append((path, recursive))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _adapter(
    monkeypatch, *paths, **kwargs
) -> tuple[WatchDogAdapter, _RecordingObserver]:
    import src.indexer.watcher.watchdog_adapter as mod

    rec = _RecordingObserver()
    monkeypatch.setattr(mod, "Observer", lambda: rec)
    adapter = WatchDogAdapter(*paths, **kwargs)
    # Наблюдения ставятся в _start_observer, а не в конструкторе: observer
    # поднимается пустым, и каждый путь добавляется отдельно, чтобы отказ по
    # одному не выключал наблюдение по всем остальным. Что именно ставится и
    # с каким recursive — ниже проверяется по-прежнему.
    adapter._start_observer()
    return adapter, rec


# ------------------------------------------------------------ _dirs_for_watch


def test_unbounded_returns_only_root(tmp_path):
    """None — рекурсивно наблюдает корень, дерево обходом не раскрываем."""
    root = tmp_path / "docs"
    root.mkdir()
    _make_tree(root)

    assert _dirs_for_watch(root, None) == [root]


def test_depth_zero_covers_root_only(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _make_tree(root)

    watched = _dirs_for_watch(root, 0)

    assert [str(p) for p in watched] == [str(root)]


def test_depth_one_covers_direct_children(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _make_tree(root, max_sub=1, depth=1)  # root/d0 (без внуков)

    watched = _rel(_dirs_for_watch(root, 1), root)

    assert watched == ["", "d0"]


def test_depth_two_does_not_descend_deeper(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _make_tree(root, max_sub=1, depth=2)  # root/d0/d0
    deep = root / "d0" / "d0" / "hidden"
    deep.mkdir()  # глубже max_depth — наблюдать не должны

    watched = _rel(_dirs_for_watch(root, 2), root)

    assert "d0/d0" in watched
    assert "d0/d0/hidden" not in watched


# ------------------------------------------------------- WatchDogAdapter


def test_adapter_bounded_schedules_each_dir_non_recursive(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()

    _, rec = _adapter(monkeypatch, root, max_depth=1)

    paths = sorted(p for p, r in rec.schedules)
    assert paths == sorted([str(root), str(root / "a"), str(root / "b")])
    assert all(r is False for _, r in rec.schedules)


def test_adapter_unbounded_schedules_root_recursive(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()

    _, rec = _adapter(monkeypatch, root, max_depth=None)

    assert rec.schedules == [(str(root), True)]


# --------------------------------------------------------------- base_watcher


def test_base_watcher_passes_max_depth(watcher_config, monkeypatch):
    """base_watcher.run передаёт cfg.scan_max_depth в WatchDogAdapter."""
    from src.indexer.watcher import base_watcher

    cfg, _live = watcher_config
    monkeypatch.setattr(cfg.file_scan_cfg, "max_depth", 3)

    _FakeAdapter.instances = []
    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FakeAdapter)

    import asyncio

    asyncio.run(_NoopWatcher().run())

    assert _FakeAdapter.instances, "WatchDogAdapter должен вызываться"
    assert _FakeAdapter.instances[0].kwargs["max_depth"] == 3


def test_base_watcher_defaults_to_unbounded(watcher_config, monkeypatch):
    """max_depth не задан — WatchDogAdapter уходит в рекурсивный режим (None)."""
    from src.indexer.watcher import base_watcher

    cfg, _live = watcher_config
    monkeypatch.setattr(cfg.file_scan_cfg, "max_depth", None)

    _FakeAdapter.instances = []
    monkeypatch.setattr(base_watcher, "WatchDogAdapter", _FakeAdapter)

    import asyncio

    asyncio.run(_NoopWatcher().run())

    assert _FakeAdapter.instances[0].kwargs["max_depth"] is None
