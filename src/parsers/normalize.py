"""Нормализация текста после разбора — одна для всех форматов.

Текст блоков приводится к виду, в котором его видят токенизатор, BM25 и человек:
невидимые и управляющие символы не дробят слова, неразрывные пробелы не отличаются
от обычных, символы шрифта Symbol становятся знаками Unicode. Поля метаданных
(название, автор, тема) — то же плюс одна строка без эмодзи, не длиннее 300 символов.

Склейка переносов по словарю документа и колонтитулы — забота очистки PDF; здесь
склеивается только мягкий перенос в конце строки: он по определению стоит внутри слова.
"""

import re
import unicodedata

# Шрифт Symbol кладёт свои знаки в область U+F020–U+F0FF (код знака + 0xF000).
# Греческие буквы — по раскладке Symbol (буква латиницы → греческая).
_SYMBOL_GREEK = dict(
    zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖαβχδεφγηιϕκλμνοπθρστυϖωξψζ",
        strict=True,
    )
)
_SYMBOL_OTHER = {
    0x22: "∀",
    0x24: "∃",
    0x27: "∋",
    0x2A: "*",
    0x2D: "-",
    0x40: "≅",
    0x5C: "∴",
    0x5E: "⊥",
    0x60: "‾",
    0x7E: "∼",
    0xA1: "ϒ",
    0xA2: "′",
    0xA3: "≤",
    0xA4: "/",
    0xA5: "∞",
    0xA6: "ƒ",
    0xAB: "↔",
    0xAC: "←",
    0xAD: "↑",
    0xAE: "→",
    0xAF: "↓",
    0xB0: "°",
    0xB1: "±",
    0xB2: "″",
    0xB3: "≥",
    0xB4: "×",
    0xB5: "∝",
    0xB6: "∂",
    0xB7: "•",
    0xB8: "÷",
    0xB9: "≠",
    0xBA: "≡",
    0xBB: "≈",
    0xBC: "…",
    0xC4: "⊗",
    0xC5: "⊕",
    0xC6: "∅",
    0xC7: "∩",
    0xC8: "∪",
    0xCE: "∈",
    0xCF: "∉",
    0xD0: "∠",
    0xD1: "∇",
    0xD2: "®",
    0xD3: "©",
    0xD4: "™",
    0xD5: "∏",
    0xD6: "√",
    0xD7: "⋅",
    0xD8: "¬",
    0xD9: "∧",
    0xDA: "∨",
    0xDB: "⇔",
    0xDC: "⇐",
    0xDE: "⇒",
    0xE5: "∑",
    0xF2: "∫",
}
# Куски больших скобок формул (⎛⎜⎝⎞⎟⎠ и т. п.) — в тексте это шум, а не знаки.
_SYMBOL_DROP = set(range(0xE6, 0xF0)) | set(range(0xF3, 0x100))


def _symbol_char(code: int) -> str:
    low = code - 0xF000
    if 0x20 <= low <= 0x7E and chr(low) in _SYMBOL_GREEK:
        return _SYMBOL_GREEK[chr(low)]
    if low in _SYMBOL_OTHER:
        return _SYMBOL_OTHER[low]
    if low in _SYMBOL_DROP:
        return ""
    if 0x20 <= low <= 0x7E:  # цифры и знаки препинания совпадают с ASCII
        return chr(low)
    return "•"  # прочее в этой области — маркеры списков шрифтов Wingdings и т. п.


_SYMBOL_MAP = {c: _symbol_char(c) for c in range(0xF020, 0xF100)}

_SPACES = {
    0x00A0: " ",
    0x2007: " ",
    0x202F: " ",
    0x2009: " ",
    0x200A: " ",
    0x2002: " ",
    0x2003: " ",
}
_C0_C1 = re.compile("[\x00-\x08\x0c\x0e-\x1f\x7f-\x9f]")
# В PDF старых «Вестников» дефис извлекается управляющим символом: «№385\x1aП»,
# «финан\x18\nсовых». Между буквами или цифрами это дефис.
_CTRL_HYPHEN = re.compile(r"(?<=\w)[\x00-\x08\x0c\x0e-\x1f\x7f-\x9f](?=\s*\w)")
_SOFT_HYPHEN_BREAK = re.compile(r"(?<=\w)­[ \t]*\n[ \t]*(?=\w)")
_SPACES_2 = re.compile(r"[ \t]{2,}")
_TRAIL = re.compile(r"[ \t]+\n")
_NEWLINES_3 = re.compile(r"\n{3,}")
# Символы формата (категория Unicode Cf): ZWSP/ZWNJ/ZWJ, U+2060, BOM, мягкий перенос,
# метки направления (список — по unicodedata 15.1). Одна регулярка вместо вызова
# unicodedata.category на каждый символ.
_FORMAT = re.compile(
    "[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2\u180e\u200b-\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd"
    "\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001"
    "\U000e0020-\U000e007f]"
)
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️]")

FIELD_MAX = 300


def normalize_text(text: str, *, pdf: bool = False) -> str:
    """Нормализовать текст блока (правила 1–4 плана)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    # Мягкий перенос в конце строки — разрыв внутри слова: склеиваем до того,
    # как невидимые символы будут удалены и признак переноса пропадёт.
    text = _SOFT_HYPHEN_BREAK.sub("", text)
    text = text.translate(_SPACES).translate(_SYMBOL_MAP)
    text = text.replace("\x0b", "\n")  # разрыв строки внутри абзаца pptx
    if pdf:
        text = _CTRL_HYPHEN.sub("-", text)
    text = _C0_C1.sub("", text)
    text = _FORMAT.sub("", text)
    text = text.replace("\t", " ")
    text = _SPACES_2.sub(" ", text)
    text = _TRAIL.sub("\n", text)
    text = _NEWLINES_3.sub("\n\n", text)
    return text.strip(" \n")


def normalize_field(value) -> str | None:
    """Поле метаданных: нормализованная строка в одну строку, без эмодзи, ≤300 символов."""
    if value is None:
        return None
    text = normalize_text(str(value))
    text = _EMOJI.sub("", text)
    text = " ".join(text.split())
    if len(text) > FIELD_MAX:
        text = text[: FIELD_MAX - 1].rstrip() + "…"
    return text or None
