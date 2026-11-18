from __future__ import annotations

from pathlib import Path

from ..models import Options
from .base import Handler, UnsupportedFile
from .csv_fmt import CsvHandler
from .docx_fmt import DocxHandler
from .pdf_fmt import PdfHandler
from .xlsx_fmt import XlsxHandler

HANDLERS = {".csv": CsvHandler, ".tsv": CsvHandler, ".xlsx": XlsxHandler, ".docx": DocxHandler, ".pdf": PdfHandler}
SUPPORTED = sorted(HANDLERS)


def open_handler(path: str | Path, opts: Options | None = None) -> Handler:
    ext = Path(path).suffix.lower()
    cls = HANDLERS.get(ext)
    if cls is None:
        raise UnsupportedFile(f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED)}")
    return cls(path, opts)


__all__ = ["open_handler", "Handler", "UnsupportedFile", "SUPPORTED"]
