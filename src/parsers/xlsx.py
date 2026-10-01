import os
import tempfile
from collections import Counter
from collections.abc import Generator
from pathlib import Path
from typing import Any, BinaryIO

import jinja2
import openpyxl

from src.utils import get_logger
from src.utils.r7_office import convert_via_r7

from .base import Chunk, Parser, parser
from .doc_model import Block, ParsedDoc
from .metadata import ooxml_properties

logger = get_logger(__name__)


@parser("xlsx", "xls")
class XlsxParser(Parser):
    """Excel parser that yields each row as a text chunk containing key-value pairs

    Excel parser with intelligent sheet filtering heuristics.

    Heuristics used to skip sheets:
    1. Metadata/Config sheets by name (config, settings, readme, etc.)
    2. Sheets with too many rows (configurable, default: 5000)
    3. Sheets with low text content ratio (configurable, default: 0.1)
    4. Sheets with too many formula errors (#REF!, #VALUE!, etc.)
    5. Empty or nearly empty sheets (< 2 rows)
    6. Sheets with no meaningful content

    Configuration:
    - max_rows_per_sheet: Maximum rows to process per sheet
    - min_text_ratio: Minimum ratio of text cells to total cells
    - sample_size_for_analysis: Number of rows to sample for analysis
    """

    def __init__(self):
        super().__init__()
        # Initialize Jinja2 template
        template_dir = Path(__file__).parent / "templates"
        self.jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.row_template = self.jinja_env.get_template("excel_row.j2")

        # Configuration for sheet filtering
        self.max_rows_per_sheet = 5000
        self.min_text_ratio = 0.1
        self.sample_size_for_analysis = 50

    def parse_io(self, file: BinaryIO) -> ParsedDoc:
        """Карточки строк — готовые чанки (`whole`). Лист, шапка, даты — шаг 3."""
        meta, titles = ooxml_properties(file, "xlsx")
        blocks = [
            Block(c["text"], kind="table_row", whole=True)
            for c in self._row_chunks_io(file)
        ]
        return ParsedDoc(blocks, meta, titles, title_from_text=False)

    def _row_chunks_io(self, file: BinaryIO) -> Generator[Chunk, Any, None]:
        # Create a temporary file since openpyxl needs a file path
        # Легаси .xls узнаём по магии OLE2-контейнера, а не по расширению:
        # парсер выбирается по расширению, но в потоковом API (вложения почты)
        # имя до нас не доходит.
        head = file.read(8)
        file.seek(0)
        legacy = head.startswith(b"\xd0\xcf\x11\xe0")

        with tempfile.NamedTemporaryFile(
            suffix=".xls" if legacy else ".xlsx", delete=False
        ) as tmp_file:
            tmp_file.write(file.read())
            tmp_file_path = tmp_file.name

        try:
            if legacy:
                # openpyxl читает только OOXML: бинарный BIFF конвертируем
                # штатным конвертером Р7-Офиса. Нет его или не справился —
                # честный пропуск с предупреждением (0 чанков = permanent),
                # как у .doc без catdoc.
                converted_path = tmp_file_path + ".xlsx"
                if not convert_via_r7(tmp_file_path, converted_path):
                    logger.warning(
                        "легаси .xls не конвертирован (Р7-Офис не найден или "
                        "не справился с файлом) — файл пропущен"
                    )
                    return
                yield from self._process_excel_file(converted_path)
            else:
                yield from self._process_excel_file(tmp_file_path)
        finally:
            for leftover in (tmp_file_path, tmp_file_path + ".xlsx"):
                if os.path.exists(leftover):
                    os.remove(leftover)

    def _process_excel_file(self, file_path: str) -> Generator[Chunk, Any, None]:
        """Process Excel file and yield chunks for each row"""
        # Get sheet names without loading the entire file
        wb = openpyxl.load_workbook(file_path, read_only=True)
        sheet_names = wb.sheetnames
        wb.close()

        chunk_idx = 0

        for sheet_name in sheet_names:
            # Skip sheets that look like metadata/config by name
            if self._is_likely_metadata_sheet(sheet_name):
                logger.info(
                    f"Skipping sheet '{sheet_name}': Likely metadata/config sheet"
                )
                continue

            # Skip sheets that don't look like they contain data
            should_skip, reason = self._should_skip_sheet(
                file_path,
                sheet_name,
                max_rows=self.max_rows_per_sheet,
                min_text_ratio=self.min_text_ratio,
                sample_size=self.sample_size_for_analysis,
            )
            if should_skip:
                logger.info(f"Skipping sheet '{sheet_name}': {reason}")
                continue

            # Detect headers for this sheet
            header_analysis = self._detect_headers_smart(file_path, sheet_name)
            header_row_index = header_analysis.get("header_row_index")

            # Ensure header_row_index is int or None
            if header_row_index is not None and not isinstance(header_row_index, int):
                header_row_index = None

            # Open workbook for this sheet
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb[sheet_name]

            headers = None
            data_row_index = 0

            for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
                # Extract headers if we found a header row
                if header_row_index is not None and row_idx == header_row_index:
                    headers = []
                    for i, cell in enumerate(row):
                        if cell is not None:
                            headers.append(str(cell).strip())
                        else:
                            headers.append(f"Column_{i + 1}")
                    continue

                # Skip rows before data starts
                if header_row_index is not None and row_idx <= header_row_index:
                    continue

                # Skip completely empty rows
                if not row or all(
                    cell is None or str(cell).strip() == "" for cell in row
                ):
                    continue

                # Create column mapping
                if headers is None:
                    headers = [f"Column_{i + 1}" for i in range(len(row))]

                # Ensure we have enough headers for all columns
                while len(headers) < len(row):
                    headers.append(f"Column_{len(headers) + 1}")

                # Create row text as key-value pairs
                row_text = self._format_row_as_text(
                    sheet_name, data_row_index, headers, row
                )

                yield Chunk(idx=chunk_idx, text=row_text)
                chunk_idx += 1
                data_row_index += 1
                if row_idx > self._config.excel_config.max_rows:
                    logger.warning(
                        f"Exceeded max_rows limit ({self._config.excel_config.max_rows}) in sheet '{sheet_name}'. Stopping further processing."
                    )
                    break

            wb.close()

    def _format_row_as_text(
        self, sheet_name: str, row_index: int, headers: list[str], row: tuple
    ) -> str:
        """Format a row as human-readable text using Jinja2 template"""
        # Create list of (column_name, cell_value) pairs
        data_pairs = []
        for i, cell_value in enumerate(row):
            column_name = headers[i] if i < len(headers) else f"Column_{i + 1}"
            if cell_value is not None:
                data_pairs.append((column_name, str(cell_value)))
            else:
                data_pairs.append((column_name, ""))

        # Render template
        return self.row_template.render(
            sheet_name=sheet_name, row_index=row_index, data_pairs=data_pairs
        )

    def _detect_headers_smart(self, file_path: str, sheet_name: str):
        """Detect headers with confidence scoring"""
        analysis = self._analyze_header_patterns(file_path, sheet_name)

        result = {
            "has_headers": analysis["header_row"] is not None,
            "header_row_index": analysis["header_row"],
            "confidence": analysis["confidence"],
        }

        return result

    def _analyze_header_patterns(
        self, file_path: str, sheet_name: str, sample_rows: int = 15
    ):
        """Analyze first few rows to detect header patterns"""
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb[sheet_name]

        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i > sample_rows:
                break
            rows.append(row)

        wb.close()

        if not rows:
            return {"header_row": None, "confidence": 0}

        row_scores = []

        for i, row in enumerate(rows):
            row_score = self._analyze_row_as_header(
                row, rows[i + 1 : i + 5] if i + 1 < len(rows) else []
            )
            row_scores.append({"row_index": i, "header_score": row_score["score"]})

        # Find best header candidate
        best_header_row = None
        best_confidence = 0.0

        if row_scores:
            for row_info in row_scores:
                if row_info["header_score"] > best_confidence:
                    best_confidence = row_info["header_score"]
                    best_header_row = row_info["row_index"]

        analysis = {
            "total_rows": len(rows),
            "row_analysis": row_scores,
            "header_row": best_header_row if best_confidence > 0.3 else None,
            "confidence": best_confidence,
        }

        return analysis

    def _analyze_row_as_header(self, row: tuple, next_rows: list[tuple]):
        """Score how likely a row is to be a header based on structural heuristics"""
        score = 0.0

        if not row or all(cell is None for cell in row):
            return {"score": 0.0}

        non_null_cells = [cell for cell in row if cell is not None]

        if not non_null_cells:
            return {"score": 0.0}

        # 1. Text vs Numbers heuristic (headers are usually text)
        text_cells = sum(1 for cell in non_null_cells if isinstance(cell, str))
        number_cells = sum(
            1 for cell in non_null_cells if isinstance(cell, (int, float))
        )

        if text_cells > number_cells:
            score += 0.4
        elif text_cells == 0 and number_cells > 0:
            score -= 0.3

        # 2. Uniqueness (headers should be mostly unique)
        unique_values = len(set(str(cell).lower() for cell in non_null_cells))
        uniqueness_ratio = unique_values / len(non_null_cells)

        if uniqueness_ratio >= 0.8:
            score += 0.3
        elif uniqueness_ratio < 0.5:
            score -= 0.2

        # 3. Length consistency (headers often have reasonable lengths)
        if text_cells > 0:
            lengths = [
                len(str(cell)) for cell in non_null_cells if isinstance(cell, str)
            ]
            if lengths:
                avg_length = sum(lengths) / len(lengths)
                if 2 <= avg_length <= 50:
                    score += 0.2
                elif avg_length > 100:
                    score -= 0.3

        # 4. Compare with next rows (data should be different type/pattern)
        if next_rows:
            data_type_consistency = self._analyze_data_consistency(next_rows)
            if data_type_consistency > 0.5:
                score += 0.3

        # 5. No purely numeric sequences (like 1,2,3,4...)
        if number_cells > 2:
            numbers = [
                cell for cell in non_null_cells if isinstance(cell, (int, float))
            ]
            if len(numbers) == len(non_null_cells) and len(numbers) > 2:
                sorted_nums = sorted(numbers)
                if numbers == sorted_nums and all(
                    abs(sorted_nums[i + 1] - sorted_nums[i]) <= 1
                    for i in range(len(sorted_nums) - 1)
                ):
                    score -= 0.4

        # 6. Empty cells penalty
        null_ratio = (len(row) - len(non_null_cells)) / len(row) if len(row) > 0 else 0
        if null_ratio > 0.5:
            score -= 0.2

        return {"score": min(1.0, max(0.0, score))}

    def _analyze_data_consistency(self, rows: list[tuple]) -> float:
        """Analyze if rows look like consistent data"""
        if not rows:
            return 0.0

        # Check type consistency column by column
        max_cols = max(len(row) for row in rows) if rows else 0
        consistency_scores = []

        for col_idx in range(max_cols):
            col_values = []
            for row in rows:
                if col_idx < len(row) and row[col_idx] is not None:
                    col_values.append(row[col_idx])

            if not col_values:
                continue

            # Check type consistency
            types = [type(val).__name__ for val in col_values]
            most_common_type = Counter(types).most_common(1)[0]
            type_consistency = most_common_type[1] / len(col_values)
            consistency_scores.append(type_consistency)

        return (
            sum(consistency_scores) / len(consistency_scores)
            if consistency_scores
            else 0.0
        )

    def _should_skip_sheet(
        self,
        file_path: str,
        sheet_name: str,
        max_rows: int = 5000,
        min_text_ratio: float = 0.1,
        sample_size: int = 50,
    ) -> tuple[bool, str]:
        """
        Analyze if a sheet should be skipped based on heuristics.

        Args:
            file_path: Path to Excel file
            sheet_name: Name of sheet to analyze
            max_rows: Maximum number of rows to process (skip if more)
            min_text_ratio: Minimum ratio of text cells to total cells (skip if less)
            sample_size: Number of rows to sample for text analysis

        Returns:
            (should_skip, reason) tuple
        """
        try:
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb[sheet_name]

            # Count total rows (approximate)
            row_count = 0
            text_cells = 0
            total_cells = 0
            formula_cells = 0
            sample_count = 0

            for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
                row_count += 1

                # Sample rows for text analysis
                if sample_count < sample_size:
                    for cell in row:
                        if cell is not None:
                            total_cells += 1
                            if isinstance(cell, str):
                                cell_str = str(cell).strip()
                                if len(cell_str) > 0:
                                    text_cells += 1
                                # Check if it looks like a formula result or error
                                if cell_str.startswith("#") or cell_str in [
                                    "#REF!",
                                    "#VALUE!",
                                    "#N/A",
                                    "#DIV/0!",
                                ]:
                                    formula_cells += 1
                    sample_count += 1

                # Quick exit if too many rows
                if row_count > max_rows:
                    wb.close()
                    return True, f"Too many rows ({row_count} > {max_rows})"

            wb.close()

            # Check text ratio
            text_ratio = 0.0
            formula_ratio = 0.0
            if total_cells > 0:
                text_ratio = text_cells / total_cells
                formula_ratio = formula_cells / total_cells

                if text_ratio < min_text_ratio:
                    return (
                        True,
                        f"Low text content ratio ({text_ratio:.2f} < {min_text_ratio})",
                    )

                # Skip sheets that are mostly formula errors
                if formula_ratio > 0.3:
                    return True, f"Too many formula errors ({formula_ratio:.2f})"

            # Check if sheet is too small to be meaningful
            if row_count < 2:
                return True, f"Too few rows ({row_count})"

            # Check if sheet has no meaningful content
            if total_cells == 0:
                return True, "No content found"

            return False, f"Sheet OK: {row_count} rows, {text_ratio:.2f} text ratio"

        except Exception as e:
            logger.warning(f"Error analyzing sheet {sheet_name}: {e}")
            return True, f"Analysis error: {e}"

    def _is_likely_metadata_sheet(self, sheet_name: str) -> bool:
        """Check if sheet name suggests it's metadata/config rather than data"""
        metadata_indicators = [
            "config",
            "settings",
            "metadata",
            "info",
            "legend",
            "notes",
            "instructions",
            "readme",
            "help",
            "template",
            "example",
            "справка",
            "настройки",
            "конфиг",
            "легенда",
            "заметки",
        ]

        sheet_name_lower = sheet_name.lower().strip()
        return any(indicator in sheet_name_lower for indicator in metadata_indicators)
