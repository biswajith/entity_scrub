"""XLSX at the zip/XML level (SPEC §6).

Covers shared strings (incl. rich text), inline strings, formula string
results and literals, numeric cells that look like IDs/phones, comments
(classic + threaded), header/footer, hyperlinks, data validation, defined
names, tables, drawings, charts, pivot caches/tables, external links and
docProps. Sheet names are reported (read-only), never renamed.
"""
from __future__ import annotations

import posixpath
import re

from lxml import etree

from ..models import Column
from .base import UnsupportedFile
from .ooxml import (AttrSlot, OoxmlHandler, RunSlot, TextSlot, literal_slots)

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_TC = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
Q = lambda t: f"{{{NS}}}{t}"  # noqa: E731
_REF = re.compile(r"([A-Z]+)(\d+)")
_INTLIKE = re.compile(r"^\d{7,19}$")


class XlsxHandler(OoxmlHandler):
    fmt = "xlsx"

    def _sheets(self) -> list[tuple[str, str]]:
        """[(sheet name, part path)] in workbook order."""
        if not self.has("xl/workbook.xml"):
            raise UnsupportedFile("Not an .xlsx workbook")
        wb = self.tree("xl/workbook.xml")
        rels = self.tree("xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{{{NS_REL}}}Relationship")}
        out = []
        for s in wb.iter(Q("sheet")):
            t = targets.get(s.get(f"{{{NS_R}}}id"), "")
            path = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
            if self.has(path) and "worksheets/" in path:
                out.append((s.get("name"), path))
        return out

    def _sst_texts(self) -> list[str]:
        if not self.has("xl/sharedStrings.xml"):
            return []
        root = self.tree("xl/sharedStrings.xml")
        return ["".join(t.text or "" for t in self._si_ts(si)) for si in root.iter(Q("si"))]

    @staticmethod
    def _si_ts(si) -> list:
        return [t for t in si.iter(Q("t")) if etree.QName(t.getparent()).localname != "rPh"]

    def _slots(self):
        out = []
        sst = self._sst_texts()
        sheets = self._sheets()
        sst_ctx: dict[int, str] = {}
        sst_header: set[int] = set()
        sst_body: set[int] = set()
        self._cols: dict[str, Column] = {}

        # worksheets
        for sname, part in sheets:
            root = self.tree(part)
            headers: dict[str, str] = {}
            first_row = None
            for row in root.iter(Q("row")):
                for c in row.iter(Q("c")):
                    ref = c.get("r") or ""
                    m = _REF.match(ref)
                    if not m:
                        continue
                    col, rnum = m.group(1), int(m.group(2))
                    if first_row is None:
                        first_row = rnum
                    t = c.get("t")
                    v = c.find(Q("v"))
                    cell_text = None
                    uid = None
                    if t == "s" and v is not None and v.text and v.text.isdigit():
                        idx = int(v.text)
                        uid = f"sst:{idx}"
                        cell_text = sst[idx] if idx < len(sst) else ""
                    elif t == "inlineStr":
                        is_ = c.find(Q("is"))
                        if is_ is not None:
                            ts = [x for x in is_.iter(Q("t")) if etree.QName(x.getparent()).localname != "rPh"]
                            uid = f"{part}!{ref}:is"
                            out.append((uid, RunSlot(part, ts, [True] * len(ts), [None] * len(ts)), "cell", None))
                            cell_text = "".join(x.text or "" for x in ts)
                    elif t == "str" and v is not None:
                        uid = f"{part}!{ref}:str"
                        out.append((uid, TextSlot(part, v), "cell", None))
                        cell_text = v.text or ""
                    elif t in (None, "n") and v is not None and v.text and _INTLIKE.match(v.text):
                        uid = f"{part}!{ref}:num"
                        out.append((uid, TextSlot(part, v), "numeric", None))
                        cell_text = v.text
                    f = c.find(Q("f"))
                    if f is not None and f.text and '"' in f.text:
                        for lid, slot in literal_slots(part, f, f"{part}!{ref}:f"):
                            out.append((lid, slot, "literal", None))
                    if cell_text is None:
                        continue
                    if rnum == first_row:
                        headers[col] = cell_text.strip()
                        if uid.startswith("sst:"):
                            sst_header.add(int(uid[4:]))
                        elif out and out[-1][0] == uid:
                            out[-1] = (uid, out[-1][1], "header", None)
                    else:
                        if uid.startswith("sst:"):
                            sst_body.add(int(uid[4:]))
                        key = f"{sname}!{col}"
                        colobj = self._cols.setdefault(key, Column(key, f"{sname} / {headers.get(col) or col}", []))
                        colobj.unit_ids.append(uid)
                        if uid.startswith("sst:") and headers.get(col):
                            sst_ctx.setdefault(int(uid[4:]), headers[col])
            # set context for inline/str cells in this sheet
            for i, (uid, slot, kind, ctx) in enumerate(out):
                if uid.startswith(part + "!") and kind == "cell":
                    m = _REF.match(uid.split("!", 1)[1])
                    if m and int(m.group(2)) != first_row and headers.get(m.group(1)):
                        out[i] = (uid, slot, kind, headers[m.group(1)])
            for hf in root.iter(Q("headerFooter")):
                for i, el in enumerate(hf):
                    if isinstance(el.tag, str) and el.text:
                        out.append((f"{part}:hf{i}", TextSlot(part, el), "meta", None))
            for i, h in enumerate(root.iter(Q("hyperlink"))):
                for attr in ("display", "tooltip"):
                    if h.get(attr):
                        out.append((f"{part}:hl{i}:{attr}", AttrSlot(part, h, attr), "attr", None))
            for i, dv in enumerate(root.iter(Q("dataValidation"))):
                for fname in ("formula1", "formula2"):
                    fe = dv.find(Q(fname))
                    if fe is not None and fe.text:
                        for lid, slot in literal_slots(part, fe, f"{part}:dv{i}:{fname}"):
                            out.append((lid, slot, "literal", None))
                for attr in ("prompt", "error", "promptTitle", "errorTitle"):
                    if dv.get(attr):
                        out.append((f"{part}:dv{i}:{attr}", AttrSlot(part, dv, attr), "attr", None))
            for i, fe in enumerate(root.iter(Q("formula"))):
                if fe.text and '"' in fe.text:
                    for lid, slot in literal_slots(part, fe, f"{part}:cf{i}"):
                        out.append((lid, slot, "literal", None))

        # shared strings
        if self.has("xl/sharedStrings.xml"):
            root = self.tree("xl/sharedStrings.xml")
            for i, si in enumerate(root.iter(Q("si"))):
                ts = self._si_ts(si)
                kind = "header" if i in sst_header and i not in sst_body else "cell"
                out.append((f"sst:{i}", RunSlot("xl/sharedStrings.xml", ts, [True] * len(ts), [None] * len(ts)),
                            kind, sst_ctx.get(i)))

        # workbook: defined names, sheet names (read-only)
        wb = self.tree("xl/workbook.xml")
        for i, dn in enumerate(wb.iter(Q("definedName"))):
            if dn.text and '"' in dn.text:
                for lid, slot in literal_slots("xl/workbook.xml", dn, f"wb:dn{i}"):
                    out.append((lid, slot, "literal", None))
        for i, s in enumerate(wb.iter(Q("sheet"))):
            out.append((f"wb:sheet{i}", AttrSlot("xl/workbook.xml", s, "name"), "readonly", None))

        # comments (classic) and threaded comments
        for part in self.parts(r"xl/comments[^/]*\.xml|xl/comments/[^/]+\.xml"):
            root = self.tree(part)
            for i, a in enumerate(root.iter(Q("author"))):
                out.append((f"{part}:author{i}", TextSlot(part, a), "meta", None))
            for i, cm in enumerate(root.iter(Q("comment"))):
                ts = [t for t in cm.iter(Q("t")) if etree.QName(t.getparent()).localname != "rPh"]
                if ts:
                    out.append((f"{part}:c{i}", RunSlot(part, ts, [True] * len(ts), [None] * len(ts)), "para", None))
        for part in self.parts(r"xl/threadedComments/threadedComment\d*\.xml"):
            root = self.tree(part)
            for i, t in enumerate(root.iter(f"{{{NS_TC}}}text")):
                out.append((f"{part}:t{i}", TextSlot(part, t), "para", None))
        for part in self.parts(r"xl/persons/person\d*\.xml"):
            root = self.tree(part)
            for i, p in enumerate(root.iter(f"{{{NS_TC}}}person")):
                if p.get("displayName"):
                    out.append((f"{part}:p{i}", AttrSlot(part, p, "displayName"), "meta", None))

        # drawings (shapes, text boxes) and charts
        for part in self.parts(r"xl/drawings/drawing\d*\.xml"):
            out.extend(self._drawing_text_slots(part, part))
        for part in self.parts(r"xl/charts/chart\d*\.xml"):
            out.extend(self._drawing_text_slots(part, part))
            root = self.tree(part)
            for i, cache in enumerate(root.iter(f"{{{NS_C}}}strCache", f"{{{NS_C}}}multiLvlStrCache")):
                for j, v in enumerate(cache.iter(f"{{{NS_C}}}v")):
                    if v.text:
                        out.append((f"{part}:sc{i}:{j}", TextSlot(part, v), "cell", None))

        # pivot caches and tables
        for part in self.parts(r"xl/pivotCache/pivotCache(Definition|Records)\d*\.xml"):
            root = self.tree(part)
            for i, s in enumerate(root.iter(Q("s"))):
                if s.get("v"):
                    out.append((f"{part}:s{i}", AttrSlot(part, s, "v"), "cell", None))
        for part in self.parts(r"xl/pivotTables/pivotTable\d*\.xml"):
            root = self.tree(part)
            for i, it in enumerate(root.iter(Q("item"))):
                if it.get("n"):
                    out.append((f"{part}:item{i}", AttrSlot(part, it, "n"), "cell", None))

        # tables (column names must stay in sync with header cells)
        for part in self.parts(r"xl/tables/table\d*\.xml"):
            root = self.tree(part)
            for i, tc in enumerate(root.iter(Q("tableColumn"))):
                out.append((f"{part}:tc{i}", AttrSlot(part, tc, "name"), "attr", None))

        # external link caches
        for part in self.parts(r"xl/externalLinks/externalLink\d*\.xml"):
            root = self.tree(part)
            for i, v in enumerate(root.iter(Q("v"))):
                if v.text:
                    out.append((f"{part}:v{i}", TextSlot(part, v), "cell", None))

        out.extend(self._vml_slots())
        out.extend(self._external_rel_slots(r".*\.rels"))
        out.extend(self._docprops_slots())
        return out

    def _finalize(self) -> None:
        if any("pivotCache" in d for d in self._dirty):
            for part in self.parts(r"xl/pivotCache/pivotCacheDefinition\d*\.xml"):
                self.tree(part).set("refreshOnLoad", "1")
                self.mark_dirty(part)

    def columns(self) -> list[Column]:
        self._enumerate()
        self._zf.close()
        return list(self._cols.values())
