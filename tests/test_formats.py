"""End-to-end per format through the Job API with the mock NER (SPEC §11)."""
import csv
import io
import json
import zipfile

import pytest

from scrub.apply import restore_file
from scrub.fake.crypto import decrypt_json
from scrub.formats import open_handler
from scrub.job import Job
from scrub.models import Options

PASS = "correct horse battery staple"
ORIGINALS = ["John Smith", "Jane Doe", "Acme Corp", "Globex Inc", "Springfield",
             "john.smith@acme.com", "jdoe@globex.com"]


def run(path, ner, include_suggested=False, edits=None):
    job = Job.create(path.name, path.read_bytes(), Options(seed=7))
    original_units = {u.id: u.text for u in open_handler(job.source).units()}
    job.detect(ner)
    st = job.state()
    assert st["status"] == "review", st["error"]
    if include_suggested:
        ent = job.entities()
        job.edit({"groups": [{"id": g["id"], "include": True} for g in ent["groups"] if g["suggested"]]})
    if edits:
        job.edit(edits)
    job.apply(PASS, ner)
    st = job.state()
    assert st["status"] == "done", st["error"]
    return job, original_units


def leak_strings(handler, path):
    return "\n".join(t for _, t in handler.leak_texts(path))


def assert_no_originals(job, extra=()):
    h = open_handler(job.output)
    blob = leak_strings(h, job.output)
    for o in ORIGINALS + list(extra):
        assert o.lower() not in blob.lower(), f"{o!r} leaked"
    rep = json.loads((job.dir / "report.json").read_text())
    assert rep["residual_hits"] == [], rep["residual_hits"]
    return blob


def restore(job, tmp_path):
    mapping = decrypt_json((job.dir / "mapping.enc").read_bytes(), PASS)
    out = tmp_path / f"restored{job.output.suffix}"
    restore_file(open_handler(job.output), mapping, out)
    return out


def test_original_deleted_after_apply(fixtures, ner):
    job, _ = run(fixtures["csv"], ner)
    assert not list(job.dir.glob("source.*"))
    assert not (job.dir / "entities.json").exists()
    assert not (job.dir / "units.jsonl").exists()
    raw = (job.dir / "mapping.enc").read_text()
    assert "John Smith" not in raw and "token" in raw


def test_csv(fixtures, ner, tmp_path):
    job, _ = run(fixtures["csv"], ner)
    out = job.output.read_bytes()
    assert out.startswith(b"\xef\xbb\xbf") and b"\r\n" in out
    text = assert_no_originals(job, extra=["JANE DOE"])
    assert "annual" in text and "Annual budget" in text  # 'Ann' never matched inside 'annual'
    rows = list(csv.reader(io.StringIO(out.decode("utf-8-sig"))))
    assert rows[0] == ["Name", "Company", "Email", "Phone", "Notes"]
    assert '"hi"' in rows[2][4]
    restored = restore(job, tmp_path)
    assert list(csv.reader(io.StringIO(restored.read_text("utf-8-sig")))) == \
        list(csv.reader(io.StringIO(fixtures["csv"].read_text("utf-8-sig"))))


def test_xlsx(fixtures, ner, tmp_path):
    job, original_units = run(fixtures["xlsx"], ner)
    assert_no_originals(job)
    with zipfile.ZipFile(job.output) as z:
        names = z.namelist()
        assert any(n.startswith("xl/charts/") for n in names)
        orig = zipfile.ZipFile(fixtures["xlsx"])
        # untouched parts are byte-identical
        assert z.read("xl/styles.xml") == orig.read("xl/styles.xml")
    from openpyxl import load_workbook
    wb = load_workbook(job.output)
    ws = wb["Clients"]
    assert ws["D2"].value != 4155550134 and isinstance(ws["D2"].value, int)
    assert ws["F2"].value == 10
    assert "Annual budget" == ws["E3"].value
    restored = restore(job, tmp_path)
    assert {u.id: u.text for u in open_handler(restored).units()} == original_units


def test_xlsx_sheet_name_blocks(fixtures, ner):
    job, _ = run(fixtures["sheetname"], ner)
    st = job.state()
    assert st["blocking"] >= 1 and not st["acknowledged"]
    hits = job.ui_hits()["hits"]
    assert any(h["locator"] == "xl/workbook.xml" for h in hits)


def test_docx(fixtures, ner, tmp_path):
    job, _ = run(fixtures["docx"], ner)
    blob = assert_no_originals(job, extra=["JANE DOE"])
    assert "annual plan" in blob
    rep = json.loads((job.dir / "report.json").read_text())
    assert rep["revisions_accepted"] >= 2
    import docx
    d = docx.Document(job.output)
    first = d.paragraphs[0]
    assert [r.bold for r in first.runs][:2] == [None, True]  # run formatting kept
    with zipfile.ZipFile(job.output) as z:
        xml = z.read("word/document.xml").decode()
        assert "w:del " not in xml and "<w:ins " not in xml and "delText" not in xml
        rels = z.read("word/_rels/document.xml.rels").decode()
        assert "mailto:" in rels and "acme.com" not in rels
        comments = z.read("word/comments.xml").decode()
        assert "Jane Doe" not in comments
    # restore equals the revisions-accepted original
    restored = restore(job, tmp_path)
    accepted = open_handler(fixtures["docx"]).units()
    assert {u.id: u.text for u in open_handler(restored).units()} == {u.id: u.text for u in accepted}


def test_pdf(fixtures, ner):
    job, _ = run(fixtures["pdf"], ner)
    blob = assert_no_originals(job)
    assert "Contact" in blob and "today" in blob and "annual report" in blob  # neighbours survive
    import pymupdf as fitz
    doc = fitz.open(job.output)
    assert doc.metadata["author"] != "Jane Doe"
    assert "John Smith" not in doc.get_toc()[0][1]
    assert not any("acme.com" in (ln.get("uri") or "") for ln in doc[0].get_links())
    assert doc[0].get_drawings(), "vector art was removed"


def test_pdf_restore_refused(fixtures):
    assert open_handler(fixtures["pdf"]).can_restore is False


def test_case_variant_gets_own_mapping(fixtures, ner):
    job, _ = run(fixtures["csv"], ner)
    mapping = decrypt_json((job.dir / "mapping.enc").read_bytes(), PASS)
    forms = {f["original"]: f["fake"] for g in mapping["groups"] for f in g["forms"]}
    assert "JANE DOE" in forms and forms["JANE DOE"] == forms["Jane Doe"].upper()


def test_add_term_and_column_whole(fixtures, ner):
    job = Job.create("c.csv", fixtures["csv"].read_bytes(), Options(seed=3))
    job.detect(ner)
    ent = job.entities()
    col = next(c for c in ent["columns"] if c["header"] == "Name")
    assert col["suggest_whole"] and col["proposed_type"] == "person"
    job.edit({"add_terms": [{"text": "budget", "type": "organization"}]})
    ent = job.entities()
    g = next(g for g in ent["groups"] if "budget" in g["forms"])
    assert g["occurrences"] == 1
    job.edit({"columns": [{"key": col["key"], "mode": "whole", "type": "person"}]})
    assert any(g["source"] == "column" or "Ann" in g["forms"] for g in job.entities()["groups"])


def test_fake_edit_collision_rejected(fixtures, ner):
    job = Job.create("c.csv", fixtures["csv"].read_bytes(), Options(seed=3))
    job.detect(ner)
    g = next(g for g in job.entities()["groups"] if g["forms"] == ["John Smith"])
    warnings = job.edit({"groups": [{"id": g["id"], "fake": "Jane Doe"}]})
    assert warnings and "not changed" in warnings[0]


@pytest.mark.slow
def test_real_model_smoke(fixtures):
    from scrub.detect.gliner_adapter import get_model
    job, _ = run(fixtures["csv"], get_model())
    assert job.state()["status"] == "done"


def test_headers_not_sent_to_ner(fixtures):
    from scrub.formats import open_handler
    for k in ("csv", "xlsx"):
        kinds = {u.text: u.kind for u in open_handler(fixtures[k]).units()}
        assert kinds["Name"] == "header" and kinds["Company"] == "header"
        assert kinds["John Smith"] == "cell"
