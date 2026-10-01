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
# пропустить неизменившуюся папку, ни заметить компакцию mbox.
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

        Только на известные: в конфиг легко попадают посторонние имена —
        Swagger подставляет в тело запроса `additionalProp1`, и такие ключи
        раньше создавали папки, которых нет на диске. Каждый проход потом
        писал по ним «mbox file not found».
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
        """Сказать вслух, что offsets.json победил offsets из конфига.

        Приоритет такой намеренно: сохранённое состояние отражает то, что
        реально прочитано. Но раньше конфиг перекрывался молча, и попытка
        поддержки «выставьте offsets и перечитайте папку» выглядела как
        неработающая ручка — API отвечал 200, а offset оставался прежним.
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

        Файл пишется неатомарно, поэтому переживает обрыв процесса в
        полузаписанном виде. Раньше JSONDecodeError отсюда поднимался до
        конструктора Index и убивал старт демона целиком — вместе с
        индексацией обычных файлов.
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

        Офсеты из конфига здесь НЕ применяются, хотя в конструкторе применяются.
        Разница принципиальна: `clear()` зовут вместе с `delete_prefix("email_")`
        при выключении почты, то есть индекс уже стёрт. Вернув сюда офсет из
        конфига, мы бы начали следующее чтение с его позиции — и все письма до
        неё, только что удалённые из индекса, не вернулись бы никогда.
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
        удалить из индекса всё по префиксу ``email_<folder>:`` — состояние
        папки здесь же сбрасывается, так что следующий scan() переиндексирует
        её с нуля.

        Два случая:
        - mbox исчез, а мы из него что-то индексировали;
        - mbox компактился: файл стал короче сохранённого offset'а/размера
          или offset больше не указывает на начало письма. Старые ключи
          (offset-диапазоны) после этого указывают на чужие байты.

        Ограничение: компакция, совпавшая с притоком новых писем большим,
        чем удалённый объём, по размеру не детектится — её ловит проверка
        ``From `` по offset'у. Точечная сверка по Message-ID — отдельная
        оптимизация (см. план), пока папка переиндексируется целиком.
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
                    self.folder_state[name] = FolderState()
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
                self.folder_state[name] = FolderState()
        return stale

    def refresh_folders(self) -> list[str]:
        """Добавить в состояние папки, появившиеся на диске после прошлого прохода.

        Демон живёт долго, а сканер создаётся один раз, поэтому читать
        список папок только в конструкторе нельзя: папка, созданная в
        Thunderbird при работающем демоне, иначе не проиндексируется до
        рестарта. Возвращает имена новых папок (для лога).
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
        # list(): refresh_folders/детект могли изменить словарь.
        for prefix, state in list(self.folder_state.items()):
            p = self.path / prefix
            if not p.is_file():
                # Папка есть в состоянии (конфиг или offsets.json), но файла
                # на диске нет — например, «Входящие» не синхронизированы
                # локально. Раньше это роняло весь проход, включая файлы.
                lg.warning("email folder %s: mbox file not found, skipping", prefix)
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # Снимок до обхода: изменения, пришедшие во время скана, увидит
            # следующий цикл (mtime уже будет другим).
            size, mtime = st.st_size, int(st.st_mtime)
            if size == state.size and mtime == state.mtime:
                # Файл не менялся с прошлого прохода — не читаем его вовсе.
                # На поллинге раз в email_poll_interval это основной путь.
                lg.debug("email preindex %s: mbox unchanged, skipping", prefix)
                continue
            # Фазовые логи на INFO: обход большого mbox идёт десятки секунд,
            # и без них по логу не отличить «читаю mbox» от «завис».
            to_read = max(0, size - state.offset)
            lg.info(
                "почта %s: читаю mbox (%.1f МБ из %.1f МБ, offset=%s)",
                prefix,
                to_read / _MB,
                size / _MB,
                state.offset,
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
                        start_offset=state.offset,
                        min_date=self.cfg.email_begin_date,
                        progress=progress,
                    )
                ):
                    if meta is None:
                        # Письмо старше email.index_last_n_days — в очередь
                        # не попадает, эмбеддинг для него не считается.
                        skipped_old += 1
                    elif meta.size > max_message_size:
                        # Размер известен из TOC, так что письмо отсекается,
                        # ни разу не прочитанное: разбор MIME держит его в
                        # памяти целиком, и на вложении в 80 МБ это заметный
                        # скачок RSS ради пары строк текста.
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
                        done = max(0, start - state.offset)
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
                    offset=max_offset, size=size, mtime=mtime
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
