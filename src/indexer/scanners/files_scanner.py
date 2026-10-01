from pathlib import Path

from src.parsers import FileMetadata
from src.utils import get_logger, wrap_iter_to_async
from src.utils.lsm_adapter import LSMTable

from ..scan_dir import iter_file_metadata
from .base import AbstractBaseScanner

lg = get_logger("app.file_scan")


class FilesScanner(AbstractBaseScanner):
    def __init__(
        self,
        lsm_checker: LSMTable[FileMetadata],
        queue_path: Path,
        reset: bool = True,
    ) -> None:
        super().__init__(
            queue_path=queue_path,
            lsm_checker=lsm_checker,
            reset=reset,
        )

    async def preindex(self):
        filter_meta = self.cfg.file_scan_cfg.filter_meta
        for path in self.cfg.scan_paths:
            lg.info(f"Scanning path: {path}")
            found = 0
            async for metadata in wrap_iter_to_async(
                iter_file_metadata(
                    path,
                    filter_func=self.cfg.file_scan_cfg.filter_files,
                    # Умолчание самой iter_file_metadata оставлено False,
                    # чтобы прямые вызовы (в том числе из тестов) не меняли
                    # поведение молча. Демон идёт по ссылкам осознанно.
                    follow_symlinks=self.cfg.file_scan_cfg.follow_symlinks,
                )
            ):
                # max_size должен работать и здесь, не только в watcher'е.
                if filter_meta is not None and not filter_meta(metadata):
                    continue
                found += 1
                yield metadata
            # Ноль по существующему пути — тоже сигнал: ни одного файла с
            # поддерживаемым расширением.
            lg.info("путь %s: найдено файлов — %s", path, found)
