"""Shared zip/XML machinery for XLSX and DOCX (SPEC §6).

Parts are parsed with lxml; only parts that actually change are
re-serialized, every other zip entry is copied byte-for-byte.

A *slot* is one unit of text with a way to write it back:
  RunSlot   - text spread across several elements (runs); run-preserving
  AttrSlot  - an attribute value
  TextSlot  - the whole .text of one element
  LiteralSlot - string literals ("...") inside a formula element
"""
from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lxml import etree

from ..models import Repl, Unit
from .base import Handler, splice, splice_runs

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_PARSER = etree.XMLParser(remove_blank_text=False, resolve_entities=False, huge_tree=True)
_RECOVER = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)

NS_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_DCT = "http://purl.org/dc/terms/"
NS_EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
NS_VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"

_LITERAL = re.compile(r'"((?:[^"]|"")*)"')


@dataclass
class RunSlot:
    part: str
    els: list
    writable: list[bool]
    ro_text: list[str | None] = field(default_factory=list)  # text for read-only pseudo runs

    def text(self) -> str:
        return "".join(t if t is not None else (e.text or "") for e, t in zip(self.els, self.ro_text))

    def apply(self, repls: list[Repl]) -> None:
        texts = [t if t is not None else (e.text or "") for e, t in zip(self.els, self.ro_text)]
        new = splice_runs(texts, repls, self.writable)
        for e, w, old, n in zip(self.els, self.writable, texts, new):
            if w and n != old:
                e.text = n
                if n != n.strip() and e.tag.endswith("}t"):
                    e.set(XML_SPACE, "preserve")


@dataclass
class AttrSlot:
    part: str
    el: object
    attr: str

    def text(self) -> str:
        return self.el.get(self.attr) or ""

    def apply(self, repls: list[Repl]) -> None:
        self.el.set(self.attr, splice(self.text(), repls))


@dataclass
class TextSlot:
    part: str
    el: object

    def text(self) -> str:
        return self.el.text or ""

    def apply(self, repls: list[Repl]) -> None:
        self.el.text = splice(self.text(), repls)


@dataclass
class LiteralSlot:
    """One string literal inside a formula; text is the unescaped literal."""
    part: str
    el: object
    index: int

    def _spans(self):
        return list(_LITERAL.finditer(self.el.text or ""))

    def text(self) -> str:
        m = self._spans()[self.index]
        return m.group(1).replace('""', '"')

    def apply(self, repls: list[Repl]) -> None:
        src = self.el.text or ""
        m = self._spans()[self.index]
        new = splice(self.text(), repls).replace('"', '""')
        self.el.text = src[: m.start(1)] + new + src[m.end(1):]


def literal_slots(part: str, el, prefix: str) -> list[tuple[str, object]]:
    out = []
    for i, _ in enumerate(_LITERAL.finditer(el.text or "")):
        out.append((f"{prefix}:lit{i}", LiteralSlot(part, el, i)))
    return out


class OoxmlHandler(Handler):
    """Subclasses implement `_slots()` -> list[(unit_id, slot, kind, context)]."""

    def _open(self, path: Path | None = None) -> None:
        self._zip_path = path or self.path
        try:
            self._zf = zipfile.ZipFile(self._zip_path)
        except zipfile.BadZipFile as e:
            from .base import UnsupportedFile
            raise UnsupportedFile("Not a valid Office (zip) file") from e
        self._names = self._zf.namelist()
        self._trees: dict[str, etree._Element] = {}
        self._dirty: set[str] = set()

    def tree(self, name: str):
        if name not in self._trees:
            self._trees[name] = etree.fromstring(self._zf.read(name), _PARSER)
        return self._trees[name]

    def has(self, name: str) -> bool:
        return name in self._names

    def parts(self, pattern: str) -> list[str]:
        rx = re.compile(pattern)
        return sorted(n for n in self._names if rx.fullmatch(n))

    def mark_dirty(self, name: str) -> None:
        self._dirty.add(name)

    def rels_of(self, part: str) -> str:
        d, _, base = part.rpartition("/")
        return f"{d}/_rels/{base}.rels" if d else f"_rels/{base}.rels"

    # -- subclass hooks -----------------------------------------------------
    def _prepare(self) -> None:
        """Hook to normalize the package before slots are enumerated."""

    def _slots(self) -> list[tuple[str, object, str, str | None]]:
        raise NotImplementedError

    # -- common slots ---------------------------------------------------------
    def _docprops_slots(self) -> list[tuple[str, object, str, str | None]]:
        out = []
        if self.has("docProps/core.xml"):
            root = self.tree("docProps/core.xml")
            for i, el in enumerate(root):
                if el.text and el.text.strip() and etree.QName(el).localname not in ("created", "modified", "revision"):
                    out.append((f"core:{etree.QName(el).localname}:{i}", TextSlot("docProps/core.xml", el), "meta", None))
        if self.has("docProps/app.xml"):
            root = self.tree("docProps/app.xml")
            for name in ("Company", "Manager", "HyperlinkBase"):
                for i, el in enumerate(root.iter(f"{{{NS_EP}}}{name}")):
                    if el.text:
                        out.append((f"app:{name}:{i}", TextSlot("docProps/app.xml", el), "meta", None))
        if self.has("docProps/custom.xml"):
            root = self.tree("docProps/custom.xml")
            for i, el in enumerate(root.iter(f"{{{NS_VT}}}lpwstr", f"{{{NS_VT}}}lpstr", f"{{{NS_VT}}}bstr")):
                if el.text:
                    out.append((f"custom:{i}", TextSlot("docProps/custom.xml", el), "meta", None))
        for part in self.parts(r"customXml/item\d+\.xml"):
            try:
                root = self.tree(part)
            except etree.XMLSyntaxError:
                continue
            for i, el in enumerate(root.iter()):
                if isinstance(el.tag, str) and el.text and el.text.strip():
                    out.append((f"{part}:t{i}", TextSlot(part, el), "meta", None))
        return out

    def _external_rel_slots(self, part_pattern: str) -> list[tuple[str, object, str, str | None]]:
        """Hyperlink targets (mailto:, https://...) in .rels files."""
        out = []
        for rels in self.parts(part_pattern):
            root = self.tree(rels)
            for i, rel in enumerate(root.iter(f"{{{NS_REL}}}Relationship")):
                if rel.get("TargetMode") == "External":
                    out.append((f"{rels}:rel{i}", AttrSlot(rels, rel, "Target"), "attr", None))
        return out

    def _vml_slots(self) -> list[tuple[str, object, str, str | None]]:
        """Legacy VML parts (comment shapes, old text boxes): every text node."""
        out = []
        for part in self.parts(r".*\.vml"):
            try:
                root = self.tree(part)
            except etree.XMLSyntaxError:
                self._trees[part] = etree.fromstring(self._zf.read(part), _RECOVER)
                root = self._trees[part]
            if root is None:
                continue
            for i, el in enumerate(root.iter()):
                if isinstance(el.tag, str) and el.text and el.text.strip():
                    out.append((f"{part}:t{i}", TextSlot(part, el), "para", None))
        return out

    def _drawing_text_slots(self, part: str, prefix: str) -> list[tuple[str, object, str, str | None]]:
        """DrawingML paragraphs (shapes, charts, SmartArt): a:p of a:t runs."""
        out = []
        root = self.tree(part)
        for i, p in enumerate(root.iter(f"{{{NS_A}}}p")):
            ts = list(p.iter(f"{{{NS_A}}}t"))
            if ts and any(t.text for t in ts):
                out.append((f"{prefix}:ap{i}", RunSlot(part, ts, [True] * len(ts), [None] * len(ts)), "para", None))
        for tag in (f"{{{NS_XDR}}}cNvPr", f"{{{NS_WP}}}docPr", f"{{{NS_PIC}}}cNvPr"):
            for i, el in enumerate(root.iter(tag)):
                for attr in ("descr", "title"):
                    if el.get(attr):
                        out.append((f"{prefix}:{etree.QName(tag).localname}{i}:{attr}", AttrSlot(part, el, attr), "attr", None))
        return out

    # -- public API -----------------------------------------------------------
    def _enumerate(self, path: Path | None = None):
        self._open(path)
        self._prepare()
        return self._slots()

    def units(self) -> list[Unit]:
        out = []
        for uid, slot, kind, ctx in self._enumerate():
            t = slot.text()
            if t and t.strip():
                out.append(Unit(uid, t, ctx, kind))
        self._zf.close()
        return out

    def write(self, repls: dict[str, list[Repl]], out_path) -> None:
        slots = self._enumerate()
        for uid, slot, kind, _ in slots:
            rs = repls.get(uid)
            if rs and kind != "readonly":
                slot.apply(rs)
                self.mark_dirty(slot.part)
        self._finalize()
        self._save(out_path)

    def _finalize(self) -> None:
        """Hook for last-minute package edits (e.g. pivot refreshOnLoad)."""

    def _save(self, out_path) -> None:
        tmp = Path(str(out_path) + ".tmp")
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in self._zf.infolist():
                if info.filename in self._dirty:
                    data = etree.tostring(self._trees[info.filename], xml_declaration=True,
                                          encoding="UTF-8", standalone=True)
                    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = info.external_attr
                    zout.writestr(zi, data)
                elif info.filename in getattr(self, "_dropped", set()):
                    continue
                else:
                    with self._zf.open(info) as src, zout.open(info, "w") as dst:
                        shutil.copyfileobj(src, dst)
        self._zf.close()
        tmp.replace(out_path)

    def leak_texts(self, path) -> list[tuple[str, str]]:
        """Every text node and attribute value of every XML part, extracted
        independently of the slot logic (SPEC §11)."""
        out = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith((".xml", ".rels", ".vml")):
                    continue
                try:
                    root = etree.fromstring(zf.read(name), _PARSER)
                except etree.XMLSyntaxError:
                    root = None
                if root is None:
                    out.append((name, zf.read(name).decode("utf-8", "replace")))
                    continue
                chunks = []
                for el in root.iter():
                    if not isinstance(el.tag, str):
                        continue
                    if el.text:
                        chunks.append(el.text)
                    for v in el.attrib.values():
                        chunks.append(v)
                # join text-node runs without separators too, so an entity
                # split across runs still shows up
                out.append((name, "\n".join(chunks)))
                out.append((name, "".join(t for t in root.itertext())))
        return out


Slot = RunSlot | AttrSlot | TextSlot | LiteralSlot
SlotList = list[tuple[str, Slot, str, str | None]]
SlotFn = Callable[[], SlotList]
