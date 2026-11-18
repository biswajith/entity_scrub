"""A job folder and the steps that run in it (SPEC §2-§3).

~/.entity-scrub/jobs/<id>/
    state.json      status, progress, options, file info
    source.<ext>    the original (deleted when the job finishes)
    units.jsonl     extracted units (deleted when the job finishes)
    entities.json   review state (deleted when the job finishes)
    output.<ext>    the scrubbed file
    mapping.enc     encrypted mapping
    report.json     counts and locators only, never original strings
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import apply as ap
from .entities import apply_edits, build_state, public_view
from .fake.crypto import check_passphrase, encrypt_json
from .formats import open_handler
from .models import Options, Unit

HOME = Path(os.environ.get("SCRUB_HOME", Path.home() / ".entity-scrub"))
JOBS = HOME / "jobs"
MAX_BYTES = int(os.environ.get("SCRUB_MAX_MB", "20")) * 1024 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jobs_dir() -> Path:
    JOBS.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    os.chmod(JOBS, 0o700)
    return JOBS


class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = jobs_dir() / job_id
        if not self.dir.is_dir():
            raise KeyError(job_id)

    # -- creation -------------------------------------------------------------
    @classmethod
    def create(cls, filename: str, data: bytes, opts: Options) -> "Job":
        if len(data) > MAX_BYTES:
            raise ValueError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB")
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        d = jobs_dir() / job_id
        d.mkdir(mode=0o700)
        ext = Path(filename).suffix.lower()
        (d / f"source{ext}").write_bytes(data)
        if opts.seed is None:
            opts.seed = int.from_bytes(os.urandom(4), "big")
        job = cls(job_id)
        job.save_state({
            "id": job_id, "filename": filename, "ext": ext, "status": "queued", "progress": [0, 0, ""],
            "error": None, "options": opts.to_dict(), "created": time.time(), "info": {},
            "acknowledged": False, "blocking": 0,
        })
        return job

    # -- state ----------------------------------------------------------------
    def state(self) -> dict:
        return json.loads((self.dir / "state.json").read_text())

    def save_state(self, st: dict) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(st))
        tmp.replace(self.dir / "state.json")

    def update(self, **kw) -> dict:
        st = self.state()
        st.update(kw)
        self.save_state(st)
        return st

    def opts(self) -> Options:
        return Options.from_dict(self.state()["options"])

    @property
    def source(self) -> Path:
        return self.dir / f"source{self.state()['ext']}"

    @property
    def output(self) -> Path:
        return self.dir / f"output{self.state()['ext']}"

    def _units(self) -> list[Unit]:
        with open(self.dir / "units.jsonl") as f:
            return [Unit.from_dict(json.loads(line)) for line in f]

    def _entities(self) -> dict:
        return json.loads((self.dir / "entities.json").read_text())

    def _save_entities(self, ent: dict) -> None:
        (self.dir / "entities.json").write_text(json.dumps(ent))

    # -- steps ----------------------------------------------------------------
    def detect(self, ner) -> None:
        try:
            self.update(status="detecting", progress=[0, 0, "reading file"])
            opts = self.opts()
            h = open_handler(self.source, opts)
            units = h.units()
            cols = h.columns()
            with open(self.dir / "units.jsonl", "w") as f:
                for u in units:
                    f.write(json.dumps(u.to_dict()) + "\n")

            def progress(done, total, msg):
                self.update(progress=[done, total, msg])

            from .detect.pipeline import detect_units
            det = detect_units(units, opts, ner, progress)
            ent = build_state(units, det, cols, opts.locale, opts.seed or 0)
            self._save_entities(ent)
            self.update(status="review", progress=[1, 1, "ready for review"],
                        info={**h.info, "units": len(units)})
        except Exception as e:  # noqa: BLE001
            self.fail(e)

    def entities(self) -> dict:
        return public_view(self._entities())

    def edit(self, edits: dict) -> list[str]:
        st = self.state()
        if st["status"] != "review":
            raise RuntimeError(f"Job is '{st['status']}', not in review")
        units = self._units()
        ent = self._entities()
        warnings = apply_edits(units, ent, edits)
        self._save_entities(ent)
        return warnings

    def apply(self, passphrase: str, ner=None) -> None:
        """Write the output, run the leak check, encrypt the mapping, clean up."""
        check_passphrase(passphrase)
        t0 = time.time()
        try:
            self.update(status="applying", progress=[0, 3, "writing output"])
            opts = self.opts()
            units = self._units()
            ent = self._entities()
            fmap = ap.form_map(ent)
            repls, occurrences = ap.compute_repls(units, fmap)
            h = open_handler(self.source, opts)
            h.write(repls, self.output)

            self.update(progress=[1, 3, "leak check"])
            hits = ap.leak_check(h, self.output, fmap)
            warnings = []
            if opts.second_pass and ner is not None:
                self.update(progress=[2, 3, "second detection pass"])
                warnings = ap.second_pass(open_handler(self.output, opts), opts, ner, fmap)

            mapping = {
                "version": 1, "job_id": self.id,
                "source": {"name": self.state()["filename"], "sha256": _sha256(self.source)},
                "output_sha256": _sha256(self.output),
                "model": os.environ.get("SCRUB_MODEL", "urchade/gliner_multi_pii-v1"),
                "groups": ap.mapping_doc(fmap), "occurrences": occurrences,
            }
            (self.dir / "mapping.enc").write_bytes(encrypt_json(mapping, passphrase))

            by_type: dict[str, int] = {}
            for o in occurrences:
                t = fmap[o["original"]][1]
                by_type[t] = by_type.get(t, 0) + 1
            st = self.state()
            report = {
                "job_id": self.id, "file": st["filename"], "format": st["ext"].lstrip("."),
                "replacements_by_type": by_type, "replacements": len(occurrences),
                "entities": len({id(v[2]) for v in fmap.values()}),
                "residual_hits": [{k: v for k, v in hit.items() if k != "text"} for hit in hits],
                "second_pass_warnings": [{k: v for k, v in w.items() if k != "text"} for w in warnings],
                "unsupported": h.info.get("unsupported", []) + st["info"].get("unsupported", []),
                "notes": h.info.get("notes", []),
                "revisions_accepted": h.info.get("revisions_accepted", 0),
                "seconds": round(time.time() - t0, 1),
            }
            (self.dir / "report.json").write_text(json.dumps(report, indent=1))
            # original strings for the UI only (not in the downloadable report)
            (self.dir / "review_hits.json").write_text(json.dumps({"hits": hits, "warnings": warnings}))
            self.update(status="done", progress=[3, 3, "done"], blocking=len(hits),
                        acknowledged=len(hits) == 0)
        except Exception as e:  # noqa: BLE001
            self.fail(e)
            return
        self.cleanup()

    def fail(self, e: Exception) -> None:
        self.update(status="failed", error=f"{type(e).__name__}: {e}")
        self.cleanup()

    def cleanup(self) -> None:
        """Delete the original and intermediate files (SPEC §2)."""
        for name in ("units.jsonl", "entities.json"):
            (self.dir / name).unlink(missing_ok=True)
        for p in self.dir.glob("source.*"):
            p.unlink(missing_ok=True)

    def ui_hits(self) -> dict:
        p = self.dir / "review_hits.json"
        return json.loads(p.read_text()) if p.exists() else {"hits": [], "warnings": []}

    def delete(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def list_jobs() -> list[dict]:
    out = []
    for d in sorted(jobs_dir().iterdir(), reverse=True):
        try:
            st = json.loads((d / "state.json").read_text())
            out.append({k: st[k] for k in ("id", "filename", "status", "created")})
        except Exception:  # noqa: BLE001
            continue
    return out


def purge(unfinished_hours: float = 24, all_days: float = 7) -> int:
    now = time.time()
    n = 0
    for d in jobs_dir().iterdir():
        try:
            st = json.loads((d / "state.json").read_text())
            age = now - st["created"]
            if age > all_days * 86400 or (st["status"] not in ("done", "failed") and age > unfinished_hours * 3600):
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except Exception:  # noqa: BLE001
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
