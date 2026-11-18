"""Plain data types shared across the library."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

PERSON = "person"
ORG = "organization"
LOCATION = "location"
ADDRESS = "address"
NER_TYPES = [PERSON, ORG, LOCATION, ADDRESS]
REGEX_TYPES = ["email", "phone", "url", "ip_address", "iban", "credit_card", "ssn"]
ALL_TYPES = NER_TYPES + REGEX_TYPES


@dataclass
class Unit:
    """One text location in a file (SPEC §3 step 2).

    id       stable locator, deterministic across re-parses of the same file
    text     the text as it appears
    context  header/label prepended for NER context (spreadsheets), or None
    kind     cell | header | numeric | para | meta | attr | literal | pdf_block | readonly
    """
    id: str
    text: str
    context: str | None = None
    kind: str = "para"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Unit":
        return Unit(**d)


@dataclass
class Span:
    start: int
    end: int
    text: str
    type: str
    score: float = 1.0
    source: str = "gliner"  # gliner | regex | user | column


@dataclass
class Repl:
    start: int
    end: int
    original: str
    new: str


@dataclass
class Column:
    """A spreadsheet column for the column view (SPEC §3 step 4)."""
    key: str
    header: str
    unit_ids: list[str] = field(default_factory=list)


@dataclass
class Options:
    types: list[str] = field(default_factory=lambda: list(ALL_TYPES))
    threshold: float = 0.5
    locale: str = "en_US"
    phone_region: str = "US"
    extra_regions: list[str] = field(default_factory=list)
    custom_patterns: list[dict] = field(default_factory=list)  # [{"name":..., "pattern":...}]
    csv_encoding: str | None = None
    csv_delimiter: str | None = None
    second_pass: bool = True
    seed: int | None = None

    @staticmethod
    def from_dict(d: dict | None) -> "Options":
        d = dict(d or {})
        known = {k: v for k, v in d.items() if k in Options.__dataclass_fields__}
        return Options(**known)

    def to_dict(self) -> dict:
        return asdict(self)
