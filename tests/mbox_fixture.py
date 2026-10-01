"""Генератор синтетических mbox-файлов и структуры почтового профиля.

Почтовый тракт от `iter_emails_as_metadata` и ниже работает с обычными
mbox-файлами и не зависит от Thunderbird, поэтому тесты гоняются на любой
машине. Единственное, что здесь эмулируется от Thunderbird, — структура
каталога `ImapMail/imap.sberbank.ru/<папка>` с парой `<name>` + `<name>.msf`,
которую ждут `find_email_root` и `list_email_folders`.
"""

import mailbox
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

# Имя папки «Отправленные» в modified-UTF-7 — ровно так её называет
# Thunderbird на диске и так она захардкожена в src/thunderbird/find_files.py.
SENT_FOLDER_MUTF7 = "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"

# Даты писем относительные, а не абсолютные.
#
# Индексатор отбирает письма окном `index_last_n_days` от «сейчас», поэтому
# фикстура с зашитыми датами живёт ровно до того дня, когда окно уползёт за
# них: 28 августа 2026 набор был зелёным, 2 сентября те же семь тестов падали
# с «assert 1 == 4», хотя код между этими датами не менялся.
#
# Все письма лежат в интервале от 5 до 2 суток назад. Это одновременно:
#   - внутри рабочих окон (30 дней в cfg/dev.yml, 90 в test_config_reload) —
#     значит вся фикстура доходит до очереди;
#   - снаружи окна в 1 день, на котором test_skipped_by_date_is_reported
#     проверяет, что по дате отсекается ВСЯ фикстура.
_NOW = datetime.now(UTC)
_DEFAULT_DATE = _NOW - timedelta(days=2)


def _base_email(
    subject: str,
    *,
    msg_id: str,
    date: datetime | None = _DEFAULT_DATE,
    frm: str = "ivanov@sberbank.ru",
    to: str = "petrov@sberbank.ru",
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg["Message-ID"] = f"<{msg_id}>"
    if date is not None:
        msg["Date"] = format_datetime(date)
    return msg


def make_plain_email(
    subject: str,
    body: str,
    *,
    msg_id: str,
    date: datetime | None = _DEFAULT_DATE,
    **kwargs,
) -> EmailMessage:
    msg = _base_email(subject, msg_id=msg_id, date=date, **kwargs)
    msg.set_content(body)
    return msg


def make_alternative_email(
    subject: str,
    body: str,
    *,
    msg_id: str,
    date: datetime | None = _DEFAULT_DATE,
    **kwargs,
) -> EmailMessage:
    """multipart/alternative: один и тот же текст как text/plain и text/html.

    Ровно этот случай даёт дубли тела в индексе (см. P2 в плане): обе версии
    проходят через `process_message` как отдельные `Body`.
    """
    msg = _base_email(subject, msg_id=msg_id, date=date, **kwargs)
    msg.set_content(body)
    msg.add_alternative(f"<html><body><p>{body}</p></body></html>", subtype="html")
    return msg


def make_email_with_attachment(
    subject: str,
    body: str,
    *,
    msg_id: str,
    filename: str,
    payload: bytes,
    maintype: str = "text",
    subtype: str = "plain",
    date: datetime | None = _DEFAULT_DATE,
    **kwargs,
) -> EmailMessage:
    msg = _base_email(subject, msg_id=msg_id, date=date, **kwargs)
    msg.set_content(body)
    msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def minimal_docx(text: str) -> bytes:
    """Минимальный .docx, который читает docx2txt: zip с word/document.xml."""
    buf = BytesIO()
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f"<w:t>{text}</w:t>"
        "</w:r></w:p></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def minimal_pptx(slides: list[list[str]]) -> bytes:
    """Минимальный .pptx для local_read_file: слайды с абзацами DrawingML.

    slides — по списку абзацев на слайд; номер слайда равен позиции в списке,
    чтобы тест мог отличить числовую сортировку slide10 от лексикографической.
    """
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        "</Types>"
    )
    buf = BytesIO()
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        for number, paragraphs in enumerate(slides, 1):
            shapes = "".join(
                "<p:sp><p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p>"
                "</p:txBody></p:sp>" % escape(paragraph)
                for paragraph in paragraphs
            )
            slide = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>" % shapes
            )
            zf.writestr("ppt/slides/slide%d.xml" % number, slide)
    return buf.getvalue()


_SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PK = "http://schemas.openxmlformats.org/package/2006/relationships"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def minimal_xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    """Минимальный .xlsx с настоящими workbook.xml и rels (имена листов).

    Ячейка в строке — либо str (общая строка, sharedStrings), либо число
    (числовая ячейка), либо кортеж ("inline", "текст") — inlineStr. Три вида
    покрывают все ветки разбора листа в мосте.
    """
    strings: list[str] = []
    plans: list[list[list[tuple]]] = []
    for _title, rows in sheets:
        plan_rows = []
        for row in rows:
            plan_row = []
            for cell in row:
                if isinstance(cell, tuple):
                    plan_row.append(("inline", cell[1]))
                elif isinstance(cell, str):
                    if cell not in strings:
                        strings.append(cell)
                    plan_row.append(("s", strings.index(cell)))
                else:
                    plan_row.append((None, str(cell)))
            plan_rows.append(plan_row)
        plans.append(plan_rows)

    def override(part: str, kind: str) -> str:
        return (
            '<Override PartName="/%s" ContentType="application/vnd.'
            "openxmlformats-officedocument.spreadsheetml.%s+xml\"/>" % (part, kind)
        )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        + override("xl/workbook.xml", "sheet.main")
        + "".join(
            override("xl/worksheets/sheet%d.xml" % n, "worksheet")
            for n in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s">' % (_SS, _OD)
        + "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
            % (escape(title), number, number)
            for number, (title, _rows) in enumerate(sheets, 1)
        )
        + "</workbook>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">' % _PK
        + "".join(
            '<Relationship Id="rId%d" Type="%s/worksheet" '
            'Target="worksheets/sheet%d.xml"/>' % (number, _OD, number)
            for number in range(1, len(sheets) + 1)
        )
        + "</Relationships>"
    )
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="%s"' % _SS
        + ' count="%d" uniqueCount="%d">' % (len(strings), len(strings))
        + "".join("<si><t>%s</t></si>" % escape(text) for text in strings)
        + "</sst>"
    )

    def sheet_xml(rows: list[list[tuple]]) -> str:
        lines = []
        for number, row in enumerate(rows, 1):
            cells = []
            column = ord("A")
            for kind, payload in row:
                ref = "%s%d" % (chr(column), number)
                column += 1
                if kind == "s":
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, payload))
                elif kind == "inline":
                    cells.append(
                        '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'
                        % (ref, escape(payload))
                    )
                else:
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, payload))
            lines.append('<row r="%d">%s</row>' % (number, "".join(cells)))
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (_SS, "".join(lines))
        )

    buf = BytesIO()
    with ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        for number, rows in enumerate(plans, 1):
            zf.writestr("xl/worksheets/sheet%d.xml" % number, sheet_xml(rows))
    return buf.getvalue()


def write_mbox(path: Path, messages: list[EmailMessage]) -> None:
    """Записать письма в mbox. mailbox.mbox сам ставит From_-строки и
    эскейпит "From " в телах — ровно то, что ждёт `_generate_toc_lazy`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    box = mailbox.mbox(str(path))
    try:
        for msg in messages:
            box.add(msg)
        box.flush()
    finally:
        box.close()


def append_to_mbox(path: Path, messages: list[EmailMessage]) -> None:
    """Дописать письма в конец существующего mbox — эмуляция прихода новых."""
    box = mailbox.mbox(str(path))
    try:
        for msg in messages:
            box.add(msg)
        box.flush()
    finally:
        box.close()


def compact_mbox(path: Path, *, drop_msg_ids: set[str]) -> None:
    """Переписать mbox без указанных писем — эмуляция compact Thunderbird.

    После этого все байтовые offset'ы сдвигаются, как при настоящей компакции.
    """
    box = mailbox.mbox(str(path))
    try:
        keep = [
            msg
            for _key, msg in sorted(box.items())
            if (msg["Message-ID"] or "").strip("<>") not in drop_msg_ids
        ]
    finally:
        box.close()
    write_mbox(path, [_as_email_message(m) for m in keep])


def _as_email_message(msg) -> EmailMessage:
    # mailbox отдаёт mboxMessage; для перезаписи хватает сырых байтов.
    from email import message_from_bytes
    from email.policy import default

    return message_from_bytes(msg.as_bytes(), policy=default)  # type: ignore[return-value]


def make_profile(
    root: Path,
    folders: dict[str, list[EmailMessage]],
    *,
    imap_host: str = "imap.sberbank.ru",
) -> Path:
    """Создать структуру профиля Thunderbird и вернуть каталог с папками.

    root/ImapMail/<host>/<folder> — mbox-файл, рядом пустой <folder>.msf
    (list_email_folders учитывает только пары).
    """
    mail_root = root / "ImapMail" / imap_host
    mail_root.mkdir(parents=True, exist_ok=True)
    for name, messages in folders.items():
        write_mbox(mail_root / name, messages)
        (mail_root / f"{name}.msf").write_bytes(b"")
    return mail_root


def default_inbox_messages() -> list[EmailMessage]:
    """Стандартный набор писем, покрывающий сценарии из плана."""
    return [
        make_plain_email(
            "Отчёт по кварталу",
            "Коллеги, во вложении отчёт по третьему кварталу. "
            "Просьба посмотреть цифры по марже.",
            msg_id="plain-1@fixture",
            date=_NOW - timedelta(days=5),
        ),
        make_alternative_email(
            "Встреча в четверг",
            "Перенос встречи по индексатору на четверг 15:00.",
            msg_id="alt-1@fixture",
            date=_NOW - timedelta(days=4),
        ),
        make_email_with_attachment(
            "Документы на подпись",
            "Направляю документ на подпись.",
            msg_id="attach-1@fixture",
            filename="договор.txt",
            payload="Текст договора о поставке оборудования.".encode(),
            date=_NOW - timedelta(days=3),
        ),
        make_plain_email(
            "Ответ по задаче",
            "Задачу закрыл, деплой прошёл успешно.\n\nС уважением,\nИванов Иван",
            msg_id="signature-1@fixture",
            date=_NOW - timedelta(days=2),
        ),
        # Письмо без Date — индексатор обязан его молча пропустить.
        make_plain_email(
            "Письмо без даты",
            "У этого письма нет заголовка Date.",
            msg_id="nodate-1@fixture",
            date=None,
        ),
    ]
