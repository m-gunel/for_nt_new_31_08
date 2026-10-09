import codecs
import re
from dataclasses import dataclass
from email.message import Message

from bs4 import BeautifulSoup
from bs4.dammit import EncodingDetector

from src.utils import get_logger

from .body_cleanup import extract_unique_body
from .lazy_iter_mbox import _decode_filename

lg = get_logger("app.email")
_patt_clean = re.compile(r"\n{2,}", re.MULTILINE | re.DOTALL)


# Блочные элементы и ячейки таблиц: get_text() склеивает текст соседних
# блоков в одно слово («суммуИтого100»). Разделитель — пробел, а не перевод
# строки: строки тела режет extract_unique_body, и лишний перевод строки
# разорвал бы блок заголовков цитаты («От:» и «Отправлено:» должны идти подряд).
_HTML_BLOCK_TAGS = frozenset(
    (
        "address article aside blockquote br dd div dl dt footer h1 h2 h3 h4 h5 h6 "
        "header hr li ol p pre section table td th tr ul"
    ).split()
)


def get_charset(body: bytes) -> str | None:
    """Кодировка из <meta> HTML-тела: и charset=, и http-equiv Content-Type."""
    return EncodingDetector.find_declared_encoding(body, is_html=True)


def _decode_body(payload: bytes, charset: str | None, is_html: bool) -> str:
    """Декодировать тело письма, не теряя его из-за кривой метки или байта.

    Метке из Content-Type (у HTML, если её нет или она негодная, — из
    <meta>) верим, битые байты заменяются на «�». Незнакомая Python метка
    (unknown-8bit, x-user-defined) и us-ascii при 8-битном тексте — то же,
    что нет метки: utf-8, а если текст в нём не читается — cp1251, кодировка
    старой русской почты.
    """
    for label in (charset, get_charset(payload) if is_html else None):
        if not label:
            continue
        try:
            if codecs.lookup(label).name != "ascii":
                return payload.decode(label, errors="replace")
        except (LookupError, ValueError):
            # Незнакомая метка, не текстовый кодек (base64), NUL в метке.
            continue
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1251", errors="replace")


def _html_text(html: str) -> str:
    """Текст HTML-тела с пробелом на границах блоков и ячеек."""
    soup = BeautifulSoup(html, features="lxml")
    for tag in soup.find_all(True):
        if tag.name in _HTML_BLOCK_TAGS:
            # Внутрь тега, а не до и после него: вставка рядом сдвигает список
            # детей родителя, и таблица в тысячи строк обходилась бы
            # квадратично.
            tag.insert(0, " ")
            tag.append(" ")
    return soup.get_text()


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
        }
        and "attachment" not in cdispo
    ):
        if (payload := part.get_payload(decode=True)) and isinstance(payload, bytes):
            body: str = _decode_body(
                payload, part.get_content_charset(), is_html=ctype == "text/html"
            )
        else:
            lg.debug("empty body")
            return
        if ctype == "text/html":
            body = _html_text(body)
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

    multipart/alternative — варианты одного текста (plain и html); плоский
    walk() отдал бы оба, и тело индексировалось бы дважды. Берём text/plain,
    иначе html, иначе вложенный multipart (например, related с html внутри).
    В письмо внутри письма (message/rfc822: пересланное вложением, исходное
    в отчёте о недоставке) спускаемся, как walk(): иначе его текст и
    вложения не индексируются.
    """
    if message.get_content_maintype() == "message" and message.is_multipart():
        # base64/QP внутри message/* запрещены RFC 2046, но встречаются:
        # парсер тогда видит вместо письма текст base64 — его не индексируем.
        cte = str(message.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte not in ("base64", "quoted-printable"):
            for inner in message.get_payload():
                yield from _iter_parts(inner)
        return
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
