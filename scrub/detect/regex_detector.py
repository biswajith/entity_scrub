"""Regex / rule-based detectors for contact details and IDs (SPEC §4.2)."""
from __future__ import annotations

import ipaddress
import re

import phonenumbers

from ..fake.generator import luhn_ok
from ..models import Options, Span

_B = r"(?<!\w)"
_E = r"(?!\w)"

EMAIL = re.compile(_B + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}" + _E)
URL = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
IPV4 = re.compile(_B + r"(?:\d{1,3}\.){3}\d{1,3}" + _E)
IBAN = re.compile(_B + r"[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?" + _E)
CARD = re.compile(_B + r"\d(?:[ -]?\d){12,18}" + _E)
SSN = re.compile(_B + r"(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}" + _E)


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    r = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in r)
    return int(num) % 97 == 1


def _ip_ok(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return not ip.is_unspecified


class RegexDetector:
    def __init__(self, opts: Options):
        self.types = set(opts.types)
        self.regions = [opts.phone_region or "US"] + [r for r in opts.extra_regions if r]
        self.custom: list[tuple[str, re.Pattern]] = []
        for c in opts.custom_patterns:
            name, pat = c.get("name"), c.get("pattern")
            if name and pat:
                self.custom.append((name, re.compile(_B + "(?:" + pat + ")" + _E)))

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        t = self.types

        def add(rx: re.Pattern, type_: str, ok=None, strip_tail: bool = False):
            for m in rx.finditer(text):
                s, e = m.start(), m.end()
                val = m.group(0)
                if strip_tail:
                    stripped = val.rstrip(".,;:!?)]}'\"")
                    e -= len(val) - len(stripped)
                    val = stripped
                if ok is None or ok(val):
                    spans.append(Span(s, e, val, type_, 1.0, "regex"))

        if "email" in t:
            add(EMAIL, "email")
        if "url" in t:
            add(URL, "url", strip_tail=True)
        if "ip_address" in t:
            add(IPV4, "ip_address", _ip_ok)
        if "iban" in t:
            add(IBAN, "iban", _iban_ok)
        if "credit_card" in t:
            add(CARD, "credit_card", lambda v: 13 <= len(re.sub(r"\D", "", v)) <= 19 and luhn_ok(re.sub(r"\D", "", v)))
        if "ssn" in t:
            add(SSN, "ssn")
        if "phone" in t and any(c.isdigit() for c in text):
            seen = set()
            for region in self.regions:
                try:
                    matcher = phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID)
                    for m in matcher:
                        if (m.start, m.end) not in seen:
                            seen.add((m.start, m.end))
                            spans.append(Span(m.start, m.end, m.raw_string, "phone", 1.0, "regex"))
                except Exception:
                    continue
        for name, rx in self.custom:
            add(rx, name)
        return spans
