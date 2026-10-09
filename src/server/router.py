import asyncio
import email
import os
import re
import shutil
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Body, HTTPException, Response

from src.common import IndexingStatus
from src.indexer import AppIndex, reconfigure_lock, restart_watcher, stop_watcher
from src.indexer.exclude import read_refusal
from src.parsers import get_parser, read_document_text
from src.server.model import (
    HealthCheckResponse,
    IndexingControlResponse,
    ReadFileRequest,
    ReadFileResponse,
    RequestFilePath,
    ResponseFilePath,
    SearchRequest,
    SearchResponse,
    UserConfig,
    UserConfigUpdate,
)
from src.thunderbird.lazy_iter_mbox import _decode_filename
from src.utils import AppConfig, get_logger
from src.utils.filename import clean_filename

lg = get_logger(__name__)

router = APIRouter()

# Выданные наружу временные файлы никто не удаляет: держим окно последних
# выдач и подчищаем самые старые.
_MAX_TEMP_DIRS = 32
# И по объёму: 32 письма по 48 МиБ — до 1,5 ГиБ, а /tmp на АРМ обычно tmpfs,
# то есть оперативная память.
_MAX_TEMP_BYTES = 256 * 1024 * 1024
_temp_dirs: deque[tuple[Path, int]] = deque()
_temp_dirs_bytes = 0
_temp_dirs_lock = threading.Lock()


def _register_temp_dir(path: Path, size: int) -> None:
    """Взять каталог выдачи под учёт и вытеснить старые.

    Размер передаётся явно: каталог регистрируется до записи файла, чтобы
    не потерять его из учёта при сбое записи, — обход каталога в этот момент
    дал бы ноль.
    """
    global _temp_dirs_bytes
    with _temp_dirs_lock:
        _temp_dirs.append((path, size))
        _temp_dirs_bytes += size
        while _temp_dirs and (
            len(_temp_dirs) > _MAX_TEMP_DIRS or _temp_dirs_bytes > _MAX_TEMP_BYTES
        ):
            # Последнюю выдачу не удаляем даже при превышении лимита: её только
            # что запросил клиент, и путь уже уехал ему в ответе.
            if len(_temp_dirs) == 1:
                break
            old, old_size = _temp_dirs.popleft()
            _temp_dirs_bytes -= old_size
            shutil.rmtree(old, ignore_errors=True)


@router.get("/health")
async def health_check() -> HealthCheckResponse:
    return HealthCheckResponse()


@router.get("/status", description="Текущий статус индексирования")
async def status_check(index: AppIndex) -> IndexingStatus:
    return await index.status()


# Примеры для «Try it out» в /docs: иначе Swagger заполняет каждый фильтр
# заглушкой «string», и запрос отвергается или молча ничего не находит.
_SEARCH_EXAMPLES = {
    "no_filters": {
        "summary": "Без фильтров",
        "value": {
            "query": "что такое википедия",
            "limit": 10,
            "query_type": "hybrid",
            "sources": ["files"],
        },
    },
    "with_filters": {
        "summary": "С фильтрами — подставьте свои значения, лишние удалите",
        "value": {
            "query": "отпуск",
            "limit": 10,
            "filters": {
                "ext": ["docx", "pdf"],
                "folder": "~/Документы",
                "modified_from": "2024-01-01",
                "modified_to": "2024-12-31",
                "author": "Иванов",
            },
        },
    },
}


@router.post(
    "/search",
    description=(
        "Поиск документов: limit — документов на источник, у каждого до трёх "
        "лучших фрагментов. Файлы, в имени которых есть код из запроса "
        "(«ЦБ_21», «5348-У»), идут первыми. filters — необязательный отбор "
        "по расширению, папке, датам и автору: без него и при пустых полях "
        "ищется весь индекс"
    ),
)
async def search(
    request: Annotated[SearchRequest, Body(openapi_examples=_SEARCH_EXAMPLES)],
    index: AppIndex,
) -> SearchResponse:
    # По limit индексатор берёт глубокий пул чанков, а SearchResponse собирает
    # из него не больше limit документов. pinned заполняет индексатор: это
    # документы, найденные по коду из имени файла, в выдаче они первые.
    pinned: list[str] = []
    results = await index.search(
        request.query,
        top_k=request.limit,
        query_type=request.query_type,
        sources=request.sources,
        pinned=pinned,
        where=request.filters.where() if request.filters else None,
    )
    return SearchResponse.from_dataframes(results, limit=request.limit, pinned=pinned)


@router.get("/config")
async def get_config(cfg: AppConfig):
    return cfg.user_config_with_default


def _merge_user_config(current: UserConfig, incoming: UserConfig) -> UserConfig:
    """Частичный апдейт: не присланные поля не затирают сохранённые.

    Иначе POST {"folders": [...]} сбрасывал бы настройки почты в дефолт.
    """
    data = current.model_dump()
    for key, value in incoming.model_dump(exclude_unset=True).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return UserConfig.model_validate(data)


# Каталоги ОС: документов пользователя в них нет, а обход и inotify по ним —
# это вся система (/usr) или псевдо-ФС (/proc, /sys). /root не в списке: под
# root демон не запускается (ConditionUser=!@system), а у root это дом.
_SYSTEM_DIRS = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
)


def _forbidden_folder(folder: str) -> bool:
    """Корень ФС, системный каталог или путь со скрытым компонентом.

    Список папок — граница чтения /read_file и local_read_file, а меняет его
    агент: инструкция в документе «добавь / (или ~/.config) в индекс» открыла
    бы ему всю ФС или конфиги с токенами.
    """
    path = os.path.abspath(os.path.expanduser(folder))
    if path == os.sep or os.path.realpath(path) == os.sep:
        return True
    if any(path == d or path.startswith(d + os.sep) for d in _SYSTEM_DIRS):
        return True
    return any(part.startswith(".") for part in Path(path).parts)


def _validate_folders(folders: list[str] | None) -> None:
    """Отклонить пути, которых нет на диске, и опасные корни.

    На несуществующем пути watcher падает на inotify целиком и перестаёт
    отслеживать все папки, хотя API ответил 200 (Swagger подставляет "string").
    """
    if folders is None:
        return
    bad = [p for p in folders if not Path(p).expanduser().is_dir()]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"не существуют или не являются каталогами: {bad}",
        )
    forbidden = [p for p in folders if _forbidden_folder(p)]
    if forbidden:
        raise HTTPException(
            status_code=400,
            detail=f"корень ФС, системные и скрытые каталоги не индексируются: {forbidden}",
        )


@router.post("/config")
async def update_config(
    user_config: UserConfigUpdate,
    cfg: AppConfig,
    index: AppIndex,
) -> Response:
    # До захвата блокировки и до записи на диск: при отказе конфиг должен
    # остаться ровно тем, что был.
    if "folders" in user_config.model_fields_set and not user_config.folders:
        # scan_paths понял бы [] и null как «настройки нет» и обходил бы пути
        # из YAML — обратное тому, о чём просили.
        raise HTTPException(
            status_code=400,
            detail=(
                "folders: пустой список и null не принимаются; "
                "остановить индексацию — POST /api/v1/deactivate"
            ),
        )
    _validate_folders(user_config.folders)
    # Перезапуск watcher'а и индексации — не атомарная последовательность.
    # Без блокировки два параллельных POST /config гонятся за общим состоянием
    # (_watcher_task/_index) и оставляют его повреждённым.
    async with reconfigure_lock():
        prev_paths = set(str(Path(p).expanduser().absolute()) for p in cfg.scan_paths)
        prev_email_enabled = cfg.email_enabled
        # max_depth читается только в AbstractFilesWatcher.run(): новая глубина
        # вступает в силу лишь после перезапуска наблюдения.
        prev_max_depth = cfg.scan_max_depth
        cfg.update_user_config(
            _merge_user_config(cfg.user_config_with_default, user_config)
        )
        folders_changed = user_config.folders is not None
        depth_changed = cfg.scan_max_depth != prev_max_depth
        if folders_changed:
            new_paths = set(
                str(Path(p).expanduser().absolute())
                for p in user_config.folders  # type: ignore[union-attr]
            )
            await stop_watcher()
            await index.stop_index()
            if to_delete := prev_paths - new_paths:
                for p in to_delete:
                    # Со слэшем: иначе delete_prefix для ~/Документы задел бы
                    # и ~/Документы_архив (так же в Watcher.on_delete_dir).
                    await index.delete_prefix(p.rstrip(os.sep) + os.sep)
        elif depth_changed:
            # Изменилась только глубина: индексация её не учитывает,
            # перезапускается лишь наблюдение.
            await stop_watcher()
        email_enabled = cfg.email_enabled
        # Явное выключение в запросе — всегда «убрать почту из индекса»:
        # prev_email_enabled читается с диска, и после ручной правки или
        # удаления user_conf.yml перехода «вкл → выкл» нет, а старые письма
        # остались бы в выдаче.
        disable_requested = (
            "email" in user_config.model_fields_set and not user_config.email.enabled
        )
        if email_enabled and not prev_email_enabled:
            # Сканер почты создаётся лениво, первый проход — сразу, но только на
            # активном индексе (БТ §5.2); иначе поллинг поднимет /activate.
            if index.active:
                await index.start_emails_polling(immediate=True)
            else:
                lg.info(
                    "почта включена, но индексация деактивирована: "
                    "поллинг начнётся после POST /api/v1/activate"
                )
        elif not email_enabled and (prev_email_enabled or disable_requested):
            await index.stop_emails_polling()
            await index.drop_email_index()
        # Перезапуск — после работы с почтой: фоновая задача start_reindex()
        # живёт вне reconfigure_lock и писала бы в БД состояния одновременно
        # с drop_email_index().
        if folders_changed:
            if index.active:
                await index.start_reindex()
                await restart_watcher()
            else:
                # БТ §5.4: при остановленной индексации список сохраняется,
                # сканирование ждёт следующего запуска.
                lg.info(
                    "индексация деактивирована: новый список папок сохранён, "
                    "сканирование начнётся после POST /api/v1/activate"
                )
        elif depth_changed:
            if index.active:
                # Без переиндексации: max_depth ограничивает только
                # inotify-наблюдения (_dirs_for_watch), а iter_file_metadata
                # обходит дерево целиком.
                await restart_watcher()
            else:
                lg.info(
                    "индексация деактивирована: новая глубина обхода "
                    "сохранена, наблюдение поднимется после POST /api/v1/activate"
                )
    return Response(status_code=200)


@router.post("/activate", description="Включить индексацию — БТ Библиотеки §5.1")
async def activate_indexing(index: AppIndex) -> IndexingControlResponse:
    """Запустить индексацию и наблюдение за файловой системой.

    Идемпотентна: на уже активном индексе идущий проход не прерывается.
    """
    async with reconfigure_lock():
        changed = await index.activate()
        await restart_watcher()
    return IndexingControlResponse(
        active=True,
        changed=changed,
        detail="индексация запущена" if changed else "индексация уже была включена",
    )


@router.post(
    "/deactivate",
    description="Остановить индексацию, сохранив индексы — БТ Библиотеки §5.2",
)
async def deactivate_indexing(index: AppIndex) -> IndexingControlResponse:
    """Прекратить сканирование и обработку новых файлов.

    Индексы остаются на диске, поиск по ним продолжает работать. Следующая
    активация продолжит с учётом изменений, а не начнёт с нуля.
    """
    async with reconfigure_lock():
        await stop_watcher()
        changed = await index.deactivate()
    return IndexingControlResponse(
        active=False,
        changed=changed,
        detail=(
            "индексация остановлена, индексы сохранены"
            if changed
            else "индексация уже была остановлена"
        ),
    )


@router.post(
    "/reset",
    description="Полная остановка и сброс до состояния «как после установки» — БТ §5.3",
)
async def reset_indexing(
    index: AppIndex,
    cfg: AppConfig,
    keep_config: bool = False,
) -> IndexingControlResponse:
    """Снести индексы и состояние. Индексация после сброса ВЫКЛЮЧЕНА.

    Иначе сброс немедленно запускал бы полный обход диска, а по БТ §5.3 он
    применяется в том числе при смене пользователя — то есть ровно тогда,
    когда лезть в файлы до явного согласия нельзя.

    `keep_config=true` оставляет пользовательский конфиг (список папок,
    настройки почты). По умолчанию он удаляется: «как после установки» —
    это и есть состояние без пользовательских настроек, ровно то, что делает
    переменная окружения RESET=1 на старте.

    В ответе `changed` всегда true, в отличие от /activate и /deactivate:
    сброс выполняется безусловно, и «уже было так» для него не определено —
    таблицы дропаются и состояние LSM пересоздаётся в любом случае.
    """
    removed_config = False
    async with reconfigure_lock():
        await stop_watcher()
        await index.reset_index()
        if not keep_config:
            path = cfg.user_conf_path
            removed_config = path.exists()
            path.unlink(missing_ok=True)
    detail = "индекс сброшен, индексация выключена"
    if removed_config:
        detail += "; пользовательский конфиг удалён"
    return IndexingControlResponse(active=False, changed=True, detail=detail)


def _extract_attachment(data: bytes, name: str) -> bytes | None:
    """Синхронный разбор письма — выполняется в отдельном потоке."""
    msg = email.message_from_bytes(data)
    if name == "body":
        return data
    for part in msg.walk():
        if raw_name := part.get_filename():
            if _decode_filename(raw_name) == name:
                return part.get_payload(decode=True)  # type: ignore
    return None


def _email_subject(data: bytes) -> str:
    msg = email.message_from_bytes(data)
    return _decode_filename(msg["Subject"]) or "message"  # type: ignore


# Ключ письма: "email_<папка>:<начало>:<конец>:<имя>". Двоеточие бывает и в
# имени папки, и в имени вложения, поэтому ни split, ни rsplit не годятся.
# Опора — два числовых поля посередине; ленивый `.+?` берёт самую левую пару.
_EMAIL_PATH_RE = re.compile(
    r"^email_(?P<prefix>.+?):(?P<start>\d+):(?P<stop>\d+):(?P<name>.*)$"
)


@router.post("/get_file_path")
async def get_file_path(req: RequestFilePath, cfg: AppConfig) -> ResponseFilePath:
    if not req.absolute_path.startswith("email_"):
        raise HTTPException(
            status_code=400,
            detail="absolute_path must start with email_",
        )
    if not cfg.email_enabled:
        raise HTTPException(
            status_code=400,
            detail="emails are disabled",
        )
    match = _EMAIL_PATH_RE.match(req.absolute_path)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail="absolute_path must look like email_<prefix>:<start>:<stop>:<name>",
        )
    folder = match["prefix"]
    name = match["name"]
    start, stop = int(match["start"]), int(match["stop"])
    if start < 0 or stop <= start:
        # f.read() с отрицательной длиной прочитал бы весь mbox в память.
        raise HTTPException(status_code=400, detail="invalid offsets in absolute_path")
    if stop - start > cfg.email_scan_cfg.max_message_size:
        # Диапазон задаёт клиент, а читается он в память целиком.
        raise HTTPException(
            status_code=400,
            detail="requested range exceeds the email size limit",
        )

    # Шаблон уже снял "email_", а email_files отдаёт голые имена папок.
    # email_files может обходить ~/.thunderbird — не в event loop.
    email_files = await asyncio.to_thread(lambda: list(cfg.email_files))
    file = {name: path for path, name in email_files}.get(folder)
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        async with aiofiles.open(file, "rb") as f:
            await f.seek(start)
            data = await f.read(stop - start)
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")

    # Разбор MIME — синхронный и потенциально долгий, уносим с event loop,
    # иначе большое вложение подвешивает все остальные запросы.
    payload = await asyncio.to_thread(_extract_attachment, data, name)
    if payload is None:
        raise HTTPException(status_code=404, detail="File not found")

    if name == "body":
        out_name = (
            clean_filename(await asyncio.to_thread(_email_subject, data)) + ".eml"
        )
    else:
        out_name = clean_filename(name)

    # Своя директория на каждый запрос: под общим предсказуемым именем
    # параллельные запросы затирают файлы друг друга.
    tmp_dir = Path(tempfile.mkdtemp(prefix="rag_file_"))
    _register_temp_dir(tmp_dir, len(payload))
    out_path = tmp_dir / out_name
    async with aiofiles.open(out_path, "bw") as f:
        await f.write(payload)
    return ResponseFilePath(absolute_temp_path=str(out_path))


# Границы чтения /read_file: больше в контекст модели всё равно не влезает
# (MAX_FILE_CHARS моста — те же 40 000 символов).
_READ_FILE_MAX_CHARS = 40_000
# Парсер копирует вход во временные каталоги, и без лимита один запрос мог бы
# занять гигабайты tmpfs.
_READ_FILE_MAX_BYTES = 64 * 1024 * 1024


def _read_file_sync(raw_path: str, cfg: AppConfig) -> ReadFileResponse:
    """Синхронная часть /read_file — файловая работа уходит с event loop."""
    # Границы — по пути, как его хранит индекс (ссылки не раскрыты); читается
    # сам файл. realpath — от нормализованного пути: от raw_path «ссылка/..»
    # увела бы чтение мимо проверенного места.
    logical = os.path.abspath(os.path.expanduser(raw_path))
    path = Path(os.path.realpath(logical))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="файл не найден или не обычный")
    refusal = read_refusal(logical, cfg.scan_paths, cfg.file_scan_cfg.follow_symlinks)
    if refusal == "outside":
        raise HTTPException(status_code=400, detail="путь вне индексируемых папок")
    if refusal == "excluded":
        raise HTTPException(
            status_code=400,
            detail="скрытые файлы и содержимое скрытых и служебных каталогов не читаются",
        )
    if path.stat().st_size > _READ_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="файл больше лимита чтения")
    parser = get_parser(path.suffix.lower().lstrip("."))
    if parser is None:
        raise HTTPException(status_code=415, detail="формат не индексируется")
    try:
        # Полный текст документа до нарезки: склейка чанков дублировала бы
        # перекрытия сплиттера.
        text = read_document_text(path) or ""
    except Exception as exc:
        lg.warning("read_file: разбор %s не удался (%s)", path, exc)
        raise HTTPException(
            status_code=422, detail="не удалось разобрать файл"
        ) from None
    if not text:
        # Разбор прошёл, но текста нет (например, .doc без конвертера): агенту
        # полезнее явный отказ, чем пустой ответ.
        raise HTTPException(
            status_code=422, detail="парсер не дал текста (нет конвертера?)"
        )
    total_chars = len(text)
    truncated = total_chars > _READ_FILE_MAX_CHARS
    return ReadFileResponse(
        text=text[:_READ_FILE_MAX_CHARS] if truncated else text,
        total_chars=total_chars,
        truncated=truncated,
    )


@router.post("/read_file", description="Полный текст файла парсерами индексации")
async def read_file(req: ReadFileRequest, cfg: AppConfig) -> ReadFileResponse:
    """Текст одного файла из индексируемых папок.

    Мост разбирает OOXML сам (stdlib), но pdf живёт только в бинаре демона
    (pymupdf), а легаси .doc/.xls — только через конвертер Р7-Офиса. Ручка
    закрывает их для local_read_file; без неё pdf в поставке был бы недоступен
    навсегда. Правила доступа повторяют индексацию (read_refusal): путь внутри
    scan_paths без скрытых и служебных каталогов ниже корня, через символьную
    ссылку — если обход по ним идёт; обычный файл, формат из реестра парсеров.
    Без этого ручка превращалась бы в произвольное чтение файловой системы.
    """
    # Парсер может запускать внешний конвертер (Р7): это секунды блокировки,
    # event loop трогать нельзя.
    return await asyncio.to_thread(_read_file_sync, req.absolute_path, cfg)
