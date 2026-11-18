"""Apply, restore and leak check (SPEC §5.3, §11)."""
from __future__ import annotations

from .detect.pipeline import detect_units
from .entities import accept_variant, form_fake
from .formats import Handler
from .models import Options, Repl, Unit
from .textutil import FormMatcher, norm_key


def form_map(state: dict) -> dict[str, tuple[str, str, dict]]:
    """exact form -> (fake, type, group) for every included group."""
    out: dict[str, tuple[str, str, dict]] = {}
    for g in state["groups"]:
        if not g["include"]:
            continue
        for f in set(g["forms"]) | set(g.get("counts", {})):
            out.setdefault(f, (form_fake(f, g), g["type"], g))
    return out


def compute_repls(units: list[Unit], fmap: dict[str, tuple[str, str, dict]]):
    """Return ({unit_id: [Repl]}, occurrences, final exact mapping)."""
    matcher = FormMatcher(fmap)
    repls: dict[str, list[Repl]] = {}
    occurrences = []
    for u in units:
        for s, e, m in matcher.finditer(u.text):
            entry = fmap.get(m)
            if entry is None:
                base = matcher.lookup(m)
                if base is None:
                    continue
                _, type_, g = fmap[base]
                if not accept_variant(m, base, type_):
                    continue
                entry = (form_fake(m, g), type_, g)
                fmap[m] = entry  # case variant found only at apply time
            fake = entry[0]
            repls.setdefault(u.id, []).append(Repl(s, e, m, fake))
            occurrences.append({"locator": u.id, "start": s, "end": e, "original": m, "fake": fake})
    return repls, occurrences


def mapping_doc(fmap: dict[str, tuple[str, str, dict]]) -> list[dict]:
    groups: dict[str, dict] = {}
    for original, (fake, type_, g) in fmap.items():
        d = groups.setdefault(g["id"], {"type": type_, "forms": []})
        d["forms"].append({"original": original, "fake": fake})
    return list(groups.values())


def leak_check(handler: Handler, out_path, fmap: dict[str, tuple[str, str, dict]]) -> list[dict]:
    """Approved originals still present in the output (blocking hits)."""
    matcher = FormMatcher(fmap)
    hits: dict[tuple[str, str], int] = {}
    examples: dict[tuple[str, str], str] = {}
    for locator, text in handler.leak_texts(out_path):
        for _, _, m in matcher.finditer(text):
            base = m if m in fmap else matcher.lookup(m)
            if base is None:
                continue
            type_ = fmap[base][1]
            if not accept_variant(m, base, type_):
                continue
            k = (locator, type_)
            hits[k] = hits.get(k, 0) + 1
            examples.setdefault(k, m)
    for leftover in handler.info.get("revision_leftovers", []):
        hits[(leftover, "tracked-change markup")] = 1
    return [{"locator": loc, "type": t, "count": n, "text": examples.get((loc, t), "")}
            for (loc, t), n in sorted(hits.items())]


def second_pass(out_handler: Handler, opts: Options, ner, fmap) -> list[dict]:
    """Detect again on the output; anything that is not a known fake is a
    non-blocking warning (SPEC §11)."""
    fake_keys = {norm_key(v[0]) for v in fmap.values()}
    fake_words = set()
    for v in fmap.values():
        fake_words.update(norm_key(v[0]).split())
    units = out_handler.units()
    det = detect_units(units, opts, ner)
    seen: dict[tuple[str, str], dict] = {}
    for uid, spans in det.items():
        for sp in spans:
            k = norm_key(sp.text)
            if not k or k in fake_keys or all(w in fake_words for w in k.split()):
                continue
            key = (sp.type, k)
            if key in seen:
                seen[key]["count"] += 1
            else:
                seen[key] = {"type": sp.type, "text": sp.text, "locator": uid, "count": 1}
    return sorted(seen.values(), key=lambda d: -d["count"])


def restore_file(handler: Handler, mapping: dict, out_path) -> int:
    """Swap fakes back to originals. Returns the number of replacements."""
    inv: dict[str, str] = {}
    for g in mapping["groups"]:
        for f in g["forms"]:
            inv[f["fake"]] = f["original"]
    matcher = FormMatcher(inv, case_sensitive=True)
    repls: dict[str, list[Repl]] = {}
    n = 0
    for u in handler.units():
        for s, e, m in matcher.finditer(u.text):
            if m in inv:
                repls.setdefault(u.id, []).append(Repl(s, e, m, inv[m]))
                n += 1
    handler.write(repls, out_path)
    return n
