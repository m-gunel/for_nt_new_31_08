import os
import typing
from logging import Logger
from pathlib import Path

from src.common import FileMetadata
from src.parsers import check_has_parser
from src.utils import get_logger

from .base_watcher import AbstractFilesWatcher

if typing.TYPE_CHECKING:
    from src.config import Config

    from ..indexer import Index

lg: Logger = get_logger(__name__, slice=-1)


class Watcher(AbstractFilesWatcher):
    def __init__(self, cfg: "Config", index: "Index") -> None:
        super().__init__()
        self.index = index

    async def on_delete(self, file: FileMetadata) -> None:
        lg.info(f"File deleted: {file.path}")
        await self.index.delete_path(file.path)
        # Поиску удаление видно и без этого: delete_where помечает хендл
        # таблицы устаревшим (_mark_stale). Здесь — обслуживание индексов с
        # дебаунсом: дообновление, в простое — компакция.
        await self.index.ensure_index()

    async def on_delete_many(self, files: list[FileMetadata]) -> None:
        """Снять с индекса пачку файлов одним фильтром.

        Не по файлу за раз: перенос папки в корзину даёт сотни удалений, а
        delete_path на каждый — по версии Lance и deletion-файлу.
        """
        for file in files:
            lg.info(f"File deleted: {file.path}")
        await self.index.delete_paths([file.path for file in files])
        # Поиску удаление видно и без этого: delete_where помечает хендл
        # таблицы устаревшим (_mark_stale). Здесь — обслуживание индексов с
        # дебаунсом: дообновление, в простое — компакция.
        await self.index.ensure_index()

    async def on_delete_dir(self, path: str) -> None:
        lg.info(f"Directory deleted: {path}")
        # Со слэшем на конце, иначе удаление /a/b задело бы и /a/bc.
        await self.index.delete_prefix(path.rstrip(os.sep) + os.sep)
        await self.index.ensure_index()

    async def on_upsert(self, files: list[FileMetadata]) -> None:
        """Проиндексировать пачку файлов одним проходом.

        Не по файлу за раз: внутри `process_stream` уже есть батчинг, и при
        массовом копировании поштучный вызов даёт сотни мелких записей
        в LanceDB вместо нескольких крупных.
        """
        filter_meta = self.cfg.file_scan_cfg.filter_meta
        to_index: list[FileMetadata] = []
        for file in files:
            if not check_has_parser(Path(file.path)):
                continue
            # max_size — как в FilesScanner.preindex: иначе многогигабайтный
            # файл из наблюдаемой папки уходит прямо в парсер.
            if filter_meta is not None and not filter_meta(file):
                continue
            if not await self.index.check_meta(file):
                continue
            to_index.append(file)

        if not to_index:
            return

        lg.info("indexing %s changed file(s)", len(to_index))
        for f in to_index:
            lg.debug("changed: %s", f.path)
        # Учитывается в /status: иначе демон выглядит простаивающим, пока
        # разбирает пачку файлов.
        self.index.watch_pending += len(to_index)
        try:
            await self.index.process_stream(to_index, count=False)
        finally:
            self.index.watch_pending = max(0, self.index.watch_pending - len(to_index))
        # Индексы строятся в конце scan_all, мимо которого идут файлы из
        # watcher'а: без этого FTS падает с «no INVERTED index».
        await self.index.ensure_index()
