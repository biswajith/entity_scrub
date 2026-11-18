"""Split long text into windows GLiNER can take without truncation (SPEC §4.1).

Token counting follows GLiNER's default whitespace splitter: words (with
inner '-' or '_') and every other non-space character count as one token.
"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r"\w+(?:[-_]\w+)*|\S")
MAX_TOKENS = 300
OVERLAP = 50
_SENT_END = {".", "!", "?", ";", "\n"}


def count_tokens(text: str) -> int:
    return sum(1 for _ in TOKEN_RE.finditer(text))


def chunk(text: str, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    """Return [(char_offset, chunk_text)] covering `text` with overlap."""
    toks = [(m.start(), m.end(), m.group(0)) for m in TOKEN_RE.finditer(text)]
    if len(toks) <= max_tokens:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(toks):
        j = min(i + max_tokens, len(toks))
        if j < len(toks):
            # prefer to end on a sentence boundary in the last third of the window
            for k in range(j - 1, i + (2 * max_tokens) // 3, -1):
                if toks[k][2] in _SENT_END:
                    j = k + 1
                    break
        s, e = toks[i][0], toks[j - 1][1]
        out.append((s, text[s:e]))
        if j >= len(toks):
            break
        i = max(j - overlap, i + 1)
    return out
