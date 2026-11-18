"""The one place that talks to the `gliner` package (SPEC §4.1).

`NerModel` is a callable: (texts, labels, threshold) -> list[list[dict]],
each dict having start, end, text, label, score (character offsets).
Tests swap it for a dictionary-based fake.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Protocol

from .chunking import MAX_TOKENS, count_tokens

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("SCRUB_MODEL", "urchade/gliner_multi_pii-v1")


class NerModel(Protocol):
    name: str

    def __call__(self, texts: list[str], labels: list[str], threshold: float) -> list[list[dict]]: ...


def pick_device() -> str:
    want = os.environ.get("SCRUB_DEVICE", "cpu").lower()
    if want == "mps":
        try:
            import torch
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        log.warning("MPS requested but not available; using CPU")
    if want == "cuda":
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
    return "cpu"


class GlinerModel:
    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 16):
        from gliner import GLiNER  # heavy import, deferred

        self.name = model_name
        self.batch_size = batch_size
        self.device = pick_device()
        self.model = GLiNER.from_pretrained(model_name)
        self.model.to(self.device)
        self.model.eval()
        self.max_len = int(getattr(self.model.config, "max_len", 384))

    def _infer(self, texts: list[str], labels: list[str], threshold: float) -> list[list[dict]]:
        m = self.model
        if hasattr(m, "inference"):
            return m.inference(texts, labels, threshold=threshold, batch_size=self.batch_size)
        return m.batch_predict_entities(texts, labels, threshold=threshold)

    def __call__(self, texts: list[str], labels: list[str], threshold: float) -> list[list[dict]]:
        for t in texts:
            n = count_tokens(t)
            assert n <= min(MAX_TOKENS + 10, self.max_len), f"chunk of {n} tokens would be truncated"
        out: list[list[dict]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i: i + self.batch_size]
            try:
                res = self._infer(batch, labels, threshold)
            except RuntimeError as e:
                if self.device == "mps":
                    log.warning("MPS failed (%s); falling back to CPU", e)
                    self.device = "cpu"
                    self.model.to("cpu")
                    res = self._infer(batch, labels, threshold)
                else:
                    raise
            out.extend(res)
        return out


_model: GlinerModel | None = None
_lock = threading.Lock()


def get_model() -> GlinerModel:
    global _model
    with _lock:
        if _model is None:
            _model = GlinerModel()
        return _model


def model_loaded() -> bool:
    return _model is not None
