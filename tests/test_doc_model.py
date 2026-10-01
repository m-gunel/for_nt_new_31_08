"""Модель документа (src/parsers/doc_model.py): нарезка и привязка чанков к месту."""

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.parsers.doc_model import Block, ParsedDoc, chunk_document, full_text, prepare

# Маленький сплиттер по символам: границы чанков предсказуемы без модели.
SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=60, chunk_overlap=0, length_function=len
)


def _words(n: int, word: str) -> str:
    return " ".join(f"{word}{i}" for i in range(n))


def test_chunk_knows_its_pages_and_position():
    doc = prepare(
        ParsedDoc(
            [
                Block(_words(12, "альфа"), page=1),
                Block(_words(12, "бета"), page=2),
                Block(_words(12, "гамма"), page=3),
            ]
        )
    )
    chunks = chunk_document(doc, SPLITTER)
    text = full_text(doc)
    assert [c["idx"] for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert text[c["char_start"] : c["char_end"]] == c["text"]
        words = c["text"].split()
        expected = {
            1 if w.startswith("альфа") else 2 if w.startswith("бета") else 3
            for w in words
        }
        assert c["page_start"] == min(expected) and c["page_end"] == max(expected)
    assert {c["page_start"] for c in chunks} == {1, 2, 3}


def test_section_path_follows_headings():
    doc = prepare(
        ParsedDoc(
            [
                Block("Глава 1. Общие положения", heading=1),
                Block(_words(8, "текст")),
                Block("1.1. Термины", heading=2),
                Block(_words(8, "термин")),
                Block("Глава 2. Порядок", heading=1),
                Block(_words(8, "порядок")),
            ]
        )
    )
    by_word = {}
    for c in chunk_document(doc, SPLITTER):
        by_word[c["text"].split()[-1].rstrip("0123456789")] = c.get("section")
    assert by_word["термин"] == "Глава 1. Общие положения › 1.1. Термины"
    assert by_word["порядок"] == "Глава 2. Порядок"


def test_whole_blocks_are_kept_as_is():
    card = "Таблица 4\nПоказатель: Активы\n1 января 2025 — Всего: 613"
    doc = prepare(
        ParsedDoc(
            [
                Block(_words(5, "проза")),
                Block(
                    card,
                    kind="table_row",
                    page=2,
                    section="Лист 2 › Таблица 4",
                    table_id="t4",
                    row_start=7,
                    row_end=7,
                    whole=True,
                ),
            ]
        )
    )
    chunks = chunk_document(doc, SPLITTER)
    row = chunks[-1]
    assert row["text"] == card
    assert row["kind"] == "table_row" and row["section"] == "Лист 2 › Таблица 4"
    assert (row["table_id"], row["row_start"], row["row_end"], row["page_start"]) == (
        "t4",
        7,
        7,
        2,
    )
    assert chunks[0]["kind"] == "text"


def test_short_and_repeated_chunks_are_dropped():
    doc = prepare(
        ParsedDoc(
            [
                Block("Стр. 2", whole=True),
                Block("Повторяющийся абзац текста документа", whole=True),
                Block("—", whole=True),
                Block("ПОВТОРЯЮЩИЙСЯ  абзац текста документа", whole=True),
                Block("Другой абзац с содержанием", whole=True),
            ]
        )
    )
    chunks = chunk_document(doc, SPLITTER)
    assert [c["text"] for c in chunks] == [
        "Повторяющийся абзац текста документа",
        "Другой абзац с содержанием",
    ]
    assert [c["idx"] for c in chunks] == [0, 1]


def test_document_of_only_short_text_is_kept():
    chunks = chunk_document(prepare(ParsedDoc([Block("Счёт 15")])), SPLITTER)
    assert [c["text"] for c in chunks] == ["Счёт 15"]


def test_page_number_pages_are_dropped():
    doc = prepare(
        ParsedDoc(
            [
                Block("Первая страница текста", page=1),
                Block("- 2 -", page=2),
                Block("Стр. 3", page=3),
                Block("Третья страница", page=4),
            ]
        )
    )
    assert [b.page for b in doc.blocks] == [1, 4]


def test_txt_lines_and_paragraphs():
    lines = [
        "Заголовок текстового файла",
        "",
        "",
        "",
        "Первый абзац строка номер один",
        "строка номер два",
        "",
        "Второй абзац текста файла",
    ]
    doc = prepare(ParsedDoc([Block(s, line=n) for n, s in enumerate(lines, 1)]))
    # Пустые строки подряд — одна граница абзаца.
    assert full_text(doc) == (
        "Заголовок текстового файла\n\nПервый абзац строка номер один\nстрока номер два\n\n"
        "Второй абзац текста файла"
    )
    chunks = chunk_document(
        doc,
        RecursiveCharacterTextSplitter(
            chunk_size=30, chunk_overlap=0, length_function=len
        ),
    )
    second = next(c for c in chunks if c["text"].startswith("Второй"))
    assert (second["line_start"], second["line_end"]) == (8, 8)
    first = next(c for c in chunks if c["text"].startswith("Первый"))
    assert first["line_start"] == 5
