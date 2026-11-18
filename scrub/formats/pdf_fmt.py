"""Text PDFs via PyMuPDF (SPEC §6).

Units are text blocks (lines joined with a space so names that wrap across
lines still match), plus metadata, bookmarks, link URIs, annotation info and
form-field values. Replacement: per-character bboxes -> one redaction rect
per line -> apply_redactions (text only; images and vector art kept) ->
draw the fake at the original baseline, size and colour.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..models import Repl, Unit
from .base import Handler, UnsupportedFile, splice

META_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")


def _garbage_ratio(chars: list[str]) -> float:
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    return bad / len(chars)


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


class PdfHandler(Handler):
    fmt = "pdf"
    can_restore = False

    def _open(self) -> fitz.Document:
        try:
            doc = fitz.open(self.path)
        except Exception as e:
            raise UnsupportedFile(f"Cannot open PDF: {e}") from e
        if doc.needs_pass:
            raise UnsupportedFile("PDF is password-protected")
        if not doc.is_pdf:
            raise UnsupportedFile("Not a PDF")
        # remove things we never keep, identically for extract and write so
        # unit ids line up: JS, embedded/attached files, hidden text, XMP
        n_emb = doc.embfile_count()
        doc.scrub(attached_files=True, clean_pages=False, embedded_files=True, hidden_text=True,
                  javascript=True, metadata=False, redactions=False, redact_images=0,
                  remove_links=False, reset_fields=False, reset_responses=False, thumbnails=True,
                  xml_metadata=True)
        if n_emb:
            self.info["notes"].append(f"Removed {n_emb} embedded file(s)")
        return doc

    def _blocks(self, page) -> tuple[list[tuple[str, str, list]], str | None]:
        """[(unit_id, text, charmap)] for one page; charmap[i] is None for a
        synthetic separator or (bbox, origin, size, color, line_key, dir)."""
        d = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
        out = []
        all_chars: list[str] = []
        for bi, b in enumerate(d["blocks"]):
            if b.get("type") != 0:
                continue
            text_parts: list[str] = []
            cmap: list = []
            for li, line in enumerate(b["lines"]):
                if li and text_parts:
                    text_parts.append(" ")
                    cmap.append(None)
                for sp in line["spans"]:
                    for ch in sp["chars"]:
                        text_parts.append(ch["c"])
                        all_chars.append(ch["c"])
                        cmap.append((ch["bbox"], ch["origin"], sp["size"], sp["color"], (bi, li), line["dir"]))
            text = "".join(text_parts)
            if text.strip():
                out.append((f"p{page.number}:b{bi}", text, cmap))
        problem = None
        if not all_chars:
            if page.get_images():
                problem = "no text layer (scanned?)"
        elif _garbage_ratio(all_chars) > 0.3:
            problem = "text not extractable (missing ToUnicode map)"
        return out, problem

    def units(self) -> list[Unit]:
        doc = self._open()
        out: list[Unit] = []
        for page in doc:
            blocks, problem = self._blocks(page)
            if problem:
                self.info["unsupported"].append(f"page {page.number + 1}: {problem}")
                continue
            for uid, text, _ in blocks:
                out.append(Unit(uid, text, None, "pdf_block"))
        out.extend(self._other_units(doc))
        doc.close()
        return out

    def _other_units(self, doc) -> list[Unit]:
        out = []
        meta = doc.metadata or {}
        for k in META_KEYS:
            if meta.get(k):
                out.append(Unit(f"meta:{k}", meta[k], None, "meta"))
        for i, entry in enumerate(doc.get_toc(simple=False)):
            if entry[1]:
                out.append(Unit(f"toc:{i}", entry[1], None, "meta"))
        for page in doc:
            for i, ln in enumerate(page.get_links()):
                if ln.get("uri"):
                    out.append(Unit(f"p{page.number}:link{i}", ln["uri"], None, "attr"))
            for i, an in enumerate(page.annots() or []):
                for k in ("content", "title", "subject"):
                    v = an.info.get(k)
                    if v:
                        out.append(Unit(f"p{page.number}:annot{i}:{k}", v, None, "meta"))
            for i, w in enumerate(page.widgets() or []):
                if isinstance(w.field_value, str) and w.field_value.strip():
                    out.append(Unit(f"p{page.number}:widget{i}", w.field_value, None, "cell"))
        return out

    # -------------------------------------------------------------------------
    def write(self, repls: dict[str, list[Repl]], out_path) -> None:
        doc = self._open()
        self._write_other(doc, repls)  # before redactions so link/annot indices match extraction
        for pno in range(len(doc)):
            page = doc.reload_page(doc[pno])
            blocks, problem = self._blocks(page)
            if problem:
                continue
            draws = []
            for uid, text, cmap in blocks:
                for r in repls.get(uid, []):
                    draws.extend(self._redact_span(page, cmap, r))
            if draws:
                links_before = [{k: v for k, v in ln.items() if k in ("kind", "from", "uri", "page", "to", "file", "zoom")}
                                for ln in page.get_links()]
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
                for d in draws:
                    self._draw(page, *d)
                # redaction drops link annotations that overlap the rects; put them back
                page = doc.reload_page(page)
                if len(page.get_links()) < len(links_before):
                    for ln in page.get_links():
                        page.delete_link(ln)
                    for ln in links_before:
                        page.insert_link(ln)
        doc.save(str(out_path), garbage=4, deflate=True, clean=True)
        doc.close()

    def _redact_span(self, page, cmap, r: Repl) -> list:
        groups: dict = {}
        for i in range(r.start, min(r.end, len(cmap))):
            c = cmap[i]
            if c is None:
                continue
            groups.setdefault(c[4], []).append(c)
        if not groups:
            return []
        lines = list(groups.values())
        pieces = _split_text(r.new, [len(g) for g in lines])
        out = []
        for chars, piece in zip(lines, pieces):
            rect = fitz.Rect(chars[0][0])
            for c in chars[1:]:
                rect |= fitz.Rect(c[0])
            # shrink to the glyph core so neighbouring characters survive
            h = rect.height
            # inset by ~20% of the edge glyph widths so the neighbouring
            # characters (often a space) are not caught by the redaction
            lw = (chars[0][0][2] - chars[0][0][0]) * 0.2
            rw = (chars[-1][0][2] - chars[-1][0][0]) * 0.2
            red = fitz.Rect(rect.x0 + lw, rect.y0 + h * 0.25, rect.x1 - rw, rect.y1 - h * 0.25)
            page.add_redact_annot(red, fill=False, cross_out=False)
            _, origin, size, color, _, direction = chars[0]
            out.append((piece, rect, origin, size, color, direction))
        return out

    def _draw(self, page, text, rect, origin, size, color, direction) -> None:
        if not text:
            return
        width = fitz.get_text_length(text, fontname="helv", fontsize=size)
        if width > rect.width and width > 0:
            # shrink to fit, but not below 70% of the original size
            size = max(size * 0.7, size * rect.width / width)
        rotate = 0
        if abs(direction[0]) < 0.5:  # vertical text
            rotate = 90 if direction[1] < 0 else 270
        page.insert_text(fitz.Point(origin), text, fontsize=size, fontname="helv", color=_rgb(color), rotate=rotate)

    def _write_other(self, doc, repls) -> None:
        meta = dict(doc.metadata or {})
        changed = False
        for k in META_KEYS:
            rs = repls.get(f"meta:{k}")
            if rs and meta.get(k):
                meta[k] = splice(meta[k], rs)
                changed = True
        if changed:
            doc.set_metadata(meta)
        toc = doc.get_toc(simple=False)
        if any(f"toc:{i}" in repls for i in range(len(toc))):
            for i, entry in enumerate(toc):
                rs = repls.get(f"toc:{i}")
                if rs:
                    entry[1] = splice(entry[1], rs)
            doc.set_toc(toc)
        for page in doc:
            links = page.get_links()
            changed_links = []
            for i, ln in enumerate(links):
                rs = repls.get(f"p{page.number}:link{i}")
                if rs and ln.get("uri"):
                    changed_links.append((ln, splice(ln["uri"], rs)))
            for ln, uri in changed_links:
                page.delete_link(ln)
                new = {k: v for k, v in ln.items() if k in ("kind", "from")}
                new["uri"] = uri
                page.insert_link(new)
            for i, an in enumerate(page.annots() or []):
                info = dict(an.info)
                hit = False
                for k in ("content", "title", "subject"):
                    rs = repls.get(f"p{page.number}:annot{i}:{k}")
                    if rs and info.get(k):
                        info[k] = splice(info[k], rs)
                        hit = True
                if hit:
                    an.set_info(info)
                    an.update()
            for i, w in enumerate(page.widgets() or []):
                rs = repls.get(f"p{page.number}:widget{i}")
                if rs and isinstance(w.field_value, str):
                    w.field_value = splice(w.field_value, rs)
                    w.update()

    # -------------------------------------------------------------------------
    def leak_texts(self, path) -> list[tuple[str, str]]:
        """Independent extraction with pdfminer + non-page text via PyMuPDF."""
        from pdfminer.high_level import extract_text

        out = []
        try:
            out.append(("pages (pdfminer)", extract_text(str(path))))
        except Exception as e:  # pdfminer can choke on odd files; fall back
            self.info["notes"].append(f"pdfminer failed during leak check: {e}")
        doc = fitz.open(path)
        for page in doc:
            out.append((f"page {page.number + 1}", page.get_text()))
        for u in self._other_units(doc):
            out.append((u.id, u.text))
        doc.close()
        return out


def _split_text(text: str, sizes: list[int]) -> list[str]:
    """Split `text` across lines roughly in proportion to `sizes`, on word
    boundaries when possible."""
    if len(sizes) == 1:
        return [text]
    words = text.split(" ")
    total = sum(sizes) or 1
    out, wi = [], 0
    for k, s in enumerate(sizes):
        if k == len(sizes) - 1:
            out.append(" ".join(words[wi:]))
            break
        want = round(len(words) * s / total)
        want = max(1, min(want, len(words) - wi - (len(sizes) - k - 1)))
        if want <= 0:
            out.append("")
            continue
        out.append(" ".join(words[wi: wi + want]))
        wi += want
    if len([w for w in out if w]) < len(sizes) and len(words) < len(sizes):
        # fewer words than lines: put everything on the first line
        return [text] + [""] * (len(sizes) - 1)
    return out


def is_pdf(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"
