"""Format handler interface (SPEC §6).

A handler enumerates every text location of a file as `Unit`s with stable
ids, and writes a copy with replacements applied to those units. Handlers
re-parse the source on every call, so ids must be deterministic.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Column, Options, Repl, Unit


class UnsupportedFile(Exception):
    pass


class Handler:
    fmt = ""
    can_restore = True

    def __init__(self, path: str | Path, opts: Options | None = None):
        self.path = Path(path)
        self.opts = opts or Options()
        self.info: dict = {"unsupported": [], "notes": []}

    def units(self) -> list[Unit]:
        raise NotImplementedError

    def columns(self) -> list[Column]:
        return []

    def write(self, repls: dict[str, list[Repl]], out_path: str | Path) -> None:
        raise NotImplementedError

    def leak_texts(self, path: str | Path) -> list[tuple[str, str]]:
        """Independent re-extraction for the leak check: [(locator, text)]."""
        raise NotImplementedError


def splice(text: str, repls: list[Repl]) -> str:
    out = []
    pos = 0
    for r in sorted(repls, key=lambda r: r.start):
        out.append(text[pos:r.start])
        out.append(r.new)
        pos = r.end
    out.append(text[pos:])
    return "".join(out)


def splice_runs(texts: list[str | None], repls: list[Repl], writable: list[bool] | None = None) -> list[str | None]:
    """Apply replacements to text spread over runs, keeping run boundaries.

    The replacement goes into the run where the span starts; the rest of the
    span is trimmed from the following runs (SPEC §6 DOCX). Read-only runs
    (tabs, breaks) are left untouched. Returns the new run texts."""
    runs = [t or "" for t in texts]
    writable = writable or [True] * len(runs)
    bounds = []
    pos = 0
    for t in runs:
        bounds.append((pos, pos + len(t)))
        pos += len(t)
    new = list(runs)
    # work right-to-left so earlier offsets stay valid
    for r in sorted(repls, key=lambda r: r.start, reverse=True):
        first = True
        for i, (s, e) in enumerate(bounds):
            if e <= r.start or s >= r.end or (s == e):
                continue
            if not writable[i]:
                continue
            a, b = max(r.start, s) - s, min(r.end, e) - s
            cur = new[i]
            # later (right-hand) replacements only changed cur after b, so the
            # prefix up to b is still original text
            head, tail = cur[:a], cur[b:]
            if first:
                new[i] = head + r.new + tail
                first = False
            else:
                new[i] = head + tail
        if first:
            # span covered only read-only runs: put text in the nearest writable run before
            for i in range(len(bounds) - 1, -1, -1):
                if writable[i] and bounds[i][1] <= r.start:
                    new[i] = new[i] + r.new
                    break
    return [n if (t is not None or n) else t for n, t in zip(new, texts)]
