"""OCR страниц PDF без текстового слоя: отбор страниц, кэш, запуск воркера.

Страница идёт в OCR, если на ней меньше 50 непробельных символов, а картинки
покрывают не меньше половины её площади: сканы целиком и вставленные в цифровые
отчёты сканы (аудиторские заключения). На актах ЦБ, аналитике и ZX Bank правило
не срабатывает ни разу (замер на корпусе стенда); суммарная площадь, а не одна
большая картинка, — из-за сканов, нарезанных на десятки полос.

Распознаёт отдельный процесс (src/ocr_worker.py) — один на файл, с таймаутом;
при остановке индексации он убивается (kill_running). Результат кэшируется по
sha256 файла в <lsm_root>/ocr: повторная индексация не распознаёт заново, а
чтение файла по запросу (/read_file) берёт только кэш — ждать OCR минутами там
нельзя.
"""

import contextlib
import contextvars
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

import pymupdf

from src.utils import get_config, get_logger

lg = get_logger(__name__)

MIN_TEXT_CHARS = 50
MIN_IMAGE_SHARE = 0.5

_cache_only: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ocr_cache_only", default=False
)
_running: set[subprocess.Popen] = set()
_lock = threading.Lock()
_warned: set[str] = set()


def needs_ocr(page: pymupdf.Page, text: str) -> bool:
    """Текста почти нет, а картинки закрывают не меньше половины страницы."""
    if len("".join(text.split())) >= MIN_TEXT_CHARS:
        return False
    area = page.rect.width * page.rect.height
    if area <= 0:
        return False
    covered = 0.0
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & page.rect
        if not box.is_empty:
            covered += box.width * box.height
    return covered / area >= MIN_IMAGE_SHARE


@contextlib.contextmanager
def cache_only():
    """Внутри — только кэш OCR, без распознавания (чтение файла по запросу)."""
    token = _cache_only.set(True)
    try:
        yield
    finally:
        _cache_only.reset(token)


def kill_running() -> None:
    """Остановка индексации: воркеры OCR не дорабатывают файл."""
    with _lock:
        procs = list(_running)
    for proc in procs:
        with contextlib.suppress(OSError):
            proc.kill()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        lg.warning(msg, *args)


def _tessdata(cfg) -> Path | None:
    path = Path(cfg.tessdata_path).resolve()
    missing = [
        f"{lang}.traineddata"
        for lang in cfg.languages.split("+")
        if not (path / f"{lang}.traineddata").is_file()
    ]
    if missing:
        _warn_once("tessdata", "OCR выключен: нет %s в %s", ", ".join(missing), path)
        return None
    return path


def _worker_cmd() -> tuple[list[str], str | None]:
    """Команда воркера: тот же бинарь с флагом, из исходников — модуль из корня."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ocr-worker"], None
    return [sys.executable, "-m", "src.ocr_worker"], str(
        Path(__file__).resolve().parents[2]
    )


def _run_worker(request: dict, timeout: float) -> tuple[dict[int, str], list[str]]:
    """Распознанные страницы и жалобы: ошибки страниц, таймаут, падение воркера."""
    cmd, cwd = _worker_cmd()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    with _lock:
        _running.add(proc)
    problems: list[str] = []
    try:
        try:
            out, err = proc.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            problems.append(f"таймаут {timeout:g} с")
    finally:
        with _lock:
            _running.discard(proc)
    if err:
        lg.debug("OCR-воркер, stderr: %s", err[-2000:])
    texts: dict[int, str] = {}
    for line in out.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:  # строка, оборванная kill
            continue
        if "text" in item:
            texts[item["page"]] = item["text"]
        else:
            problems.append(f"стр. {item['page']}: {item.get('error')}")
    if proc.returncode and not problems:
        problems.append(f"воркер вышел с кодом {proc.returncode}: {err.strip()[-300:]}")
    return texts, problems


def _sha256(file: BinaryIO) -> str:
    pos = file.tell()
    file.seek(0)
    h = hashlib.sha256()
    while block := file.read(1 << 20):
        h.update(block)
    file.seek(pos)
    return h.hexdigest()


def _cache_file(sha: str) -> Path:
    return Path(get_config().lsm_root) / "ocr" / f"{sha}.json"


def _load(sha: str) -> dict[int, str]:
    try:
        data = json.loads(_cache_file(sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): v for k, v in data.items()}


def _save(sha: str, pages: dict[int, str]) -> None:
    path = _cache_file(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def recognize(file: BinaryIO, pages: list[int]) -> dict[int, str]:
    """Текст страниц `pages` (номера с 1) — из кэша или распознанный воркером.

    Страницы сверх лимита, не уложившиеся во время или упавшие не попадают в
    ответ; о них — одна строка WARNING на файл.
    """
    cfg = get_config().ocr_cfg
    if not cfg.enabled or not pages:
        return {}
    path = getattr(file, "name", None)
    if not isinstance(path, str) or not Path(path).is_file():
        # Вложение письма живёт только в памяти: воркеру нечего открыть.
        _warn_once("memory", "OCR пропускает PDF без файла на диске (вложения писем)")
        return {}
    sha = _sha256(file)
    cached = _load(sha)
    wanted = pages[: cfg.max_pages_per_file]
    todo = [n for n in wanted if n not in cached]
    if todo and not _cache_only.get() and (tessdata := _tessdata(cfg)) is not None:
        started = time.monotonic()
        request = {
            "path": path,
            "pages": todo,
            "dpi": cfg.dpi,
            "language": cfg.languages,
            "tessdata": str(tessdata),
        }
        texts, problems = _run_worker(request, cfg.max_seconds_per_file)
        if texts:
            cached.update(texts)
            _save(sha, cached)
        lg.info(
            "OCR %s: %d стр. за %.1f с",
            path,
            len(texts),
            time.monotonic() - started,
        )
        if len(pages) > len(wanted):
            problems.append(f"лимит {cfg.max_pages_per_file} стр. на файл")
        if problems:
            got = sum(1 for n in pages if n in cached)
            lg.warning(
                "OCR %s: распознано %d из %d стр. (%s)",
                path,
                got,
                len(pages),
                "; ".join(problems[:3]),
            )
    return {n: cached[n] for n in pages if n in cached}
