"""CSV: same dialect, equal values (SPEC §6)."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from charset_normalizer import from_bytes

from ..models import Column, Repl, Unit
from .base import Handler, UnsupportedFile, splice

csv.field_size_limit(1 << 30)


class CsvHandler(Handler):
    fmt = "csv"

    def _read(self, path: Path | None = None):
        raw = (path or self.path).read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        enc = self.opts.csv_encoding
        if not enc:
            if bom:
                enc = "utf-8-sig"
            else:
                best = from_bytes(raw[:2_000_000]).best()
                enc = best.encoding if best else "utf-8"
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            raise UnsupportedFile(f"Cannot decode CSV as {enc}: {e}") from e
        newline = "\r\n" if "\r\n" in text[:100_000] else ("\r" if "\r" in text[:100_000] and "\n" not in text[:100_000] else "\n")
        delim = self.opts.csv_delimiter
        quote = '"'
        if not delim:
            try:
                d = csv.Sniffer().sniff(text[:64_000], delimiters=",;\t|")
                delim, quote = d.delimiter, d.quotechar or '"'
            except csv.Error:
                delim = ","
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim, quotechar=quote))
        self.info.update(encoding=enc, delimiter=delim, quotechar=quote, bom=bom,
                         newline={"\r\n": "CRLF", "\n": "LF", "\r": "CR"}[newline])
        self._newline = newline
        return rows, enc, delim, quote

    def units(self) -> list[Unit]:
        rows, *_ = self._read()
        header = rows[0] if rows else []
        out = []
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                if not val.strip():
                    continue
                ctx = header[c].strip() if r > 0 and c < len(header) and header[c].strip() else None
                out.append(Unit(f"r{r}c{c}", val, ctx, "header" if r == 0 else "cell"))
        return out

    def columns(self) -> list[Column]:
        rows, *_ = self._read()
        if not rows:
            return []
        header = rows[0]
        width = max(len(r) for r in rows)
        cols = []
        for c in range(width):
            h = header[c] if c < len(header) else f"Column {c + 1}"
            ids = [f"r{r}c{c}" for r in range(1, len(rows)) if c < len(rows[r]) and rows[r][c].strip()]
            cols.append(Column(f"c{c}", h or f"Column {c + 1}", ids))
        return cols

    def write(self, repls: dict[str, list[Repl]], out_path) -> None:
        rows, enc, delim, quote = self._read()
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                rs = repls.get(f"r{r}c{c}")
                if rs:
                    row[c] = splice(val, rs)
        buf = io.StringIO(newline="")
        w = csv.writer(buf, delimiter=delim, quotechar=quote, lineterminator=self._newline, quoting=csv.QUOTE_MINIMAL)
        w.writerows(rows)
        Path(out_path).write_bytes(buf.getvalue().encode(enc))

    def leak_texts(self, path) -> list[tuple[str, str]]:
        raw = Path(path).read_bytes()
        enc = self.info.get("encoding") or "utf-8"
        return [("file", raw.decode(enc, errors="replace"))]
