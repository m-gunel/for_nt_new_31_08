"""OCR страниц PDF без текстового слоя (src/parsers/ocr.py, src/ocr_worker.py).

Скан делается прямо в тесте: страница с текстом → картинка 300 dpi → новая
страница из одной картинки. Распознавание — настоящим Tesseract из
models/tessdata; без данных такие тесты пропускаются.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pymupdf
import pytest

from src.parsers import file_chunks, ocr, read_document_text
from src.parsers.base import SkipFile
from src.utils import get_config

ROOT = Path(__file__).resolve().parents[1]
HAS_TESSDATA = all(
    (ROOT / "models/tessdata" / f"{lang}.traineddata").is_file()
    for lang in ("rus", "eng")
)
needs_tessdata = pytest.mark.skipif(not HAS_TESSDATA, reason="нет models/tessdata")

TOTAL = "Итого активы составляют 8 453 023 тыс. рублей"


def _page_with_text(doc: pymupdf.Document, text: str) -> pymupdf.Page:
    page = doc.new_page()
    page.insert_htmlbox(
        pymupdf.Rect(60, 80, 540, 400), f"<p style='font-size:16px'>{text}</p>"
    )
    return page


def _make_pdf(path: Path, pages: list[tuple[str, str]]) -> Path:
    """("text", …) — страница с текстовым слоем, ("scan", …) — та же, но картинкой."""
    out = pymupdf.open()
    for kind, text in pages:
        if kind == "text":
            _page_with_text(out, text)
            continue
        with pymupdf.open() as tmp:
            pix = _page_with_text(tmp, text).get_pixmap(dpi=300)
        page = out.new_page()
        page.insert_image(page.rect, stream=pix.tobytes("png"))
    out.save(str(path))
    return path


@pytest.fixture
def ocr_cache(tmp_path, monkeypatch):
    """Кэш OCR — во временной папке, а не в хранилище демона."""
    cache = tmp_path / "ocr_cache"
    monkeypatch.setattr(ocr, "_cache_file", lambda sha: cache / f"{sha}.json")
    return cache


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def test_needs_ocr_rule(tmp_path):
    path = _make_pdf(tmp_path / "rule.pdf", [("text", TOTAL), ("scan", TOTAL)])
    with pymupdf.open(path) as doc:
        logo = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(200)
        logo.insert_image(pymupdf.Rect(20, 20, 80, 80), pixmap=pix)
        verdicts = [ocr.needs_ocr(p, p.get_text()) for p in doc]
    assert verdicts == [False, True, False]  # текст / скан / логотип на пустой странице


@needs_tessdata
def test_scan_is_recognized_in_order(tmp_path, ocr_cache):
    path = _make_pdf(
        tmp_path / "mixed.pdf",
        [
            ("text", "Первая страница отчёта банка"),
            ("scan", TOTAL),
            ("text", "Третья страница"),
        ],
    )
    chunks = list(file_chunks(path))
    text = "\n".join(c["text"] for c in chunks)
    assert "активы" in text.lower() and "8453023" in _digits(text)
    assert text.index("Первая") < text.lower().index("итого") < text.index("Третья")
    assert json.loads(next(ocr_cache.iterdir()).read_text())  # кэш записан


@needs_tessdata
def test_read_file_takes_ocr_only_from_cache(tmp_path, ocr_cache, monkeypatch):
    path = _make_pdf(
        tmp_path / "scan.pdf",
        [("text", "Сопроводительное письмо банка"), ("scan", TOTAL)],
    )
    real_popen, indexing = ocr.subprocess.Popen, {"on": False}

    def guarded(*args, **kwargs):
        assert indexing["on"], "чтение файла не должно запускать OCR"
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(ocr.subprocess, "Popen", guarded)
    assert "8453023" not in _digits(read_document_text(path))  # кэша ещё нет
    indexing["on"] = True
    list(file_chunks(path))  # индексация заполняет кэш
    indexing["on"] = False
    assert "8453023" in _digits(read_document_text(path))


@needs_tessdata
def test_page_limit(tmp_path, ocr_cache, monkeypatch, caplog):
    monkeypatch.setattr(get_config().ocr_cfg, "max_pages_per_file", 1)
    path = _make_pdf(
        tmp_path / "two.pdf", [("scan", TOTAL), ("scan", "Вторая страница скана")]
    )
    with caplog.at_level("WARNING"):
        text = "\n".join(c["text"] for c in file_chunks(path))
    assert "8453023" in _digits(text) and "вторая" not in text.lower()
    assert any(
        "1 из 2" in r.getMessage() and "лимит" in r.getMessage() for r in caplog.records
    )


def test_without_tessdata_worker_is_not_started(tmp_path, ocr_cache, monkeypatch):
    monkeypatch.setattr(get_config().ocr_cfg, "tessdata_path", str(tmp_path / "empty"))
    monkeypatch.setattr(ocr, "_warned", set())

    def no_worker(*args, **kwargs):
        raise AssertionError("без tessdata воркер не запускается")

    monkeypatch.setattr(ocr.subprocess, "Popen", no_worker)
    path = _make_pdf(tmp_path / "scan.pdf", [("scan", TOTAL)])
    with pytest.raises(SkipFile) as err:
        list(file_chunks(path))
    assert not err.value.permanent and "сканов 1" in err.value.reason


def test_kill_running_stops_worker(tmp_path, ocr_cache, monkeypatch):
    monkeypatch.setattr(
        ocr,
        "_worker_cmd",
        lambda: ([sys.executable, "-c", "import time; time.sleep(30)"], None),
    )
    monkeypatch.setattr(ocr, "_tessdata", lambda cfg: tmp_path)
    path = _make_pdf(tmp_path / "scan.pdf", [("scan", TOTAL)])
    threading.Timer(0.5, ocr.kill_running).start()
    started = time.monotonic()
    with open(path, "rb") as f:
        assert ocr.recognize(f, [1]) == {}
    assert time.monotonic() - started < 10


def test_main_dispatches_ocr_worker(tmp_path):
    """`main.py --ocr-worker` — воркер, а не сервер: отвечает и выходит сразу."""
    path = _make_pdf(tmp_path / "text.pdf", [("text", TOTAL)])
    done = subprocess.run(
        [sys.executable, "main.py", "--ocr-worker"],
        input=json.dumps({"path": str(path), "pages": [], "dpi": 200,
                          "language": "rus", "tessdata": str(tmp_path)}),
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )  # fmt: skip
    assert done.returncode == 0 and done.stdout == ""
