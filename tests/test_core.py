import pytest

from scrub.fake.crypto import BadPassphrase, decrypt_json, encrypt_json
from scrub.fake.generator import CollisionChecker, FakeGenerator, luhn_ok
from scrub.formats.base import splice_runs
from scrub.detect.chunking import MAX_TOKENS, chunk, count_tokens
from scrub.detect.pipeline import merge_spans
from scrub.detect.regex_detector import RegexDetector
from scrub.models import Options, Repl, Span
from scrub.textutil import FormMatcher, apply_case, norm_key


def test_matcher_word_bounded_and_longest_first():
    m = FormMatcher(["Ann", "John Smith", "John"])
    hits = [x[2] for x in m.finditer("Ann and Annual; John Smith met John.")]
    assert hits == ["Ann", "John Smith", "John"]


def test_matcher_case_insensitive_lookup():
    m = FormMatcher(["John Smith"])
    (s, e, t), = list(m.finditer("JOHN SMITH said"))
    assert t == "JOHN SMITH" and m.lookup(t) == "John Smith"


def test_matcher_case_sensitive():
    m = FormMatcher(["Peter Walker"], case_sensitive=True)
    assert not list(m.finditer("PETER WALKER"))


def test_apply_case():
    assert apply_case("Peter Walker", "JOHN SMITH") == "PETER WALKER"
    assert apply_case("Peter Walker", "john smith") == "peter walker"
    assert apply_case("Peter Walker", "john SMITH") == "peter WALKER"
    assert apply_case("Peter Walker", "John Smith") == "Peter Walker"


def test_norm_key():
    assert norm_key("  John  Smith's ") == "john smith"
    assert norm_key('"Acme Corp".') == "acme corp"


def test_splice_runs_keeps_boundaries():
    runs = ["Meeting with ", "Jo", "hn Sm", "ith of Acme"]
    text = "".join(runs)
    s = text.index("John Smith")
    new = splice_runs(runs, [Repl(s, s + 10, "John Smith", "Peter Walker")])
    assert "".join(new) == "Meeting with Peter Walker of Acme"
    assert new[0] == "Meeting with " and new[1] == "Peter Walker" and new[2] == ""


def test_splice_runs_multiple_in_one_run():
    runs = ["Ann met Bob"]
    new = splice_runs(runs, [Repl(0, 3, "Ann", "Kim"), Repl(8, 11, "Bob", "Alexander")])
    assert new == ["Kim met Alexander"]


def test_splice_runs_readonly_runs_kept():
    runs = ["John", "\t", "Smith"]
    new = splice_runs(runs, [Repl(0, 4, "John", "Pete"), Repl(5, 10, "Smith", "Walker")], [True, False, True])
    assert new == ["Pete", "\t", "Walker"]


def test_collision_rules():
    chk = CollisionChecker(["Peter Walker owns the shop", "John Smith"])
    chk.add_original("John Smith")
    assert chk.problem("Peter Walker") == "fake appears in the source file"
    assert chk.problem("Smith Industries") == "fake reuses a word from a real name"
    chk.add_fake("Mary Jones")
    assert chk.problem("Mary Jones") is not None
    assert chk.problem("Jones") is not None  # word-bounded inside another fake
    assert chk.problem("Alice Brown") is None


def test_fake_shapes():
    chk = CollisionChecker([])
    g = FakeGenerator("en_US", 1, chk)
    f = g.make("person", "Mr. John Smith")
    assert f.startswith("Mr. ") and len(f.split()) == 3
    assert g.make("organization", "Acme Corp.").endswith(" Corp.")
    ph = g.make("phone", "(415) 555-0134")
    assert len(ph) == len("(415) 555-0134") and ph[0] == "(" and ph[4:6] == ") "
    cc = g.make("credit_card", "4111 1111 1111 1111")
    assert cc[0] == "4" and luhn_ok(cc.replace(" ", ""))
    assert g.make("person", "JANE DOE").isupper()
    assert g.make("email", "john.smith@acme.com", {"john": "Peter", "smith": "Walker"}) == "peter.walker@example.com"


def test_fakes_deterministic_per_seed():
    a = FakeGenerator("en_US", 42, CollisionChecker([])).make("person", "John Smith")
    b = FakeGenerator("en_US", 42, CollisionChecker([])).make("person", "John Smith")
    assert a == b


def test_crypto_roundtrip_and_wrong_pass():
    blob = encrypt_json({"a": 1}, "correct horse battery")
    assert decrypt_json(blob, "correct horse battery") == {"a": 1}
    with pytest.raises(BadPassphrase):
        decrypt_json(blob, "wrong passphrase!!")
    with pytest.raises(ValueError):
        encrypt_json({}, "short")


def test_chunking_never_exceeds_limit():
    text = ("John Smith works at Acme Corp. " * 200).strip()
    chunks = chunk(text)
    assert len(chunks) > 1
    assert all(count_tokens(c) <= MAX_TOKENS for _, c in chunks)
    for off, c in chunks:
        assert text[off:off + len(c)] == c
    # every token is covered
    assert chunks[0][0] == 0 and chunks[-1][0] + len(chunks[-1][1]) == len(text)


def test_merge_spans_containment_and_partial():
    a = Span(0, 20, "1 Main St, 94105 SF", "address", 0.8, "gliner")
    b = Span(11, 16, "94105", "ssn", 1.0, "regex")
    assert [s.text for s in merge_spans([a, b])] == ["1 Main St, 94105 SF"]
    c = Span(0, 10, "Ann Lee 55", "person", 0.8, "gliner")
    d = Span(8, 20, "555 0134 999", "phone", 1.0, "regex")
    out = merge_spans([c, d])
    assert [(s.text, s.source) for s in out] == [("Ann Lee", "gliner"), ("555 0134 999", "regex")]


def test_regex_detector():
    r = RegexDetector(Options())
    txt = "Mail john@x.com, call (415) 555-0134 or +44 20 7946 0958, card 4111 1111 1111 1111, ssn 123-45-6789, ip 10.1.2.3"
    types = sorted({s.type for s in r.detect(txt)})
    assert types == ["credit_card", "email", "ip_address", "phone", "ssn"]


def test_regex_numeric_cell_phone():
    r = RegexDetector(Options())
    assert [s.type for s in r.detect("4155550134")] == ["phone"]


def test_initials_stay_initials():
    g = FakeGenerator("en_US", 3, CollisionChecker([]))
    f = g.make("person", "JD")
    assert len(f) == 2 and f.isupper() and f != "JD"
