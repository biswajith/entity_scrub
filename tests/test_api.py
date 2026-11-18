import time

import pytest
from fastapi.testclient import TestClient

PASS = "correct horse battery staple"


@pytest.fixture
def client(ner, monkeypatch):
    monkeypatch.setenv("SCRUB_PRELOAD", "0")
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "_ner_override", ner)
    with TestClient(app_mod.app) as c:
        yield c


def wait(c, job_id, statuses=("review", "done", "failed"), timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get(f"/jobs/{job_id}").json()
        if st["status"] in statuses:
            return st
        time.sleep(0.05)
    raise TimeoutError(st)


def test_full_flow_and_restore(client, fixtures):
    data = fixtures["xlsx"].read_bytes()
    r = client.post("/jobs", files={"file": ("clients.xlsx", data)}, data={"options": '{"seed": 1}'})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    assert wait(client, jid)["status"] == "review"

    ent = client.get(f"/jobs/{jid}/entities").json()
    assert ent["groups"] and ent["columns"]
    r = client.put(f"/jobs/{jid}/entities", json={"add_terms": [{"text": "Hidden", "type": "organization"}]})
    assert r.status_code == 200

    assert client.post(f"/jobs/{jid}/apply", json={"passphrase": "short"}).status_code == 400
    assert client.get(f"/jobs/{jid}/output").status_code == 409
    assert client.post(f"/jobs/{jid}/apply", json={"passphrase": PASS}).status_code == 200
    st = wait(client, jid, ("done", "failed"))
    assert st["status"] == "done", st["error"]

    # 'Hidden' was added as a term but is also a sheet name -> blocking hit
    assert st["blocking"] >= 1
    assert client.get(f"/jobs/{jid}/output").status_code == 409
    hits = client.get(f"/jobs/{jid}/hits").json()["hits"]
    assert hits and hits[0]["text"]
    rep = client.get(f"/jobs/{jid}/report").json()
    assert all("text" not in h for h in rep["residual_hits"])  # no originals in the report
    client.post(f"/jobs/{jid}/acknowledge")
    out = client.get(f"/jobs/{jid}/output")
    assert out.status_code == 200 and out.content[:2] == b"PK"
    mapping = client.get(f"/jobs/{jid}/mapping").content

    bad = client.post("/restore", files={"file": ("x.xlsx", out.content), "mapping": ("m.enc", mapping)},
                      data={"passphrase": "wrong passphrase 123"})
    assert bad.status_code == 400
    res = client.post("/restore", files={"file": ("x.xlsx", out.content), "mapping": ("m.enc", mapping)},
                      data={"passphrase": PASS})
    assert res.status_code == 200 and int(res.headers["x-replacements"]) > 0

    assert client.delete(f"/jobs/{jid}").status_code == 200
    assert client.get(f"/jobs/{jid}").status_code == 404


def test_rejects_unsupported_and_large(client, monkeypatch):
    assert client.post("/jobs", files={"file": ("a.txt", b"hi")}).status_code == 415
    import scrub.job as job
    monkeypatch.setattr(job, "MAX_BYTES", 10)
    assert client.post("/jobs", files={"file": ("a.csv", b"x" * 20)}).status_code == 413


def test_pdf_restore_refused(client):
    r = client.post("/restore", files={"file": ("a.pdf", b"%PDF-"), "mapping": ("m.enc", b"{}")},
                    data={"passphrase": PASS})
    assert r.status_code == 400


def test_health(client):
    h = client.get("/health").json()
    assert h["ok"] and ".pdf" in h["supported"]
