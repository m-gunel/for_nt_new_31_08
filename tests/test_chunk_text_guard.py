"""Охрана: текст и границы чанков обычных PDF и txt не меняются от правок разбора.

Эталонные хэши — от разбора обычных файлов. Правки для дефектных случаев (знаки
F0xx, битые байты txt, закладки без букв, PDF без текста) их менять не должны.
"""

import codecs
import hashlib
import json

import pymupdf
import pytest

from src.parsers import file_chunks

_PARA = (
    "Сумма кредита составила 100 млн руб., ставка 12,5 % годовых. Заёмщик обязуется "
    "погашать долг ежемесячно; просрочка — штраф 0,1 % в день (см. п. 4.2). "
)
_TXT = "\n".join(
    f"{n}. Раздел {n}\n" + _PARA * 6 + "\n• Первый пункт\n• Второй пункт\n"
    for n in range(1, 6)
)


def _digest(path) -> tuple[int, str]:
    # Чанк целиком без метаданных и пути; карточку пропускаем: в ней даты файла.
    rows = [
        {k: v for k, v in r.items() if k not in ("meta", "path")}
        for r in file_chunks(path)
        if r["kind"] != "card"
    ]
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    return len(rows), hashlib.sha256(raw).hexdigest()[:16]


def test_plain_pdf_chunks_are_unchanged(tmp_path):
    path = tmp_path / "отчёт.pdf"
    with pymupdf.open() as doc:
        for n in range(1, 4):
            page = doc.new_page()
            page.insert_htmlbox(
                page.rect + (50, 50, -50, -50),
                f"<h1>Раздел {n}. Кредитный риск</h1><p>{_PARA * 12}</p>"
                "<ul><li>Первый пункт</li><li>Второй пункт</li></ul>",
            )
        doc.set_toc([[1, f"Раздел {n}. Кредитный риск", n] for n in range(1, 4)])
        doc.save(str(path))
    assert _digest(path) == (4, "457eb304923ec377")


_SAME = (5, "767f8bf55b7d82b4")  # один и тот же текст в любой кодировке


@pytest.mark.parametrize(
    ("data", "golden"),
    [
        (_TXT.encode(), _SAME),
        (codecs.BOM_UTF8 + _TXT.encode(), _SAME),
        (_TXT.encode("cp1251"), _SAME),
        (codecs.BOM_UTF16_LE + _TXT.encode("utf-16-le"), _SAME),
        # Один битый байт — по-прежнему UTF-8 с заменой в конце.
        (_TXT.encode() + b"\xff", (5, "c4714501e9a6c534")),
    ],
    ids=["utf-8", "utf-8-sig", "cp1251", "utf-16", "utf-8-broken"],
)
def test_plain_txt_chunks_are_unchanged(tmp_path, data, golden):
    path = tmp_path / "заметки.txt"
    path.write_bytes(data)
    assert _digest(path) == golden
