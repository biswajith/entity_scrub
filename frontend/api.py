"""Thin HTTP client for the backend."""
from __future__ import annotations

import json
import os

import requests

BASE = os.environ.get("SCRUB_API", "http://127.0.0.1:8765")
S = requests.Session()


class ApiError(Exception):
    pass


def _check(r: requests.Response) -> requests.Response:
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ApiError(f"{r.status_code}: {detail}")
    return r


def health() -> dict | None:
    try:
        return S.get(f"{BASE}/health", timeout=3).json()
    except requests.RequestException:
        return None


def jobs() -> list[dict]:
    return _check(S.get(f"{BASE}/jobs", timeout=10)).json()


def create(name: str, data: bytes, options: dict) -> dict:
    return _check(S.post(f"{BASE}/jobs", files={"file": (name, data)},
                         data={"options": json.dumps(options)}, timeout=120)).json()


def state(job_id: str) -> dict:
    return _check(S.get(f"{BASE}/jobs/{job_id}", timeout=10)).json()


def entities(job_id: str) -> dict:
    return _check(S.get(f"{BASE}/jobs/{job_id}/entities", timeout=60)).json()


def edit(job_id: str, edits: dict) -> dict:
    return _check(S.put(f"{BASE}/jobs/{job_id}/entities", json=edits, timeout=300)).json()


def apply(job_id: str, passphrase: str) -> dict:
    return _check(S.post(f"{BASE}/jobs/{job_id}/apply", json={"passphrase": passphrase}, timeout=30)).json()


def hits(job_id: str) -> dict:
    return _check(S.get(f"{BASE}/jobs/{job_id}/hits", timeout=10)).json()


def acknowledge(job_id: str) -> dict:
    return _check(S.post(f"{BASE}/jobs/{job_id}/acknowledge", timeout=10)).json()


def report(job_id: str) -> dict:
    return _check(S.get(f"{BASE}/jobs/{job_id}/report", timeout=10)).json()


def download(job_id: str, what: str) -> tuple[bytes, str]:
    r = _check(S.get(f"{BASE}/jobs/{job_id}/{what}", timeout=120))
    cd = r.headers.get("content-disposition", "")
    name = cd.split("filename=")[-1].strip('"') if "filename=" in cd else what
    return r.content, name


def delete(job_id: str) -> None:
    _check(S.delete(f"{BASE}/jobs/{job_id}", timeout=10))


def restore(name: str, data: bytes, mapping: bytes, passphrase: str) -> tuple[bytes, str, int]:
    r = _check(S.post(f"{BASE}/restore", files={"file": (name, data), "mapping": ("mapping.enc", mapping)},
                      data={"passphrase": passphrase}, timeout=300))
    cd = r.headers.get("content-disposition", "")
    fname = cd.split("filename=")[-1].strip('"') if "filename=" in cd else name
    return r.content, fname, int(r.headers.get("x-replacements", "0"))
