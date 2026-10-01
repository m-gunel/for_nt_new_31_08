from watchfiles import BaseFilter, Change

from src.utils import ConfigMixin


class Filter(BaseFilter, ConfigMixin):
    def __call__(self, change: Change, path: str) -> bool:
        ret = (
            self.cfg.file_scan_cfg.filter_files(path)
            if self.cfg.file_scan_cfg.filter_files
            else True
        )
        if not ret:
            return False
        ret = (
            self.cfg.file_scan_cfg.filter_dirs(path)
            if self.cfg.file_scan_cfg.filter_dirs
            else True
        )
        if not ret:
            return False
        return True
