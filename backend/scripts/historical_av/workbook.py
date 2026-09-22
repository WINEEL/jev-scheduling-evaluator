"""A minimal read-only .xlsx reader: enough to inspect a schedule workbook.

Deliberately tiny and dependency-free. A workbook is a zip of XML, and the
part of it a schedule tab uses is small: shared strings, a sheet's cell
values, and Excel's date serials. Adding a spreadsheet library to the backend
so one local validation script can read one file would be a production
dependency taken on for a script that is not production.

It reads values only -- no formulas, styles, merged cells or charts -- and a
cell holding a formula yields that formula's **last cached value**, which is
what a schedule tab stores. It never writes.
"""

from __future__ import annotations

import datetime
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = ["WorkbookError", "Sheet", "Workbook", "excel_serial_to_date"]

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"m": _MAIN, "r": _RELS}

#: Excel's day 0. The 1900 leap-year bug means serials count from 1899-12-30.
_EPOCH = datetime.date(1899, 12, 30)
#: Serial bounds for a plausible schedule date (roughly 1982..2073). Anything
#: outside is a number that happens to sit in a date column, not a date.
_MIN_SERIAL, _MAX_SERIAL = 30_000, 63_000

_CELL_REF = re.compile(r"^([A-Z]+)")


class WorkbookError(RuntimeError):
    """A workbook could not be read without guessing."""


def excel_serial_to_date(value: str) -> datetime.date | None:
    """Convert an Excel date serial to a date, or ``None`` if it is not one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (_MIN_SERIAL < number < _MAX_SERIAL):
        return None
    return _EPOCH + datetime.timedelta(days=int(number))


def _column_index(ref: str) -> int:
    match = _CELL_REF.match(ref or "")
    if not match:
        raise WorkbookError(f"unreadable cell reference {ref!r}")
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - 64)
    return index - 1


@dataclass(frozen=True, slots=True)
class Sheet:
    """One worksheet, as a rectangular grid of stripped strings."""

    name: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def header(self) -> tuple[str, ...]:
        return self.rows[0] if self.rows else ()

    @property
    def body(self) -> tuple[tuple[str, ...], ...]:
        return self.rows[1:]


class Workbook:
    """Read-only access to the sheets of one .xlsx file."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        try:
            self._zip = zipfile.ZipFile(self._path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise WorkbookError(f"{self._path.name}: not a readable .xlsx") from exc
        self._shared = self._read_shared_strings()
        self._targets = self._read_sheet_index()

    @property
    def sheet_names(self) -> tuple[str, ...]:
        return tuple(self._targets)

    def _read_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
        return [
            "".join(node.text or "" for node in si.iter(f"{{{_MAIN}}}t"))
            for si in root.findall("m:si", _NS)
        ]

    def _read_sheet_index(self) -> dict[str, str]:
        try:
            rels_xml = self._zip.read("xl/_rels/workbook.xml.rels")
            book_xml = self._zip.read("xl/workbook.xml")
        except KeyError as exc:
            raise WorkbookError(f"{self._path.name}: missing workbook part") from exc
        targets = {
            rel.get("Id"): rel.get("Target") for rel in ET.fromstring(rels_xml)
        }
        out: dict[str, str] = {}
        sheets = ET.fromstring(book_xml).find("m:sheets", _NS)
        for sheet in sheets if sheets is not None else []:
            target = targets.get(sheet.get(f"{{{_RELS}}}id"), "")
            if not target:
                continue
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            out[sheet.get("name") or ""] = target
        if not out:
            raise WorkbookError(f"{self._path.name}: no worksheets found")
        return out

    def sheet(self, name: str) -> Sheet:
        target = self._targets.get(name)
        if target is None:
            raise WorkbookError(f"{self._path.name}: no sheet named {name!r}")
        root = ET.fromstring(self._zip.read(target))
        data = root.find("m:sheetData", _NS)
        rows: list[tuple[str, ...]] = []
        for row_node in data.findall("m:row", _NS) if data is not None else []:
            cells: dict[int, str] = {}
            for cell in row_node.findall("m:c", _NS):
                value = self._cell_value(cell)
                if value:
                    cells[_column_index(cell.get("r", ""))] = value
            width = max(cells) + 1 if cells else 0
            rows.append(tuple(cells.get(i, "") for i in range(width)))
        return Sheet(name=name, rows=tuple(rows))

    def _cell_value(self, cell: ET.Element) -> str:
        kind = cell.get("t")
        if kind == "inlineStr":
            inline = cell.find("m:is", _NS)
            if inline is None:
                return ""
            return "".join(n.text or "" for n in inline.iter(f"{{{_MAIN}}}t")).strip()
        node = cell.find("m:v", _NS)
        if node is None or node.text is None:
            return ""
        if kind == "s":
            try:
                return self._shared[int(node.text)].strip()
            except (ValueError, IndexError) as exc:
                raise WorkbookError("shared-string index out of range") from exc
        return node.text.strip()
