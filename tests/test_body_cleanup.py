"""Извлечение текста письма: очистка цитат/подписи и дедуп alternative."""

from email import message_from_bytes

from src.thunderbird.body_cleanup import extract_unique_body
from src.thunderbird.get_body import Attachment, Body, process_message
from tests.mbox_fixture import (
    make_alternative_email,
    make_email_with_attachment,
    make_plain_email,
)


def test_signature_is_cut():
    body = "Задачу закрыл.\n\nС уважением,\nИванов Иван"
    assert extract_unique_body(body) == "Задачу закрыл.\n"


def test_signature_kept_when_disabled():
    body = "Задачу закрыл.\n\nС уважением,\nИванов"
    assert extract_unique_body(body, skip_signature=False) == body


def test_regards_mid_sentence_not_cut():
    """«с уважением» посреди предложения — не подпись."""
    body = "Мы относимся с уважением к срокам.\nПродолжение письма."
    assert extract_unique_body(body) == body


def test_single_header_like_line_not_cut():
    """«Тема: отпуск» как живой текст — не граница цитаты."""
    body = "Коллеги, ответ ниже.\nТема: отпуск согласован.\nСпасибо."
    assert extract_unique_body(body) == body


def test_header_block_cuts():
    body = (
        "Мой ответ.\n"
        "From: Petrov\n"
        "Sent: Monday\n"
        "To: Ivanov\n"
        "Subject: Re: отчёт\n"
        "старый текст"
    )
    assert extract_unique_body(body) == "Мой ответ."


def test_quoted_lines_cut():
    body = "Согласен.\n> а что по срокам?\n> и по бюджету?"
    assert extract_unique_body(body) == "Согласен."


def test_on_wrote_cut():
    body = "Ответ.\nOn Mon, Aug 3, Petrov wrote:\n> вопрос"
    assert extract_unique_body(body) == "Ответ."


def test_dashes_separator_cuts_but_arrow_does_not():
    assert extract_unique_body("Текст.\n-------\nпересланное") == "Текст."
    body = "См. схему:\n-------> вход слева\nи дальше текст."
    assert extract_unique_body(body) == body


def test_line_start_regards_cuts_even_mid_letter():
    """Подпись ищется по началу строки, а не по месту в письме.

    Абзац «С уважением к …» посреди текста тоже обрывает письмо — так и
    описано в extract_unique_body.
    """
    body = (
        "Коллеги, добрый день.\nС уважением к вашему решению, предлагаю ещё\nвариант."
    )
    assert extract_unique_body(body) == "Коллеги, добрый день."
    assert extract_unique_body(body, skip_signature=False) == body


def _roundtrip(msg):
    return message_from_bytes(msg.as_bytes())


def test_alternative_yields_single_body():
    """plain и html версии одного текста — один Body, не два."""
    msg = _roundtrip(
        make_alternative_email("Встреча", "Перенос встречи на четверг.", msg_id="alt@t")
    )
    bodies = [p for p in process_message(msg) if isinstance(p, Body)]
    assert len(bodies) == 1
    assert "Перенос встречи" in bodies[0].body


def test_plain_email_single_body():
    msg = _roundtrip(make_plain_email("Тест", "Просто текст.", msg_id="p@t"))
    parts = list(process_message(msg))
    assert len(parts) == 1
    assert isinstance(parts[0], Body)


def test_mixed_yields_body_and_attachment():
    msg = _roundtrip(
        make_email_with_attachment(
            "Док",
            "Во вложении.",
            msg_id="a@t",
            filename="файл.txt",
            payload="содержимое".encode(),
        )
    )
    parts = list(process_message(msg))
    kinds = {type(p) for p in parts}
    assert kinds == {Body, Attachment}
    att = next(p for p in parts if isinstance(p, Attachment))
    assert att.original_name == "файл.txt"
    assert att.payload == "содержимое".encode()
