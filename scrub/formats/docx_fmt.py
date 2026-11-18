"""DOCX at the XML level (SPEC §6).

Tracked changes are always accepted first. Then every paragraph of every
story part (body, headers, footers, foot/endnotes, comments, text boxes,
glossary) becomes one unit whose text is the concatenation of its w:t /
w:instrText runs, including runs inside hyperlinks, content controls and
smart tags. Comment authors, field instructions, alt text, hyperlink
targets, people.xml and docProps are covered too.
"""
from __future__ import annotations

from lxml import etree

from .ooxml import AttrSlot, OoxmlHandler, RunSlot

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
Q = lambda t: f"{{{W}}}{t}"  # noqa: E731

STORY_PARTS = (r"word/document\.xml|word/header\d*\.xml|word/footer\d*\.xml|word/footnotes\.xml|"
               r"word/endnotes\.xml|word/comments\.xml|word/glossary/document\.xml")
TEXT_TAGS = {Q("t"), Q("instrText"), Q("delText")}
PSEUDO = {Q("tab"): "\t", Q("br"): "\n", Q("cr"): "\n", Q("noBreakHyphen"): "-"}
REVISION_DROP = ("del", "moveFrom")
REVISION_UNWRAP = ("ins", "moveTo")
PR_CHANGES = ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange",
              "tblGridChange", "tblPrExChange", "numberingChange")
RANGE_MARKERS = ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
                 "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
                 "customXmlDelRangeEnd")
REVISION_TAGS = {Q(t) for t in REVISION_DROP + REVISION_UNWRAP + PR_CHANGES}


def accept_revisions(root) -> int:
    """Accept all tracked changes in one part, in place. Returns the count."""
    n = 0
    # paragraphs whose paragraph mark was deleted merge with the next one
    for p in list(root.iter(Q("p"))):
        mark_del = p.find(f"{Q('pPr')}/{Q('rPr')}/{Q('del')}")
        if mark_del is None:
            continue
        n += 1
        mark_del.getparent().remove(mark_del)
        nxt = p.getnext()
        if nxt is None or nxt.tag != Q("p"):
            continue
        content = [c for c in p if c.tag != Q("pPr")]
        anchor = nxt.find(Q("pPr"))
        idx = 0 if anchor is None else list(nxt).index(anchor) + 1
        for c in reversed(content):
            nxt.insert(idx, c)
        p.getparent().remove(p)
    for tag in REVISION_DROP:
        for el in list(root.iter(Q(tag))):
            n += 1
            # a w:del inside rPr marks a deleted paragraph mark (handled above)
            if el.getparent() is not None:
                el.getparent().remove(el)
    for tag in REVISION_UNWRAP:
        for el in list(root.iter(Q(tag))):
            parent = el.getparent()
            if parent is None:
                continue
            n += 1
            if etree.QName(parent).localname == "rPr":  # inserted paragraph mark
                parent.remove(el)
                continue
            idx = list(parent).index(el)
            for c in reversed(list(el)):
                parent.insert(idx, c)
            parent.remove(el)
    for tag in PR_CHANGES + RANGE_MARKERS:
        for el in list(root.iter(Q(tag))):
            if tag in PR_CHANGES:
                n += 1
            el.getparent().remove(el)
    # cells inserted/deleted in tables
    for el in list(root.iter(Q("cellIns"), Q("cellDel"), Q("cellMerge"))):
        n += 1
        el.getparent().remove(el)
    return n


def _own_runs(p) -> list:
    """Text and pseudo-run elements whose nearest w:p ancestor is `p`."""
    out = []
    for el in p.iter():
        if el.tag in TEXT_TAGS or el.tag in PSEUDO:
            anc = next(el.iterancestors(Q("p")), None)
            if anc is p:
                out.append(el)
    return out


class DocxHandler(OoxmlHandler):
    fmt = "docx"

    def _prepare(self) -> None:
        if not self.has("word/document.xml"):
            from .base import UnsupportedFile
            raise UnsupportedFile("Not a .docx document")
        total = 0
        for part in self.parts(STORY_PARTS):
            n = accept_revisions(self.tree(part))
            if n:
                self.mark_dirty(part)
                total += n
        self.info["revisions_accepted"] = total

    def _slots(self):
        out = []
        for part in self.parts(STORY_PARTS):
            root = self.tree(part)
            for i, p in enumerate(root.iter(Q("p"))):
                runs = _own_runs(p)
                if not runs:
                    continue
                writable = [r.tag in TEXT_TAGS for r in runs]
                ro = [None if w else PSEUDO[r.tag] for r, w in zip(runs, writable)]
                out.append((f"{part}:p{i}", RunSlot(part, runs, writable, ro), "para", None))
            for i, c in enumerate(root.iter(Q("comment"))):
                for attr in ("author", "initials"):
                    if c.get(Q(attr)):
                        out.append((f"{part}:cm{i}:{attr}", AttrSlot(part, c, Q(attr)), "meta", None))
            for i, f in enumerate(root.iter(Q("fldSimple"))):
                if f.get(Q("instr")):
                    out.append((f"{part}:fld{i}", AttrSlot(part, f, Q("instr")), "attr", None))
            for i, el in enumerate(root.iter(Q("sdtPr"))):
                for tagname in ("alias", "tag"):
                    t = el.find(Q(tagname))
                    if t is not None and t.get(Q("val")):
                        out.append((f"{part}:sdt{i}:{tagname}", AttrSlot(part, t, Q("val")), "attr", None))
            out.extend(self._drawing_text_slots(part, f"{part}:dml"))
        for part in self.parts(r"word/people\.xml"):
            root = self.tree(part)
            for i, pe in enumerate(root.iter(f"{{{W15}}}person")):
                if pe.get(f"{{{W15}}}author"):
                    out.append((f"{part}:pe{i}", AttrSlot(part, pe, f"{{{W15}}}author"), "meta", None))
        out.extend(self._vml_slots())
        out.extend(self._external_rel_slots(r"word/_rels/.*\.rels"))
        out.extend(self._docprops_slots())
        return out

    def leak_texts(self, path):
        out = super().leak_texts(path)
        # also flag any revision markup left behind (SPEC §11)
        import zipfile
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.startswith("word/") and name.endswith(".xml"):
                    root = etree.fromstring(zf.read(name))
                    for el in root.iter():
                        if el.tag in REVISION_TAGS:
                            self.info.setdefault("revision_leftovers", []).append(name)
                            break
        return out
