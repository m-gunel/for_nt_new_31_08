"""Тело письма: кодировки, письмо внутри письма, текст HTML-версии."""

import base64
import email
from email.message import Message

import aiofiles
import pytest

from src.common import FileMetadata
from src.parsers import _process_email_chunks
from src.server.router import _extract_attachment
from src.thunderbird.get_body import Attachment, Body, process_message
from tests.mbox_fixture import (
    make_alternative_email,
    make_email_with_attachment,
    make_plain_email,
)

_TEXT = "Коллеги, отчёт по марже готов."


def _raw(content_type: bytes, payload: bytes) -> bytes:
    return (
        b"From: a@b.ru\r\nSubject: t\r\nMessage-ID: <t@fixture>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type + b"\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + payload
    )


def _bodies(msg: Message) -> list[str]:
    return [p.body for p in process_message(msg) if isinstance(p, Body)]


@pytest.mark.parametrize(
    ("content_type", "payload"),
    [
        (b"text/plain", _TEXT.encode("cp1251")),
        (b"text/plain; charset=unknown-8bit", _TEXT.encode("cp1251")),
        (b"text/plain; charset=x-user-defined", _TEXT.encode()),
        (b"text/plain; charset=us-ascii", _TEXT.encode("cp1251")),
        (b"text/plain; charset=utf-8", _TEXT.encode() + b"\xff"),
        (b"text/plain; charset=windows-1251", _TEXT.encode("cp1251") + b"\x98"),
    ],
    ids=[
        "без-charset-cp1251",
        "unknown-8bit",
        "x-user-defined",
        "us-ascii-с-8-битным-текстом",
        "utf-8-с-битым-байтом",
        "windows-1251-с-байтом-0x98",
    ],
)
def test_body_survives_bad_charset(content_type, payload):
    """Кривая метка charset или битый байт не стоят письму всего тела."""
    bodies = _bodies(email.message_from_bytes(_raw(content_type, payload)))

    assert len(bodies) == 1 and _TEXT in bodies[0]


@pytest.mark.parametrize(
    ("meta", "encoding"),
    [
        (b'<meta charset="windows-1251">', "cp1251"),
        (
            b'<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">',
            "koi8-r",
        ),
    ],
    ids=["meta-charset", "meta-http-equiv"],
)
def test_html_charset_taken_from_meta(meta, encoding):
    """HTML без charset в Content-Type декодируется по <meta>."""
    html = (
        b"<html><head>"
        + meta
        + b"</head><body>"
        + _TEXT.encode(encoding)
        + b"</body></html>"
    )

    assert _bodies(email.message_from_bytes(_raw(b"text/html", html))) == [_TEXT]


@pytest.mark.asyncio
async def test_cp1251_body_is_indexed_not_marked_empty(tmp_path):
    """Тело в cp1251 без charset даёт чанки, а не пометку «текста нет»."""
    raw = _raw(b"text/plain", _TEXT.encode("cp1251"))
    mbox = tmp_path / "INBOX"
    mbox.write_bytes(raw)
    meta = FileMetadata(f"email_INBOX:0:{len(raw)}", len(raw), 0)
    fail_calls: list[bool] = []

    async def fail_cb(meta, *, permanent):
        fail_calls.append(permanent)

    async with aiofiles.open(mbox, "rb") as f:
        chunks = [c async for c in _process_email_chunks({"INBOX": f}, meta, fail_cb)]

    assert chunks and all(c["path"].endswith(":body") for c in chunks)
    assert fail_calls == []


def _forwarded_as_attachment() -> bytes:
    """«Переслать как вложение»: внутри message/rfc822 — текст и report.txt."""
    inner = make_email_with_attachment(
        "Отчёт",
        "Текст пересланного письма про маржу",
        msg_id="fwd-inner@fixture",
        filename="report.txt",
        payload="Текст вложения пересланного".encode(),
    )
    outer = make_plain_email("Fwd: Отчёт", "См. ниже", msg_id="fwd-outer@fixture")
    outer.add_attachment(inner, filename="Fwd.eml")
    return outer.as_bytes()


def test_forwarded_message_is_descended():
    """Тело и вложения письма внутри письма разбираются, как при walk()."""
    parts = list(process_message(email.message_from_bytes(_forwarded_as_attachment())))

    assert [p.body for p in parts if isinstance(p, Body)] == [
        "См. ниже",
        "Текст пересланного письма про маржу",
    ]
    assert [p.original_name for p in parts if isinstance(p, Attachment)] == [
        "report.txt"
    ]


@pytest.mark.asyncio
async def test_forwarded_attachment_is_indexed_and_openable(tmp_path):
    """Вложение пересланного письма попадает в индекс, и его ключ открывается."""
    raw = _forwarded_as_attachment()
    mbox = tmp_path / "INBOX"
    mbox.write_bytes(raw)
    meta = FileMetadata(f"email_INBOX:0:{len(raw)}", len(raw), 0)

    async def fail_cb(meta, *, permanent):
        return None

    async with aiofiles.open(mbox, "rb") as f:
        paths = {
            c["path"] async for c in _process_email_chunks({"INBOX": f}, meta, fail_cb)
        }

    assert f"{meta.path}:report.txt" in paths
    assert (
        _extract_attachment(raw, "report.txt") == "Текст вложения пересланного".encode()
    )


def test_forwarded_alternative_yields_single_body():
    """Из alternative внутри пересланного письма берётся одна версия."""
    inner = make_alternative_email(
        "Встреча", "Перенос встречи на четверг.", msg_id="alt-inner@fixture"
    )
    outer = make_plain_email("Fwd: Встреча", "Пересылаю", msg_id="alt-outer@fixture")
    outer.add_attachment(inner, filename="Fwd.eml")

    assert _bodies(email.message_from_bytes(outer.as_bytes())) == [
        "Пересылаю",
        "Перенос встречи на четверг.",
    ]


def test_base64_rfc822_is_not_indexed_as_text():
    """message/rfc822 в base64 (вопреки RFC 2046) не превращается в тело-мусор."""
    inner = make_plain_email("Отчёт", "Текст пересланного", msg_id="b64@fixture")
    raw = (
        "From: a@b.ru\r\nSubject: Fwd\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
        "--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nСм. ниже\r\n"
        "--XX\r\nContent-Type: message/rfc822\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(inner.as_bytes()).decode()
        + "\r\n--XX--\r\n"
    ).encode()

    assert _bodies(email.message_from_bytes(raw)) == ["См. ниже"]


def _html(html: str) -> Message:
    return email.message_from_bytes(_raw(b"text/html; charset=utf-8", html.encode()))


def test_html_blocks_are_not_glued():
    """Текст соседних блоков и ячеек HTML не склеивается в одно слово."""
    html = (
        '<div dir="ltr">Добрый день!<div>Счёт №5 на сумму</div>'
        "<table><tr><td>Итого</td><td>100</td></tr></table></div>"
    )

    [body] = _bodies(_html(html))

    assert body.split() == [
        "Добрый",
        "день!",
        "Счёт",
        "№5",
        "на",
        "сумму",
        "Итого",
        "100",
    ]


def test_html_inline_markup_does_not_split_words():
    """Разметка внутри слова (жирная часть слова) слово не разрывает."""
    assert _bodies(_html("<p><b>Сбер</b>Бизнес</p>")) == ["СберБизнес"]


def test_html_reply_header_block_is_still_cut():
    """Блок заголовков цитаты Outlook в HTML по-прежнему отрезается."""
    html = (
        '<p class="MsoNormal">Мой ответ.</p>\n'
        '<p class="MsoNormal"><b>От:</b> Петров Пётр<br>\n'
        "<b>Отправлено:</b> 5 октября 2026 г.<br>\n"
        "<b>Кому:</b> Иванов Иван<br>\n"
        "<b>Тема:</b> RE: отчёт</p>\n"
        '<p class="MsoNormal">старый текст</p>'
    )

    assert _bodies(_html(html)) == ["Мой ответ."]
