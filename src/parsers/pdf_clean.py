"""Очистка текста PDF до нарезки: колонтитулы, номера страниц, переносы, заголовки.

- Колонтитул — строка из верхних или нижних пяти непустых строк страницы, которая (числа
  заменены на #) повторяется в той же зоне на половине страниц; чётные и нечётные
  считаются отдельно (зеркальные колонтитулы), в документе до трёх страниц — на всех.
  Первое вхождение остаётся: в колонтитуле бывают банк и период.
- Номер страницы — число в той же зоне с постоянной разницей «число − индекс страницы».
- Перенос в конце строки склеивается по словарю самого документа: слитная форма в нём
  чаще — склеить, через дефис чаще — оставить; левая часть начинает другие составные
  слова («стресс-тест») — оставить; частицы (-то, -либо), «из-за», прописная справа
  («Северо-Западный») — дефис остаётся. Ложных склеек — 66 на 93 800 (прототип lex2).
- Заголовок — строка оглавления PDF (закладки) на своей странице, а без оглавления —
  короткая строка «Глава 2.», «Раздел II», «Статья 5.», «Приложение 1».
Порядок чтения не меняется: get_text() без сортировки верен для одной и двух колонок.
"""

import math
import re
from collections import Counter

from .doc_model import Block

L = "A-Za-zА-Яа-яЁё"
WCH = f"[{L}]"

# ---------- колонтитулы и номера страниц ----------

ZONE_LINES = 5
_PAGE_NUM = re.compile(
    r"(?:(?:стр\.?|страница|page)\s*)?[-–—]?\s*(\d{1,4})\s*[-–—]?(?:\s*(?:из|/|of)\s*\d{1,4})?",
    re.I,
)
_NUMBER = re.compile(rf"(?<![{L}\d])\d+(?![{L}\d])")
_UNITS = re.compile(
    r"\b(тыс|млн|млрд)\.?\s*(руб|долл|евро)", re.I
)  # «(в млн рублей)» — не колонтитул


def _key(line: str) -> str:
    return re.sub(r"\s+", " ", _NUMBER.sub("#", line.lower())).strip()


def strip_furniture(pages: list[str]) -> list[str]:
    """Страницы без повторяющихся колонтитулов (кроме первого вхождения) и номеров."""
    n = len(pages)
    lines = [p.split("\n") for p in pages]
    zones = []
    for page in lines:
        idx = [i for i, x in enumerate(page) if x.strip()]
        top = idx[:ZONE_LINES]
        zones.append(
            [(i, "t") for i in top]
            + [(i, "b") for i in idx[-ZONE_LINES:] if i not in top]
        )
    need = n if n <= 3 else max(2, math.ceil(0.5 * n))
    total, by_parity = Counter(), [Counter(), Counter()]
    for pi, zone in enumerate(zones):
        keys = {(z, _key(lines[pi][i])) for i, z in zone}
        total.update(keys)
        by_parity[pi % 2].update(keys)
    halves = [math.ceil(n / 2), n // 2]
    repeated = {
        key
        for key, c in total.items()
        if re.search("[a-zа-яё]", key[1])
        and not _UNITS.search(key[1])
        and c >= 2
        and (
            c >= need
            or (
                n >= 4
                and any(
                    by_parity[j][key] >= max(2, math.ceil(0.5 * halves[j]))
                    for j in (0, 1)
                )
            )
        )
    }
    offsets, page_num = Counter(), {}
    for pi, zone in enumerate(zones):
        seen = set()
        for i, _ in zone:
            if m := _PAGE_NUM.fullmatch(lines[pi][i].strip()):
                off = int(m.group(1)) - pi
                page_num[(pi, i)] = off
                if off not in seen:
                    seen.add(off)
                    offsets[off] += 1
    good = {o for o, c in offsets.items() if c >= max(2, math.ceil(0.5 * n))}
    drop, first = set(), set()
    for pi, zone in enumerate(zones):
        for i, z in zone:
            key = (z, _key(lines[pi][i]))
            if key in repeated:
                if key not in first:
                    first.add(key)
                    continue
                drop.add((pi, i))
            elif page_num.get((pi, i), "нет") in good:
                drop.add((pi, i))
    return [
        "\n".join(x for i, x in enumerate(page) if (pi, i) not in drop)
        for pi, page in enumerate(lines)
    ]


# ---------- переносы ----------

_HY = "‐‑-"
# Правая часть — в lookahead: слово через три строки узкой колонки («кредитно-\nобесце-\n
# ненные») склеивается за два шага. Между частями допустима одна пустая строка.
_BREAK = re.compile(rf"({WCH}+)([{_HY}])[^\S\n]*\n(?:[^\S\n]*\n)?[^\S\n]*(?=({WCH}+))")
_BREAK_FULL = re.compile(rf"{WCH}+[{_HY}][^\S\n]*\n(?:[^\S\n]*\n)?[^\S\n]*{WCH}+")
_WORD = re.compile(rf"{WCH}+(?:-{WCH}+)*")
_KEEP_RIGHT = {"то", "либо", "нибудь", "таки"}
_KEEP_PAIRS = {
    ("из", "за"),
    ("из", "под"),
    ("кое", "что"),
    ("кое", "как"),
    ("кое", "где"),
}


def doc_lexicon(text: str) -> Counter:
    """Слова документа (без разорванных переносом) и начала составных слов."""
    lex = Counter(w.lower() for w in _WORD.findall(_BREAK_FULL.sub(" ", text)))
    lefts = Counter()
    for w, c in lex.items():
        if "-" in w:
            lefts["\0left:" + w.split("-", 1)[0]] += c
    lex.update(lefts)
    return lex


def _join(left: str, right: str, lex: Counter) -> bool:
    ll, rl = left.lower(), right.lower()
    joined, hyphened = lex.get(ll + rl, 0), lex.get(ll + "-" + rl, 0)
    if hyphened > joined:
        return False
    if joined:
        return True
    if len(ll) >= 3 and lex.get("\0left:" + ll, 0):
        return False  # «стресс-тест», «кредитно-обесцененный» в другой форме
    if rl in _KEEP_RIGHT or (ll, rl) in _KEEP_PAIRS:
        return False
    if ll == "по" and re.search("(ому|ему|ски|цки|ьи)$", rl):
        return False
    if ll in ("во", "в") and re.match("(перв|втор|трет|четв)", rl):
        return False
    return not (right[0].isupper() and not (left.isupper() and right.isupper()))


def dehyphenate(text: str, lex: Counter) -> str:
    def rep(m: re.Match) -> str:
        left, _, right = m.groups()
        return left if _join(left, right, lex) else left + "-"

    return _BREAK.sub(rep, text)


# ---------- заголовки ----------

# Ключевое слово и номер; у «Статьи» — номер с точкой («Статья 5.»), чтобы ссылка
# «Статья 5 Федерального закона…» в начале строки не стала заголовком.
_HEADING = re.compile(
    # Ключевое слово — в любом регистре, а название после номера — с прописной.
    r"^(?i:(?P<kw>часть|раздел|глава|статья|приложение))\s+(№\s*)?(?P<num>[\dIVXLC]+|[А-Я])"
    r"(?P<tail>\.|\s*$|\s+(?=[А-ЯЁ«\"(])|\s+к\s)"
)
_LEVEL = {"часть": 1, "раздел": 1, "приложение": 1, "глава": 2, "статья": 3}
HEADING_MAX_CHARS = 150


def heading_level(line: str) -> int | None:
    """Уровень заголовка по нумерации или None."""
    line = line.strip()
    if not line or len(line) > HEADING_MAX_CHARS or line.endswith((",", ";", "-")):
        return None
    m = _HEADING.match(line)
    if m is None:
        return None
    kw = m.group("kw").lower()
    if kw == "статья" and m.group("tail") != ".":
        return None
    return _LEVEL[kw]


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower().replace("ё", "е")).strip()


def split_headings(
    block: Block, toc: list[tuple[int, str]], numbering: bool
) -> list[Block]:
    """Страница → блоки текста и заголовков.

    toc — (уровень, название) закладок этой страницы; numbering — искать заголовки по
    нумерации (у документа без оглавления).
    """
    lines = block.text.split("\n")
    marks: dict[int, int] = {}
    for level, title in toc:
        key = _norm(title)[:60]
        for i, line in enumerate(lines):
            nl = _norm(line)
            if (
                i not in marks
                and nl
                and (nl.startswith(key) or (len(nl) >= 8 and key.startswith(nl)))
            ):
                marks[i] = max(1, min(level, 9))
                break
    if numbering:
        for i, line in enumerate(lines):
            if level := heading_level(line):
                marks[i] = level
    if not marks:
        return [block]
    out: list[Block] = []
    buf: list[str] = []
    i = 0
    while i < len(lines):
        if i not in marks:
            buf.append(lines[i])
            i += 1
            continue
        if buf:
            out.append(Block("\n".join(buf), page=block.page))
            buf = []
        level, text = marks[i], lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # «ГЛАВА 1» и название отдельной строкой ниже — один заголовок.
        if (
            nxt
            and i + 1 not in marks
            and re.fullmatch(r"\w+\s+(№\s*)?[\dIVXLCА-Я]+\.?", text)
            and len(nxt) <= HEADING_MAX_CHARS
            and not nxt[:1].isdigit()
        ):
            text = f"{text} {nxt}"
            i += 1
        out.append(Block(text, page=block.page, heading=level))
        i += 1
    if buf:
        out.append(Block("\n".join(buf), page=block.page))
    return [b for b in out if b.text.strip()]


def clean_pages(blocks: list[Block], toc: list[tuple[int, str, int]]) -> list[Block]:
    """Страницы PDF после нормализации: колонтитулы, переносы, заголовки."""
    pages = strip_furniture([b.text for b in blocks])
    lex = doc_lexicon("\n".join(pages))
    by_page: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in toc:
        by_page.setdefault(page, []).append((level, title))
    out: list[Block] = []
    for b, text in zip(blocks, pages):
        b.text = dehyphenate(text, lex).strip("\n")
        if b.text.strip():
            out += split_headings(b, by_page.get(b.page or 0, []), numbering=not toc)
    return out
