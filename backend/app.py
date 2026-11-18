"""FastAPI layer over the `scrub` library (SPEC §7).

Run:  uvicorn backend.app:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from scrub.apply import restore_file
from scrub.fake.crypto import BadPassphrase, decrypt_json
from scrub.formats import SUPPORTED, UnsupportedFile, open_handler
from scrub.job import MAX_BYTES, Job, list_jobs, purge
from scrub.models import Options

log = logging.getLogger("entity-scrub")
logging.basicConfig(level=logging.INFO)

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scrub-worker")
_ner_override = None  # tests inject a fake model here
_ner_lock = threading.Lock()


def get_ner():
    if _ner_override is not None:
        return _ner_override
    from scrub.detect.gliner_adapter import get_model
    return get_model()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    n = purge()
    if n:
        log.info("purged %d old job(s)", n)
    if os.environ.get("SCRUB_PRELOAD", "1") == "1" and _ner_override is None:
        _worker.submit(get_ner)  # warm the model in the background
    yield


app = FastAPI(title="Entity Scrub", version="0.1.0", lifespan=lifespan)


def _job(job_id: str) -> Job:
    try:
        return Job(job_id)
    except KeyError:
        raise HTTPException(404, "No such job")


def _media(ext: str) -> str:
    return {
        ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(ext, "application/octet-stream")


@app.get("/health")
def health():
    from scrub.detect import gliner_adapter as ga
    return {"ok": True, "model": ga.DEFAULT_MODEL, "model_loaded": ga.model_loaded() or _ner_override is not None,
            "device": ga.pick_device(), "supported": SUPPORTED, "max_mb": MAX_BYTES // (1024 * 1024)}


@app.get("/jobs")
def jobs():
    return list_jobs()


@app.post("/jobs")
async def create_job(file: UploadFile = File(...), options: str = Form("{}")):
    data = await file.read()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED:
        raise HTTPException(415, f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED)}")
    try:
        opts = Options.from_dict(json.loads(options or "{}"))
        job = Job.create(file.filename or f"upload{ext}", data, opts)
    except ValueError as e:
        raise HTTPException(413 if "larger" in str(e) else 400, str(e))
    _worker.submit(lambda: job.detect(get_ner()))
    return job.state()


@app.get("/jobs/{job_id}")
def job_state(job_id: str):
    return _job(job_id).state()


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    _job(job_id).delete()
    return {"deleted": job_id}


@app.get("/jobs/{job_id}/entities")
def get_entities(job_id: str):
    job = _job(job_id)
    if job.state()["status"] != "review":
        raise HTTPException(409, "Entities are only available while the job is in review")
    return job.entities()


@app.get("/jobs/{job_id}/columns")
def get_columns(job_id: str):
    return get_entities(job_id)["columns"]


@app.put("/jobs/{job_id}/entities")
def put_entities(job_id: str, edits: dict = Body(...)):
    job = _job(job_id)
    try:
        warnings = job.edit(edits)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"warnings": warnings, **job.entities()}


@app.post("/jobs/{job_id}/apply")
def apply_job(job_id: str, body: dict = Body(...)):
    job = _job(job_id)
    if job.state()["status"] != "review":
        raise HTTPException(409, "Job is not in review")
    passphrase = body.get("passphrase") or ""
    from scrub.fake.crypto import check_passphrase
    try:
        check_passphrase(passphrase)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job.update(status="applying")
    _worker.submit(lambda: job.apply(passphrase, get_ner() if job.opts().second_pass else None))
    return job.state()


@app.get("/jobs/{job_id}/hits")
def hits(job_id: str):
    """Residual hits and second-pass warnings WITH text, for the UI only."""
    return _job(job_id).ui_hits()


@app.post("/jobs/{job_id}/acknowledge")
def acknowledge(job_id: str):
    job = _job(job_id)
    return job.update(acknowledged=True)


def _done_job(job_id: str) -> Job:
    job = _job(job_id)
    st = job.state()
    if st["status"] != "done":
        raise HTTPException(409, f"Job is '{st['status']}'")
    return job


@app.get("/jobs/{job_id}/output")
def output(job_id: str):
    job = _done_job(job_id)
    st = job.state()
    if st["blocking"] and not st["acknowledged"]:
        raise HTTPException(409, "Output still contains approved originals; acknowledge first")
    stem = Path(st["filename"]).stem
    return FileResponse(job.output, media_type=_media(st["ext"]), filename=f"{stem}.scrubbed{st['ext']}")


@app.get("/jobs/{job_id}/mapping")
def mapping(job_id: str):
    job = _done_job(job_id)
    stem = Path(job.state()["filename"]).stem
    return FileResponse(job.dir / "mapping.enc", media_type="application/json", filename=f"{stem}.mapping.enc")


@app.get("/jobs/{job_id}/report")
def report(job_id: str):
    job = _done_job(job_id)
    return json.loads((job.dir / "report.json").read_text())


@app.post("/restore")
async def restore(file: UploadFile = File(...), mapping: UploadFile = File(...), passphrase: str = Form(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext == ".pdf":
        raise HTTPException(400, "PDF restore is not supported in v1; keep your original PDF")
    if ext not in SUPPORTED:
        raise HTTPException(415, f"Unsupported file type '{ext}'")
    try:
        mp = decrypt_json(await mapping.read(), passphrase)
    except BadPassphrase as e:
        raise HTTPException(400, str(e))
    tmpdir = Path(tempfile.mkdtemp(prefix="scrub-restore-"))
    os.chmod(tmpdir, 0o700)
    src = tmpdir / f"in{ext}"
    out = tmpdir / f"out{ext}"
    src.write_bytes(await file.read())
    try:
        h = open_handler(src, Options())
        n = restore_file(h, mp, out)
    except UnsupportedFile as e:
        raise HTTPException(400, str(e))
    name = mp.get("source", {}).get("name") or f"restored{ext}"
    stem = Path(name).stem

    def _rm():
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return FileResponse(out, media_type=_media(ext), filename=f"{stem}.restored{ext}",
                        headers={"X-Replacements": str(n)}, background=BackgroundTask(_rm))


@app.exception_handler(UnsupportedFile)
async def _unsupported(_req, exc):
    return Response(str(exc), status_code=400)
