"""Карточка документа: строка kind="card" после чанков документа.

Карточка несёт имя файла и свойства документа (название, автор, тема…), чтобы
запрос «найди файл …» и код из имени находили документ одной строкой, а не
случайным куском текста.
"""

import aiofiles
import pytest

from src.common import CARD_IDX, CARD_KIND, document_card_text
from src.parsers import _process_email_chunks, file_chunks, read_document_text
from src.thunderbird.lazy_iter_mbox import iter_emails_as_metadata
from tests.mbox_fixture import (
    make_email_with_attachment,
    make_profile,
    minimal_docx,
)


def test_card_text_lists_nonempty_properties_in_order():
    meta = {
        "original_name": "Отчёт ЦБ_21.pdf",
        "title": "Квартальный обзор банковского сектора",
        "author": "Иванов И.И.",
        "last_modified_by": "Петров П.П.",
        "subject": "Надзор",
        "keywords": "капитал;  резервы\n ликвидность",
        "description": "",
        "category": "не входит в карточку",
    }
    assert document_card_text(meta) == (
        "Документ: Отчёт ЦБ_21\n"
        "Название: Квартальный обзор банковского сектора\n"
        "Автор: Иванов И.И.\n"
        "Изменил: Петров П.П.\n"
        "Тема: Надзор\n"
        "Ключевые слова: капитал; резервы ликвидность"
    )


def test_card_text_drops_values_already_in_card():
    meta = {
        "original_name": "Положение 5348-У.docx",
        # Совпадает с именем файла с точностью до регистра, ё и знаков.
        "title": "положение  5348 у",
        "author": "Сёмин А.",
        # Тот же человек — повторять «Изменил» незачем.
        "last_modified_by": "СЕМИН А",
        "subject": "Положение",
    }
    assert document_card_text(meta) == "Документ: Положение 5348-У\nАвтор: Сёмин А."


def test_card_text_compares_whole_words_without_labels():
    # «Иван» — не начало «Иванова», «банк» — не конец «Сбербанк».
    assert (
        document_card_text({"original_name": "Отчет_Иванова.docx", "author": "Иван"})
        == "Документ: Отчет_Иванова\nАвтор: Иван"
    )
    assert (
        document_card_text({"original_name": "Сбербанк_2024.xlsx", "keywords": "банк"})
        == "Документ: Сбербанк_2024\nКлючевые слова: банк"
    )
    assert (
        document_card_text(
            {
                "original_name": "a.txt",
                "author": "Петрова А.",
                "last_modified_by": "Петров",
            }
        )
        == "Документ: a\nАвтор: Петрова А.\nИзменил: Петров"
    )
    # Подписи полей — не значения: название «Документ» не повтор строки «Документ: …».
    assert (
        document_card_text({"original_name": "scan_001.pdf", "title": "Документ"})
        == "Документ: scan_001\nНазвание: Документ"
    )


def test_card_text_cuts_long_values():
    meta = {"original_name": "a.txt", "description": "слово " * 200}
    line = document_card_text(meta).split("\n")[1]
    assert line.startswith("Описание: слово")
    assert len(line) == len("Описание: ") + 300


def test_card_text_without_name_and_properties_is_empty():
    assert document_card_text({}) == ""
    assert document_card_text(None) == ""
    assert document_card_text({"title": "Без имени"}) == "Название: Без имени"


def _txt(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


def test_one_card_after_all_chunks(tmp_path):
    lines = [
        f"Абзац {i}: прибыль банка выросла на {i} процентов за квартал."
        for i in range(200)
    ]
    path = _txt(tmp_path / "Отчёт ЦБ_21.txt", "\n\n".join(lines))
    rows = list(file_chunks(path))

    cards = [r for r in rows if r["kind"] == CARD_KIND]
    assert len(rows) > 2 and len(cards) == 1
    card = rows[-1]
    assert card is cards[0]
    assert card["idx"] == CARD_IDX
    assert [r["idx"] for r in rows[:-1]] == list(range(len(rows) - 1))
    # Документ и путь — как у чанков; места в тексте у карточки нет.
    assert card["path"] == rows[0]["path"] and card["meta"] is rows[0]["meta"]
    assert all(
        card[k] is None for k in ("page_start", "line_start", "char_start", "section")
    )
    assert card["text"] == document_card_text(rows[0]["meta"])
    assert card["text"].startswith("Документ: Отчёт ЦБ_21")


def test_no_card_for_document_without_chunks(tmp_path):
    path = _txt(tmp_path / "пустой.txt", "  \n\n \n")
    assert list(file_chunks(path)) == []


def test_full_text_has_no_card(tmp_path):
    path = _txt(tmp_path / "заметка.txt", "Заметка о встрече по бюджету отдела.")
    assert read_document_text(path) == "Заметка о встрече по бюджету отдела."


@pytest.mark.asyncio
async def test_email_attachment_gets_card_body_does_not(tmp_path):
    msg = make_email_with_attachment(
        "Отчёт за квартал",
        "Коллеги, во вложении отчёт за квартал.",
        msg_id="card-1@fixture",
        filename="Отчёт ЦБ_21.docx",
        payload=minimal_docx("во вложении квартальные цифры по марже банка"),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    root = make_profile(tmp_path / "tb-profile", {"INBOX": [msg]})
    [meta] = [
        m
        for _start, m in iter_emails_as_metadata(
            str(root / "INBOX"), prefix="email_INBOX:"
        )
        if m is not None
    ]

    async def fail_cb(meta, *, permanent):
        raise AssertionError(f"письмо не разобрано: {permanent=}")

    async with aiofiles.open(root / "INBOX", "rb") as f:
        rows = [c async for c in _process_email_chunks({"INBOX": f}, meta, fail_cb)]

    body = [r for r in rows if r["path"].endswith(":body")]
    attachment = [r for r in rows if r["path"].endswith(":Отчёт ЦБ_21.docx")]
    assert body and attachment and len(body) + len(attachment) == len(rows)
    assert all(r.get("kind") != CARD_KIND for r in body)
    assert [r["kind"] for r in attachment][-1] == CARD_KIND
    assert sum(r["kind"] == CARD_KIND for r in attachment) == 1
    card = attachment[-1]
    assert card["text"].startswith("Документ: Отчёт ЦБ_21")
    assert card["file_meta"] == meta
