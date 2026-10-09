"""Разбор .docx без python-docx: zipfile + lxml, в порядке документа.

- абзацы и таблицы в порядке тела; w:sdt, w:customXml, w:smartTag — внутрь;
- номер пункта восстанавливается по numbering.xml (numPr в абзаце или в его стиле);
- заголовок (outlineLvl абзаца или стиля, стили «heading N» / «Заголовок N») — уровень
  блока, из уровней складывается путь раздела;
- правки рецензирования приняты: текст w:ins и w:moveTo берём, w:del, w:delText и
  w:moveFrom — нет; комментарии и коды полей выбрасываем, результат поля оставляем;
- таблица → серия чанков с шапкой (gridSpan и vMerge учитываются), название — подпись
  «Таблица N…» над ней или ближайший заголовок;
- рамки (w:txbxContent) — после абзаца, без дубля из mc:Fallback; сноски — после абзаца;
- знаки шрифта Symbol (w:sym) → Unicode; колонтитулы — один раз, в начале.

Исключения отдаются вызывающему коду (src/parsers/docx.py): кроме MemoryError,
он откатывается на docx2txt.
"""

import posixpath
import re
import zipfile
from typing import BinaryIO

from lxml import etree

from ._tables import Budget, table_blocks
from .doc_model import Block
from .ooxml_limits import MAX_XML_BYTES

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


def q(tag: str) -> str:
    return "{%s}%s" % (W, tag)


VAL = q("val")
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
_CAPTION = re.compile(r"^(таблица|табл\.)\s*(№\s*)?[\dIVXА-Я]", re.I)

_EN = "abcdefghijklmnopqrstuvwxyz"
_RU = "абвгдежзиклмнопрстуфхцчшщэюя"  # как Word: без ё, й, ъ, ы, ь
_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]  # fmt: skip


def roman(n: int) -> str:
    out = ""
    for v, s in _ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def letters(n: int, abc: str = _EN) -> str:
    if n <= 0:
        return str(n)
    return abc[(n - 1) % len(abc)] * ((n - 1) // len(abc) + 1)


def fmt_num(n: int, fmt: str | None) -> str:
    match fmt:
        case "decimalZero":
            return f"{n:02d}"
        case "lowerLetter":
            return letters(n)
        case "upperLetter":
            return letters(n).upper()
        case "lowerRoman":
            return roman(n)
        case "upperRoman":
            return roman(n).upper()
        case "russianLower":
            return letters(n, _RU)
        case "russianUpper":
            return letters(n, _RU).upper()
        case "none":
            return ""
    return str(n)  # decimal и вся экзотика (ordinal, cardinalText…) — цифрой


def _ival(el, default=None):
    if el is None:
        return default
    try:
        return int(el.get(VAL))
    except (TypeError, ValueError):
        return default


def _ival_attr(el, name, default=None):
    try:
        return int(el.get(q(name)))
    except (TypeError, ValueError):
        return default


class _Lvl:
    __slots__ = ("start", "fmt", "text", "restart", "lgl", "pstyle")

    def __init__(self, el):
        self.start = _ival(el.find(q("start")), 1)
        f = el.find(q("numFmt"))
        self.fmt = f.get(VAL) if f is not None else "decimal"
        t = el.find(q("lvlText"))
        self.text = t.get(VAL) if t is not None else None
        self.restart = _ival(el.find(q("lvlRestart")))
        lg = el.find(q("isLgl"))
        self.lgl = lg is not None and lg.get(VAL) not in ("0", "false")
        ps = el.find(q("pStyle"))
        self.pstyle = ps.get(VAL) if ps is not None else None


class Numbering:
    """Счётчики автонумерации Word в порядке обхода документа."""

    def __init__(self, numbering, styles):
        self.abstract: dict[int, dict[int, _Lvl]] = {}
        self.abs_link: dict[int, str] = {}
        self.nums: dict[int, tuple[int, dict[int, int], dict[int, _Lvl]]] = {}
        self.style_num: dict[str, tuple[int | None, int | None]] = {}
        self.counters: dict[tuple, dict[int, int]] = {}
        if numbering is not None:
            for a in numbering.iterchildren(q("abstractNum")):
                aid = _ival_attr(a, "abstractNumId")
                self.abstract[aid] = {
                    _ival_attr(lv, "ilvl", 0): _Lvl(lv)
                    for lv in a.iterchildren(q("lvl"))
                }
                link = a.find(q("numStyleLink"))
                if link is not None:
                    self.abs_link[aid] = link.get(VAL)
            for n in numbering.iterchildren(q("num")):
                starts, lvls = {}, {}
                for o in n.iterchildren(q("lvlOverride")):
                    il = _ival_attr(o, "ilvl", 0)
                    if (so := o.find(q("startOverride"))) is not None:
                        starts[il] = _ival(so, 1)
                    if (lv := o.find(q("lvl"))) is not None:
                        lvls[il] = _Lvl(lv)
                self.nums[_ival_attr(n, "numId")] = (
                    _ival(n.find(q("abstractNumId"))),
                    starts,
                    lvls,
                )
        if styles is not None:
            raw, based = {}, {}
            for s in styles.iterchildren(q("style")):
                sid = s.get(q("styleId"))
                if (np_ := s.find(q("pPr") + "/" + q("numPr"))) is not None:
                    raw[sid] = (_ival(np_.find(q("numId"))), _ival(np_.find(q("ilvl"))))
                if (b := s.find(q("basedOn"))) is not None:
                    based[sid] = b.get(VAL)
            for sid in set(raw) | set(based):
                cur, seen = sid, 0
                while cur is not None and seen < 10:
                    if cur in raw:
                        self.style_num[sid] = raw[cur]
                        break
                    cur, seen = based.get(cur), seen + 1

    def _levels(self, num_id, depth=0):
        if (n := self.nums.get(num_id)) is None:
            return None
        aid, starts, lvls = n
        levels = self.abstract.get(aid)
        link = self.abs_link.get(aid)
        if link and depth < 3:  # numStyleLink → стиль → его num → настоящий abstractNum
            sn = self.style_num.get(link)
            if (
                sn
                and sn[0]
                and sn[0] != num_id
                and (r := self._levels(sn[0], depth + 1))
            ):
                aid, levels = r[0], r[1]
        if levels is None:
            return None
        return aid, {**levels, **lvls} if lvls else levels, starts

    def label(self, ppr) -> str | None:
        num_id = ilvl = style = None
        if ppr is not None:
            if (np_ := ppr.find(q("numPr"))) is not None:
                num_id = _ival(np_.find(q("numId")))
                ilvl = _ival(np_.find(q("ilvl")))
            if (ps := ppr.find(q("pStyle"))) is not None:
                style = ps.get(VAL)
        if num_id is None and style in self.style_num:
            num_id, style_ilvl = self.style_num[style]
            ilvl = style_ilvl if ilvl is None else ilvl
        if not num_id:  # нет нумерации или numId=0 («снять нумерацию стиля»)
            return None
        if (r := self._levels(num_id)) is None:
            return None
        aid, levels, starts = r
        if ilvl is None:
            ilvl = next(
                (k for k, lv in levels.items() if style and lv.pstyle == style), 0
            )
        if (lv := levels.get(ilvl)) is None:
            return None
        # startOverride — список начинается заново: свои счётчики на numId.
        # Без него numId одного abstractNum продолжают общую нумерацию (как в Word).
        cnt = self.counters.setdefault(("num", num_id) if starts else ("abs", aid), {})
        cur = cnt.get(ilvl)
        cnt[ilvl] = starts.get(ilvl, lv.start) if cur is None else cur + 1
        for k in [k for k in cnt if k > ilvl]:
            if (lk := levels.get(k)) is not None and lk.restart == 0:
                continue
            del cnt[k]
        if lv.fmt == "bullet":
            return "-"
        if not lv.text:
            return None

        def rep(m):
            k = int(m.group(1)) - 1
            lk = levels.get(k)
            v = cnt.get(k)
            if v is None:
                v = starts.get(k, lk.start if lk else 1)
            return fmt_num(v, "decimal" if lv.lgl else (lk.fmt if lk else "decimal"))

        return re.sub(r"%([1-9])", rep, lv.text).strip() or None


class Styles:
    """Уровни заголовков и стили названия из styles.xml (с наследованием basedOn)."""

    def __init__(self, styles):
        self.level: dict[str, int] = {}
        self.title: set[str] = set()
        if styles is None:
            return
        raw, based = {}, {}
        for s in styles.iterchildren(q("style")):
            sid = s.get(q("styleId"))
            name_el = s.find(q("name"))
            name = (name_el.get(VAL) if name_el is not None else "").strip().lower()
            outline = _ival(s.find(q("pPr") + "/" + q("outlineLvl")))
            if outline is not None and outline < 9:
                raw[sid] = outline + 1
            elif m := re.fullmatch(r"(heading|заголовок)\s*(\d)", name):
                raw[sid] = int(m.group(2))
            if name in ("title", "название"):
                self.title.add(sid)
            if (b := s.find(q("basedOn"))) is not None:
                based[sid] = b.get(VAL)
        for sid in set(raw) | set(based):
            cur, seen = sid, 0
            while cur is not None and seen < 10:
                if cur in raw:
                    self.level[sid] = raw[cur]
                    break
                cur, seen = based.get(cur), seen + 1


# Шрифт Symbol хранит знаки в частной области F0xx; docx2txt их просто теряет.
_SYMBOL = {0x2D: "−", 0x2B: "+", 0x3D: "=", 0x3C: "<", 0x3E: ">", 0xA3: "≤", 0xB3: "≥",
           0xB1: "±", 0xB4: "×", 0xB8: "÷", 0xB9: "≠", 0xBB: "≈", 0xBA: "≡", 0xA5: "∞",
           0xD6: "√", 0xE5: "∑", 0xF2: "∫", 0xB7: "•", 0xD7: "·", 0xAE: "→", 0xAC: "←",
           0xDE: "⇒", 0xB0: "°", 0xA2: "′", 0xB2: "″"}  # fmt: skip
_SYMBOL.update({0x41 + i: c for i, c in enumerate("ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ")})
_SYMBOL.update({0x61 + i: c for i, c in enumerate("αβχδεφγηιϕκλμνοπθρστυϖωξψζ")})

_T, _TAB, _BR, _CR = q("t"), q("tab"), q("br"), q("cr")
_NBH, _SH, _SYM = q("noBreakHyphen"), q("softHyphen"), q("sym")
_BOX, _FN, _EN_REF = q("txbxContent"), q("footnoteReference"), q("endnoteReference")
_MT = "{%s}t" % M
_R, _VA = q("r"), q("rPr") + "/" + q("vertAlign")
# moveFrom — старое место перенесённого текста; Fallback дублирует Choice.
_SKIP = {q("pPr"), q("rPr"), q("delText"), q("instrText"), q("delInstrText"), q("tblPr"),
         "{%s}Fallback" % MC, q("commentReference"), q("moveFrom"), q("del")}  # fmt: skip


def _kids(el, tag):
    """Дочерние tag, в том числе обёрнутые в w:sdt / w:customXml (строки и ячейки таблиц)."""
    for ch in el:
        if ch.tag == tag:
            yield ch
        elif ch.tag == q("sdt"):
            if (c := ch.find(q("sdtContent"))) is not None:
                yield from _kids(c, tag)
        elif ch.tag == q("customXml"):
            yield from _kids(ch, tag)


def _sym(el) -> str:
    try:
        code = int(el.get(q("char")) or "", 16)
    except ValueError:
        return ""
    if code >= 0xF000:
        font = (el.get(q("font")) or "").lower()
        return _SYMBOL.get(code - 0xF000, "") if font == "symbol" else ""
    return chr(code)


def _run_text(el, parts: list[str], boxes: list, notes: list) -> None:
    for ch in el:
        tag = ch.tag
        if not isinstance(tag, str) or tag in _SKIP:
            continue
        if tag in (_T, _MT):
            parts.append(ch.text or "")
        elif tag == _TAB:
            parts.append(" ")
        elif tag in (_BR, _CR):
            parts.append("\n")
        elif tag == _NBH:
            parts.append("-")
        elif tag == _SH:
            continue
        elif tag == _SYM:
            parts.append(_sym(ch))
        elif tag == _BOX:
            boxes.append(ch)
        elif tag in (_FN, _EN_REF):
            notes.append(("fn" if tag == _FN else "en", ch.get(q("id"))))
        else:
            if tag == _R and parts and parts[-1][-1:].isdigit():
                # Надстрочная цифра сноски сразу после числа («14» + «¹») иначе
                # склеилась бы в другое число «141» — ставим «^».
                va = ch.find(_VA)
                t = ch.find(_T)
                if (
                    va is not None
                    and va.get(VAL) == "superscript"
                    and t is not None
                    and (t.text or "")[:1].isdigit()
                ):
                    parts.append("^")
            _run_text(ch, parts, boxes, notes)


_WS = re.compile(r"[ \t   ]+")


def _clean(s: str) -> str:
    s = _WS.sub(" ", s)
    return "\n".join(x.strip() for x in s.split("\n") if x.strip())


class _Doc:
    def __init__(self, z: zipfile.ZipFile, budget: Budget):
        self.z = z
        self.budget = budget
        # Основная часть и её стили — по связям пакета: генераторы кладут её и
        # под другим именем (word/document2.xml). Нет связи или её цели в архиве
        # (OPC сравнивает имена без учёта регистра) — имена Word.
        names = set(z.namelist())
        main = self._rels("").get("officeDocument")
        self.main = main if main in names else "word/document.xml"
        rels = self._rels(self.main)

        def part(kind: str, word: str) -> str:
            return rels[kind] if rels.get(kind) in names else word

        styles = self._xml(part("styles", "word/styles.xml"))
        self.num = Numbering(self._xml(part("numbering", "word/numbering.xml")), styles)
        self.styles = Styles(styles)
        self.notes = {
            "fn": self._notes(part("footnotes", "word/footnotes.xml"), "footnote"),
            "en": self._notes(part("endnotes", "word/endnotes.xml"), "endnote"),
        }
        self.titles: list[str] = []  # абзац стиля «Title», первый заголовок 1-го уровня
        self.tables = 0
        self.last_heading: str | None = None
        self.prev_text: str | None = None  # для подписи «Таблица N…» над таблицей

    def _xml(self, name: str):
        try:
            info = self.z.getinfo(name)
        except KeyError:
            return None
        if info.file_size > MAX_XML_BYTES:
            raise ValueError(f"{name}: {info.file_size} Б — слишком большой XML")
        return etree.fromstring(self.z.read(name), _PARSER)

    def _rels(self, part: str) -> dict[str, str]:
        """Связи части (part="" — пакета): тип связи → имя цели в архиве.

        Битый или отсутствующий .rels — пустой словарь: остаются имена Word.
        """
        folder, base = posixpath.split(part)
        try:
            root = self._xml(posixpath.join(folder, "_rels", base + ".rels"))
        except etree.XMLSyntaxError:
            return {}
        out: dict[str, str] = {}
        for rel in root.iter(_REL) if root is not None else ():
            target = rel.get("Target") or ""
            if not target or rel.get("TargetMode") == "External":
                continue
            name = posixpath.normpath(
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.join(folder, target)
            )
            out.setdefault((rel.get("Type") or "").rsplit("/", 1)[-1], name)
        return out

    def _notes(self, name: str, tag: str) -> dict[str, str]:
        out = {}
        if (root := self._xml(name)) is None:
            return out
        for n in root.iterchildren(q(tag)):
            if n.get(q("type")) in (None, "normal"):
                parts = []
                for p in n.iter(q("p")):
                    pp: list[str] = []
                    _run_text(p, pp, [], [])
                    parts.append("".join(pp))
                if txt := _clean(" ".join(parts)):
                    out[n.get(q("id"))] = txt
        return out

    def _heading(self, ppr) -> int | None:
        if ppr is None:
            return None
        outline = _ival(ppr.find(q("outlineLvl")))
        if outline is not None:
            return outline + 1 if outline < 9 else None
        ps = ppr.find(q("pStyle"))
        return self.styles.level.get(ps.get(VAL)) if ps is not None else None

    def para(self, p, out: list[Block], in_cell: bool) -> None:
        parts: list[str] = []
        boxes: list = []
        notes: list = []
        _run_text(p, parts, boxes, notes)
        ppr = p.find(q("pPr"))
        label = self.num.label(ppr)
        if txt := _clean("".join(parts)):
            text = f"{label} {txt}" if label else txt
            level = None if in_cell else self._heading(ppr)
            if not in_cell:
                ps = ppr.find(q("pStyle")) if ppr is not None else None
                if (
                    ps is not None
                    and ps.get(VAL) in self.styles.title
                    or (level == 1 and not self.titles)
                ):
                    self.titles.append(txt)
                if level:
                    self.last_heading = text
                self.prev_text = text
            out.append(Block(text, heading=level))
        for b in boxes:
            self.body(b, out, in_cell)
        for kind, nid in notes:
            if t := self.notes[kind].get(nid):
                out.append(Block(f"[сноска {nid}] {t}", kind="footnote"))

    def cell_text(self, tc) -> str:
        sub: list[Block] = []
        self.body(tc, sub, in_cell=True)
        return " ".join(b.text.replace("\n", " ") for b in sub).strip()

    def table(self, tbl, out: list[Block], in_cell: bool) -> None:
        rows = []  # [(строка-шапка?, [(столбец, ширина, текст)])]
        merged_down = []  # по строкам: столбцы, где начинается вертикальное объединение
        above: dict[int, str] = {}
        for tr in _kids(tbl, q("tr")):
            trpr = tr.find(q("trPr"))
            is_hdr = trpr is not None and trpr.find(q("tblHeader")) is not None
            col = _ival(trpr.find(q("gridBefore")), 0) if trpr is not None else 0
            cells, starts = [], set()
            for tc in _kids(tr, q("tc")):
                tcpr = tc.find(q("tcPr"))
                span = (
                    max(_ival(tcpr.find(q("gridSpan")), 1), 1)
                    if tcpr is not None
                    else 1
                )
                vm = tcpr.find(q("vMerge")) if tcpr is not None else None
                if vm is not None and vm.get(VAL) != "restart":
                    text = above.get(col, "")  # продолжение объединения — текст сверху
                else:
                    text = self.cell_text(tc)
                    above[col] = text
                    if vm is not None:
                        starts.add(col)
                cells.append((col, span, text))
                col += span
            rows.append((is_hdr, cells))
            merged_down.append(starts)
        if in_cell:  # вложенная таблица — строками «a | b» через «; » внутри ячейки
            lines = [" | ".join(t for _, _, t in cells if t) for _, cells in rows]
            out.append(Block("; ".join(x for x in lines if x)))
            return
        self.tables += 1
        header, body = _header_and_body(rows, merged_down)
        caption = (
            self.prev_text
            if self.prev_text and _CAPTION.match(self.prev_text)
            else None
        )
        out += table_blocks(
            header,
            body,
            self.budget,
            title=caption or self.last_heading,
            table_id=f"t{self.tables}",
        )
        self.prev_text = None

    def body(self, container, out: list[Block], in_cell: bool = False) -> None:
        for el in container:
            tag = el.tag
            if tag == q("p"):
                self.para(el, out, in_cell)
            elif tag == q("tbl"):
                self.table(el, out, in_cell)
            elif tag == q("sdt"):
                if (c := el.find(q("sdtContent"))) is not None:
                    self.body(c, out, in_cell)
            elif tag in (q("customXml"), q("ins"), q("smartTag"), q("moveTo")):
                self.body(el, out, in_cell)
            elif tag == "{%s}AlternateContent" % MC:
                if (ch := el.find("{%s}Choice" % MC)) is not None:
                    self.body(ch, out, in_cell)

    def headers(self) -> list[Block]:
        """Колонтитулы — один раз, без номеров страниц."""
        seen, out = set(), []
        for name in sorted(self.z.namelist()):
            if (
                re.fullmatch(r"word/(header|footer)\d*\.xml", name)
                and (root := self._xml(name)) is not None
            ):
                sub: list[Block] = []
                self.body(root, sub, in_cell=True)
                for b in sub:
                    t = b.text
                    if t not in seen and not re.fullmatch(
                        r"[\d\s\-–—/]*|(стр\.?|страница)?\s*\d+(\s*из\s*\d+)?", t, re.I
                    ):
                        seen.add(t)
                        out.append(Block(t))
        return out


def _colnums(cells) -> bool:
    """Строка номеров столбцов «1 | 2 | 3» — шум."""
    vals = [t for _, _, t in cells if t]
    return len(vals) >= 2 and all(re.fullmatch(r"\d{1,2}[а-я]?", v) for v in vals)


def _header_and_body(
    rows, merged_down
) -> tuple[list[str], list[tuple[list[str], int]]]:
    """Шапка по столбцам сетки («верх / низ») и строки тела с номерами строк таблицы.

    Шапка — строки с флагом tblHeader; без флага — первая строка, а если в ней есть
    объединение (gridSpan или начало vMerge), то и следующие (до трёх строк).
    """
    numbered = [(i + 1, cells) for i, (_, cells) in enumerate(rows)]
    plain = [
        ([t for _, _, t in cells], n) for n, cells in numbered if not _colnums(cells)
    ]
    ncols = max((c + s for _, cells in rows for c, s, _ in cells), default=0)
    n_hdr = 0
    while n_hdr < len(rows) and rows[n_hdr][0]:
        n_hdr += 1
    if not n_hdr and len(rows) >= 2:
        n_hdr = 1
        while (
            n_hdr < min(3, len(rows) - 1)
            and rows[n_hdr - 1][1]
            and any(
                s > 1 or c in merged_down[n_hdr - 1] for c, s, _ in rows[n_hdr - 1][1]
            )
        ):
            n_hdr += 1
    hdr_rows = [cells for _, cells in rows[:n_hdr] if not _colnums(cells)]
    body = [
        ([t for _, _, t in cells], n)
        for n, cells in numbered[n_hdr:]
        if not _colnums(cells)
    ]
    if ncols < 2 or not body or not hdr_rows:
        return [], plain
    heads: list[list[str]] = [[] for _ in range(ncols)]
    for cells in hdr_rows:
        for c, s, t in cells:
            for k in range(c, min(c + s, ncols)):
                if t and (not heads[k] or heads[k][-1] != t):
                    heads[k].append(t)
    header = [" / ".join(h) for h in heads]
    if sum(1 for h in header if h) < 2:
        return [], plain
    return header, body


def parse_docx(file: BinaryIO, budget: Budget) -> tuple[list[Block], list[str]]:
    """Блоки документа и кандидаты в название (абзац «Title», первый заголовок 1-го уровня)."""
    with zipfile.ZipFile(file) as z:
        d = _Doc(z, budget)
        doc = d._xml(d.main)
        body = doc.find(q("body")) if doc is not None else None
        if body is None:
            raise ValueError(f"в архиве нет {d.main} с w:body")
        out = d.headers()
        d.body(body, out)
        return out, d.titles
