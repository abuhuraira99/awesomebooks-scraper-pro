"""Turn an uploaded CSV / XLSX / TXT file into a clean list of ISBNs to check.

The ISBN column is detected automatically: a header named like ``isbn``,
``ean``, ``barcode`` or ``sku`` wins; otherwise the column containing the most
valid ISBNs is used. Every uploaded row is kept (so the export lines up with the
client's file), with invalid cells flagged instead of silently dropped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

from .isbn import extract_isbn, looks_like_scientific_notation

MAX_ROWS = 200_000
HEADER_PRIORITY = (
    re.compile(r"isbn", re.I),
    re.compile(r"\b(ean|gtin|barcode|upc)\b", re.I),
    re.compile(r"\bsku\b|seller.?sku|item.?(code|number|id)|product.?(code|id)", re.I),
)


class UploadError(ValueError):
    pass


@dataclass
class UploadRow:
    row_no: int
    raw_sku: str
    isbn: str | None
    problem: str | None = None


@dataclass
class ParsedUpload:
    rows: list[UploadRow] = field(default_factory=list)
    column_label: str = ""
    valid: int = 0
    invalid: int = 0
    duplicates: int = 0

    @property
    def unique(self) -> int:
        return self.valid - self.duplicates


def _read_table(filename: str, data: bytes) -> list[list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise UploadError(f"Couldn't open the Excel file: {exc}") from exc
        ws = wb.worksheets[0]
        table = []
        for row in ws.iter_rows(values_only=True):
            table.append(["" if v is None else (f"{v:.0f}" if isinstance(v, float) and v.is_integer() else str(v)) for v in row])
            if len(table) > MAX_ROWS:
                break
        wb.close()
        return table
    if suffix == ".xls":
        raise UploadError("Old .xls files aren't supported. In Excel choose File > Save As > .xlsx or .csv.")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 always decodes
        raise UploadError("Couldn't read the file's text encoding.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)][: MAX_ROWS + 1]


def _detect_column(table: list[list[str]]) -> tuple[int, bool, str]:
    """Return (column index, first row is a header, label)."""
    header = [c.strip() for c in table[0]] if table else []
    for pattern in HEADER_PRIORITY:
        for idx, name in enumerate(header):
            if name and pattern.search(name) and not extract_isbn(name):
                return idx, True, name
    width = max((len(r) for r in table[:500]), default=0)
    best, best_hits = 0, -1
    for idx in range(width):
        hits = sum(1 for r in table[:500] if idx < len(r) and extract_isbn(r[idx]))
        if hits > best_hits:
            best, best_hits = idx, hits
    has_header = (
        bool(table)
        and best < len(table[0])
        and not extract_isbn(table[0][best])
        and bool(re.search(r"[a-zA-Z]{3,}", table[0][best] or ""))
    )
    label = header[best] if has_header and best < len(header) else f"Column {chr(65 + best) if best < 26 else best + 1}"
    return best, has_header, label


def parse_upload(filename: str, data: bytes) -> ParsedUpload:
    if not data:
        raise UploadError("The file is empty.")
    table = [r for r in _read_table(filename, data) if any((c or "").strip() for c in r)]
    if not table:
        raise UploadError("No rows found in the file.")
    col, has_header, label = _detect_column(table)
    body = table[1:] if has_header else table
    if len(body) > MAX_ROWS:
        raise UploadError(f"Files are limited to {MAX_ROWS:,} rows per run.")

    result = ParsedUpload(column_label=label)
    seen: set[str] = set()
    for offset, row in enumerate(body, start=2 if has_header else 1):
        raw = (row[col] if col < len(row) else "").strip()
        if not raw:
            continue
        isbn = extract_isbn(raw)
        problem = None
        if isbn is None:
            problem = (
                "Excel turned this number into scientific notation; re-export the column as text"
                if looks_like_scientific_notation(raw)
                else "Not a valid ISBN"
            )
            result.invalid += 1
        else:
            result.valid += 1
            if isbn in seen:
                result.duplicates += 1
            seen.add(isbn)
        result.rows.append(UploadRow(row_no=offset, raw_sku=raw, isbn=isbn, problem=problem))
    if result.valid == 0:
        raise UploadError(f"No valid ISBNs found (checked '{label}'). Make sure the file has one ISBN-10 or ISBN-13 per row.")
    return result
