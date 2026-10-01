import codecs
from typing import BinaryIO

from .base import Parser, parser
from .doc_model import Block, ParsedDoc

# Старший байт символа UTF-16: 0x00 — латиница и знаки, 0x04 — кириллица.
_HIGH = {0x00, 0x04}


def _utf16_without_bom(sample: bytes) -> str | None:
    """UTF-16 без BOM: в каждой второй позиции — старшие байты латиницы и кириллицы.

    Проверяется до UTF-8: такие байты (\x1e\x04B\x04…) сходят за правильный UTF-8.
    """
    sample = sample[: len(sample) // 2 * 2]
    if len(sample) < 8:
        return None
    odd, even = sample[1::2], sample[0::2]
    odd_high = sum(b in _HIGH for b in odd) / len(odd)
    even_high = sum(b in _HIGH for b in even) / len(even)
    if odd_high >= 0.9 and even_high < 0.5:
        return "utf-16-le"
    if even_high >= 0.9 and odd_high < 0.5:
        return "utf-16-be"
    return None


def decode_text(data: bytes) -> tuple[str, str]:
    """Текст и кодировка: BOM → UTF-16 без BOM → UTF-8 (единичные битые байты
    допустимы) → cp1251, в которой лежат старые русские txt."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace"), "utf-16"
    if enc := _utf16_without_bom(data[:4096]):
        return data.decode(enc, "replace"), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        if text.count("\ufffd") <= max(3, len(text) // 1000):
            return text, "utf-8"
        return data.decode("cp1251", "replace"), "cp1251"


@parser("txt")
class TxtParser(Parser):
    """Парсер для обычных текстовых файлов (.txt)."""

    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Строка — блок: чанк знает свои строки в исходном файле."""
        text, encoding = decode_text(file.read())
        # Только \n и \r: splitlines() режет ещё по \x0b, \x0c, \x1c–\x1e, U+2028 —
        # и номера строк разошлись бы с редактором.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        blocks = [Block(line, line=n) for n, line in enumerate(lines, 1)]
        return ParsedDoc(blocks, {"encoding": encoding})
