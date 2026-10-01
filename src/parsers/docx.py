import os
import shutil
import subprocess
import tempfile
from typing import BinaryIO

from docx2txt import process

from src.utils import get_logger
from src.utils.r7_office import convert_via_r7, find_x2t

from ._tables import Budget
from .base import Parser, parser
from .doc_model import Block, ParsedDoc
from .docx_ooxml import parse_docx
from .metadata import ooxml_properties

lg = get_logger(__name__)

BUDGET_TOKENS = 450  # чанк таблицы; «Документ: <имя>» сверху уложит его в 500


@parser("docx")
class DocxParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Структура документа по OOXML (src/parsers/docx_ooxml.py); сбой — docx2txt."""
        meta, titles = ooxml_properties(file, "docx")
        try:
            blocks, found = parse_docx(
                file, Budget(BUDGET_TOKENS, self._config.ov_model.count_tokens)
            )
        except Exception as exc:
            lg.warning(
                "docx: разбор по OOXML не удался (%s: %s) — текст через docx2txt",
                type(exc).__name__,
                exc,
            )
            file.seek(0)
            return ParsedDoc([Block(process(file))], meta, titles)
        return ParsedDoc(blocks, meta, titles + found)


def _find_doc_converter() -> tuple[str, list[str]] | None:
    """Найти доступный конвертер .doc → текст.

    Целевая ОС продукта — Linux (SberOS), где `textutil` не существует.
    Ни один из конвертеров не является обязательной зависимостью: если ничего
    не нашлось, файл пропускается с предупреждением.

    Порядок — от лёгкого к тяжёлому. На АРМ SberOS нет ни catdoc, ни
    antiword, ни LibreOffice, зато везде стоит Р7-Офис (пакет sber-r7-office)
    — его встроенный конвертер x2t закрывает .doc на типовой машине.
    """
    if shutil.which("textutil"):  # macOS
        return "textutil", ["textutil", "-convert", "txt", "{src}", "-output", "{dst}"]
    if shutil.which("catdoc"):  # Linux, самый лёгкий вариант
        return "catdoc", ["catdoc", "-w", "{src}"]
    if shutil.which("antiword"):
        return "antiword", ["antiword", "{src}"]
    if shutil.which("soffice"):  # LibreOffice, тяжёлый, но часто есть
        return "soffice", [
            "soffice",
            "--headless",
            "--convert-to",
            "txt:Text",
            "--outdir",
            "{outdir}",
            "{src}",
        ]
    if find_x2t():  # Р7-Офис: стандартный офисный пакет АРМ Сбера
        return "r7-x2t", []
    return None


@parser("doc")
class DocParser(Parser):
    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        # Р7-Офис переводит .doc в .docx — тот же разбор со структурой (заголовки,
        # нумерация, таблицы). Нет Р7 или не справился — конвертеры в голый текст.
        if find_x2t():
            with tempfile.TemporaryDirectory() as tmp_dir:
                src = os.path.join(tmp_dir, "doc.doc")
                dst = os.path.join(tmp_dir, "doc.docx")
                with open(src, "wb") as out:
                    shutil.copyfileobj(file, out)
                if convert_via_r7(src, dst):
                    with open(dst, "rb") as converted:
                        return DocxParser().parse_io(converted)
            file.seek(0)
        converter = _find_doc_converter()
        if converter is None:
            lg.warning(
                "не найден конвертер для .doc (нужен один из: textutil, catdoc, "
                "antiword, soffice, Р7-Офис) — файл пропущен"
            )
            return ParsedDoc([])

        name, argv = converter
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = os.path.join(tmp_dir, "temp_doc.doc")
            dst = os.path.join(tmp_dir, "temp_doc.txt")
            with open(src, "wb") as tmp_file:
                tmp_file.write(file.read())
            try:
                txt = _convert_doc(name, argv, src, dst, tmp_dir)
            except Exception as exc:
                # Битый или нестандартный .doc не должен ронять индексацию.
                lg.warning("не удалось разобрать .doc через %s: %s", name, exc)
                return ParsedDoc([])
        return ParsedDoc([Block(txt)])


def _convert_doc(name: str, argv: list[str], src: str, dst: str, outdir: str) -> str:
    if name == "r7-x2t":
        # У x2t свои правила вызова (LD_LIBRARY_PATH, рабочий каталог,
        # непредсказуемый код возврата) — его ведёт convert_via_r7, а не
        # шаблон argv с {src}/{dst}.
        if not convert_via_r7(src, dst):
            raise RuntimeError("Р7-Офис не смог сконвертировать файл")
        with open(dst, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    cmd = [a.format(src=src, dst=dst, outdir=outdir) for a in argv]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=120,
    )
    # catdoc/antiword печатают текст в stdout, textutil/soffice — в файл.
    if name in {"catdoc", "antiword"}:
        return result.stdout.decode("utf-8", errors="replace")
    out_path = (
        dst
        if os.path.exists(dst)
        else os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".txt")
    )
    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()
