import asyncio
import email
import os
import re
import shutil
import tempfile
import threading
from collections import deque
from pathlib import Path

import aiofiles
from fastapi import APIRouter, HTTPException, Response

from src.common import IndexingStatus
from src.indexer import AppIndex, reconfigure_lock, restart_watcher, stop_watcher
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
)
from src.thunderbird.lazy_iter_mbox import _decode_filename
from src.utils import AppConfig, get_logger
from src.utils.filename import clean_filename

lg = get_logger(__name__)

router = APIRouter()

# Демон живёт долго, а выданные наружу временные файлы никто не удаляет.
# Держим ограниченное окно последних выдач и подчищаем самые старые, чтобы
# не забивать диск пользователя.
_MAX_TEMP_DIRS = 32
# Одного счётчика мало: письмо ограничено 48 МиБ, поэтому 32 выдачи — это до
# полутора гигабайт. На АРМ каталог временных файлов обычно смонтирован как
# tmpfs, то есть занимает оперативную память, а не диск.
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


@router.post("/search")
async def search(request: SearchRequest, index: AppIndex) -> SearchResponse:
    results = await index.search(
        request.query,
        top_k=request.limit,
        query_type=request.query_type,
        sources=request.sources,
    )
    return SearchResponse.from_dataframes(results)


@router.get("/config")
async def get_config(cfg: AppConfig):
    return cfg.user_config_with_default


def _merge_user_config(current: UserConfig, incoming: UserConfig) -> UserConfig:
    """Частичный апдейт: не присланные поля не затирают сохранённые.

    Раньше на диск писалась вся модель запроса, и POST {"folders": [...]}
    молча сбрасывал настройки почты в дефолт (email.enabled=false).
    """
    data = current.model_dump()
    for key, value in incoming.model_dump(exclude_unset=True).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return UserConfig.model_validate(data)


def _validate_folders(folders: list[str] | None) -> None:
    """Отклонить пути, которых нет на диске.

    Сохранённый несуществующий путь тих и разрушителен: обход не находит по
    нему файлов, а watcher падает на inotify целиком и перестаёт отслеживать
    вообще все папки. Swagger подставляет в тело запроса "string" — этого
    достаточно, чтобы индексация выглядела сломанной, хотя API ответил 200.
    """
    if folders is None:
        return
    bad = [p for p in folders if not Path(p).expanduser().is_dir()]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"не существуют или не являются каталогами: {bad}",
        )


@router.post("/config")
async def update_config(
    user_config: UserConfig,
    cfg: AppConfig,
    index: AppIndex,
) -> Response:
    # До захвата блокировки и до записи на диск: при отказе конфиг должен
    # остаться ровно тем, что был.
    _validate_folders(user_config.folders)
    # Перезапуск watcher'а и индексации — не атомарная последовательность.
    # Без блокировки два параллельных POST /config гонятся за общим состоянием
    # (_watcher_task/_index) и оставляют его повреждённым.
    async with reconfigure_lock():
        prev_paths = set(str(Path(p).expanduser().absolute()) for p in cfg.scan_paths)
        prev_email_enabled = cfg.email_enabled
        # Глубина обхода читается ровно один раз — в AbstractFilesWatcher.run().
        # Применить её можно только перезапуском наблюдения, поэтому смену
        # надо заметить здесь: без этого POST /config {"max_depth": N} отвечал
        # 200 и не менял ничего до следующего /activate или рестарта демона.
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
                    # Со слэшем на конце: delete_prefix снимает всё, что
                    # начинается с этой строки, и без разделителя удаление
                    # ~/Документы задело бы и ~/Документы_архив. Ровно та же
                    # причина, по которой слэш добавлен в Watcher.on_delete_dir.
                    await index.delete_prefix(p.rstrip(os.sep) + os.sep)
        elif depth_changed:
            # Список папок тот же, изменилась только глубина: останавливать
            # индексацию незачем — она глубину не учитывает, — но наблюдение
            # придётся поднять заново, иначе новое значение не вступит в силу.
            await stop_watcher()
        email_enabled = cfg.email_enabled
        # Пришло ли выключение явно в этом запросе. Одного перехода
        # «было включено -> стало выключено» мало: prev_email_enabled читается
        # с диска, и если пользователь до вызова поправил user_conf.yml руками
        # или удалил его, переход не наступает — почта остаётся выключенной, а
        # уже проиндексированные письма навсегда остаются в выдаче поиска.
        # Явный запрос трактуем как «убрать почту из индекса», сколько бы раз
        # его ни повторили.
        disable_requested = (
            "email" in user_config.model_fields_set and not user_config.email.enabled
        )
        if email_enabled and not prev_email_enabled:
            # Включение почты через API: сканер досоздаётся лениво,
            # рестарт демона больше не нужен; первый проход — сразу.
            #
            # Но только на активном индексе: иначе включение почты запускало бы
            # обход ящика на деактивированном демоне — обход в дверь, закрытую
            # БТ §5.2. Настройка сохранена выше, поллинг поднимет /activate.
            if index.active:
                await index.start_emails_polling(immediate=True)
            else:
                lg.info(
                    "почта включена, но индексация деактивирована: "
                    "поллинг начнётся после POST /api/v1/activate"
                )
        elif not email_enabled and (prev_email_enabled or disable_requested):
            await index.stop_emails_polling()
            # Router не должен знать ни про число таблиц, ни про устройство
            # сканера.
            await index.drop_email_index()
        # Перезапуск — последним шагом секции, после всей работы по почте.
        # Раньше start_reindex() создавал фоновую задачу в середине
        # обработчика, и она шла сверять удалённые файлы в ту же БД
        # состояния, куда drop_email_index() в этот момент писал.
        # reconfigure_lock от этого не спасает: задача живёт вне лока.
        if folders_changed:
            if index.active:
                await index.start_reindex()
                await restart_watcher()
            else:
                # БТ §5.4: «Если индексация остановлена, то новая папка
                # добавляется в список, но фактическое сканирование не
                # начинается до следующего запуска». Список уже сохранён выше.
                lg.info(
                    "индексация деактивирована: новый список папок сохранён, "
                    "сканирование начнётся после POST /api/v1/activate"
                )
        elif depth_changed:
            if index.active:
                # Переиндексация здесь не нужна: max_depth ограничивает только
                # постановку inotify-наблюдений (_dirs_for_watch), а полный
                # проход iter_file_metadata обходит дерево целиком независимо
                # от него. Достаточно поднять наблюдение с новым значением.
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


# Ключ письма: "email_<папка>:<начало>:<конец>:<имя>". Двоеточие может быть
# И в имени папки ("Входящие:Архив"), И в имени вложения ("Отчёт:Q3.pdf"),
# поэтому не годится ни split слева, ни rsplit справа: первый отъедает имя
# папки, второй — имя вложения. Опорные точки — два ЧИСЛОВЫХ поля посередине;
# ленивый `.+?` берёт самую левую пару, которая ими является.
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
    # Числа гарантированы самим шаблоном (\d+), int здесь уже не может упасть.
    start, stop = int(match["start"]), int(match["stop"])
    if start < 0 or stop <= start:
        # Отрицательная длина чтения (f.read(stop - start)) прочитала бы
        # весь mbox в память целиком.
        raise HTTPException(status_code=400, detail="invalid offsets in absolute_path")
    if stop - start > cfg.email_scan_cfg.max_message_size:
        # Диапазон приходит от клиента, а читается в память целиком: без
        # верхней границы один запрос может забрать весь ящик.
        raise HTTPException(
            status_code=400,
            detail="requested range exceeds the email size limit",
        )

    # cfg.email_files отдаёт голые имена папок — шаблон выше уже снял префикс
    # "email_", поэтому сверяем напрямую, как и _process_email_chunks.
    # cfg.email_files может обходить ~/.thunderbird — не в event loop.
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
    # realpath, а не abspath: симлинк из индексируемой папки наружу не должен
    # превращать ручку в чтение произвольного места ФС.
    path = Path(os.path.realpath(os.path.expanduser(raw_path)))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="файл не найден или не обычный")
    inside = False
    for root in cfg.scan_paths:
        root_real = os.path.realpath(os.path.expanduser(root))
        if str(path) == root_real or str(path).startswith(
            root_real.rstrip(os.sep) + os.sep
        ):
            inside = True
            break
    if not inside:
        raise HTTPException(status_code=400, detail="путь вне индексируемых папок")
    if path.name.startswith("."):
        raise HTTPException(status_code=400, detail="скрытые файлы не читаются")
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
        # Парсер честно отработал, но текста нет: например, .doc на машине
        # без конвертера. Это не сбой разбора, и агенту полезнее явный отказ,
        # чем пустой ответ.
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
    навсегда. Правила доступа повторяют индексацию: realpath-путь внутри
    scan_paths, обычный файл, формат из реестра парсеров — без этого ручка
    превращалась бы в произвольное чтение файловой системы.
    """
    # Парсер может запускать внешний конвертер (Р7): это секунды блокировки,
    # event loop трогать нельзя.
    return await asyncio.to_thread(_read_file_sync, req.absolute_path, cfg)
