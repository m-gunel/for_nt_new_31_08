"""Воркер OCR: страницы PDF → текст (Tesseract, встроенный в PyMuPDF).

Отдельный процесс того же бинаря: `main --ocr-worker` (из исходников —
`python -m src.ocr_worker`). pymupdf держит GIL на всё время C-вызова, и
распознавание в процессе демона замораживало бы поиск и HTTP на секунды на
страницу; аллокатор Leptonica в MuPDF к тому же глобальный — второй OCR в том
же процессе падает. Здесь только stdlib и pymupdf: ни OpenVINO, ни сервера.

Запрос — JSON в stdin: {"path", "pages" (номера с 1), "dpi", "language",
"tessdata"}. Ответ — по строке JSON на страницу сразу после распознавания:
{"page", "text"} или {"page", "error"}. Если родитель убьёт воркер по таймауту,
уже распознанные страницы останутся в его stdout.
"""

import json
import os
import sys

# Потолок pixmap (~A3 при 300 dpi): страница-плакат иначе заняла бы сотни МБ.
MAX_PIXELS = 20_000_000
# Целевой nice, а не приращение: поток пула, запустивший воркер, может быть уже с nice.
NICE = 10


def _ocr(page, dpi: int, language: str, tessdata: str) -> str:
    import pymupdf

    area = page.rect.width * page.rect.height * (dpi / 72) ** 2
    if area > MAX_PIXELS:
        dpi = int(dpi * (MAX_PIXELS / area) ** 0.5)
    # Только RGB: по серому pixmap pdfocr молча отдаёт пустой текст (PyMuPDF 1.28.2).
    pix = page.get_pixmap(dpi=dpi)
    data = pix.pdfocr_tobytes(compress=False, language=language, tessdata=tessdata)
    with pymupdf.open("pdf", data) as doc:
        return doc[0].get_text()


def main() -> int:
    request = json.load(sys.stdin)
    try:
        os.nice(max(0, NICE - os.nice(0)))
    except OSError:
        pass
    import pymupdf

    parent = os.getppid()
    with pymupdf.open(request["path"]) as doc:
        for n in request["pages"]:
            if os.getppid() != parent:  # демон умер — распознавать незачем
                return 1
            try:
                text = _ocr(
                    doc[n - 1], request["dpi"], request["language"], request["tessdata"]
                )
                line = {"page": n, "text": text}
            except Exception as exc:  # одна страница не валит остальные
                line = {"page": n, "error": f"{type(exc).__name__}: {exc}"}
            sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
