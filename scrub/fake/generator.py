"""Fake value generation with shape preservation and collision rules (SPEC §5.1)."""
from __future__ import annotations

import random
import re
import string

from faker import Faker

from ..models import ADDRESS, LOCATION, ORG, PERSON
from ..textutil import apply_case, contains_word_bounded, strip_possessive, words

TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "mx", "rev"}
ORG_SUFFIXES = [
    "Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Incorporated", "Corporation", "Company", "Limited",
    "Holdings", "Group", "GmbH", "Inc.", "Inc", "Corp.", "Corp", "Co.", "Ltd.", "Ltd", "LLC", "L.L.C.",
    "LLP", "PLC", "plc", "AG", "SA", "S.A.", "BV", "B.V.", "NV", "N.V.", "Pty Ltd", "SARL", "SAS", "KG", "Oy", "AB",
]
_SUFFIX_RE = re.compile(
    r"[\s,]+(" + "|".join(re.escape(s) for s in sorted(ORG_SUFFIXES, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)


def split_title(name: str) -> tuple[str, str]:
    """'Mr. John Smith' -> ('Mr. ', 'John Smith')."""
    m = re.match(r"^((?:" + "|".join(TITLES) + r")\.?\s+)", name, re.IGNORECASE)
    if m:
        return m.group(1), name[m.end():]
    return "", name


def split_org_suffix(name: str) -> tuple[str, str]:
    """'Acme Corp.' -> ('Acme', ' Corp.')."""
    m = _SUFFIX_RE.search(name)
    if m and m.start() > 0:
        return name[: m.start()], name[m.start():]
    return name, ""


def _rand_like(s: str, rng: random.Random, keep_prefix: int = 0) -> str:
    """Format-preserving random: digits->digits, letters->letters, rest kept."""
    out = []
    for i, ch in enumerate(s):
        if i < keep_prefix:
            out.append(ch)
        elif ch.isdigit():
            out.append(rng.choice(string.digits))
        elif ch.isupper():
            out.append(rng.choice(string.ascii_uppercase))
        elif ch.islower():
            out.append(rng.choice(string.ascii_lowercase))
        else:
            out.append(ch)
    return "".join(out)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _luhn_fix(digits: list[str]) -> list[str]:
    for d in range(10):
        digits[-1] = str(d)
        if luhn_ok("".join(digits)):
            return digits
    return digits


_COMMON_WORDS = {w.casefold().strip(".") for w in ORG_SUFFIXES for w in w.split()} | TITLES | {
    "and", "the", "of", "for", "de", "la", "le", "von", "van", "der", "street", "st", "road", "rd",
    "avenue", "ave", "suite", "city", "lake", "port", "north", "south", "east", "west", "new"}


class CollisionChecker:
    """Enforces the collision rules of SPEC §5.1.

    A fake is rejected when it:
      * appears word-bounded anywhere in the source text,
      * equals, contains, or is word-bounded inside another fake or original,
      * is already used.
    """

    def __init__(self, source_texts: list[str]):
        self.source_words: set[str] = set()
        self._texts = source_texts
        for t in source_texts:
            self.source_words.update(words(t))
        self.used: set[str] = set()        # casefolded fakes + originals
        self._index: dict[str, set[str]] = {}  # word -> strings containing it
        self.original_words: set[str] = set()  # name words of real entities

    def _register(self, s: str) -> None:
        k = s.casefold()
        self.used.add(k)
        for w in set(words(s)):
            self._index.setdefault(w, set()).add(k)

    def add_original(self, s: str, name_like: bool = True) -> None:
        self._register(s)
        if name_like:
            self.original_words.update(w for w in words(s) if len(w) >= 3 and w not in _COMMON_WORDS)

    def add_fake(self, s: str) -> None:
        self._register(s)

    def in_source(self, fake: str) -> bool:
        ws = words(fake)
        if not ws or not all(w in self.source_words for w in ws):
            return False
        return any(contains_word_bounded(t, fake) for t in self._texts)

    def problem(self, fake: str, original: str | None = None) -> str | None:
        """Return a reason string if `fake` violates a rule, else None."""
        k = fake.casefold()
        if not fake.strip():
            return "fake value is empty"
        if original is not None and k == original.casefold():
            return "fake equals the original"
        if k in self.used:
            return "fake is already used by another value"
        if self.in_source(fake):
            return "fake appears in the source file"
        if any(w in self.original_words for w in words(fake)):
            return "fake reuses a word from a real name"
        candidates: set[str] = set()
        for w in set(words(fake)):
            candidates |= self._index.get(w, set())
        for other in candidates:
            if contains_word_bounded(other, fake) or contains_word_bounded(fake, other):
                return "fake overlaps another fake or original"
        return None


class FakeGenerator:
    def __init__(self, locale: str, seed: int, checker: CollisionChecker):
        self.faker = Faker(locale)
        self.faker.seed_instance(seed)
        self.rng = random.Random(seed)
        self.checker = checker
        from faker.providers.address.en_US import Provider as _AddressProvider
        self._countries = {c.casefold() for c in _AddressProvider.countries}

    # -- public -------------------------------------------------------------
    def make(self, type_: str, original: str, linked_names: dict | None = None) -> str:
        """Generate a fake for `original`, retrying until collision-free."""
        last = ""
        # names: draw a few valid candidates and keep the one closest in
        # length to the original, so layouts (PDF lines, column widths) hold
        want = 6 if type_ in (PERSON, ORG, LOCATION, ADDRESS) else 1
        candidates: list[str] = []
        for attempt in range(60):
            fake = self._make_once(type_, original, linked_names, attempt)
            fake = apply_case(fake, original)
            last = fake
            if fake not in candidates and self.checker.problem(fake, original) is None:
                candidates.append(fake)
                if len(candidates) >= want:
                    break
        if candidates:
            fake = min(candidates, key=lambda c: abs(len(c) - len(original)))
            self.checker.add_fake(fake)
            return fake
        # extremely unlikely: fall back to a tagged random token
        fake = f"{last} {self.rng.randint(1000, 9999)}"
        self.checker.add_fake(fake)
        return fake

    # -- per type -----------------------------------------------------------
    def _make_once(self, type_: str, original: str, linked: dict | None, attempt: int) -> str:
        f = self.faker
        if type_ == PERSON:
            return self.person(original)
        if type_ == ORG:
            return self.org(original)
        if type_ == LOCATION:
            core, poss = strip_possessive(original)
            if core.casefold() in self._countries:
                return f.country() + poss
            return f.city() + poss
        if type_ == ADDRESS:
            if "," in original or "\n" in original:
                return f.address().replace("\n", ", ")
            return f.street_address()
        if type_ == "email":
            return self.email(original, linked or {}, attempt)
        if type_ == "phone":
            return self.phone(original)
        if type_ == "credit_card":
            return self.card(original)
        if type_ == "iban":
            return _rand_like(original, self.rng, keep_prefix=2)
        if type_ == "ip_address":
            return f.ipv4_private()
        if type_ == "url":
            m = re.match(r"^(https?://)?(www\.)?([^/\s]+)(.*)$", original, re.IGNORECASE)
            scheme = (m.group(1) or "") if m else ""
            www = (m.group(2) or "") if m else ""
            path = m.group(4) if m else ""
            path = _rand_like(path, self.rng) if path else ""
            return f"{scheme}{www}{f.domain_word()}-{f.domain_word()}.example{path}"
        # ssn, custom ids and anything else: same shape, random characters
        return _rand_like(original, self.rng)

    def person(self, original: str) -> str:
        f = self.faker
        core, poss = strip_possessive(original)
        title, rest = split_title(core)
        if "," in rest:  # "Smith, John"
            last, _, first = rest.partition(",")
            return f"{title}{f.last_name()}, {self._name_tok(first.strip(), f.first_name)}{poss}"
        toks = rest.split()
        if not toks:
            return original
        if len(toks) == 1 and 2 <= len(toks[0].replace(".", "")) <= 3 and toks[0].replace(".", "").isupper():
            # initials like "JD" or "J.D."
            return title + "".join(self.rng.choice(string.ascii_uppercase) if c.isalpha() else c
                                   for c in toks[0]) + poss
        if len(toks) == 1:
            gen = f.last_name if title else f.first_name
            return f"{title}{self._name_tok(toks[0], gen)}{poss}"
        out = [self._name_tok(toks[0], f.first_name)]
        for mid in toks[1:-1]:
            out.append(self._name_tok(mid, f.first_name))
        out.append(self._name_tok(toks[-1], f.last_name))
        return f"{title}{' '.join(out)}{poss}"

    def _name_tok(self, tok: str, gen) -> str:
        bare = tok.rstrip(".")
        if len(bare) == 1:  # an initial: keep it an initial, different letter
            letter = self.rng.choice(string.ascii_uppercase)
            return letter + tok[len(bare):]
        return gen()

    def org(self, original: str) -> str:
        core, poss = strip_possessive(original)
        _, suffix = split_org_suffix(core)
        base = self.faker.company()
        base, _ = split_org_suffix(base)
        base = base.split(",")[0].strip()
        return f"{base}{suffix}{poss}"

    def email(self, original: str, linked: dict, attempt: int) -> str:
        """`linked` maps casefolded real name tokens -> fake tokens."""
        local, _, _domain = original.partition("@")
        new_local = local
        replaced = False
        for real, fake in sorted(linked.items(), key=lambda kv: -len(kv[0])):
            if len(real) >= 3 and real in new_local.casefold():
                idx = new_local.casefold().index(real)
                new_local = new_local[:idx] + fake.lower() + new_local[idx + len(real):]
                replaced = True
        if not replaced:
            new_local = self.faker.user_name()
        if attempt:
            new_local += str(attempt)
        return f"{new_local}@example.com"

    def phone(self, original: str) -> str:
        keep = 0
        m = re.match(r"^\+\d{1,3}", original)
        if m:
            keep = m.end()
        out = list(_rand_like(original, self.rng, keep_prefix=keep))
        # keep a leading trunk '0' so the number still looks local
        for i, ch in enumerate(original):
            if i >= keep and ch.isdigit():
                if ch == "0":
                    out[i] = "0"
                elif out[i] == "0":
                    out[i] = str(self.rng.randint(2, 9))
                break
        return "".join(out)

    def card(self, original: str) -> str:
        positions = [i for i, c in enumerate(original) if c.isdigit()]
        digits = [self.rng.choice(string.digits) for _ in positions]
        digits[0] = original[positions[0]]  # keep network prefix digit (4=Visa etc.)
        digits = _luhn_fix(digits)
        out = list(original)
        for p, d in zip(positions, digits):
            out[p] = d
        return "".join(out)
