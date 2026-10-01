import abc
import asyncio
import os
from contextlib import ExitStack
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
        """Get file metadata from path, handling errors gracefully."""
        # abspath — как в iter_file_metadata: индекс хранит абсолютные пути,
        # а watchdog отдаёт путь в том виде, в каком его передали в schedule.
        path = os.path.abspath(path)
        try:
            stat = os.stat(path)
            return FileMetadata(
                path=path, size=stat.st_size, mtime_seconds=int(stat.st_mtime)
            )
        except OSError:
            # If we can't stat the file (e.g., it was deleted), return metadata with zeros
            return FileMetadata(path=path, size=0, mtime_seconds=0)

    def _watch_paths(self) -> list[Path]:
        """Каталоги для наблюдения, из которых убраны несуществующие.

        inotify отказывает по отсутствующему пути при старте эмиттера, и
        падает при этом весь observer — вместе с наблюдением за живыми
        папками. Одного мусорного пути, сохранённого в user_conf.yml, хватало,
        чтобы демон остался вообще без слежения за файловой системой.
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
        with ExitStack() as stack:
            adapter = WatchDogAdapter(
                *paths,
                watch_filter=self.filter,
                ignore_permission_denied=True,
                max_depth=self.cfg.scan_max_depth,
            )
            stack.callback(adapter.close)
            # Нужен в _handle_new_dirs: на каталог, перемещённый внутрь
            # наблюдаемого дерева, наблюдение приходится ставить вручную.
            self._adapter = adapter
            try:
                # Не синхронный вход в контекст: постановка inotify-наблюдения
                # обходит всё дерево и заняла бы event loop целиком.
                watcher = await adapter.start_async()
            except OSError as exc:
                # errno нужен в логе: ENOSPC/EMFILE — упёрлись в лимит
                # fs.inotify.max_user_watches, ENOENT/ENOTDIR — каталог исчез
                # между проверкой и стартом эмиттера.
                lg.error(
                    "не удалось начать наблюдение (errno=%s %s), watcher "
                    "остановлен; при ENOSPC/EMFILE поднимите "
                    "fs.inotify.max_user_watches",
                    exc.errno,
                    exc.strerror,
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
                    if not check_path(Path(event.path)):
                        # пропускаем изменения в исключаемых местах
                        continue
                    target = dir_paths if event.is_directory else file_paths
                    target.add(os.path.abspath(event.path))

                await self._handle_missing_dirs(dir_paths)
                await self._handle_new_dirs(dir_paths)
                await self._handle_files(file_paths)

    async def _handle_missing_dirs(self, dir_paths: set[str]) -> None:
        """Снять с индекса поддеревья исчезнувших каталогов."""
        for path in sorted(dir_paths):
            if os.path.isdir(path):
                continue
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

        Событий по вложенным файлам может не прийти вовсе: перенос каталога —
        это один rename, ядро шлёт единственный IN_MOVED_TO по самому
        каталогу. Раньше до адаптера не доходило даже это (DirCreatedEvent не
        был подписан), и каталог, перенесённый снаружи внутрь, оставался
        непроиндексированным до перезапуска демона.

        Одним механизмом закрываются три сценария: перенос каталога извне,
        каталог, ставший читаемым, и подкаталог, созданный после старта.
        Переименование внутри дерева тоже: старый путь снимается с индекса в
        _handle_missing_dirs, новый обходится здесь.
        """
        adapter = getattr(self, "_adapter", None)
        for path in sorted(dir_paths):
            if not os.path.isdir(path):
                # Каталога нет — это ветка _handle_missing_dirs.
                continue
            if adapter is not None:
                # У IN_MOVED_TO нет бита IN_CREATE, поэтому watchdog не
                # добавляет watch на ПЕРЕМЕЩЁННЫЙ внутрь каталог, хотя на
                # созданный обычным mkdir добавляет. Без этого файлы,
                # появившиеся в нём уже после переноса, не замечаются.
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
                await self.on_upsert(metas)

    async def _handle_files(self, file_paths: set[str]) -> None:
        """Развести пути на удалённые и живые ПО ФАКТУ, а не по типу события.

        Порядок и склейка событий отличаются между FSEvents, inotify и Windows:
        удаление легко перекрывается более поздним `modified` по тому же пути.
        Тогда запись остаётся в индексе навсегда — полный проход удалённые
        файлы не замечает.
        """
        missing = [p for p in file_paths if not os.path.exists(p)]
        existing = [p for p in file_paths if os.path.exists(p)]

        for path in missing:
            try:
                await self.on_delete(self._get_file_metadata(path))
            except Exception as e:
                lg.exception(f"Error handling delete {path}: {e}")

        if existing:
            try:
                await self.on_upsert([self._get_file_metadata(p) for p in existing])
            except Exception as e:
                lg.exception(f"Error handling changes {existing[:3]}...: {e}")

    @abc.abstractmethod
    async def on_delete(self, file: FileMetadata) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def on_delete_dir(self, path: str) -> None:
        """Снять с индекса всё поддерево исчезнувшего каталога."""
        raise NotImplementedError()

    @abc.abstractmethod
    async def on_upsert(self, files: list[FileMetadata]) -> None:
        """Проиндексировать пачку добавленных/изменённых файлов."""
        raise NotImplementedError()
