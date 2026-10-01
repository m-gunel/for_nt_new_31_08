#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sber_indexer_loadtest.py
========================

Нагрузочный стенд для локального семантического индексатора документов SberOS
(архитектура «СберКот»: rubert-tiny2 int8 на OpenVINO + LanceDB + uvicorn/FastAPI,
извлечение текста PyMuPDF/lxml/python-pptx, отслеживание новых файлов watchfiles).

Скрипт закрывает 4 вопроса продакта и базовые потребности инженеров индексатора:

  Вопрос продакта                                  -> Фаза стенда
  ------------------------------------------------ ------------------------------
  1. Нагрузка при запуске индексатора              -> phase 0  baseline_idle
  2. Длительность индексации и деградация          -> phase 1  bulk_index
  3. Нагрузка при появлении новых файлов           -> phase 2  incremental
  4. Общая фоновая нагрузка                         -> phase 0 + phase 4 (idle/cooldown)
  + Поисковые запросы >= сутки для CPU/RAM         -> phase 3  sustained (24h+)

Потребности инженеров:
  * корпус 500-1000 файлов (pdf / docx / txt, БЕЗ excel), большие и малые   -> генератор корпуса
  * дозагрузка и устойчивость к новым файлам (не падать)                     -> phase 2 drip
  * поисковая нагрузка >= суток, оценка CPU/RAM                              -> phase 3
  * отсутствие значимого влияния на прочие приложения SberOS               -> probe-латентность
                                                                              + опц. сэмплер PSI

ЧТО ИЗМЕРЯЕТ САМ СТЕНД (метрики системы вы снимаете своим инструментом):
  * длительность и throughput индексации (файлов/с, оценка чанков/с);
  * латентность поиска p50/p95/p99 и error-rate по фазам — это прямой сигнал
    «деградации»: рост латентности probe во время bulk/incremental показывает,
    как индексация мешает поиску;
  * временную разметку фаз (markers) + NDJSON-события, чтобы ваш сборщик метрик
    (sberos-resource-manager / psutil) выровнял CPU/RAM/энергопотребление по фазам;
  * ОПЦИОНАЛЬНО: /proc/pressure/{cpu,memory,io} (PSI) — прямой индикатор влияния
    на соседние приложения; и per-process CPU/RSS индексатора через psutil.

ПОДКЛЮЧЕНИЕ К РЕАЛЬНОМУ ИНДЕКСАТОРУ
  Индексация запускается двумя способами (--index-mode):
    watch_folder (по умолчанию) — стенд просто раскладывает файлы в каталог,
        который индексатор уже отслеживает (watchfiles). API нужен только для поиска.
    api          — стенд дёргает HTTP-эндпоинт индексации (POST путь/загрузка).
  Поиск, статус и (опц.) индексация настраиваются в секции ENDPOINTS ниже или
  флагами. Если не знаете точных путей — запустите:  --discover  (стенд прочитает
  /openapi.json у uvicorn/FastAPI и распечатает доступные маршруты).

БЫСТРЫЙ СТАРТ
  # 0. Проверить сам стенд на встроенном мок-индексаторе (без реального сервиса):
  python3 sber_indexer_loadtest.py --self-test

  # 1. Посмотреть маршруты реального сервиса:
  python3 sber_indexer_loadtest.py --base-url http://127.0.0.1:8000 --discover

  # 2. Быстрый прогон (короткие фазы) чтобы убедиться, что интеграция верна:
  python3 sber_indexer_loadtest.py --base-url http://127.0.0.1:8000 \
      --watched-dir /home/user/Documents/_index \
      --num-files 600 --smoke

  # 3. Полный прогон на сутки:
  python3 sber_indexer_loadtest.py --base-url http://127.0.0.1:8000 \
      --watched-dir /home/user/Documents/_index \
      --num-files 800 --sustained-hours 24 \
      --search-qps 2 --probe-qps 0.5 \
      --sample-psi --sample-pid $(pgrep -f indexer | head -1) \
      --out ./loadtest_run

Зависимости: только стандартная библиотека Python 3.9+.
psutil — опционально (для per-process сэмплера). Файлы pdf/docx/txt генерируются
без внешних библиотек.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import random
import re
import shutil
import signal
import string
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
#  Утилиты времени / логирования
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()

def mono() -> float:
    return time.monotonic()

_print_lock = threading.Lock()
_logfile = None
_run_t0 = None  # monotonic начала прогона — для колонки "+MM:SS"

def set_logfile(path: Path):
    global _logfile
    _logfile = path.open("a", encoding="utf-8")

def set_run_t0():
    global _run_t0
    _run_t0 = mono()

def _elapsed_str() -> str:
    if _run_t0 is None:
        return "      "
    s = int(mono() - _run_t0)
    return f"+{s//60:02d}:{s%60:02d}"

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {_elapsed_str()} {msg}"
    with _print_lock:
        print(line, flush=True)
        if _logfile:
            _logfile.write(line + "\n")
            _logfile.flush()


# --------------------------------------------------------------------------- #
#  Генерация текстового наполнения
# --------------------------------------------------------------------------- #

RU_WORDS = (
    "договор акт отчёт заявка клиент платёж счёт реквизиты система сервер модуль "
    "конфигурация процесс память ядро планировщик нагрузка индексация документ файл "
    "поиск запрос результат метрика анализ производительность оптимизация ошибка "
    "исправление релиз версия сборка тестирование внедрение пилот регламент политика "
    "безопасность агент мониторинг ресурс диск сеть протокол сообщение очередь поток"
).split()

EN_WORDS = (
    "system server module config process memory core scheduler load index document "
    "file search query result metric analysis performance optimization error fix "
    "release version build test deploy pilot policy security agent monitor resource "
    "disk network protocol message queue thread latency throughput vector embedding"
).split()

# Редкие «маячки» — гарантируют попадания в поиске; их же используем как запросы-hit.
def make_keyword_pool(n: int, seed: int) -> List[str]:
    rnd = random.Random(seed)
    pool = []
    for i in range(n):
        tag = "".join(rnd.choice(string.ascii_uppercase) for _ in range(4))
        pool.append(f"SBERKEY-{tag}-{i:04d}")
    return pool


def make_paragraph(rnd: random.Random, keywords: List[str]) -> str:
    words = []
    n = rnd.randint(20, 60)
    for _ in range(n):
        src = RU_WORDS if rnd.random() < 0.6 else EN_WORDS
        w = rnd.choice(src)
        words.append(w)
    # с некоторой вероятностью вставляем маячок
    if keywords and rnd.random() < 0.5:
        pos = rnd.randint(0, len(words))
        words.insert(pos, rnd.choice(keywords))
    s = " ".join(words)
    return s[0].upper() + s[1:] + "."


def make_document_text(rnd: random.Random, n_paragraphs: int,
                       keywords: List[str]) -> List[str]:
    return [make_paragraph(rnd, keywords) for _ in range(n_paragraphs)]


# --------------------------------------------------------------------------- #
#  Писатели файлов (чистый stdlib) — pdf / docx / txt
# --------------------------------------------------------------------------- #

def write_txt(path: Path, paragraphs: List[str]) -> None:
    path.write_text("\n\n".join(paragraphs), encoding="utf-8")


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def write_docx(path: Path, paragraphs: List[str]) -> None:
    """Минимальный валидный .docx (OOXML), читается lxml/python-docx. UTF-8/кириллица ок."""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def write_pdf(path: Path, paragraphs: List[str]) -> None:
    """
    Минимальный многостраничный PDF (Helvetica), из которого PyMuPDF корректно
    извлекает текст. Тело — латиница (стандартный шрифт без кириллических глифов),
    чтобы извлечение текста было надёжным; объём и структура реалистичны для
    нагрузки на извлечение+эмбеддинг. Кириллицу несут txt/docx.
    """
    # Разложим абзацы в строки по ширине и по страницам.
    def wrap(text: str, width: int = 95) -> List[str]:
        words, line, out = text.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > width:
                out.append(line); line = w
            else:
                line = (line + " " + w).strip()
        if line:
            out.append(line)
        return out

    # Латинизируем (кириллицу заменяем на транслит-заглушку 'x'), т.к. Helvetica.
    def latinize(s: str) -> str:
        return "".join(ch if ord(ch) < 128 else "x" for ch in s)

    all_lines: List[str] = []
    for p in paragraphs:
        all_lines.extend(wrap(latinize(p)))
        all_lines.append("")  # пустая строка между абзацами

    lines_per_page = 52
    pages = [all_lines[i:i + lines_per_page]
             for i in range(0, max(1, len(all_lines)), lines_per_page)] or [[""]]

    objects: List[bytes] = []

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # object 1 = Catalog, 2 = Pages, 3 = Font, then per-page: Page + Contents
    page_obj_ids: List[int] = []
    content_objs: List[Tuple[int, bytes]] = []
    next_id = 4
    for page_lines in pages:
        content_id = next_id; next_id += 1
        page_id = next_id; next_id += 1
        page_obj_ids.append(page_id)
        stream_lines = ["BT", "/F1 11 Tf", "13 TL", "50 780 Td"]
        for ln in page_lines:
            stream_lines.append(f"({esc(ln)}) Tj T*")
        stream_lines.append("ET")
        stream = ("\n".join(stream_lines)).encode("latin-1", "replace")
        content = (f"{content_id} 0 obj<</Length {len(stream)}>>stream\n").encode() \
                  + stream + b"\nendstream endobj\n"
        page = (f"{page_id} 0 obj<</Type/Page/Parent 2 0 R"
                f"/MediaBox[0 0 612 792]/Resources<</Font<</F1 3 0 R>>>>"
                f"/Contents {content_id} 0 R>>endobj\n").encode()
        content_objs.append((content_id, content))
        content_objs.append((page_id, page))

    kids = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    obj1 = b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    obj2 = (f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(page_obj_ids)}>>endobj\n").encode()
    obj3 = b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"

    # Собираем с корректным xref.
    all_objs = [(1, obj1), (2, obj2), (3, obj3)] + content_objs
    all_objs.sort(key=lambda t: t[0])
    max_id = all_objs[-1][0]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: Dict[int, int] = {}
    for oid, data in all_objs:
        offsets[oid] = buf.tell()
        buf.write(data)
    xref_pos = buf.tell()
    buf.write(b"xref\n")
    buf.write(f"0 {max_id + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        off = offsets.get(oid, 0)
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer<</Size {max_id + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF".encode())
    path.write_bytes(buf.getvalue())


# --------------------------------------------------------------------------- #
#  Генератор корпуса
# --------------------------------------------------------------------------- #

# Профиль размеров: (доля, диапазон абзацев). Малые/средние/большие файлы.
SIZE_PROFILE = [
    (0.55, (3, 15)),     # малые: несколько абзацев, единицы КБ
    (0.33, (40, 150)),   # средние: десятки КБ, несколько страниц PDF
    (0.12, (400, 1200)), # большие: сотни абзацев, многостраничные / до нескольких МБ
]
# Состав корпуса. В сборке 2026-08-25 зарегистрированы парсеры pdf/docx/doc/pptx/txt
# (src/parsers/*.py, декоратор @parser). Требование продакта: pdf, doc, txt (без excel).
TYPE_MIX = [("pdf", 0.40), ("docx", 0.35), ("txt", 0.25)]

_TYPE_WEIGHTS = {"pdf": 0.55, "docx": 0.45, "txt": 0.30}


def set_type_mix(types_csv: str) -> None:
    """Задаёт состав корпуса, напр. "pdf,docx" или "pdf,docx,txt"."""
    global TYPE_MIX
    types = [t.strip().lower() for t in types_csv.split(",") if t.strip()]
    types = [t for t in types if t in ("pdf", "docx", "txt")]
    if not types:
        return
    raw = [(t, _TYPE_WEIGHTS[t]) for t in types]
    total = sum(w for _, w in raw)
    TYPE_MIX = [(t, w / total) for t, w in raw]


@dataclass
class CorpusManifest:
    root: str
    total: int
    by_type: Dict[str, int]
    keywords: List[str]                       # глобальный пул маячков (для запросов-hit)
    files: List[Dict[str, Any]] = field(default_factory=list)


def _pick(rnd: random.Random, weighted: List[Tuple[Any, float]]):
    r = rnd.random(); acc = 0.0
    for item, w in weighted:
        acc += w
        if r <= acc:
            return item
    return weighted[-1][0]


def generate_one(idx: int, dst_dir: Path, rnd: random.Random,
                 keywords: List[str]) -> Dict[str, Any]:
    ftype = _pick(rnd, TYPE_MIX)
    _, (lo, hi) = None, _pick(rnd, [(band, frac) for frac, band in SIZE_PROFILE])
    n_par = rnd.randint(lo, hi)
    # набор маячков конкретного файла — подмножество глобального пула
    file_keywords = rnd.sample(keywords, k=min(len(keywords), rnd.randint(1, 4)))
    paragraphs = make_document_text(rnd, n_par, file_keywords)
    name = f"doc_{idx:05d}.{ftype}"
    path = dst_dir / name
    if ftype == "txt":
        write_txt(path, paragraphs)
    elif ftype == "docx":
        write_docx(path, paragraphs)
    else:
        write_pdf(path, paragraphs)
    return {
        "name": name, "type": ftype, "paragraphs": n_par,
        "size": path.stat().st_size, "keywords": file_keywords,
    }


def generate_corpus(staging: Path, num_files: int, seed: int,
                    keyword_pool_size: int = 60) -> CorpusManifest:
    staging.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    keywords = make_keyword_pool(keyword_pool_size, seed)
    log(f"Генерация корпуса: {num_files} файлов -> {staging}")
    manifest = CorpusManifest(root=str(staging), total=num_files,
                              by_type={"pdf": 0, "docx": 0, "txt": 0},
                              keywords=keywords)
    t0 = mono()
    for i in range(num_files):
        info = generate_one(i, staging, rnd, keywords)
        manifest.by_type[info["type"]] += 1
        manifest.files.append(info)
        if (i + 1) % 100 == 0:
            log(f"  сгенерировано {i + 1}/{num_files}")
    total_bytes = sum(f["size"] for f in manifest.files)
    log(f"Корпус готов: {num_files} файлов, {total_bytes/1e6:.1f} МБ, "
        f"pdf={manifest.by_type['pdf']} docx={manifest.by_type['docx']} "
        f"txt={manifest.by_type['txt']}, за {mono()-t0:.1f}с")
    return manifest


# --------------------------------------------------------------------------- #
#  HTTP-клиент
# --------------------------------------------------------------------------- #

@dataclass
class HttpResult:
    ok: bool
    status: int
    elapsed_ms: float
    body: Any = None
    error: str = ""


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _full(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def request(self, method: str, path: str,
                json_body: Optional[dict] = None,
                params: Optional[dict] = None) -> HttpResult:
        url = self._full(path)
        if params:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        t0 = mono()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                elapsed = (mono() - t0) * 1000
                body: Any
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = raw[:512].decode("utf-8", "replace")
                return HttpResult(True, resp.status, elapsed, body)
        except urllib.error.HTTPError as e:
            return HttpResult(False, e.code, (mono() - t0) * 1000,
                              error=f"HTTP {e.code}")
        except Exception as e:
            return HttpResult(False, 0, (mono() - t0) * 1000, error=str(e))


# --------------------------------------------------------------------------- #
#  Адаптеры эндпоинтов (настраиваются под реальный API)
# --------------------------------------------------------------------------- #

@dataclass
class Endpoints:
    """
    Контракт реального индексатора (ragsearch / «СберКот»):
      POST /api/v1/search  {"query": "...", "limit": N, "query_type": "hybrid"}
      GET  /api/v1/status  — прогресс индексации (схема ответа автоопределяется)
      GET  /api/v1/health  — хелсчек
      POST /api/v1/config  — задать folders; по логу это делает
                             stop watcher -> clear queue -> restart watcher -> scan,
                             т.е. полный перезапуск индексации (чистый t0 для замера).
    Плейсхолдеры в payload: {query}, {top_k}, {path}.
    """
    # поиск
    search_method: str = "POST"
    search_path: str = "/api/v1/search"
    search_json: Optional[dict] = None
    search_params: Optional[dict] = None
    search_results_key: str = "results"          # если ответ — список, берётся целиком
    # статус/прогресс
    status_method: str = "GET"
    status_path: str = "/api/v1/status"
    status_indexed_key: str = "indexed"          # автоподбор из STATUS_KEY_CANDIDATES
    status_queue_key: str = "queue"
    # хелсчек
    health_path: str = "/api/v1/health"
    # индексация через API (нужно только при --index-mode api)
    index_method: str = "POST"
    index_path: str = "/index"
    index_json: Optional[dict] = None
    # триггер полной индексации через конфиг
    config_path: str = "/api/v1/config"

    def build_search(self, query: str, top_k: int):
        def sub(obj):
            if obj is None:
                return None
            s = json.dumps(obj)
            s = s.replace('"{query}"', json.dumps(query)).replace("{query}", query)
            s = s.replace('"{top_k}"', str(top_k)).replace("{top_k}", str(top_k))
            return json.loads(s)
        return self.search_method, self.search_path, sub(self.search_json), sub(self.search_params)


# Кандидаты имён полей в /api/v1/status (схема ответа заранее неизвестна).
STATUS_KEY_CANDIDATES_INDEXED = (
    "indexed", "indexed_files", "files_indexed", "indexed_count",
    "processed", "processed_files", "done", "completed",
)
STATUS_KEY_CANDIDATES_QUEUE = (
    "files_to_index", "to_index", "remaining_files",
    "queue", "queued", "pending", "remaining", "in_queue", "queue_size",
)
STATUS_KEY_CANDIDATES_PROGRESS = ("progress", "percent", "pct", "completion")
STATUS_KEY_CANDIDATES_ACTIVE = ("is_indexing", "indexing", "running", "in_progress")
STATUS_KEY_CANDIDATES_TOTAL = ("total", "total_files", "files_total")


def default_endpoints(query_type: str = "hybrid") -> Endpoints:
    return Endpoints(
        search_method="POST", search_path="/api/v1/search",
        search_json={"query": "{query}", "limit": "{top_k}",
                     "query_type": query_type},
        search_results_key="results",
        status_method="GET", status_path="/api/v1/status",
        status_indexed_key="indexed", status_queue_key="queue",
        health_path="/api/v1/health",
        index_method="POST", index_path="/index",
        index_json={"path": "{path}"},
        config_path="/api/v1/config",
    )


def build_config_body(folders: List[str],
                     email_folder: str = "",
                     email_enabled: bool = False) -> dict:
    """Тело POST /api/v1/config.

    ВНИМАНИЕ (обход известного дефекта индексатора): парсер вызывает
    email_files() для КАЖДОГО файла независимо от email.enabled, и если
    "email root" не найден — падает с ValueError: email root not found
    (src/config.py:432), что валит всю индексацию. Если у индексатора нет
    ~/.thunderbird, можно попробовать указать существующий каталог как
    email root через --email-folder.
    """
    return {
        "email": {"enabled": bool(email_enabled), "index_last_n_days": None,
                  "offsets": None, "path_to_email_folder": email_folder or ""},
        "folders": folders,
        "sberchat": {"enabled": False},
    }


# --------------------------------------------------------------------------- #
#  Сэмплеры (опциональные): PSI и per-process
# --------------------------------------------------------------------------- #

class Sampler(threading.Thread):
    def __init__(self, out_path: Path, interval: float, sample_psi: bool,
                 pid: Optional[int]):
        super().__init__(daemon=True)
        self.out_path = out_path
        self.interval = interval
        self.sample_psi = sample_psi
        self.pid = pid
        self._stop = threading.Event()
        self._phase = "init"
        self._psutil = None
        self._proc = None
        if pid:
            try:
                import psutil  # type: ignore
                self._psutil = psutil
                self._proc = psutil.Process(pid)
                self._proc.cpu_percent(None)  # прайминг
            except Exception as e:
                log(f"psutil недоступен или pid не найден ({e}); "
                    f"per-process сэмплер выключен")
                self.pid = None

    def set_phase(self, phase: str):
        self._phase = phase

    def _read_psi(self) -> Dict[str, Any]:
        out = {}
        for res in ("cpu", "memory", "io"):
            try:
                txt = Path(f"/proc/pressure/{res}").read_text()
                m = re.search(r"some avg10=([\d.]+).*?total=(\d+)", txt, re.S)
                if m:
                    out[f"psi_{res}_some_avg10"] = float(m.group(1))
            except Exception:
                pass
        return out

    def _read_proc(self) -> Dict[str, Any]:
        if not self._proc:
            return {}
        try:
            with self._proc.oneshot():
                return {
                    "proc_cpu_pct": self._proc.cpu_percent(None),
                    "proc_rss_mb": self._proc.memory_info().rss / 1e6,
                    "proc_threads": self._proc.num_threads(),
                }
        except Exception:
            return {}

    def run(self):
        with self.out_path.open("w", encoding="utf-8") as f:
            while not self._stop.wait(self.interval):
                rec: Dict[str, Any] = {"ts": now_iso(), "phase": self._phase}
                if self.sample_psi:
                    rec.update(self._read_psi())
                if self.pid:
                    rec.update(self._read_proc())
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
#  Запись латентностей и статистика
# --------------------------------------------------------------------------- #

def percentiles(values: List[float], ps=(50, 95, 99)) -> Dict[str, float]:
    if not values:
        return {f"p{p}": 0.0 for p in ps}
    s = sorted(values)
    out = {}
    for p in ps:
        k = (len(s) - 1) * (p / 100.0)
        lo = int(k); hi = min(lo + 1, len(s) - 1)
        out[f"p{p}"] = s[lo] + (s[hi] - s[lo]) * (k - lo)
    return out


class LatencyLog:
    def __init__(self, ndjson_path: Path):
        self._lock = threading.Lock()
        self._f = ndjson_path.open("w", encoding="utf-8")
        self.records: List[Dict[str, Any]] = []

    def add(self, phase: str, kind: str, res: HttpResult, n_results: int = -1):
        rec = {"ts": now_iso(), "phase": phase, "kind": kind,
               "ok": res.ok, "status": res.status,
               "latency_ms": round(res.elapsed_ms, 2), "n_results": n_results}
        with self._lock:
            self.records.append(rec)
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def close(self):
        self._f.close()

    def summary(self) -> Dict[str, Any]:
        by_phase: Dict[str, Dict[str, List[float]]] = {}
        for r in self.records:
            key = (r["phase"], r["kind"])
            b = by_phase.setdefault(r["phase"], {})
            lst = b.setdefault(r["kind"], [])
            if r["ok"]:
                lst.append(r["latency_ms"])
        out: Dict[str, Any] = {}
        for phase, kinds in by_phase.items():
            out[phase] = {}
            for kind, lat in kinds.items():
                total = sum(1 for r in self.records
                            if r["phase"] == phase and r["kind"] == kind)
                errs = total - len(lat)
                st = percentiles(lat)
                st.update({"count": total, "errors": errs,
                           "error_rate": round(errs / total, 4) if total else 0.0,
                           "mean_ms": round(sum(lat) / len(lat), 2) if lat else 0.0})
                out[phase][kind] = st
        return out


# --------------------------------------------------------------------------- #
#  Движок поисковой нагрузки (open-loop с ограничением in-flight)
# --------------------------------------------------------------------------- #

class SearchEngine:
    def __init__(self, client: HttpClient, ep: Endpoints, manifest: CorpusManifest,
                 latlog: LatencyLog, top_k: int, max_inflight: int):
        self.client = client
        self.ep = ep
        self.manifest = manifest
        self.latlog = latlog
        self.top_k = top_k
        self.rnd = random.Random(1234)
        self.pool = ThreadPoolExecutor(max_workers=max_inflight)
        self.sem = threading.Semaphore(max_inflight)
        self.dropped = 0
        self._drop_lock = threading.Lock()
        # словарь запросов: маячки (hit), обычные слова, заведомые промахи (miss)
        self.hit_terms = list(manifest.keywords)
        self.generic_terms = RU_WORDS + EN_WORDS
        self.miss_terms = [f"ZZMISS{i:04d}QQ" for i in range(200)]
        # окно последних запросов для живой сводки + порог «медленного» запроса
        from collections import deque
        self.window = deque(maxlen=2000)      # (mono_ts, latency_ms, ok)
        self.slow_ms = 1000.0                 # всплеск: сразу в лог
        self._win_lock = threading.Lock()

    def window_stats(self, last_sec: float = 30.0) -> Dict[str, Any]:
        cutoff = mono() - last_sec
        with self._win_lock:
            recent = [(l, ok) for t, l, ok in self.window if t >= cutoff]
        lat = [l for l, ok in recent if ok]
        errs = sum(1 for _, ok in recent if not ok)
        st = percentiles(lat)
        st.update({"count": len(recent), "errors": errs,
                   "max_ms": round(max(lat), 1) if lat else 0.0})
        return st

    def _pick_query(self) -> str:
        r = self.rnd.random()
        if r < 0.5:
            base = self.rnd.choice(self.hit_terms)
        elif r < 0.85:
            base = " ".join(self.rnd.sample(self.generic_terms,
                                            k=self.rnd.randint(1, 4)))
        else:
            base = self.rnd.choice(self.miss_terms)
        return base

    def _do_one(self, phase: str, kind: str):
        try:
            method, path, jbody, params = self.ep.build_search(
                self._pick_query(), self.top_k)
            res = self.client.request(method, path, jbody, params)
            n = -1
            if res.ok:
                if isinstance(res.body, list):
                    n = len(res.body)
                elif isinstance(res.body, dict):
                    for key in (self.ep.search_results_key, "results",
                                "items", "documents", "hits", "data"):
                        val = res.body.get(key)
                        if isinstance(val, list):
                            n = len(val)
                            break
            self.latlog.add(phase, kind, res, n)
            with self._win_lock:
                self.window.append((mono(), res.elapsed_ms, res.ok))
            # всплеск или ошибка — немедленно в таймлайн, это и есть маркер пика
            if not res.ok:
                log(f"  !! ОШИБКА поиска [{phase}]: {res.error or res.status} "
                    f"({res.elapsed_ms:.0f} мс)")
            elif res.elapsed_ms >= self.slow_ms:
                log(f"  !! МЕДЛЕННЫЙ поиск [{phase}]: {res.elapsed_ms:.0f} мс "
                    f"(порог {self.slow_ms:.0f})")
        finally:
            self.sem.release()

    def fire(self, phase: str, kind: str = "search"):
        """Неблокирующий запуск одного запроса, если есть свободный слот."""
        if self.sem.acquire(blocking=False):
            self.pool.submit(self._do_one, phase, kind)
        else:
            with self._drop_lock:
                self.dropped += 1

    def shutdown(self):
        self.pool.shutdown(wait=True)


# --------------------------------------------------------------------------- #
#  Оркестратор фаз
# --------------------------------------------------------------------------- #

@dataclass
class RunConfig:
    base_url: str
    out_dir: Path
    watched_dir: Optional[Path]
    index_mode: str                     # "watch_folder" | "api"
    num_files: int
    seed: int
    top_k: int
    search_qps: float
    probe_qps: float
    max_inflight: int
    baseline_sec: int
    incremental_sec: int
    incremental_batch: int
    incremental_period_sec: float
    sustained_sec: int
    cooldown_sec: int
    status_poll_sec: float
    bulk_settle_sec: int
    sample_interval: float
    sample_psi: bool
    sample_pid: Optional[int]
    post_config: bool = True          # триггерить индексацию через POST /api/v1/config
    query_type: str = "hybrid"
    email_folder: str = ""            # обход "email root not found": путь к email root
    email_enabled: bool = False       # включать ли email-индексацию (по умолчанию нет)
    safe_mode: bool = False           # НЕ искать во время индексации (обход race-бага)
    phases: Optional[List[str]] = None  # None = все 5 фаз по порядку
    realistic_sec: int = 3600
    searches_per_hour: float = 20.0   # среднее число поисков/час у живого пользователя
    files_per_hour: float = 12.0      # среднее число новых файлов/час
    realistic_midrun_files: int = 0   # одна «доиндексация»: пачка файлов в середине фазы


class Orchestrator:
    def __init__(self, cfg: RunConfig, ep: Endpoints):
        self.cfg = cfg
        self.ep = ep
        self.client = HttpClient(cfg.base_url)
        self.out = cfg.out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.staging = self.out / "corpus_staging"
        self.markers: List[Dict[str, Any]] = []
        self.latlog = LatencyLog(self.out / "requests.ndjson")
        self.stop_flag = threading.Event()
        self.manifest: Optional[CorpusManifest] = None
        self.engine: Optional[SearchEngine] = None
        self.sampler: Optional[Sampler] = None
        # каталог, в который реально кладём файлы для индексации
        self.index_target = cfg.watched_dir if cfg.index_mode == "watch_folder" else self.staging

    # ---- вспомогательное -------------------------------------------------- #
    def marker(self, name: str, extra: Optional[dict] = None):
        rec = {"marker": name, "ts": now_iso(), "mono": round(mono(), 3)}
        if extra:
            rec.update(extra)
        self.markers.append(rec)
        if self.sampler:
            self.sampler.set_phase(name)
        log(f"===== MARKER: {name} {extra or ''}")

    _status_logged = False

    @staticmethod
    def _find_key(d: dict, preferred: str, candidates: tuple) -> Optional[int]:
        """Ищет числовое поле по имени (вкл. вложенные dict на 1 уровень)."""
        def scan(obj):
            for name in (preferred,) + candidates:
                if name in obj and isinstance(obj[name], (int, float)):
                    return int(obj[name])
            return None
        v = scan(d)
        if v is not None:
            return v
        for sub in d.values():
            if isinstance(sub, dict):
                v = scan(sub)
                if v is not None:
                    return v
        return None

    @staticmethod
    def _find_any(d: dict, candidates: tuple):
        """Ищет поле (число/строку/bool) по именам, вкл. вложенные dict на 1 уровень."""
        def scan(obj):
            for name in candidates:
                if name in obj:
                    return obj[name]
            return None
        v = scan(d)
        if v is not None:
            return v
        for sub in d.values():
            if isinstance(sub, dict):
                v = scan(sub)
                if v is not None:
                    return v
        return None

    def indexer_alive(self) -> bool:
        """Отвечает ли health. status=0 (Connection refused) = процесс умер."""
        r = self.client.request("GET", self.ep.health_path)
        return r.ok

    def poll_status(self) -> Tuple[Optional[int], Optional[int]]:
        idx, q, _, _ = self.poll_status_full()
        return idx, q

    def poll_status_full(self):
        """Возвращает (indexed, remaining, progress_0_1, is_indexing)."""
        res = self.client.request(self.ep.status_method, self.ep.status_path)
        if not (res.ok and isinstance(res.body, dict)):
            return (None, None, None, None)
        b = res.body
        if not Orchestrator._status_logged:
            Orchestrator._status_logged = True
            log(f"Схема /status: {json.dumps(b, ensure_ascii=False)[:400]}")
        idx = self._find_key(b, self.ep.status_indexed_key,
                             STATUS_KEY_CANDIDATES_INDEXED)
        q = self._find_key(b, self.ep.status_queue_key, STATUS_KEY_CANDIDATES_QUEUE)
        prog = self._find_any(b, STATUS_KEY_CANDIDATES_PROGRESS)
        if isinstance(prog, (int, float)):
            prog = float(prog)
            if prog > 1.0:          # шкала 0..100 -> 0..1
                prog = prog / 100.0
        else:
            prog = None
        active = self._find_any(b, STATUS_KEY_CANDIDATES_ACTIVE)
        if not isinstance(active, bool):
            active = None
        return (idx, q, prog, active)

    def _paced_search_loop(self, phase: str, duration_sec: float, qps: float,
                           kind: str = "search"):
        """Гонит поиск с целевым темпом qps в течение duration_sec."""
        if qps <= 0 or not self.engine:
            self._sleep_interruptible(duration_sec)
            return
        interval = 1.0 / qps
        end = mono() + duration_sec
        next_t = mono()
        last_report = mono()
        while mono() < end and not self.stop_flag.is_set():
            now = mono()
            if now >= next_t:
                self.engine.fire(phase, kind)
                next_t += interval
                # если сильно отстали (сервер тормозит) — не копим лавину
                if next_t < now - interval:
                    next_t = now + interval
            else:
                time.sleep(min(0.02, next_t - now))
            if now - last_report > 30:
                idx, q = self.poll_status()
                st = self.engine.window_stats(30.0)
                left = int(end - now)
                log(f"  [{phase}] поиск за 30с: n={st['count']} "
                    f"p50={st['p50']:.0f} p95={st['p95']:.0f} "
                    f"max={st['max_ms']:.0f} мс, err={st['errors']} | "
                    f"indexed={idx} queue={q} | осталось {left//60}м{left%60:02d}с")
                last_report = now

    def _sleep_interruptible(self, sec: float):
        end = mono() + sec
        while mono() < end and not self.stop_flag.is_set():
            time.sleep(min(0.5, end - mono()))

    # ---- индексация ------------------------------------------------------- #
    def _place_files(self, files: List[Dict[str, Any]], verbose: bool = True):
        """Кладёт файлы из staging в целевой каталог (watch_folder) или дёргает API (api)."""
        assert self.index_target is not None
        self.index_target.mkdir(parents=True, exist_ok=True)
        if verbose and len(files) <= 10:
            names = ", ".join(f["name"] for f in files)
            log(f"  + файлы в {self.index_target.name}/: {names}")
        for i, info in enumerate(files, 1):
            src = self.staging / info["name"]
            if verbose and len(files) > 10 and i % 100 == 0:
                log(f"  ... разложено {i}/{len(files)} файлов")
            if self.cfg.index_mode == "watch_folder":
                shutil.copy(src, self.index_target / info["name"])   # copy (не copy2): свежий mtime
            else:
                # api-режим: сначала кладём файл в staging (уже там), затем говорим индексатору путь
                target = str((self.index_target / info["name"]).resolve())
                if self.cfg.index_mode == "api" and self.ep.index_json:
                    body = json.loads(json.dumps(self.ep.index_json)
                                      .replace("{path}", target))
                    self.client.request(self.ep.index_method, self.ep.index_path, body)

    def wait_until_indexed(self, expected: int, timeout_sec: int) -> Dict[str, Any]:
        """Ждёт, пока indexed достигнет expected или очередь опустеет; параллельно probe-поиск."""
        t0 = mono()
        last = -1
        last_report = mono()
        seen_active = False        # видели ли активную индексацию (защита от гонки)
        grace_until = t0 + 8       # даём индексатору стартовать после POST /config
        while mono() - t0 < timeout_sec and not self.stop_flag.is_set():
            # probe-поиск во время индексации = измерение деградации
            if self.engine and self.cfg.probe_qps > 0:
                self.engine.fire("bulk_index", "probe")
            idx, q, prog, active = self.poll_status_full()
            # СМЕРТЬ СЕРВИСА: статус не отвечает -> проверяем health. Индексатор
            # может упасть по assert SQLite LSM при restart watcher (дефект 27.08).
            if idx is None and prog is None and active is None:
                if not self.indexer_alive():
                    log("=" * 60)
                    log("!! ИНДЕКСАТОР УПАЛ: health не отвечает (Connection refused).")
                    log("!! Процесс аварийно завершился — порт 8000 закрыт.")
                    log("!! Смотри indexer_stdout.log: вероятен assert SQLite LSM")
                    log("!! (assert_db_state в lsm_main.c) при 'restart watcher'.")
                    log("!! Прекращаю прогон, чтобы не гонять час впустую.")
                    log("=" * 60)
                    self._abort = "indexer_crashed"
                    self.marker("aborted", {"reason": "indexer_crashed",
                                            "elapsed_sec": round(mono() - t0, 1)})
                    return {"reason": "indexer_crashed", "indexed": last,
                            "elapsed_sec": round(mono() - t0, 1)}
            if active:
                seen_active = True
            if mono() - last_report > 30:
                st = self.engine.window_stats(30.0) if self.engine else {}
                rate = f", ~{idx/(mono()-t0):.1f} файл/с" if idx else ""
                pct = f" {prog*100:.0f}%" if prog is not None else ""
                log(f"  [bulk_index]{pct} indexed={idx} осталось={q}"
                    f"{rate} idx_active={active} | "
                    f"probe p95={st.get('p95', 0):.0f} мс")
                last_report = mono()

            done = None
            # 1) явный флаг: индексация была активна и завершилась
            if active is False and (seen_active or mono() > grace_until):
                done = "is_indexing_false"
            # 2) прогресс достиг 100%
            elif prog is not None and prog >= 0.999 and mono() > grace_until:
                done = "progress_100"
            # 3) остаток очереди опустел
            elif q == 0 and mono() > grace_until and (seen_active or idx):
                done = "queue_empty"
            # 4) fallback по счётчику
            elif idx is not None and idx >= expected:
                done = "count_reached"
            if done:
                return {"reason": done, "indexed": idx,
                        "progress": prog, "elapsed_sec": round(mono() - t0, 1)}
            last = idx if idx is not None else last
            time.sleep(self.cfg.status_poll_sec)
        return {"reason": "timeout", "indexed": last,
                "elapsed_sec": round(mono() - t0, 1)}

    # ---- фазы ------------------------------------------------------------- #
    def phase_baseline(self):
        self.marker("baseline_idle_start")
        self._sleep_interruptible(self.cfg.baseline_sec)
        self.marker("baseline_idle_end")

    def phase_bulk(self):
        assert self.manifest
        # начальную порцию (напр. 85%) грузим как «первичную индексацию пользователя»
        bulk_n = int(len(self.manifest.files) * 0.85)
        bulk_files = self.manifest.files[:bulk_n]

        # 1) Сначала раскладываем файлы в целевую папку (watcher ещё может быть
        #    не нацелен на неё — это ок).
        self._place_files(bulk_files)

        # 2) Триггер первичной индексации. У реального индексатора POST /config
        #    делает stop watcher -> clear queue -> restart watcher -> scan path,
        #    т.е. это и есть «запуск индексатора» из вопроса продакта — чистый t0.
        trigger = "files_only"
        if self.cfg.post_config and self.index_target is not None:
            folders = [str(self.index_target.resolve())]
            body = build_config_body(folders, self.cfg.email_folder,
                                     self.cfg.email_enabled)
            log(f"POST {self.ep.config_path} folders={folders}"
                + (f" email_root={self.cfg.email_folder!r} "
                   f"email_enabled={self.cfg.email_enabled}"
                   if self.cfg.email_folder or self.cfg.email_enabled else ""))
            res = self.client.request("POST", self.ep.config_path, body)
            trigger = f"post_config({res.status})"
            if not res.ok:
                detail = ""
                if res.body:
                    detail = f" | ответ сервера: {json.dumps(res.body, ensure_ascii=False)[:300]}"
                log(f"!! ОШИБКА: POST {self.ep.config_path} -> {res.status} "
                    f"{res.error}{detail}")
                log(f"!! Индексация НЕ переключилась на тестовую папку. "
                    f"Смотри трейсбек в консоли uvicorn (где запущен main). "
                    f"Частые причины: индексатор уже занят старой очередью, "
                    f"нет прав на папку, папка вне разрешённого корня.")
            else:
                log(f"POST /config принят (200): индексатор пересканирует {folders[0]}")
                # POST /config запускает stop watcher -> clear queue -> restart watcher.
                # Именно тут индексатор падает по assert SQLite LSM на «грязной»
                # очереди. Проверяем, что процесс пережил переконфигурацию.
                time.sleep(3)
                if not self.indexer_alive():
                    log("=" * 60)
                    log("!! ИНДЕКСАТОР УПАЛ СРАЗУ ПОСЛЕ POST /config.")
                    log("!! Health не отвечает — процесс аварийно завершён.")
                    log("!! Причина в indexer_stdout.log; типично: assert_db_state")
                    log("!! (SQLite LSM) при 'restart watcher' на накопленной очереди.")
                    log("!! Лечится полным сбросом ~/.files-search перед прогоном.")
                    log("=" * 60)
                    self._abort = "indexer_crashed_on_config"
                    self.marker("aborted", {"reason": "crash_after_post_config"})
                    return

        self.marker("bulk_index_start", {"files": len(bulk_files),
                                         "mode": self.cfg.index_mode,
                                         "trigger": trigger})
        idx0, _ = self.poll_status()
        base = 0 if self.cfg.post_config else (idx0 or 0)
        result = self.wait_until_indexed(base + len(bulk_files),
                                         self.cfg.bulk_settle_sec)
        self.marker("bulk_index_end", result)
        log(f"Индексация {len(bulk_files)} файлов: {result}")

    def phase_incremental(self):
        self.marker("incremental_start", {"files": len(self.incremental_files),
                                          "batch": self.cfg.incremental_batch})
        remaining = list(self.incremental_files)
        end = mono() + self.cfg.incremental_sec
        while remaining and mono() < end and not self.stop_flag.is_set():
            batch = [remaining.pop() for _ in range(min(self.cfg.incremental_batch,
                                                        len(remaining)))]
            self.marker("incremental_batch", {"n": len(batch)})
            self._place_files(batch)
            # probe-поиск во время дозагрузки + короткое ожидание
            slice_end = mono() + self.cfg.incremental_period_sec
            while mono() < slice_end and not self.stop_flag.is_set():
                if self.engine and self.cfg.probe_qps > 0:
                    self.engine.fire("incremental", "probe")
                time.sleep(1.0 / max(self.cfg.probe_qps, 0.1))
        # если файлы остались — досыпаем разом
        if remaining:
            self._place_files(remaining)
        self.marker("incremental_end")

    def phase_sustained(self):
        self.marker("sustained_start", {"hours": round(self.cfg.sustained_sec/3600, 2),
                                        "search_qps": self.cfg.search_qps})
        # фоновая лёгкая дозагрузка новых файлов во время длинного поиска — реалистичный
        # стационар; генерируем «свежие» файлы небольшими порциями.
        drip = threading.Thread(target=self._sustained_drip, daemon=True)
        drip.start()
        self._paced_search_loop("sustained", self.cfg.sustained_sec,
                                self.cfg.search_qps, "search")
        self.stop_flag_local = True
        drip.join(timeout=5)
        self.marker("sustained_end")

    def _sustained_drip(self):
        """Раз в ~5 минут добавляет пару новых файлов, чтобы поиск и watcher работали вместе."""
        assert self.manifest
        rnd = random.Random(999)
        i = 100000
        end = mono() + self.cfg.sustained_sec
        while mono() < end and not self.stop_flag.is_set():
            self._sleep_interruptible(300)
            if self.stop_flag.is_set() or mono() >= end:
                break
            info = generate_one(i, self.staging, rnd, self.manifest.keywords)
            i += 1
            self._place_files([info])
            self.marker("sustained_drip_file", {"name": info["name"]})

    def _wait_indexing_settled(self, timeout: int = 1800):
        """SAFE-MODE: ждём is_indexing=false, чтобы поиск не пересёкся с индексацией."""
        log("  SAFE-MODE: жду окончания индексации перед поиском (is_indexing=false)...")
        t0 = mono()
        while mono() - t0 < timeout and not self.stop_flag.is_set():
            idx, q, prog, active = self.poll_status_full()
            if active is False and mono() - t0 > 10:
                log(f"  SAFE-MODE: индексация завершена за {int(mono()-t0)}с — "
                    f"можно искать")
                return
            time.sleep(3)
        log("  SAFE-MODE: таймаут ожидания индексации, начинаю поиск")

    def phase_realistic(self):
        """Эмуляция ОДНОГО живого пользователя: редкие вспышки поиска + тонкий
        ручеёк новых файлов. Отвечает на вопрос «мешает ли индексатор другим
        приложениям при обычной работе», в отличие от стресс-фазы sustained."""
        import math
        assert self.manifest
        sph = max(self.cfg.searches_per_hour, 0.1)
        fph = max(self.cfg.files_per_hour, 0.0)
        if self.cfg.safe_mode:
            self._wait_indexing_settled()
        self.marker("realistic_start", {
            "hours": round(self.cfg.realistic_sec / 3600, 2),
            "searches_per_hour": sph, "files_per_hour": fph,
            "safe_mode": self.cfg.safe_mode})
        log(f"  Профиль пользователя: ~{sph:.0f} поисков/час вспышками, "
            f"~{fph:.0f} новых файлов/час. Между действиями — простой.")

        rnd = random.Random(20260714)
        end = mono() + self.cfg.realistic_sec
        # средние интервалы (Пуассон): сессия поиска и появление файла
        # сессия = 1..4 запроса подряд, поэтому делим часовой бюджет на среднюю пачку
        mean_burst = 2.2
        sess_interval = 3600.0 / (sph / mean_burst)     # сек между сессиями поиска
        file_interval = 3600.0 / fph if fph > 0 else 1e12
        next_session = mono() + rnd.expovariate(1.0 / sess_interval)
        next_file = mono() + rnd.expovariate(1.0 / file_interval)
        fidx = 200000
        last_report = mono()
        n_search = 0
        n_files = 0
        midrun_done = False

        while mono() < end and not self.stop_flag.is_set():
            now = mono()
            # вспышка поиска
            if now >= next_session and self.engine:
                burst = rnd.choices([1, 2, 3, 4], weights=[45, 30, 18, 7])[0]
                for i in range(burst):
                    if self.stop_flag.is_set():
                        break
                    self.engine.fire("realistic", "search")
                    n_search += 1
                    # уточнение запроса через 4..18 с
                    if i < burst - 1:
                        self._sleep_interruptible(rnd.uniform(4, 18))
                next_session = mono() + rnd.expovariate(1.0 / sess_interval)
            # разовая «доиндексация»: пачка файлов в середине фазы
            if (self.cfg.realistic_midrun_files > 0 and not midrun_done
                    and mono() - (end - self.cfg.realistic_sec) >= self.cfg.realistic_sec / 2):
                batch = [generate_one(fidx + i, self.staging, rnd,
                                      self.manifest.keywords)
                         for i in range(self.cfg.realistic_midrun_files)]
                fidx += len(batch)
                self.marker("realistic_reindex_batch", {"n": len(batch)})
                log(f"  Доиндексация: добавляю {len(batch)} новых файлов")
                self._place_files(batch)
                n_files += len(batch)
                midrun_done = True
            # появление нового файла (доиндексация в фоне)
            if mono() >= next_file:
                info = generate_one(fidx, self.staging, rnd, self.manifest.keywords)
                fidx += 1
                self._place_files([info], verbose=False)
                n_files += 1
                self.marker("realistic_new_file", {"name": info["name"]})
                next_file = mono() + rnd.expovariate(1.0 / file_interval)
            # периодический отчёт + контроль живости сервиса
            if mono() - last_report > 300:
                if not self.indexer_alive():
                    log("!! ИНДЕКСАТОР УПАЛ во время пользовательской фазы "
                        "(health не отвечает). Прекращаю прогон.")
                    self._abort = "indexer_crashed"
                    self.marker("aborted", {"reason": "indexer_crashed_realistic"})
                    break
                st = self.engine.window_stats(300.0) if self.engine else {}
                left = int(end - mono())
                log(f"  [realistic] за 5 мин: поисков={st.get('count', 0)} "
                    f"p95={st.get('p95', 0):.0f} max={st.get('max_ms', 0):.0f} мс | "
                    f"всего поисков={n_search} файлов={n_files} | "
                    f"осталось {left//60}м")
                last_report = mono()
            time.sleep(1.0)

        self.marker("realistic_end", {"total_searches": n_search,
                                      "total_new_files": n_files})
        log(f"  Итог реалистичной фазы: {n_search} поисков, {n_files} новых файлов "
            f"за {self.cfg.realistic_sec//60} мин")

    def phase_cooldown(self):
        self.marker("cooldown_start")
        self._sleep_interruptible(self.cfg.cooldown_sec)
        self.marker("cooldown_end")

    # ---- запуск ----------------------------------------------------------- #
    def run(self):
        cfg = self.cfg
        if cfg.index_mode == "watch_folder" and not cfg.watched_dir:
            log("ОШИБКА: для --index-mode watch_folder нужен --watched-dir")
            return 2

        set_logfile(self.out / "timeline.log")
        set_run_t0()
        log(f"=== ПРОГОН НАЧАТ === url={cfg.base_url} mode={cfg.index_mode} "
            f"файлов={cfg.num_files} фазы={cfg.phases or 'все'}")

        # переиспользуем корпус, если staging уже сгенерирован тем же seed/размером
        manifest_path = self.out / "corpus_manifest.json"
        reuse = False
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (m.get("total") == cfg.num_files
                        and all((self.staging / f["name"]).exists()
                                for f in m.get("files", [])[:5])):
                    self.manifest = CorpusManifest(**{k: m[k] for k in
                        ("root", "total", "by_type", "keywords", "files")})
                    reuse = True
                    log(f"Корпус переиспользован из {self.staging} "
                        f"({m['total']} файлов)")
            except Exception:
                pass
        if not reuse:
            self.manifest = generate_corpus(self.staging, cfg.num_files, cfg.seed)
            manifest_path.write_text(
                json.dumps(asdict(self.manifest), ensure_ascii=False, indent=2),
                encoding="utf-8")

        self.engine = SearchEngine(self.client, self.ep, self.manifest,
                                   self.latlog, cfg.top_k, cfg.max_inflight)

        if cfg.sample_psi or cfg.sample_pid:
            self.sampler = Sampler(self.out / "samples.ndjson", cfg.sample_interval,
                                   cfg.sample_psi, cfg.sample_pid)
            self.sampler.start()

        # проверка связи: /health, затем /status
        h = self.client.request("GET", self.ep.health_path)
        log(f"GET {self.ep.health_path}: "
            f"{'OK ' + str(h.status) if h.ok else 'FAIL ' + (h.error or str(h.status))}")
        ping = self.poll_status()
        log(f"Проверка статуса индексатора: indexed={ping[0]} queue={ping[1]} "
            f"(None = эндпоинт не отвечает/схема не распознана — прогон продолжится, "
            f"но ожидание завершения индексации будет по таймауту)")

        # дефолт для запуска incremental без bulk
        bulk_n = int(len(self.manifest.files) * 0.85)
        self.incremental_files = self.manifest.files[bulk_n:]

        phase_map = {
            "baseline": self.phase_baseline,
            "bulk": self.phase_bulk,
            "incremental": self.phase_incremental,
            "sustained": self.phase_sustained,
            "realistic": self.phase_realistic,
            "cooldown": self.phase_cooldown,
        }
        order = self.cfg.phases or ["baseline", "bulk", "incremental",
                                    "sustained", "cooldown"]

        run_started = now_iso()
        try:
            for name in order:
                if self.stop_flag.is_set():
                    break
                phase_map[name]()
        except KeyboardInterrupt:
            log("Прерывание пользователем — сохраняю частичные результаты")
            self.stop_flag.set()
        finally:
            if self.engine:
                self.engine.shutdown()
            if self.sampler:
                self.sampler.stop()
            self.latlog.close()
            self._write_summary(run_started)
        return 0

    def _write_summary(self, run_started: str):
        summary = {
            "run_started": run_started,
            "run_finished": now_iso(),
            "base_url": self.cfg.base_url,
            "index_mode": self.cfg.index_mode,
            "config": {k: (str(v) if isinstance(v, Path) else v)
                       for k, v in asdict(self.cfg).items()},
            "corpus": self.manifest.by_type if self.manifest else {},
            "markers": self.markers,
            "latency_by_phase": self.latlog.summary(),
            "dropped_requests": self.engine.dropped if self.engine else 0,
        }
        (self.out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.out / "markers.json").write_text(
            json.dumps(self.markers, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Готово. Артефакты в {self.out}:")
        log("  summary.json        — сводка + латентность по фазам (p50/p95/p99)")
        log("  markers.json        — временная разметка фаз для вашего сборщика метрик")
        log("  requests.ndjson     — каждый запрос (ts/phase/latency/status)")
        log("  corpus_manifest.json— состав корпуса и маячки для поиска")
        if self.cfg.sample_psi or self.cfg.sample_pid:
            log("  samples.ndjson      — PSI / per-process CPU-RSS индексатора")
        # короткий вывод латентности в консоль
        lat = self.latlog.summary()
        for phase in ("bulk_index", "incremental", "sustained"):
            for kind in ("probe", "search"):
                st = lat.get(phase, {}).get(kind)
                if st:
                    log(f"  {phase}/{kind}: p50={st['p50']:.0f} p95={st['p95']:.0f} "
                        f"p99={st['p99']:.0f} ms, n={st['count']}, "
                        f"err={st['error_rate']*100:.1f}%")


# --------------------------------------------------------------------------- #
#  OpenAPI discovery
# --------------------------------------------------------------------------- #

def discover(base_url: str):
    client = HttpClient(base_url)
    for path in ("/openapi.json", "/docs/openapi.json"):
        res = client.request("GET", path)
        if res.ok and isinstance(res.body, dict) and "paths" in res.body:
            log(f"OpenAPI найден на {path}. Маршруты:")
            for p, methods in res.body["paths"].items():
                for m, meta in methods.items():
                    summ = meta.get("summary", "") if isinstance(meta, dict) else ""
                    print(f"  {m.upper():6s} {p:40s} {summ}")
            return 0
    log("OpenAPI не найден. Попробуйте открыть в браузере "
        f"{base_url.rstrip('/')}/docs или задайте эндпоинты вручную флагами.")
    return 1


# --------------------------------------------------------------------------- #
#  Встроенный мок-индексатор для --self-test
# --------------------------------------------------------------------------- #

def run_mock_server(port: int, watched_dir: Path, stop: threading.Event):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state = {"indexed": 0}

    def count_files() -> int:
        try:
            return sum(1 for _ in watched_dir.glob("*.*"))
        except Exception:
            return 0

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # тихо
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if "/status" in self.path:
                # имитируем «догоняющую» индексацию: indexed растёт к числу файлов
                target = count_files()
                if state["indexed"] < target:
                    state["indexed"] = min(target, state["indexed"] +
                                           max(1, (target - state["indexed"]) // 3 + 1))
                remaining = max(0, target - state["indexed"])
                prog = (state["indexed"] / target) if target else 0.0
                self._send(200, {
                    "files_to_index": remaining,
                    "files_indexed": state["indexed"],
                    "total_files": target,
                    "is_indexing": remaining > 0,
                    "progress": round(prog, 3),
                    "type": "update"})
            elif "/health" in self.path:
                self._send(200, {"status": "ok"})
            elif self.path.startswith("/openapi.json"):
                self._send(200, {"paths": {
                    "/api/v1/search": {"post": {"summary": "semantic search"}},
                    "/api/v1/status": {"get": {"summary": "index status"}},
                    "/api/v1/config": {"post": {"summary": "set config"}},
                    "/api/v1/health": {"get": {"summary": "health"}},
                }})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln)
            if "/search" in self.path:
                # проверка контракта: должно прийти query/limit/query_type
                try:
                    body = json.loads(raw)
                    assert "query" in body and "limit" in body and "query_type" in body
                except Exception:
                    self._send(422, {"error": "bad search body"}); return
                time.sleep(random.uniform(0.01, 0.05))  # имитация инференса+ANN
                k = random.randint(0, 5)
                self._send(200, {"results": [{"id": i} for i in range(k)]})
            elif "/config" in self.path:
                state["indexed"] = 0  # имитация clear queue + rescan
                self._send(200, {"ok": True})
            elif self.path.startswith("/index"):
                self._send(200, {"accepted": True})
            else:
                self._send(404, {"error": "not found"})

    httpd = HTTPServer(("127.0.0.1", port), H)
    httpd.timeout = 0.5
    log(f"[mock] индексатор слушает http://127.0.0.1:{port}")
    while not stop.is_set():
        httpd.handle_request()
    httpd.server_close()


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  Интерактивное меню
# --------------------------------------------------------------------------- #

MENU_SETTINGS_FILE = Path("./loadtest_settings.json")

DEFAULT_SETTINGS = {
    "base_url": "http://127.0.0.1:8000",
    "watched_dir": "",
    "out_dir": "./loadtest_run",
    "num_files": 800,
    "search_qps": 2.0,
    "probe_qps": 0.5,
    "query_type": "hybrid",
    "post_config": True,
    "sample_psi": True,
    "sample_pid": 0,
    "searches_per_hour": 20,
    "files_per_hour": 12,
    "email_folder": "",
    "email_enabled": False,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if MENU_SETTINGS_FILE.exists():
        try:
            s.update(json.loads(MENU_SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return s


def save_settings(s: dict):
    MENU_SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


def _ask(prompt: str, default):
    raw = input(f"  {prompt} [{default}]: ").strip()
    if not raw:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "y", "д", "да", "true", "yes")
    if isinstance(default, int):
        try: return int(raw)
        except ValueError: return default
    if isinstance(default, float):
        try: return float(raw)
        except ValueError: return default
    return raw


def _make_cfg(s: dict, phases: List[str], durations: dict) -> "RunConfig":
    # safe-mode: строго один поиск за раз (max_inflight=1). _reset_memory() зовётся
    # на каждом encode -> два параллельных поиска = гонка release_memory().
    _inflight = 1 if s.get("safe_mode") else 16
    return RunConfig(
        base_url=s["base_url"],
        out_dir=Path(s["out_dir"]),
        watched_dir=Path(s["watched_dir"]) if s["watched_dir"] else None,
        index_mode="watch_folder",
        num_files=int(s["num_files"]), seed=42, top_k=10,
        search_qps=float(s["search_qps"]), probe_qps=float(s["probe_qps"]),
        max_inflight=_inflight,
        realistic_sec=durations.get("realistic", 3600),
        searches_per_hour=float(s.get("searches_per_hour", 20)),
        files_per_hour=float(s.get("files_per_hour", 12)),
        baseline_sec=durations.get("baseline", 300),
        incremental_sec=durations.get("incremental", 900),
        incremental_batch=5,
        incremental_period_sec=45.0,
        sustained_sec=durations.get("sustained", 1800),
        cooldown_sec=durations.get("cooldown", 300),
        status_poll_sec=2.0,
        bulk_settle_sec=durations.get("bulk_settle", 1500),
        sample_interval=2.0,
        sample_psi=bool(s["sample_psi"]),
        sample_pid=int(s["sample_pid"]) or None,
        post_config=bool(s["post_config"]),
        query_type=s["query_type"],
        email_folder=s.get("email_folder", ""),
        email_enabled=bool(s.get("email_enabled", False)),
        phases=phases,
    )


def _run_cfg(cfg: "RunConfig", s: dict):
    ep = default_endpoints(query_type=s["query_type"])
    orch = Orchestrator(cfg, ep)
    def handle_sigint(signum, frame):
        log("SIGINT — останавливаюсь корректно...")
        orch.stop_flag.set()
    old = signal.signal(signal.SIGINT, handle_sigint)
    try:
        orch.run()
    finally:
        signal.signal(signal.SIGINT, old)


def _print_hour_plan(s: dict):
    print("""
  План сценария «1 час» (пассивный мониторинг снимаете своим инструментом):
    +00:00  baseline    5 мин   фон в простое (до каких-либо действий)
    +05:00  bulk        <=25м   POST /config -> первичная индексация 85% корпуса,
                                probe-поиск во время индексации (деградация)
    ~+30:00 incremental 15 мин  дозагрузка новых файлов порциями по 5 шт / 45 с
    ~+45:00 sustained   30 мин  поисковая нагрузка {qps} зап/с + редкие новые файлы
    конец   cooldown    5 мин   возврат в простой
  Каждое действие пишется в консоль и timeline.log с меткой [ЧЧ:ММ:СС] +MM:SS.
  Всплеск >1с латентности или ошибка логируются НЕМЕДЛЕННО отдельной строкой.
""".format(qps=s["search_qps"]))


def check_connectivity(s: dict):
    ep = default_endpoints(query_type=s["query_type"])
    client = HttpClient(s["base_url"], timeout=10)
    h = client.request("GET", ep.health_path)
    print(f"  GET {ep.health_path:22s} -> "
          f"{'OK ' + str(h.status) if h.ok else 'FAIL ' + (h.error or str(h.status))}")
    st = client.request("GET", ep.status_path)
    body = json.dumps(st.body, ensure_ascii=False)[:300] if st.ok else st.error
    print(f"  GET {ep.status_path:22s} -> {st.status} {body}")
    m, p, jb, pr = ep.build_search("тест производительность", 5)
    sr = client.request(m, p, jb, pr)
    print(f"  {m} {p:21s} -> {sr.status} за {sr.elapsed_ms:.0f} мс, "
          f"тело: {json.dumps(sr.body, ensure_ascii=False)[:200] if sr.ok else sr.error}")


def reset_and_reindex(s: dict):
    """Отдельный шаг: переключить индексатор на тестовую папку и дождаться конца.
    Полезно для отладки POST /config (ошибка 500) в изоляции."""
    ep = default_endpoints(query_type=s["query_type"])
    client = HttpClient(s["base_url"], timeout=30)
    folder = s["watched_dir"]
    if not folder:
        print("  !! Сначала задайте папку в Настройках (7)")
        return
    Path(folder).mkdir(parents=True, exist_ok=True)

    # текущее состояние
    st = client.request("GET", ep.status_path)
    print(f"  до сброса: {json.dumps(st.body, ensure_ascii=False)[:200] if st.ok else st.error}")

    body = build_config_body([str(Path(folder).resolve())],
                             s.get("email_folder", ""),
                             bool(s.get("email_enabled", False)))
    print(f"  POST {ep.config_path} folders=[{Path(folder).resolve()}]")
    res = client.request("POST", ep.config_path, body)
    if not res.ok:
        print(f"  !! 500/ошибка: {res.status} {res.error}")
        if res.body:
            print(f"  !! тело ответа: {json.dumps(res.body, ensure_ascii=False)[:400]}")
        print("  -> Открой консоль uvicorn (там где запущен main) и посмотри трейсбек —")
        print("     это точная причина. Либо перезапусти сервис, чтобы снять старую очередь,")
        print("     затем повтори этот пункт.")
        return
    print(f"  OK {res.status}: индексатор пересканирует папку. Жду завершения...")

    t0 = mono()
    seen_active = False
    while mono() - t0 < 3600:
        idx, q, prog, active = _poll_status_full_standalone(client, ep)
        if active:
            seen_active = True
        pct = f"{prog*100:.0f}%" if prog is not None else "?"
        print(f"    +{int(mono()-t0):04d}с  indexed={idx} осталось={q} "
              f"прогресс={pct} active={active}")
        if (active is False and seen_active) or (prog is not None and prog >= 0.999):
            print(f"  Готово: индексация завершена за {int(mono()-t0)}с, indexed={idx}")
            return
        # ранний выход: за 20с индексация так и не началась и очередь пуста
        if (mono() - t0 > 20 and not seen_active
                and (q in (0, None)) and (not idx)):
            print("  Индексатор не начал индексацию (очередь пуста, active=False).")
            print("  Проверь: 1) в папке есть файлы; 2) POST /config реально применился;")
            print("           3) статус не устаревший. Файлов в папке: "
                  f"{sum(1 for _ in Path(folder).glob('*.*'))}")
            return
        time.sleep(5)
    print("  Таймаут ожидания (60 мин).")


def _poll_status_full_standalone(client, ep):
    res = client.request("GET", ep.status_path)
    if not (res.ok and isinstance(res.body, dict)):
        return (None, None, None, None)
    b = res.body
    idx = Orchestrator._find_key(b, ep.status_indexed_key, STATUS_KEY_CANDIDATES_INDEXED)
    q = Orchestrator._find_key(b, ep.status_queue_key, STATUS_KEY_CANDIDATES_QUEUE)
    prog = Orchestrator._find_any(b, STATUS_KEY_CANDIDATES_PROGRESS)
    if isinstance(prog, (int, float)):
        prog = float(prog); prog = prog/100.0 if prog > 1.0 else prog
    else:
        prog = None
    active = Orchestrator._find_any(b, STATUS_KEY_CANDIDATES_ACTIVE)
    active = active if isinstance(active, bool) else None
    return (idx, q, prog, active)


def menu():
    s = load_settings()
    while True:
        print("\n" + "=" * 62)
        print("  НАГРУЗОЧНЫЙ СТЕНД ИНДЕКСАТОРА SberOS (ragsearch)")
        print(f"  url: {s['base_url']}")
        print(f"  папка индексации: {s['watched_dir'] or '(не задана!)'}")
        print(f"  корпус: {s['num_files']} файлов | поиск: {s['search_qps']} зап/с"
              f" | query_type: {s['query_type']}")
        print("=" * 62)
        print("  1) СТРЕСС «1 час»  (простой -> индексация -> дозагрузка -> поиск 2 зап/с -> простой)")
        print("  9) РЕАЛИСТИЧНЫЙ пользователь (редкие вспышки поиска + ручеёк файлов)")
        print("  2) Сценарий с моими длительностями")
        print("  3) Только первичная индексация (bulk + probe)")
        print("  4) Только поисковая нагрузка")
        print("  5) Только дозагрузка новых файлов")
        print("  6) Проверка связи (health / status / пробный поиск)")
        print("  8) Сброс + переиндексация тестовой папки (POST /config, отладка 500)")
        print("  7) Настройки")
        print("  0) Выход")
        choice = input("Выбор: ").strip()

        if choice == "0":
            return 0
        elif choice == "6":
            check_connectivity(s)
        elif choice == "8":
            reset_and_reindex(s)
        elif choice == "9":
            if not s["watched_dir"]:
                print("  !! Сначала задайте папку индексации в Настройках (7)")
                continue
            hours = _ask("Длительность, часов", 1.0)
            do_bulk = _ask("Сделать первичную индексацию в начале? (y/n)", True)
            print(f"""
  Реалистичный профиль (эмуляция одного пользователя):
    ~{s['searches_per_hour']} поисков/час вспышками по 1-4 запроса, между ними простой
    ~{s['files_per_hour']} новых файлов/час тонким ручейком (доиндексация)
    {'+ первичная индексация корпуса в начале' if do_bulk else 'без первичной индексации'}
  Это отвечает на вопрос «мешает ли индексатор другим приложениям при
  обычной работе», а НЕ на «где потолок нагрузки» (для потолка — пункт 1).
""")
            if input("Запустить? (y/n): ").strip().lower() not in ("y", "д", "да"):
                continue
            phases = (["baseline", "bulk", "realistic", "cooldown"] if do_bulk
                      else ["baseline", "realistic", "cooldown"])
            cfg = _make_cfg(s, phases,
                            {"baseline": 120, "bulk_settle": 1800,
                             "realistic": int(hours * 3600), "cooldown": 120})
            _run_cfg(cfg, s)
        elif choice == "7":
            s["base_url"] = _ask("URL индексатора", s["base_url"])
            s["watched_dir"] = _ask("Папка для индексации (стенд её создаст)",
                                    s["watched_dir"])
            s["out_dir"] = _ask("Каталог артефактов", s["out_dir"])
            s["num_files"] = _ask("Размер корпуса, файлов", s["num_files"])
            s["search_qps"] = _ask("Поиск, запросов/с", s["search_qps"])
            s["probe_qps"] = _ask("Probe во время индексации, запросов/с",
                                  s["probe_qps"])
            s["query_type"] = _ask("query_type (hybrid/vector/text)",
                                   s["query_type"])
            s["searches_per_hour"] = _ask("[реалистичный режим] поисков/час",
                                          s.get("searches_per_hour", 20))
            s["files_per_hour"] = _ask("[реалистичный режим] новых файлов/час",
                                       s.get("files_per_hour", 12))
            s["post_config"] = _ask("Триггерить индексацию POST /config (y/n)",
                                    s["post_config"])
            s["sample_psi"] = _ask("Писать PSI /proc/pressure (y/n)",
                                   s["sample_psi"])
            s["sample_pid"] = _ask("PID индексатора для CPU/RSS (0 = выкл)",
                                   s["sample_pid"])
            save_settings(s)
            print("  Сохранено в", MENU_SETTINGS_FILE)
            continue

        if choice in ("1", "2", "3", "5") and not s["watched_dir"]:
            print("  !! Сначала задайте папку индексации в Настройках (7)")
            continue

        if choice == "1":
            _print_hour_plan(s)
            if input("Запустить? (y/n): ").strip().lower() not in ("y", "д", "да"):
                continue
            cfg = _make_cfg(s, ["baseline", "bulk", "incremental",
                                "sustained", "cooldown"],
                            {"baseline": 300, "bulk_settle": 1500,
                             "incremental": 900, "sustained": 1800,
                             "cooldown": 300})
            _run_cfg(cfg, s)
        elif choice == "2":
            d = {}
            d["baseline"] = _ask("Простой до нагрузки, сек", 300)
            d["bulk_settle"] = _ask("Макс. ожидание индексации, сек", 1800)
            d["incremental"] = _ask("Дозагрузка, сек", 900)
            d["sustained"] = _ask("Поисковая нагрузка, сек", 1800)
            d["cooldown"] = _ask("Простой после, сек", 300)
            cfg = _make_cfg(s, ["baseline", "bulk", "incremental",
                                "sustained", "cooldown"], d)
            _run_cfg(cfg, s)
        elif choice == "3":
            settle = _ask("Макс. ожидание индексации, сек", 1800)
            cfg = _make_cfg(s, ["baseline", "bulk", "cooldown"],
                            {"baseline": 60, "bulk_settle": settle,
                             "cooldown": 120})
            _run_cfg(cfg, s)
        elif choice == "4":
            dur = _ask("Длительность поиска, сек", 1800)
            cfg = _make_cfg(s, ["sustained"], {"sustained": dur})
            _run_cfg(cfg, s)
        elif choice == "5":
            dur = _ask("Длительность дозагрузки, сек", 900)
            cfg = _make_cfg(s, ["incremental"], {"incremental": dur})
            _run_cfg(cfg, s)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Нагрузочный стенд для семантического индексатора SberOS",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8000",
                   help="базовый URL API индексатора")
    p.add_argument("--out", default="./loadtest_run", help="каталог артефактов")
    p.add_argument("--index-mode", choices=["watch_folder", "api"],
                   default="watch_folder",
                   help="как запускать индексацию: раскладкой файлов в отслеживаемый "
                        "каталог или через HTTP-эндпоинт")
    p.add_argument("--watched-dir", default=None,
                   help="каталог, отслеживаемый индексатором (для watch_folder)")
    p.add_argument("--num-files", type=int, default=800,
                   help="размер корпуса (инженеры просили 500-1000)")
    p.add_argument("--seed", type=int, default=42, help="seed генерации корпуса")
    p.add_argument("--top-k", type=int, default=10, help="top_k для поиска")

    p.add_argument("--search-qps", type=float, default=2.0,
                   help="темп поиска в фазе sustained (запросов/с)")
    p.add_argument("--probe-qps", type=float, default=0.5,
                   help="темп probe-поиска во время bulk/incremental (для замера деградации)")
    p.add_argument("--max-inflight", type=int, default=16,
                   help="макс. одновременных HTTP-запросов")

    p.add_argument("--baseline-sec", type=int, default=180,
                   help="длительность фазы простоя (фон при запуске)")
    p.add_argument("--incremental-sec", type=int, default=1800,
                   help="длительность фазы дозагрузки")
    p.add_argument("--incremental-batch", type=int, default=5,
                   help="сколько новых файлов добавлять за итерацию")
    p.add_argument("--incremental-period-sec", type=float, default=30.0,
                   help="пауза между порциями дозагрузки")
    p.add_argument("--sustained-hours", type=float, default=24.0,
                   help="длительность длинной поисковой фазы (часов)")
    p.add_argument("--cooldown-sec", type=int, default=180,
                   help="длительность фазы возврата в простой")

    p.add_argument("--status-poll-sec", type=float, default=2.0)
    p.add_argument("--bulk-settle-sec", type=int, default=3600,
                   help="макс. ожидание завершения первичной индексации")

    p.add_argument("--sample-interval", type=float, default=2.0,
                   help="период встроенного сэмплера, с")
    p.add_argument("--sample-psi", action="store_true",
                   help="писать /proc/pressure/{cpu,memory,io} (влияние на соседей)")
    p.add_argument("--sample-pid", type=int, default=None,
                   help="pid индексатора для per-process CPU/RSS (нужен psutil)")

    p.add_argument("--query-type", default="hybrid",
                   choices=["hybrid", "vector", "text"],
                   help="query_type в теле POST /api/v1/search")
    p.add_argument("--safe-mode", action="store_true",
                   help="ОБХОД race-бага индексатора: не слать поиск, пока идёт "
                        "индексация. Поиск во время индексации вызывает "
                        "release_memory() на занятой модели -> RuntimeError -> "
                        "эмбеддер умирает НАВСЕГДА (все /search = 500). "
                        "В safe-mode: сначала индексация, потом поиск.")
    p.add_argument("--file-types", default="pdf,docx,txt",
                   help="состав корпуса (сборка 2026-08-25 индексирует pdf/docx/doc/pptx/txt)")
    p.add_argument("--email-folder", default="",
                   help="ОБХОД дефекта 'email root not found': путь к каталогу, "
                        "который индексатор примет как email root (напр. "
                        "~/.thunderbird). Пустой = как раньше.")
    p.add_argument("--email-enabled", action="store_true",
                   help="включить индексацию почты (по умолчанию выключена; "
                        "для НТ документов почта не нужна)")
    p.add_argument("--no-post-config", action="store_true",
                   help="не дёргать POST /api/v1/config перед bulk-фазой "
                        "(если папка уже прописана в ~/.ragsearch/user_conf.yml)")

    # эндпоинты (переопределение)
    p.add_argument("--search-path", default=None)
    p.add_argument("--search-method", default=None)
    p.add_argument("--status-path", default=None)

    p.add_argument("--discover", action="store_true",
                   help="прочитать /openapi.json и распечатать маршруты, затем выйти")
    p.add_argument("--scenario", choices=["stress", "user"], default=None,
                   help="headless-сценарий без меню (для вызова из bash): "
                        "stress = максимум нагрузки; user = пользовательский путь "
                        "(30 поисков/час, 1 индексация, 1 доиндексация)")
    p.add_argument("--minutes", type=int, default=60,
                   help="общая длительность headless-сценария, минут")
    p.add_argument("--searches-per-hour", type=float, default=30.0,
                   help="[user] поисков в час")
    p.add_argument("--reindex-files", type=int, default=10,
                   help="[user] размер разовой доиндексации в середине часа")
    p.add_argument("--menu", action="store_true",
                   help="интерактивное меню (также запускается при вызове без аргументов)")
    p.add_argument("--self-test", action="store_true",
                   help="прогон против встроенного мок-индексатора (короткие фазы)")
    p.add_argument("--smoke", action="store_true",
                   help="короткие фазы для проверки интеграции с реальным сервисом")
    return p


def run_scenario(args) -> int:
    """Headless-запуск для вызова из bash-оболочки. Настройки берутся из
    loadtest_settings.json (если есть) и переопределяются флагами CLI."""
    s = load_settings()
    if args.base_url != "http://127.0.0.1:8000" or not s.get("base_url"):
        s["base_url"] = args.base_url
    if args.watched_dir:
        s["watched_dir"] = args.watched_dir
    if args.out != "./loadtest_run":
        s["out_dir"] = args.out
    if not s.get("watched_dir"):
        print("ОШИБКА: не задана папка индексации (--watched-dir или настройки меню)")
        return 2
    s["num_files"] = args.num_files if args.num_files != 800 else s.get("num_files", 800)
    s["search_qps"] = args.search_qps
    s["probe_qps"] = args.probe_qps
    s["query_type"] = args.query_type
    s["safe_mode"] = bool(args.safe_mode)
    if args.file_types:
        set_type_mix(args.file_types)
        log(f"Состав корпуса: {', '.join(t for t, _ in TYPE_MIX)}")
    if args.email_folder:
        s["email_folder"] = args.email_folder
    if args.email_enabled:
        s["email_enabled"] = True
    if args.sample_pid:
        s["sample_pid"] = args.sample_pid
    s["sample_psi"] = True if args.sample_psi else s.get("sample_psi", True)

    total = args.minutes * 60
    # Монитор идёт ровно `total` секунд и в конце гасит питон (stop_load).
    # Поэтому baseline короткий, а поисковая фаза ставится НА ВСЁ ОКНО — чтобы
    # запросы шли непрерывно и не «выпали», даже если индексация затянулась.
    if args.scenario == "stress":
        baseline = 15
        if args.safe_mode:
            s["probe_qps"] = 0.0
        incremental = min(300, max(60, total // 4))
        cfg = _make_cfg(s, ["baseline", "bulk", "incremental",
                            "sustained", "cooldown"],
                        {"baseline": baseline, "bulk_settle": total,
                         "incremental": incremental, "sustained": total,
                         "cooldown": 0})
        cfg.safe_mode = args.safe_mode
        cfg.out_dir = Path(s["out_dir"])
        log(f"HEADLESS STRESS: {args.minutes} мин, поиск {s['search_qps']} зап/с "
            f"до конца окна (bulk -> дозагрузка -> непрерывный поиск)")
    else:  # user
        baseline = 15
        s["searches_per_hour"] = args.searches_per_hour
        s["files_per_hour"] = 0.0       # фоновый ручеёк выключен: ровно 1 доиндексация
        # ВАЖНО: во время индексации поиск тоже должен быть «пользовательским»,
        # а не 0.5 зап/с. Ставим probe = searches_per_hour, переведённые в qps.
        s["probe_qps"] = round(args.searches_per_hour / 3600.0, 5)
        if args.safe_mode:
            s["probe_qps"] = 0.0        # ни одного поиска во время индексации
        cfg = _make_cfg(s, ["baseline", "bulk", "realistic", "cooldown"],
                        {"baseline": baseline, "bulk_settle": total,
                         "realistic": total, "cooldown": 0})
        cfg.realistic_midrun_files = 0 if args.safe_mode else args.reindex_files
        cfg.safe_mode = args.safe_mode
        cfg.out_dir = Path(s["out_dir"])
        if args.safe_mode:
            log("SAFE-MODE: поиск во время индексации ОТКЛЮЧЁН, доиндексация "
                "в середине отключена (обход race-бага release_memory).")
        log(f"HEADLESS USER: {args.minutes} мин, ~{args.searches_per_hour:.0f} "
            f"поисков/час (на всё окно), 1 индексация + 1 доиндексация "
            f"({args.reindex_files} файлов в середине)")
    _run_cfg(cfg, s)
    return 0


def main(argv: List[str]) -> int:
    try:
        os.umask(0)   # файлы/каталоги стенда доступны всем (важно при запуске под sudo)
    except Exception:
        pass
    if not argv or argv == ["--menu"]:
        try:
            return menu()
        except (KeyboardInterrupt, EOFError):
            print("\nВыход.")
            return 0

    args = build_arg_parser().parse_args(argv)

    if args.scenario:
        return run_scenario(args)

    if args.discover:
        return discover(args.base_url)

    ep = default_endpoints(query_type=args.query_type)
    if args.search_path:   ep.search_path = args.search_path
    if args.search_method: ep.search_method = args.search_method
    if args.status_path:   ep.status_path = args.status_path

    out_dir = Path(args.out)
    watched = Path(args.watched_dir) if args.watched_dir else None

    sustained_sec = int(args.sustained_hours * 3600)
    baseline = args.baseline_sec
    incremental = args.incremental_sec
    cooldown = args.cooldown_sec
    num_files = args.num_files
    index_mode = args.index_mode
    base_url = args.base_url

    stop_mock = threading.Event()
    mock_thread = None

    if args.self_test:
        log("SELF-TEST: короткие фазы + встроенный мок-индексатор")
        out_dir = Path("./selftest_run")
        watched = out_dir / "watched"
        watched.mkdir(parents=True, exist_ok=True)
        index_mode = "watch_folder"
        base_url = "http://127.0.0.1:8077"
        num_files = 60
        baseline, incremental, cooldown = 3, 12, 3
        sustained_sec = 15
        mock_thread = threading.Thread(
            target=run_mock_server, args=(8077, watched, stop_mock), daemon=True)
        mock_thread.start()
        time.sleep(0.5)
    elif args.smoke:
        baseline, incremental, cooldown = 10, 60, 10
        sustained_sec = 120

    cfg = RunConfig(
        base_url=base_url, out_dir=out_dir, watched_dir=watched,
        index_mode=index_mode, num_files=num_files, seed=args.seed,
        top_k=args.top_k, search_qps=args.search_qps, probe_qps=args.probe_qps,
        max_inflight=args.max_inflight, baseline_sec=baseline,
        incremental_sec=incremental, incremental_batch=args.incremental_batch,
        incremental_period_sec=args.incremental_period_sec,
        sustained_sec=sustained_sec, cooldown_sec=cooldown,
        status_poll_sec=args.status_poll_sec, bulk_settle_sec=args.bulk_settle_sec,
        sample_interval=args.sample_interval, sample_psi=args.sample_psi,
        sample_pid=args.sample_pid,
        post_config=not args.no_post_config, query_type=args.query_type,
    )

    orch = Orchestrator(cfg, ep)

    def handle_sigint(signum, frame):
        log("SIGINT — останавливаюсь корректно...")
        orch.stop_flag.set()
    signal.signal(signal.SIGINT, handle_sigint)

    rc = orch.run()

    if mock_thread:
        stop_mock.set()
        mock_thread.join(timeout=2)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
