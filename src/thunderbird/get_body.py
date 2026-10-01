import re
from dataclasses import dataclass
from email.message import Message

from bs4 import BeautifulSoup

from src.utils import get_logger

from .body_cleanup import extract_unique_body
from .lazy_iter_mbox import _decode_filename

lg = get_logger("app.email")
_patt_clean = re.compile(r"\n{2,}", re.MULTILINE | re.DOTALL)


def get_charset(body: str):
    soup = BeautifulSoup(body, features="lxml")
    if soup.head and soup.head.find("meta", charset=True):
        if meta := soup.head.find("meta", charset=True):
            return meta.get("charset")


@dataclass
class Body:
    body: str


@dataclass
class Attachment:
    payload: bytes
    original_name: str


def _process_message(part: Message):
    from src.parsers import get_parser

    ctype = part.get_content_type()
    cdispo = str(part.get("Content-Disposition") or "")
    if (
        ctype
        in {
            "text/plain",
            "text/html",
            # "multipart/alternative",
        }
        and "attachment" not in cdispo
    ):
        charset = part.get_content_charset()
        # print("Charset: ", charset)
        # print("CType: ", ctype)
        if (payload := part.get_payload(decode=True)) and isinstance(payload, bytes):
            try:
                body: str = (
                    payload.decode(encoding=charset)
                    if charset is not None
                    else payload.decode()
                )
            except Exception as exc:
                lg.warning("exception while processing message: %s", exc)
                return
        else:
            lg.debug("empty body")
            return
        if ctype == "text/html":
            body = BeautifulSoup(body, features="lxml").get_text()
        if body is not None:
            body = _patt_clean.sub("\n\n", body.strip())
            body = extract_unique_body(body).strip()
            return Body(body=body)
    else:
        if _file_name := part.get_filename():
            _file_name = _decode_filename(_file_name) if _file_name else None
        else:
            return
        if _file_name and (parts := _file_name.split(".")):
            ext = parts[-1].lower()
        else:
            return
        if get_parser(ext):
            return Attachment(
                payload=part.get_payload(decode=True),  # type: ignore
                original_name=_file_name,
            )


def _iter_parts(message: Message):
    """Обход MIME-дерева, отдающий из multipart/alternative одну версию.

    multipart/alternative — это варианты одного и того же текста (plain и
    html). Плоский walk() отдавал оба, и тело письма индексировалось дважды:
    двойной чанкинг, двойной эмбеддинг, дубли в выдаче. Берём text/plain
    (он уже готовый текст), иначе html, иначе вложенный multipart
    (например, related с html внутри).
    """
    if message.get_content_maintype() != "multipart":
        yield message
        return
    payload = message.get_payload()
    if not isinstance(payload, list):
        return
    if message.get_content_subtype() == "alternative":
        best = None
        for part in payload:
            ctype = part.get_content_type()
            if ctype == "text/plain":
                best = part
                break
            if best is None and (
                ctype == "text/html" or part.get_content_maintype() == "multipart"
            ):
                best = part
        if best is not None:
            yield from _iter_parts(best)
        return
    for part in payload:
        yield from _iter_parts(part)


def process_message(message: Message, encoding: str = "utf-8"):
    for part in _iter_parts(message):
        if body := _process_message(part):
            yield body
