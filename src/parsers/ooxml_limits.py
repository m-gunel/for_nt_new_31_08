"""Защита от zip-бомбы: объём OOXML после распаковки проверяется до чтения частей.

Входные лимиты (max_size индексатора, лимит /read_file) считают сжатый размер, а
deflate сжимает до ~1000:1. Размер части берём из центрального каталога
(ZipInfo.file_size): zipfile не отдаёт больше заявленного, поэтому проверка до
чтения держит и python-pptx, и openpyxl, и docx2txt.
"""

import re
import zipfile
from typing import BinaryIO

MAX_XML_BYTES = 64 * 1024 * 1024  # XML-часть, которую разбирают в дерево целиком
MAX_UNPACKED_BYTES = 256 * 1024 * 1024  # все части, которые читатель держит в памяти
# Живой XML жмётся в 5–20 раз; сотни к одному — признак бомбы.
MAX_RATIO = 100

# Листы xlsx openpyxl (read_only) обходит потоком и останавливается на max_rows,
# а сводные кэши не читает вовсе: в общий объём их не считаем. Лист — любое имя
# в xl/worksheets: генераторы называют их не только sheetN.xml.
XLSX_STREAMED = re.compile(r"xl/worksheets/[^/]+\.xml|xl/pivotCache/.+")
# Читаются целиком, но у живых файлов бывают больше MAX_XML_BYTES (общие строки
# выгрузки на сотни тысяч строк, огромный текст docx): их отвергаем только по
# сжатию, а в общий объём считаем.
XLSX_LARGE = re.compile(r"xl/sharedStrings\.xml")
DOCX_LARGE = re.compile(r"word/document\.xml")
# Картинки и вложенные объекты docx не читают ни parse_docx, ни docx2txt.
DOCX_UNREAD = re.compile(r"word/(media|embeddings)/.+")

_XML = re.compile(r".+\.(xml|rels)", re.I)


def oversize_reason(
    file: BinaryIO,
    streamed: re.Pattern | None = None,
    large: re.Pattern | None = None,
) -> str | None:
    """Почему пакет не читать, или None. Позиция файла после проверки — в начале.

    streamed — части, которые читатель не держит в памяти целиком или не читает:
    для них только проверка коэффициента сжатия. large — части, которые читаются
    целиком и законно бывают большими: предел части — только при сжатии больше
    MAX_RATIO:1, в общий объём они идут. Битый архив — None: его отвергнет
    штатный разбор.
    """
    try:
        with zipfile.ZipFile(file) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        return None
    finally:
        file.seek(0)
    total = 0
    for info in infos:
        name, size = info.filename, info.file_size
        bomb = size > MAX_XML_BYTES and size > MAX_RATIO * max(info.compress_size, 1)
        bomb_reason = (
            f"{name}: {size} Б после распаковки, сжатие больше "
            f"{MAX_RATIO}:1 — похоже на zip-бомбу"
        )
        if streamed is not None and streamed.fullmatch(name):
            if bomb:
                return bomb_reason
            continue
        if large is not None and large.fullmatch(name):
            if bomb:
                return bomb_reason
        elif size > MAX_XML_BYTES and _XML.fullmatch(name):
            return f"{name}: {size} Б после распаковки — больше {MAX_XML_BYTES} Б"
        total += size
    if total > MAX_UNPACKED_BYTES:
        return f"после распаковки {total} Б — больше {MAX_UNPACKED_BYTES} Б"
    return None
