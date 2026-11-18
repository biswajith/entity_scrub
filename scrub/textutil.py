"""Text helpers shared by detection, apply, restore and the leak check.

The central piece is `FormMatcher`: it finds every occurrence of a set of
surface forms in a text, word-bounded, longest match first, without overlaps
(SPEC §5.3). It compiles the forms into a trie-shaped regex so thousands of
forms stay fast.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Iterator

_WS = re.compile(r"\s+")
_EDGE_PUNCT = "\"'“”‘’`.,;:!?()[]{}<>«»"
_POSSESSIVE = re.compile(r"(?:'s|’s|'|’)$", re.IGNORECASE)


def collapse_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def strip_possessive(s: str) -> tuple[str, str]:
    """'Smith's' -> ('Smith', "'s"). Only strips when something is left."""
    m = _POSSESSIVE.search(s)
    if m and m.start() > 1 and s[m.start() - 1].isalpha():
        return s[: m.start()], s[m.start():]
    return s, ""


def norm_key(s: str) -> str:
    """Grouping key: case-folded, whitespace collapsed, edge punctuation and
    possessive stripped. Used for display grouping only; mappings keep exact
    strings."""
    s = unicodedata.normalize("NFKC", s)
    s = collapse_ws(s).strip(_EDGE_PUNCT + " ")
    s, _ = strip_possessive(s)
    return s.casefold().strip(_EDGE_PUNCT + " ")


def has_letters(s: str) -> bool:
    return any(c.isalpha() for c in s)


def is_numeric_like(s: str) -> bool:
    """True for strings with no letters (numbers, dates, punctuation)."""
    return not has_letters(s)


# ---------------------------------------------------------------------------
# Case transfer
# ---------------------------------------------------------------------------

def _case_style(tok: str) -> str:
    letters = [c for c in tok if c.isalpha()]
    if not letters:
        return "none"
    if all(c.isupper() for c in letters):
        return "upper" if len(letters) > 1 else "title"
    if all(c.islower() for c in letters):
        return "lower"
    if letters[0].isupper() and all(c.islower() for c in letters[1:]):
        return "title"
    return "mixed"


def _apply_style(tok: str, style: str) -> str:
    if style == "upper":
        return tok.upper()
    if style == "lower":
        return tok.lower()
    if style == "title":
        return tok[:1].upper() + tok[1:].lower() if tok else tok
    return tok


def apply_case(fake: str, original: str) -> str:
    """Give `fake` the letter case of `original` (SPEC §5.1).

    'JOHN SMITH' + 'Peter Walker' -> 'PETER WALKER'. Token-wise when the
    token counts match, so 'john SMITH' -> 'peter WALKER'.
    """
    whole = _case_style(original)
    if whole in ("upper", "lower"):
        return _apply_style(fake, whole)
    o_toks = original.split()
    f_toks = fake.split(" ")
    if len(o_toks) == len(f_toks) and len(o_toks) > 1:
        return " ".join(_apply_style(f, _case_style(o)) if _case_style(o) in ("upper", "lower") else f
                        for f, o in zip(f_toks, o_toks))
    return fake


# ---------------------------------------------------------------------------
# Trie regex matcher
# ---------------------------------------------------------------------------

def _trie_pattern(words: Iterable[str]) -> str:
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = True

    def build(node: dict) -> str:
        end = "" in node
        branches = [re.escape(ch) + build(child) for ch, child in sorted(node.items()) if ch != ""]
        if not branches:
            return ""
        if len(branches) == 1:
            body = branches[0]
            alt = body if not end else f"(?:{body})?"
            return alt
        alt = "(?:" + "|".join(branches) + ")"
        return alt + ("?" if end else "")

    return build(trie)


class FormMatcher:
    """Finds word-bounded occurrences of `forms` in text.

    case_sensitive=False matches case variants too ('JOHN SMITH' for
    'John Smith'); callers resolve the variant via `lookup`.
    """

    def __init__(self, forms: Iterable[str], case_sensitive: bool = False):
        self.case_sensitive = case_sensitive
        forms = [f for f in set(forms) if f and f.strip()]
        self.forms = forms
        self._by_key: dict[str, str] = {}
        for f in sorted(forms, key=len, reverse=True):
            self._by_key.setdefault(self._key(f), f)
        if not forms:
            self._re = None
            return
        keys = {self._key(f) for f in forms}
        pat = _trie_pattern(keys)
        flags = 0 if case_sensitive else re.IGNORECASE
        self._re = re.compile(r"(?<!\w)(?:" + pat + r")(?!\w)", flags)

    def _key(self, s: str) -> str:
        return s if self.case_sensitive else s.lower()

    def lookup(self, matched: str) -> str | None:
        """Return the registered form a matched string belongs to."""
        return self._by_key.get(self._key(matched))

    def finditer(self, text: str) -> Iterator[tuple[int, int, str]]:
        if self._re is None or not text:
            return
        for m in self._re.finditer(text):
            if m.end() > m.start():
                yield m.start(), m.end(), m.group(0)

    def search(self, text: str) -> bool:
        return self._re is not None and bool(self._re.search(text or ""))


def contains_word_bounded(haystack: str, needle: str) -> bool:
    """Case-insensitive word-bounded containment."""
    if not needle:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack, re.IGNORECASE) is not None


_WORD = re.compile(r"\w+", re.UNICODE)


def words(s: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(s)]
