import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple

import rich
import rich.progress

from src.common import FileMetadata
from src.thunderbird.lazy_iter_mbox import iter_emails_as_metadata
from src.utils import get_logger, wrap_iter_to_async
from src.utils.lsm_adapter import LSMTable

from .base import AbstractBaseScanner

lg = get_logger("app.email")

# Версия формата offsets.json. v1 — плоский {folder: offset}; v2 добавляет
# size/mtime файла на момент последнего успешного обхода: без них нельзя ни
# пропустить неизменившуюся папку, ни заметить компакцию mbox. Поля
# min_date/max_size версию не меняют: старый код их пропускает, а файл с
# version=3 он разобрал бы как v1 и принял ключ "version" за папку.
_STATE_VERSION = 2

_UNKNOWN = -1

_MB = 1024 * 1024

# Как часто докладывать о прогрессе чтения mbox: обход многогигабайтного
# ящика идёт минутами, и без периодических отметок неясно, жив ли он.
_PROGRESS_EVERY_SECONDS = 15.0


class FolderState(NamedTuple):
    offset: int = 0
    size: int = _UNKNOWN
    mtime: int = _UNKNOWN
    # Границы, с которыми папка прочитана: нижняя дата окна index_last_n_days
    # (unix-время) и max_message_size. Письма за ними пропущены, а offset ушёл
    # дальше них. None — без ограничения или неизвестно (файл старой версии).
    min_date: int | None = None
    max_size: int | None = None


def _limits_widened(state: FolderState, min_date: int | None, max_size: int) -> bool:
    """Шире ли текущие границы тех, с которыми папка уже прочитана.

    None в состоянии перечитывания не требует: без ограничения шире некуда,
    а границы из offsets.json старой версии неизвестны, и полный обход всех
    папок после обновления был бы лишним.
    """
    date_widened = state.min_date is not None and (
        min_date is None or min_date < state.min_date
    )
    size_widened = state.max_size is not None and max_size > state.max_size
    return date_widened or size_widened


def _optional_int(value) -> int | None:
    # min_date/max_size есть не во всех v2-файлах.
    return None if value is None else int(value)


class EmailsScanner(AbstractBaseScanner):
    def __init__(
        self,
        queue_path: Path,
        email_path: Path,
        folders: list[str],
        lsm_checker: LSMTable[FileMetadata],
        reset: bool = False,
    ):
        super().__init__(
            queue_path=queue_path,
            lsm_checker=lsm_checker,
            reset=reset,
        )
        self.path = email_path
        # Свежий список папок с диска — основа состояния: папку, созданную
        # после прошлого прохода, иначе не увидит ни preindex, ни
        # detect_stale_folders, и её письма не проиндексируются никогда.
        self.folder_state = {n: FolderState() for n in folders}
        self._apply_configured_offsets()
        if (p := queue_path.parent / "offsets.json").exists() and not reset:
            # Сохранённое состояние поверх: у известных папок — их реальные
            # offset/size/mtime, у новых остаётся FolderState() (полный обход).
            saved = self._load_state(p)
            self._warn_about_overridden_offsets(saved)
            self.folder_state.update(saved)

    def _apply_configured_offsets(self) -> None:
        """Наложить offsets из конфига на состояние уже известных папок.

        Только на известные: Swagger подставляет в тело запроса
        `additionalProp1`, и такие ключи создавали бы несуществующие папки.
        """
        email_cfg = self.cfg.email_config
        if not (email_cfg and email_cfg.offsets):
            return
        for name, offset in email_cfg.offsets.items():
            if name not in self.folder_state:
                lg.warning("offset для неизвестной почтовой папки %s пропущен", name)
                continue
            self.folder_state[name] = FolderState(offset=offset)

    def _warn_about_overridden_offsets(self, saved: dict[str, FolderState]) -> None:
        """Сообщить в лог, что offsets.json перекрыл offsets из конфига.

        Приоритет намеренный: сохранённое состояние отражает реально
        прочитанное. Без сообщения offsets в конфиге выглядят неработающими.
        """
        email_cfg = self.cfg.email_config
        if not (email_cfg and email_cfg.offsets):
            return
        overridden = [
            name
            for name, offset in email_cfg.offsets.items()
            if name in saved and saved[name].offset != offset
        ]
        if overridden:
            lg.info(
                "offsets из конфига перекрыты сохранённым offsets.json для папок: %s; "
                "чтобы конфиг вступил в силу, удалите файл состояния",
                overridden,
            )

    @staticmethod
    def _load_state(p: Path) -> dict[str, FolderState]:
        """Прочитать offsets.json. Битый файл — не повод не индексировать.

        _save_state пишет атомарно, но файл всё равно может оказаться битым:
        ручная правка, сбой диска, файл от старой версии демона. Исключение
        отсюда ушло бы в конструктор Index и остановило бы старт демона вместе
        с индексацией файлов.
        """
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, dict) and raw.get("version") == _STATE_VERSION:
                folders = raw.get("folders")
                if not isinstance(folders, dict):
                    raise ValueError("v2 state without 'folders' mapping")
                return {
                    name: FolderState(
                        offset=int(st.get("offset", 0)),
                        size=int(st.get("size", _UNKNOWN)),
                        mtime=int(st.get("mtime", _UNKNOWN)),
                        min_date=_optional_int(st.get("min_date")),
                        max_size=_optional_int(st.get("max_size")),
                    )
                    for name, st in folders.items()
                    if isinstance(st, dict)
                }
            if not isinstance(raw, dict):
                raise ValueError(f"unexpected state type: {type(raw).__name__}")
            # v1: плоский {folder: offset}. size/mtime неизвестны — пропуск
            # неизменившихся папок заработает после первого прохода.
            return {
                name: FolderState(offset=off)
                for name, off in raw.items()
                if isinstance(off, int)
            }
        except (OSError, ValueError, TypeError) as e:
            lg.warning(
                "offsets.json повреждён (%s), почта будет переиндексирована с нуля: %s",
                p,
                e,
            )
            return {}

    def _dump_state(self) -> dict:
        return {
            "version": _STATE_VERSION,
            "folders": {name: st._asdict() for name, st in self.folder_state.items()},
        }

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback,
    ) -> bool | None:
        try:
            await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            self._save_state()

    def _save_state(self) -> None:
        """Атомарно записать offsets.json: временный файл + os.replace.

        Прямая запись в целевой файл оставляет его пустым или обрезанным,
        если процесс умер посреди дампа, — и следующий старт читает мусор.
        """
        os.makedirs(self.queu_path.parent, exist_ok=True)
        target = self.queu_path.parent / "offsets.json"
        tmp = target.with_suffix(".json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._dump_state(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError:
            lg.exception("не удалось сохранить %s", target)
            with suppress(OSError):
                os.remove(tmp)

    async def clear(self):
        """Полный сброс почтового состояния: очередь, offsets.json, позиции.

        Офсеты из конфига здесь НЕ применяются, в отличие от конструктора:
        `clear()` зовут вместе с `delete_prefix("email_")`, и чтение с офсета
        навсегда потеряло бы письма до него, уже стёртые из индекса.
        """
        await super().clear()
        if (p := self.queu_path.parent / "offsets.json").exists():
            os.remove(p)
        self.folder_state = {n: FolderState() for n in self.folder_state}

    def _offset_still_valid(self, mbox: Path, state: FolderState) -> bool:
        """Указывает ли сохранённый offset на начало письма.

        offset хранит начало последнего обработанного письма, поэтому в
        живом файле по нему стоит ``From ``-строка. После компакции
        (Thunderbird переписывает mbox при окончательном удалении писем)
        все смещения сдвигаются, и это почти наверняка не так.
        """
        if state.offset <= 0:
            return True
        try:
            with open(mbox, "rb") as f:
                f.seek(state.offset)
                return f.read(5) == b"From "
        except OSError:
            return False

    def detect_stale_folders(self) -> list[str]:
        """Папки, чей индекс больше не соответствует файлу на диске.

        Возвращает имена папок, для которых вызывающая сторона должна
        удалить из индекса всё по префиксу ``email_<folder>:`` и затем
        вызвать `forget_folder`. Состояние здесь не сбрасывается: если
        удаление упадёт или проход отменят, папка будет найдена снова.

        Два случая:
        - mbox исчез, а мы из него что-то индексировали;
        - mbox компактился: файл стал короче сохранённого offset'а/размера
          или offset больше не указывает на начало письма. Старые ключи
          (offset-диапазоны) после этого указывают на чужие байты.

        Компакцию, совпавшую с притоком писем больше удалённого объёма, по
        размеру не видно — её ловит проверка ``From `` по offset'у. Папка
        переиндексируется целиком.
        """
        self.refresh_folders()
        stale: list[str] = []
        for name, state in list(self.folder_state.items()):
            mbox = self.path / name
            indexed_something = state.offset > 0 or state.size != _UNKNOWN
            if not mbox.is_file():
                if indexed_something:
                    lg.warning(
                        "email folder %s: mbox vanished, dropping its index",
                        name,
                    )
                    stale.append(name)
                else:
                    # Удалять из индекса нечего, а оставленный ключ давал бы
                    # WARNING «mbox file not found» на каждом поллинге.
                    del self.folder_state[name]
                continue
            if not indexed_something:
                continue
            try:
                st = mbox.stat()
            except OSError:
                continue
            shrunk = st.st_size < max(state.offset, state.size)
            if shrunk or not self._offset_still_valid(mbox, state):
                lg.warning(
                    "email folder %s: mbox was compacted "
                    "(size %s -> %s, offset %s), reindexing from scratch",
                    name,
                    state.size,
                    st.st_size,
                    state.offset,
                )
                stale.append(name)
        return stale

    def forget_folder(self, name: str) -> None:
        """Сбросить состояние папки после удаления её чанков из индекса.

        Исчезнувшую папку убираем из состояния совсем: иначе preindex писал
        бы по ней WARNING на каждом поллинге. Компактированную обнуляем —
        следующий scan() перечитает её с нуля.
        """
        if (self.path / name).is_file():
            self.folder_state[name] = FolderState()
        else:
            self.folder_state.pop(name, None)

    def refresh_folders(self) -> list[str]:
        """Добавить в состояние папки, появившиеся на диске после прошлого прохода.

        Сканер создаётся один раз на жизнь демона: без этого папка, созданная
        в Thunderbird при работающем демоне, ждала бы рестарта. Возвращает
        имена новых папок (для лога).
        """
        from src.thunderbird.find_files import list_email_folders

        new = [
            name
            for name in list_email_folders(self.path)
            if name not in self.folder_state
        ]
        for name in new:
            self.folder_state[name] = FolderState()
        return new

    async def preindex(self):
        # `email_enabled` — единый источник истины: он трактует «не настроено»
        # как «выключено», иначе почтовые папки обходятся при отсутствующей
        # секции email.
        if not self.cfg.email_enabled:
            return
        if new_folders := self.refresh_folders():
            lg.info("обнаружены новые почтовые папки: %s", len(new_folders))
        max_message_size = self.cfg.email_scan_cfg.max_message_size
        # Одна граница на проход: ею читаем и её же запоминаем в состоянии.
        min_date = self.cfg.email_begin_date
        min_ts = int(min_date.timestamp()) if min_date else None
        # list(): refresh_folders/детект могли изменить словарь.
        for prefix, state in list(self.folder_state.items()):
            p = self.path / prefix
            if not p.is_file():
                # Папка есть в состоянии (конфиг или offsets.json), а файла
                # нет — например, «Входящие» не синхронизированы локально.
                lg.warning("email folder %s: mbox file not found, skipping", prefix)
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # Снимок до обхода: изменения, пришедшие во время скана, увидит
            # следующий цикл (mtime уже будет другим).
            size, mtime = st.st_size, int(st.st_mtime)
            # Окно или лимит размера расширили: письма, отсечённые прежними
            # границами, лежат до offset'а, и чтением хвоста их не достать.
            # Уже проиндексированные отсеет check_meta в scan().
            reread = _limits_widened(state, min_ts, max_message_size)
            if reread:
                lg.info(
                    "почта %s: окно или лимит размера расширены — читаю mbox с начала",
                    prefix,
                )
            elif size == state.size and mtime == state.mtime:
                # Файл не менялся с прошлого прохода — не читаем его вовсе.
                # На поллинге раз в email_poll_interval это основной путь.
                lg.debug("email preindex %s: mbox unchanged, skipping", prefix)
                continue
            start_offset = 0 if reread else state.offset
            # Фазовые логи на INFO: обход большого mbox идёт десятки секунд,
            # и без них по логу не отличить «читаю mbox» от «завис».
            to_read = max(0, size - start_offset)
            lg.info(
                "почта %s: читаю mbox (%.1f МБ из %.1f МБ, offset=%s)",
                prefix,
                to_read / _MB,
                size / _MB,
                start_offset,
            )
            cnt = 0
            skipped_old = 0
            skipped_big = 0
            max_offset = state.offset
            started = time.monotonic()
            next_report = started + _PROGRESS_EVERY_SECONDS
            with rich.progress.Progress(
                disable=not os.environ.get("SHOW_PROGRESS") == "Y"
            ) as progress:
                async for start, meta in wrap_iter_to_async(
                    iter_emails_as_metadata(
                        str(p),
                        prefix=f"email_{prefix}:",  # type: ignore
                        start_offset=start_offset,
                        min_date=min_date,
                        progress=progress,
                    )
                ):
                    if meta is None:
                        # Письмо старше email.index_last_n_days — в очередь
                        # не попадает, эмбеддинг для него не считается.
                        skipped_old += 1
                    elif meta.size > max_message_size:
                        # Размер известен из TOC, и письмо отсекается
                        # непрочитанным: разбор MIME держал бы его в памяти
                        # целиком.
                        skipped_big += 1
                        lg.debug(
                            "почта %s: письмо %s Б больше лимита %s Б — пропущено",
                            prefix,
                            meta.size,
                            max_message_size,
                        )
                    else:
                        yield meta
                        cnt += 1
                    max_offset = max(max_offset, start)
                    if (now := time.monotonic()) >= next_report:
                        next_report = now + _PROGRESS_EVERY_SECONDS
                        done = max(0, start - start_offset)
                        share = done / to_read if to_read else 1.0
                        elapsed = now - started
                        eta = elapsed / share - elapsed if share > 0.01 else None
                        lg.info(
                            "почта %s: %.0f%% (%s в очередь, %s старых пропущено, "
                            "%s пропущено по размеру, %.0f c прошло%s)",
                            prefix,
                            share * 100,
                            cnt,
                            skipped_old,
                            skipped_big,
                            elapsed,
                            ", осталось ~{0:.0f} c".format(eta) if eta else "",
                        )
                self.folder_state[prefix] = FolderState(
                    offset=max_offset,
                    size=size,
                    mtime=mtime,
                    min_date=min_ts,
                    max_size=max_message_size,
                )
                lg.info(
                    "почта %s: mbox прочитан за %.0f c — %s писем в очередь, "
                    "%s пропущено по дате, %s пропущено по размеру",
                    prefix,
                    time.monotonic() - started,
                    cnt,
                    skipped_old,
                    skipped_big,
                )
