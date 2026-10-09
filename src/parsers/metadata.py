"""Метаданные документа: файловая система, содержимое, свойства самого файла.

- даты: создание — время рождения файла (st_birthtime на macOS, statx на Linux), а не
  ctime: ctime — это смена атрибутов, на копиях и после chmod он позже изменения;
  нет времени рождения — поле пустое;
- источник: file:// URI, адрес загрузки из атрибута user.xdg.origin.url (его ставят
  браузеры на Linux);
- содержимое: sha256 (дубли файлов, кэш OCR), MIME по сигнатуре, размер;
- свойства файла: PDF (`doc.metadata`), OOXML (`docProps/core.xml`) — общий разбор
  для docx, pptx, xlsx;
- название: свойства → закладка/заголовок → первая содержательная строка; мусор
  («Microsoft Word - …», «Документ1», пути) и не подтверждённое текстом отбрасывается.
"""

import ctypes
import hashlib
import os
import re
import sys
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET

from src.common.doc_meta import datetime_from_timestamp

from . import ooxml_limits
from .normalize import normalize_field

_OOXML_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_OLE_MIME = {"doc": "application/msword", "xls": "application/vnd.ms-excel"}
_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
_STATX_BTIME = 0x800
_AT_FDCWD = -100


def birth_time(path: str, st: os.stat_result | None = None) -> datetime | None:
    """Время рождения файла или None, если ФС/ОС его не знает (тогда не ctime)."""
    st = st or os.stat(path)
    bt = getattr(st, "st_birthtime", None)  # macOS, BSD, Windows
    if bt:
        return datetime_from_timestamp(bt)
    if sys.platform.startswith("linux"):
        return _statx_btime(path)
    return None


def _statx_btime(path: str) -> datetime | None:
    # os.statx появился только в Python 3.15 — зовём системный вызов glibc напрямую.
    try:
        statx = ctypes.CDLL(None, use_errno=True).statx
    except (OSError, AttributeError):
        return None
    statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    statx.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(256)  # sizeof(struct statx)
    if statx(_AT_FDCWD, os.fsencode(path), 0, _STATX_BTIME, buf) != 0:
        return None
    raw = buf.raw
    if not int.from_bytes(raw[0:4], sys.byteorder) & _STATX_BTIME:  # stx_mask
        return None
    sec = int.from_bytes(raw[80:88], sys.byteorder, signed=True)  # stx_btime.tv_sec
    nsec = int.from_bytes(raw[88:92], sys.byteorder)
    return datetime_from_timestamp(sec + nsec / 1e9) if sec > 0 else None


def origin_url(path: str) -> str | None:
    getxattr = getattr(os, "getxattr", None)  # только Linux
    if getxattr is None:
        return None
    try:
        return normalize_field(
            getxattr(path, "user.xdg.origin.url").decode("utf-8", "replace")
        )
    except OSError:
        return None


def file_metadata(path: str) -> dict:
    """Поля файловой системы: имя, расширение, размер, даты, источник."""
    p = Path(path)
    st = os.stat(path)
    # Дата, которую не примет колонка timestamp("ns"), — пусто: иначе запись
    # батча с этим файлом обрывает весь проход.
    modified = datetime_from_timestamp(st.st_mtime)
    born = birth_time(path, st)
    return {
        "original_name": p.name,
        "ext": p.suffix[1:].lower(),
        "size": st.st_size,
        # Копия с сохранённой датой изменения (cp -p, распаковка архива) рождается
        # позже, чем менялось содержимое: документ существовал уже к дате изменения.
        "date_created": min(born, modified) if born and modified else born,
        "date_modified": modified,
        "source_type": "file",
        "source_uri": p.absolute().as_uri(),
        "origin_url": origin_url(path),
    }


def sniff_mime(head: bytes, ext: str) -> str | None:
    """MIME по сигнатуре содержимого; расширение уточняет тип внутри контейнера."""
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return _OOXML_MIME.get(ext, "application/zip")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return _OLE_MIME.get(ext, "application/x-ole-storage")
    if ext == "txt":
        return "text/plain"
    return None


def content_metadata(file: BinaryIO, ext: str) -> dict:
    """sha256, размер и MIME одним проходом по содержимому; позиция возвращается в начало."""
    file.seek(0)
    h, size, head = hashlib.sha256(), 0, b""
    while data := file.read(1 << 20):
        if not head:
            head = data[:16]
        h.update(data)
        size += len(data)
    file.seek(0)
    return {"sha256": h.hexdigest(), "size": size, "mime": sniff_mime(head, ext)}


_PDF_DATE = re.compile(
    r"^D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-]\d{2}'?\d{2}'?)?"
)


def plausible(dt: datetime | None) -> datetime | None:
    if dt is None or not 1980 <= dt.year <= datetime.now().year + 1:
        return None
    return dt


def _local(dt: datetime) -> datetime:
    """Дата с часовым поясом → локальное время без пояса (как даты файловой системы)."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def parse_pdf_date(value: str | None) -> datetime | None:
    """`D:YYYYMMDDHHmmSS+HH'mm'` → datetime; части после года необязательны."""
    if not value or not (m := _PDF_DATE.match(value.strip())):
        return None
    y, mo, d, hh, mi, ss, tz = m.groups()
    iso = f"{y}-{mo or '01'}-{d or '01'}T{hh or '00'}:{mi or '00'}:{ss or '00'}"
    if tz == "Z":
        iso += "+00:00"
    elif tz:
        tz = tz.replace("'", "")  # +03'00' → +0300
        iso += f"{tz[:3]}:{tz[3:5] or '00'}"
    try:
        return plausible(_local(datetime.fromisoformat(iso)))
    except ValueError:
        return None


def parse_w3cdtf(value: str | None) -> datetime | None:
    """Дата OOXML (W3CDTF: `2024-05-01T10:00:00Z`, `2024-05-01`)."""
    if not value:
        return None
    try:
        return plausible(_local(datetime.fromisoformat(value.strip())))
    except ValueError:
        return None


def pdf_properties(doc) -> tuple[dict, list[str]]:
    """Поля из pymupdf.Document и кандидаты в название: metadata.title, закладка 1-го уровня."""
    md = doc.metadata or {}
    meta = {
        "author": md.get("author") or None,
        "subject": md.get("subject") or None,
        "keywords": md.get("keywords") or None,
        "doc_created": parse_pdf_date(md.get("creationDate")),
        "doc_modified": parse_pdf_date(md.get("modDate")),
        "page_count": doc.page_count,
    }
    titles = [md.get("title") or ""]
    try:
        titles += [t for level, t, *_ in doc.get_toc() if level == 1][:1]
    except Exception:  # битое оглавление не повод терять документ
        pass
    return meta, titles


def ooxml_properties(file: BinaryIO, ext: str) -> tuple[dict, list[str]]:
    """Свойства docx/pptx/xlsx из docProps/core.xml и объём из состава пакета.

    Объём: pptx — слайдов, xlsx — листов, docx — ≈страниц по разрывам, которые Word
    сохранил при последней вёрстке (lastRenderedPageBreak); нет их — пусто. Счётчик
    Pages из app.xml не берём: генераторы пишут туда «1». Позиция файла — в начало.
    """
    meta: dict = {}
    titles: list[str] = []
    try:
        with zipfile.ZipFile(file) as z:
            names = z.namelist()
            # Читаются целиком: часть больше предела не трогаем (zip-бомба).
            small = {
                i.filename
                for i in z.infolist()
                if i.file_size <= ooxml_limits.MAX_XML_BYTES
            }
            if "docProps/core.xml" in small:
                root = ET.fromstring(z.read("docProps/core.xml"))

                def get(tag: str) -> str | None:
                    el = root.find(tag, _CORE_NS)
                    return el.text if el is not None and el.text else None

                meta.update(
                    author=get("dc:creator"),
                    last_modified_by=get("cp:lastModifiedBy"),
                    subject=get("dc:subject"),
                    keywords=get("cp:keywords"),
                    description=get("dc:description"),
                    category=get("cp:category"),
                    doc_created=parse_w3cdtf(get("dcterms:created")),
                    doc_modified=parse_w3cdtf(get("dcterms:modified")),
                )
                titles.append(get("dc:title") or "")
            if ext == "pptx":
                meta["page_count"] = sum(
                    1 for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
                )
            elif ext == "xlsx":
                meta["page_count"] = sum(
                    1 for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
                )
            elif ext == "docx" and "word/document.xml" in small:
                breaks = z.read("word/document.xml").count(b"lastRenderedPageBreak")
                meta["page_count"] = breaks + 1 if breaks else None
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
        ET.ParseError,
        KeyError,
        OSError,
    ):
        pass
    finally:
        file.seek(0)
    return meta, titles


_MS_PREFIX = re.compile(
    r"^microsoft\s+(office\s+)?(word|excel|powerpoint)\s*-\s*", re.I
)
_EXT_SUFFIX = re.compile(r"\.(docx?|xlsx?|pptx?|pdf|txt|rtf|odt)$", re.I)
_JUNK = re.compile(
    r"^(please read first|untitled|без названия|(документ|книга|презентация|лист|слайд|document|book|"
    r"presentation|sheet|slide)\s*\d*|содержание|оглавление|введение|contents|introduction)$",
    re.I,
)
_LETTERS = re.compile(r"[^\W\d_]")
_TITLE_WORD = re.compile(r"[^\W\d_]{4,}")


def _fold(s: str) -> str:
    return s.lower().replace("ё", "е")


def clean_title(title: str | None, head: str) -> str | None:
    """Название или None: мусор и то, что не подтверждается началом текста, отбрасываются."""
    t = normalize_field(title)
    if not t:
        return None
    t = _EXT_SUFFIX.sub("", _MS_PREFIX.sub("", t)).strip(" .-–—")
    if (
        not t
        or _JUNK.match(t)
        or "\\" in t
        or t.startswith("/")
        or len(_LETTERS.findall(t)) < 3
    ):
        return None
    words = _TITLE_WORD.findall(_fold(t))[:8]
    if words:
        h = _fold(head)
        if sum(w[:6] in h for w in words) / len(words) < 0.5:
            return None
    return t


# Штампы над текстом официальных документов — не название.
_STAMP = re.compile(
    r"^(зарегистрирован|утвержд|согласован|приложени|форма\b|код формы|конфиденциально"
    r"|для служебного пользования|экземпляр)",
    re.I,
)


def first_line_title(text: str) -> str | None:
    """Первая содержательная строка среди первых пяти непустых: есть слово из 3+ букв,
    это не штамп («Зарегистрировано…») и не абзац (≤150 символов)."""
    lines = [
        s for s in (line.strip(" .-–—•*#") for line in text.split("\n", 200)[:200]) if s
    ]
    for line in lines[:5]:
        if (
            len(line) <= 150
            and re.search(r"[^\W\d_]{3,}", line)
            and not _STAMP.match(line)
        ):
            return normalize_field(line)
    return None


def language(text: str) -> str | None:
    """ru / en / ru,en по доле кириллицы и латиницы в первых 100 000 символах."""
    sample = text[:100_000]
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", sample))
    lat = len(re.findall(r"[a-zA-Z]", sample))
    if cyr + lat < 20:
        return None
    share = cyr / (cyr + lat)
    return "ru" if share >= 0.7 else "en" if share <= 0.3 else "ru,en"
