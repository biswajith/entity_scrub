"""Generate synthetic test files with the tricky cases from SPEC §11.

All names are made up. Usage: python scripts/make_fixtures.py [outdir]
"""
from __future__ import annotations

import sys
from pathlib import Path

# Names the mock NER "knows" (tests/conftest.py)
KNOWN = {
    "John Smith": "person", "Jane Doe": "person", "Ann": "person",
    "Acme Corp": "organization", "Globex Inc": "organization", "Springfield": "location",
}

CSV_TEXT = (
    "Name,Company,Email,Phone,Notes\r\n"
    "John Smith,Acme Corp,john.smith@acme.com,(415) 555-0134,\"Met John Smith in Springfield; annual review\"\r\n"
    "Jane Doe,Globex Inc,jdoe@globex.com,+44 20 7946 0958,\"JANE DOE called, said \"\"hi\"\". Ann agreed\"\r\n"
    "Ann,Acme Corp,ann@acme.com,,Annual budget\r\n"
)


def make_csv(out: Path) -> Path:
    p = out / "clients.csv"
    p.write_bytes(b"\xef\xbb\xbf" + CSV_TEXT.encode("utf-8"))
    return p


def make_xlsx(out: Path, sheet_name: str = "Clients") -> Path:
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.chart import BarChart, Reference
    from openpyxl.comments import Comment
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(["Name", "Company", "Email", "Phone", "Notes", "Amount"])
    ws.append(["John Smith", "Acme Corp", "john.smith@acme.com", 4155550134, "Met John Smith in Springfield", 10])
    ws.append(["Jane Doe", "Globex Inc", "jdoe@globex.com", "+44 20 7946 0958", "Annual budget", 20])
    ws.append(["Ann", "Acme Corp", "ann@acme.com", None, "Ann approved", 30])
    ws["E5"] = CellRichText([TextBlock(InlineFont(b=True), "Call Jo"), "hn Smith today"])
    ws["G2"] = '="Contact: John Smith"'
    ws["H2"] = "Email John"
    ws["H2"].hyperlink = "mailto:john.smith@acme.com"
    ws["B2"].comment = Comment("Ask John Smith about Globex Inc", "Jane Doe")
    dv = DataValidation(type="list", formula1='"Acme Corp,Globex Inc"')
    ws.add_data_validation(dv)
    dv.add("B6")
    ws.oddHeader.center.text = "Prepared by Jane Doe"
    chart = BarChart()
    chart.title = "Revenue for Acme Corp"
    chart.add_data(Reference(ws, min_col=6, min_row=1, max_row=4), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=4))
    ws.add_chart(chart, "J2")
    hidden = wb.create_sheet("Hidden")
    hidden.append(["Owner"])
    hidden.append(["Jane Doe"])
    hidden.sheet_state = "hidden"
    wb.properties.creator = "Jane Doe"
    wb.properties.title = "Acme Corp clients"
    p = out / ("clients.xlsx" if sheet_name == "Clients" else "sheetname.xlsx")
    wb.save(p)
    return p


def make_docx(out: Path) -> Path:
    import docx
    from docx.oxml.ns import qn
    from lxml import etree

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    d = docx.Document()
    d.core_properties.author = "Jane Doe"
    d.core_properties.title = "Notes on Acme Corp"
    p = d.add_paragraph("Meeting with ")
    p.add_run("Jo").bold = True
    p.add_run("hn Sm").italic = True
    p.add_run("ith of Acme Corp in Springfield. The annual plan was approved.")
    d.add_paragraph("JANE DOE will follow up.\tAnn too.")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "Globex Inc"
    t.cell(0, 1).text = "jdoe@globex.com"
    d.sections[0].header.paragraphs[0].text = "Confidential - Acme Corp"
    d.sections[0].footer.paragraphs[0].text = "Author: Jane Doe"
    # comment
    cp = d.add_paragraph("See comment.")
    d.add_comment(cp.runs, text="John Smith should check this", author="Jane Doe", initials="JD")
    # hyperlink to mailto
    hp = d.add_paragraph("Write to ")
    rid = d.part.relate_to("mailto:john.smith@acme.com", docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hl = etree.SubElement(hp._p, qn("w:hyperlink"))
    hl.set(qn("r:id"), rid)
    r = etree.SubElement(hl, qn("w:r"))
    etree.SubElement(r, qn("w:t")).text = "John Smith"
    # tracked changes: a deletion containing a real name and an insertion
    tp = d.add_paragraph("Partner: ")
    dl = etree.SubElement(tp._p, qn("w:del"))
    dl.set(qn("w:id"), "1"); dl.set(qn("w:author"), "Jane Doe"); dl.set(qn("w:date"), "2026-01-01T00:00:00Z")
    dr = etree.SubElement(dl, qn("w:r"))
    etree.SubElement(dr, qn("w:delText")).text = "Globex Inc"
    ins = etree.SubElement(tp._p, qn("w:ins"))
    ins.set(qn("w:id"), "2"); ins.set(qn("w:author"), "Jane Doe"); ins.set(qn("w:date"), "2026-01-01T00:00:00Z")
    ir = etree.SubElement(ins, qn("w:r"))
    etree.SubElement(ir, qn("w:t")).text = "Acme Corp"
    # a VML text box inside a paragraph
    xml = (f'<w:r xmlns:w="{W}" xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape style="width:200pt;height:40pt">'
           f'<v:textbox><w:txbxContent><w:p><w:r><w:t>Box: John Smith</w:t></w:r></w:p></w:txbxContent>'
           f'</v:textbox></v:shape></w:pict></w:r>')
    bp = d.add_paragraph("Before box ")
    bp._p.append(etree.fromstring(xml))
    p = out / "notes.docx"
    d.save(p)
    return p


def make_pdf(out: Path) -> Path:
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(40, 40, 560, 300), color=(0.8, 0.8, 0.8), fill=(0.95, 0.95, 1))
    page.insert_text((72, 100), "Contact John Smith at Acme Corp today.", fontsize=12)
    page.insert_text((72, 130), "The annual report from Springfield is ready.", fontsize=12)
    # a name wrapping across two lines in one block
    page.insert_textbox(fitz.Rect(72, 150, 175, 200), "Reviewed by Jane Doe yesterday", fontsize=12)
    page.insert_text((72, 230), "Email: john.smith@acme.com", fontsize=10, color=(0.2, 0.2, 0.8))
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 220, 300, 235), "uri": "mailto:john.smith@acme.com"})
    annot = page.add_text_annot((400, 100), "Checked by Jane Doe")
    annot.set_info(title="Jane Doe")
    annot.update()
    doc.set_metadata({"author": "Jane Doe", "title": "Acme Corp report"})
    doc.set_toc([[1, "John Smith profile", 1]])
    p = out / "report.pdf"
    doc.save(p)
    return p


def make_all(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    return {"csv": make_csv(out), "xlsx": make_xlsx(out), "docx": make_docx(out), "pdf": make_pdf(out),
            "sheetname": make_xlsx(out, sheet_name="John Smith")}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    for k, v in make_all(target).items():
        print(k, v)
