"""Run regex + GLiNER over text units and merge the spans (SPEC §4)."""
from __future__ import annotations

import time
from typing import Callable

from ..models import NER_TYPES, Options, Span, Unit
from ..textutil import is_numeric_like
from .chunking import chunk
from .regex_detector import RegexDetector

ProgressFn = Callable[[int, int, str], None]


def merge_spans(spans: list[Span]) -> list[Span]:
    """Resolve overlaps (SPEC §4.3): containment wins; on partial overlap
    regex wins and the GLiNER span is trimmed to its non-overlapping part."""
    # 1) drop spans fully contained in another (prefer the longer; tie -> regex)
    spans = sorted(spans, key=lambda s: (s.start, -(s.end - s.start), s.source != "regex"))
    kept: list[Span] = []
    for s in spans:
        if any(k.start <= s.start and s.end <= k.end for k in kept):
            continue
        kept = [k for k in kept if not (s.start <= k.start and k.end <= s.end)]
        kept.append(s)
    # 2) partial overlaps
    kept.sort(key=lambda s: s.start)
    out: list[Span] = []
    for s in kept:
        if out and s.start < out[-1].end:
            prev = out[-1]
            if prev.source == "regex" and s.source != "regex":
                s = Span(prev.end, s.end, s.text[prev.end - s.start:], s.type, s.score, s.source)
            elif s.source == "regex" and prev.source != "regex":
                cut = s.start - prev.start
                out[-1] = Span(prev.start, s.start, prev.text[:cut], prev.type, prev.score, prev.source)
            else:  # same source: keep the higher score
                if s.score > prev.score:
                    out[-1] = s
                continue
        out.append(s)
    # trim whitespace and drop fragments shorter than 2 chars
    res = []
    for s in out:
        lead = len(s.text) - len(s.text.lstrip())
        trail = len(s.text) - len(s.text.rstrip())
        txt = s.text.strip()
        if len(txt) >= 2:
            res.append(Span(s.start + lead, s.end - trail, txt, s.type, s.score, s.source))
    return res


def detect_units(
    units: list[Unit],
    opts: Options,
    ner=None,
    progress: ProgressFn | None = None,
) -> dict[str, list[Span]]:
    """Return {unit_id: [Span]} with offsets relative to unit.text."""
    regex = RegexDetector(opts)
    text_by_id = {u.id: u.text for u in units}
    labels = [t for t in opts.types if t in NER_TYPES]
    result: dict[str, list[Span]] = {u.id: [] for u in units}

    for u in units:
        if u.kind != "readonly":
            result[u.id].extend(regex.detect(u.text))

    # NER: dedupe identical (context, text) inputs, chunk, batch.
    ner_inputs: dict[str, list[tuple[str, int]]] = {}  # model input -> [(unit_id, prefix_len)]
    if ner is not None and labels:
        for u in units:
            if u.kind in ("numeric", "readonly", "header") or len(u.text.strip()) < 2 or is_numeric_like(u.text):
                continue
            prefix = f"{u.context}: " if u.context else ""
            ner_inputs.setdefault(prefix + u.text, []).append((u.id, len(prefix)))

    texts = list(ner_inputs)
    chunks: list[tuple[int, int, str]] = []  # (text_idx, offset, chunk_text)
    for ti, t in enumerate(texts):
        for off, c in chunk(t):
            chunks.append((ti, off, c))

    total = len(chunks)
    if progress:
        progress(0, total, "detecting")
    step = 64
    t0 = time.time()
    for i in range(0, total, step):
        batch = chunks[i: i + step]
        preds = ner([c for _, _, c in batch], labels, opts.threshold)
        for (ti, off, _c), ents in zip(batch, preds):
            for ent in ents:
                s, e = ent["start"] + off, ent["end"] + off
                for uid, plen in ner_inputs[texts[ti]]:
                    us, ue = s - plen, e - plen
                    if ue <= 0:
                        continue  # entity inside the header prefix
                    us = max(us, 0)
                    utext = text_by_id[uid]
                    result[uid].append(Span(us, ue, utext[us:ue], ent["label"], float(ent["score"]), "gliner"))
        if progress:
            done = min(i + step, total)
            eta = (time.time() - t0) / done * (total - done) if done else 0
            progress(done, total, f"detecting (eta {int(eta)}s)")

    return {uid: merge_spans(sp) for uid, sp in result.items()}

