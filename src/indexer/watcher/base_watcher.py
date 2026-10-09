import abc
import asyncio
import errno
import os
from contextlib import AsyncExitStack
from logging import Logger
from pathlib import Path

from src.common import FileMetadata
from src.indexer.watcher.watchdog_adapter import WatchDogAdapter
from src.utils import ConfigMixin, get_logger, wrap_exception_logger

from ..exclude import check_path
from ..scan_dir import iter_file_metadata
from .filter import Filter

lg: Logger = get_logger(__name__, slice=-1)


class AbstractFilesWatcher(abc.ABC, ConfigMixin):
    @property
    def filter(self):
        return Filter()

    def _get_file_metadata(self, path: str) -> FileMetadata:
        # abspath — как в iter_file_metadata: индекс хранит абсолютные пути,
        # а watchdog отдаёт путь в том виде, в каком его передали в schedule.
        path = os.path.abspath(path)
        try:
            stat = os.stat(path)
            return FileMetadata(
                path=path, size=stat.st_size, mtime_seconds=int(stat.st_mtime)
            )
        except OSError:
            # Файл уже исчез — нулевые метаданные.
            return FileMetadata(path=path, size=0, mtime_seconds=0)

    def _watch_paths(self) -> list[Path]:
        """Каталоги для наблюдения, из которых убраны несуществующие.

        inotify отказывает по отсутствующему пути при старте эмиттера, и
        падает весь observer — вместе с наблюдением за живыми папками.
        """
        paths: list[Path] = []
        missing: list[str] = []
        unreadable: list[str] = []
        for raw in self.cfg.scan_paths:
            path = Path(raw).expanduser()
            if not path.is_dir():
                missing.append(str(path))
                continue
            # Одного is_dir() мало: у каталога с правами 0o000 он вернёт True,
            # inotify молча не встанет (watchdog глотает EACCES), и путь
            # выглядит наблюдаемым, не будучи им. Нужны и чтение, и обход.
            if not os.access(path, os.R_OK | os.X_OK):
                unreadable.append(str(path))
                continue
            paths.append(path)
        if missing:
            lg.warning("не отслеживаю каталоги, которых нет на диске: %s", missing)
        if unreadable:
            lg.warning("не отслеживаю каталоги без прав на чтение: %s", unreadable)
        return paths

    @wrap_exception_logger(lg)
    async def run(self):
        paths = self._watch_paths()
        if not paths:
            lg.warning("нет ни одного существующего каталога — watcher не запущен")
            return
        # Исключения сверяются ниже корня, как в полном проходе: корень вида
        # ~/Build иначе терял бы все свои события.
        roots = [os.path.abspath(p) for p in paths]
        async with AsyncExitStack() as stack:
            adapter = WatchDogAdapter(
                *paths,
                watch_filter=self.filter,
                ignore_permission_denied=True,
                max_depth=self.cfg.scan_max_depth,
            )
            # Закрытие — в потоке: observer.stop() ждёт, пока поток постановки
            # отпустит observer, а на большом дереве это секунды и минуты.
            stack.push_async_callback(asyncio.to_thread, adapter.close)
            # Нужен в _handle_new_dirs: на каталог, перемещённый внутрь
            # наблюдаемого дерева, наблюдение приходится ставить вручную.
            self._adapter = adapter
            try:
                # Не синхронный вход в контекст: постановка inotify-наблюдения
                # обходит всё дерево и заняла бы event loop целиком.
                watcher = await adapter.start_async()
            except OSError as exc:
                # errno нужен в логе: ENOSPC — исчерпан fs.inotify.max_user_watches,
                # EMFILE — fs.inotify.max_user_instances, ENOENT/ENOTDIR — каталог
                # исчез между проверкой и стартом эмиттера.
                limit = {
                    errno.ENOSPC: "fs.inotify.max_user_watches",
                    errno.EMFILE: "fs.inotify.max_user_instances",
                }.get(exc.errno)
                lg.error(
                    "не удалось начать наблюдение (errno=%s %s), watcher остановлен%s",
                    exc.errno,
                    exc.strerror,
                    f"; поднимите {limit}" if limit else "",
                    exc_info=True,
                )
                return
            async for changes in watcher:
                # Схлопываем пачку: за окно ожидания один и тот же путь часто
                # приходит несколько раз (added + modified при копировании,
                # серия modified при сохранении из редактора).
                dir_paths: set[str] = set()
                file_paths: set[str] = set()
                for event in changes:
                    path = os.path.abspath(event.path)
                    if not check_path(Path(path), roots):
                        continue
                    target = dir_paths if event.is_directory else file_paths
                    target.add(path)

                await self._handle_missing_dirs(dir_paths)
                await self._handle_new_dirs(dir_paths)
                await self._handle_files(file_paths)

    async def _handle_missing_dirs(self, dir_paths: set[str]) -> None:
        """Снять с индекса поддеревья исчезнувших каталогов."""
        adapter = getattr(self, "_adapter", None)
        # Есть только у WatchDogAdapter: заглушкам в тестах не нужен.
        unwatch = getattr(adapter, "unwatch_dir", None)
        for path in sorted(dir_paths):
            if os.path.isdir(path):
                continue
            if unwatch is not None:
                try:
                    await asyncio.to_thread(unwatch, path)
                except Exception:
                    lg.exception("не удалось снять наблюдение с %s", path)
            try:
                await self.on_delete_dir(path)
            except Exception as e:
                lg.exception(f"Error handling deleted dir {path}: {e}")

    def _collect_dir_files(self, path: str) -> list[FileMetadata]:
        """Обойти появившийся каталог теми же правилами, что и полный проход."""
        cfg = self.cfg
        return list(
            iter_file_metadata(
                path,
                filter_func=cfg.file_scan_cfg.filter_files,
                filter_meta=cfg.file_scan_cfg.filter_meta,
                follow_symlinks=cfg.file_scan_cfg.follow_symlinks,
            )
        )

    async def _handle_new_dirs(self, dir_paths: set[str]) -> None:
        """Проиндексировать каталоги, появившиеся в наблюдаемом дереве.

        Перенос каталога — один rename: ядро шлёт единственный IN_MOVED_TO по
        самому каталогу, событий по вложенным файлам нет. Так же
        обрабатываются каталог, ставший читаемым, и подкаталог, созданный
        после старта. При переименовании внутри дерева старый путь снимает
        _handle_missing_dirs, новый обходится здесь.
        """
        adapter = getattr(self, "_adapter", None)
        for path in sorted(dir_paths):
            if not os.path.isdir(path):
                # Каталога нет — это ветка _handle_missing_dirs.
                continue
            if adapter is not None:
                try:
                    await asyncio.to_thread(adapter.watch_dir, path)
                except Exception:
                    lg.exception("не удалось поставить наблюдение на %s", path)
            try:
                metas = await asyncio.to_thread(self._collect_dir_files, path)
            except Exception:
                lg.exception("не удалось обойти появившийся каталог %s", path)
                continue
            if metas:
                lg.info(
                    "появился каталог %s: файлов к индексации %s", path, len(metas)
                )
                # Как в _handle_files: сбой записи (нет места, LanceDB,
                # инференс) не должен завершать наблюдение.
                try:
                    await self.on_upsert(metas)
                except Exception:
                    lg.exception("не удалось проиндексировать каталог %s", path)

    async def _handle_files(self, file_paths: set[str]) -> None:
        """Развести пути на удалённые и живые ПО ФАКТУ, а не по типу события.

        Порядок и склейка событий отличаются между FSEvents, inotify и Windows:
        удаление легко перекрывается более поздним `modified` по тому же пути,
        и запись осталась бы в индексе навсегда — полный проход удалённые
        файлы не замечает.
        """
        missing = [p for p in file_paths if not os.path.exists(p)]
        existing = [p for p in file_paths if os.path.exists(p)]

        if missing:
            try:
                await self.on_delete_many([self._get_file_metadata(p) for p in missing])
            except Exception as e:
                lg.exception(f"Error handling delete {missing[:3]}...: {e}")

        if existing:
            try:
                await self.on_upsert([self._get_file_metadata(p) for p in existing])
            except Exception as e:
                lg.exception(f"Error handling changes {existing[:3]}...: {e}")

    @abc.abstractmethod
    async def on_delete(self, file: FileMetadata) -> None:
        raise NotImplementedError()

    async def on_delete_many(self, files: list[FileMetadata]) -> None:
        """Снять с индекса пачку исчезнувших файлов.

        По умолчанию — по одному через on_delete; Watcher переопределяет его
        одним пакетным удалением.
        """
        for file in files:
            try:
                await self.on_delete(file)
            except Exception as e:
                lg.exception(f"Error handling delete {file.path}: {e}")

    @abc.abstractmethod
    async def on_delete_dir(self, path: str) -> None:
        """Снять с индекса всё поддерево исчезнувшего каталога."""
        raise NotImplementedError()

    @abc.abstractmethod
    async def on_upsert(self, files: list[FileMetadata]) -> None:
        """Проиндексировать пачку добавленных/изменённых файлов."""
        raise NotImplementedError()
