"""Entity groups, fakes, variants, column view and review edits (SPEC §3-§5).

State is a plain dict so it can be saved as JSON between the detect step and
the apply step:

    {"seed": int, "locale": str, "next_id": int,
     "groups": [group, ...], "columns": [column, ...]}

group = {id, type, forms: {exact: detected_count}, fake, include, suggested,
         source, parent, part, score, contexts: [str], counts: {form: n},
         total, cols: [col_key]}
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .fake.generator import CollisionChecker, FakeGenerator, split_org_suffix, split_title
from .models import NER_TYPES, ORG, PERSON, Column, Span, Unit
from .textutil import FormMatcher, apply_case, collapse_ws, norm_key, strip_possessive

CONTEXT_CHARS = 50


# ---------------------------------------------------------------------------
# form -> fake derivation and case-variant policy
# ---------------------------------------------------------------------------

def form_fake(form: str, group: dict) -> str:
    """Fake for one exact surface form of a group (case + possessive kept)."""
    fake = group["fake"]
    if group["type"] in NER_TYPES:
        core, poss = strip_possessive(form)
        fcore, _ = strip_possessive(fake)
        return apply_case(fcore, core) + poss
    return apply_case(fake, form)


def accept_variant(matched: str, form: str, type_: str) -> bool:
    """Should a case-insensitive hit count as this form? (SPEC §5.3)

    Names are only matched in lower case when the reviewed form itself is
    lower case, so a first name like 'Will' never replaces the verb 'will'.
    Emails, URLs, IDs etc. match in any case."""
    if matched == form or type_ not in NER_TYPES:
        return True
    if matched.islower() and not form.islower():
        return False
    return True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_group(state: dict, type_: str, source: str, **kw) -> dict:
    gid = f"g{state['next_id']}"
    state["next_id"] += 1
    g = {
        "id": gid, "type": type_, "forms": {}, "fake": "", "include": True, "suggested": False,
        "source": source, "parent": None, "part": None, "score": 1.0, "contexts": [],
        "counts": {}, "total": 0, "cols": [],
    }
    g.update(kw)
    state["groups"].append(g)
    return g


def _rep_form(g: dict) -> str:
    return max(g["forms"].items(), key=lambda kv: (kv[1], len(kv[0])))[0] if g["forms"] else ""


def _checker(units: list[Unit], state: dict, skip_gid: str | None = None) -> CollisionChecker:
    chk = CollisionChecker([u.text for u in units])
    for g in state["groups"]:
        for f in g["forms"]:
            chk.add_original(f, name_like=g["type"] in NER_TYPES)
        if g["fake"] and g["id"] != skip_gid:
            chk.add_fake(g["fake"])
    return chk


def _generator(state: dict, chk: CollisionChecker, salt: int = 0) -> FakeGenerator:
    return FakeGenerator(state["locale"], state["seed"] + salt, chk)


def _snippet(text: str, s: int, e: int) -> str:
    a, b = max(0, s - CONTEXT_CHARS), min(len(text), e + CONTEXT_CHARS)
    return collapse_ws(("…" if a else "") + text[a:s] + "[" + text[s:e] + "]" + text[e:b] + ("…" if b < len(text) else ""))


def _name_parts(name: str) -> tuple[str, list[str]]:
    core, _ = strip_possessive(name)
    title, rest = split_title(core)
    return title, rest.split()


# ---------------------------------------------------------------------------
# build from detections
# ---------------------------------------------------------------------------

def build_state(
    units: list[Unit],
    detections: dict[str, list[Span]],
    columns: list[Column],
    locale: str,
    seed: int,
) -> dict:
    state = {"seed": seed, "locale": locale, "next_id": 1, "groups": [], "columns": []}
    by_key: dict[tuple[str, str], dict] = {}
    text_by_id = {u.id: u.text for u in units}

    for uid, spans in detections.items():
        for sp in spans:
            k = (sp.type, norm_key(sp.text))
            if not k[1]:
                continue
            g = by_key.get(k)
            if g is None:
                g = _new_group(state, sp.type, sp.source, score=sp.score)
                by_key[k] = g
            g["forms"][sp.text] = g["forms"].get(sp.text, 0) + 1
            g["score"] = max(g["score"], sp.score) if sp.source == "gliner" else g["score"]
            if len(g["contexts"]) < 2:
                g["contexts"].append(_snippet(text_by_id[uid], sp.start, sp.end))

    _assign_fakes(units, state)
    _link_variants(units, state)
    state["columns"] = _column_view(units, detections, columns)
    recount(units, state)
    return state


def _assign_fakes(units: list[Unit], state: dict) -> None:
    chk = _checker(units, state)
    gen = _generator(state, chk)
    # persons with more tokens first so their parts can seed variants/emails
    order = sorted(
        [g for g in state["groups"] if not g["fake"]],
        key=lambda g: (g["type"] != PERSON, g["type"] == "email", -len(_rep_form(g).split())),
    )
    linked = _email_links(state)
    for g in order:
        rep = _rep_form(g)
        core = strip_possessive(rep)[0] if g["type"] in NER_TYPES else rep
        if g["type"] == "email":
            linked = _email_links(state)
        g["fake"] = gen.make(g["type"], core, linked)


def _email_links(state: dict) -> dict[str, str]:
    """casefolded real first/last name -> fake first/last (SPEC §5.1)."""
    out: dict[str, str] = {}
    for g in state["groups"]:
        if g["type"] != PERSON or not g["fake"] or not g["include"]:
            continue
        _, rt = _name_parts(_rep_form(g))
        _, ft = _name_parts(g["fake"])
        if len(rt) >= 2 and len(ft) >= 2:
            for r, f in ((rt[0], ft[0]), (rt[-1], ft[-1])):
                r = r.strip(".,").casefold()
                if len(r) >= 3:
                    out.setdefault(r, f.strip(".,"))
    return out


def _variant_candidates(g: dict) -> list[tuple[str, str, str]]:
    """[(candidate_text, part, fake)] for a multi-token person/org group."""
    rep = _rep_form(g)
    out = []
    if g["type"] == PERSON:
        _, rt = _name_parts(rep)
        _, ft = _name_parts(g["fake"])
        if len(rt) < 2 or len(ft) < 2 or "," in rep:
            return []
        first, last = rt[0], rt[-1]
        ffirst, flast = ft[0], ft[-1]
        if len(last.rstrip(".")) > 1:
            out.append((last, "last", flast))
            for t in ("Mr.", "Mr", "Mrs.", "Mrs", "Ms.", "Ms", "Dr.", "Dr"):
                out.append((f"{t} {last}", "title_last", f"{t} {flast}"))
        if len(first.rstrip(".")) > 1:
            out.append((first, "first", ffirst))
    elif g["type"] == ORG:
        base, suffix = split_org_suffix(strip_possessive(rep)[0])
        fbase, _ = split_org_suffix(strip_possessive(g["fake"])[0])
        if suffix and base.strip() and fbase.strip():
            out.append((base.strip(), "base", fbase.strip()))
    return out


def _link_variants(units: list[Unit], state: dict) -> None:
    """Link standalone surnames / titles / org bases to their full form and
    suggest (unticked) variants that occur in the text (SPEC §4.3)."""
    cands: dict[str, list[tuple[dict, str, str, str]]] = defaultdict(list)
    for g in list(state["groups"]):
        if g["type"] in (PERSON, ORG) and g["parent"] is None:
            for text, part, fake in _variant_candidates(g):
                cands[norm_key(text)].append((g, text, part, fake))
    if not cands:
        return

    by_key = {(g["type"], norm_key(f)): g for g in state["groups"] for f in g["forms"]}
    chk = _checker(units, state)

    # occurrences of candidate strings in the text
    matcher = FormMatcher([t for lst in cands.values() for _, t, _, _ in lst])
    occ: Counter = Counter()
    for u in units:
        for _, _, m in matcher.finditer(u.text):
            occ[norm_key(m)] += 1

    for key, lst in cands.items():
        parents = {id(p): p for p, _, _, _ in lst}
        if len(parents) > 1:
            continue  # ambiguous: two people share this surname
        parent, text, part, fake = lst[0]
        existing = by_key.get((parent["type"], key))
        if existing is parent:
            continue
        if existing is not None:
            if existing["parent"] is None and len(existing["forms"]) and all(len(f.split()) <= 2 for f in existing["forms"]):
                if chk.in_source(fake):
                    continue
                existing.update(parent=parent["id"], part=part, fake=fake)
            continue
        if occ.get(key, 0) == 0 or chk.in_source(fake) or fake.casefold() in chk.used:
            continue
        g = _new_group(state, parent["type"], "variant", parent=parent["id"], part=part,
                       fake=fake, include=False, suggested=True, score=0.0)
        g["forms"][text] = 0
        chk.add_fake(fake)
        by_key[(parent["type"], key)] = g


# ---------------------------------------------------------------------------
# column view (spreadsheets)
# ---------------------------------------------------------------------------

def _column_view(units: list[Unit], detections: dict[str, list[Span]], columns: list[Column]) -> list[dict]:
    text_by_id = {u.id: u.text for u in units}
    out = []
    for c in columns:
        vals = [uid for uid in c.unit_ids if text_by_id.get(uid, "").strip()]
        if not vals:
            continue
        whole: Counter = Counter()
        any_hit = 0
        for uid in vals:
            t = text_by_id[uid].strip()
            spans = detections.get(uid, [])
            if spans:
                any_hit += 1
            for sp in spans:
                if len(sp.text.strip()) >= 0.8 * len(t):
                    whole[sp.type] += 1
                    break
        ptype, n_whole = whole.most_common(1)[0] if whole else ("", 0)
        rate = n_whole / len(vals)
        sample: list[str] = []
        for uid in vals:
            v = text_by_id[uid]
            if v not in sample:
                sample.append(v)
            if len(sample) >= 5:
                break
        out.append({
            "key": c.key, "header": c.header, "n": len(vals), "sample": sample,
            "proposed_type": ptype, "hit_rate": round(rate, 2), "any_rate": round(any_hit / len(vals), 2),
            "suggest_whole": bool(ptype) and rate >= 0.5, "mode": "ner", "type": ptype or PERSON,
            "unit_ids": vals,
        })
    return out


# ---------------------------------------------------------------------------
# counting (propagated occurrences, SPEC §5.3 step 4)
# ---------------------------------------------------------------------------

def recount(units: list[Unit], state: dict) -> None:
    form_owner: dict[str, dict] = {}
    for g in state["groups"]:
        g["counts"] = {}
        g["total"] = 0
        for f in g["forms"]:
            form_owner.setdefault(f, g)
    matcher = FormMatcher(form_owner)
    for u in units:
        for _, _, m in matcher.finditer(u.text):
            f = matcher.lookup(m)
            g = form_owner.get(f) if f else None
            if g is None or not accept_variant(m, f, g["type"]):
                continue
            g["counts"][m] = g["counts"].get(m, 0) + 1
            g["total"] += 1


# ---------------------------------------------------------------------------
# review edits
# ---------------------------------------------------------------------------

def apply_edits(units: list[Unit], state: dict, edits: dict) -> list[str]:
    """Apply review edits; return a list of human-readable warnings."""
    warnings: list[str] = []
    groups = {g["id"]: g for g in state["groups"]}
    salt = state.setdefault("edit_salt", 0)

    for e in edits.get("groups", []):
        g = groups.get(e.get("id"))
        if g is None:
            continue
        if "include" in e:
            g["include"] = bool(e["include"])
            if g["include"]:
                g["suggested"] = False
        new_type = e.get("type")
        new_fake = e.get("fake")
        if new_type and new_type != g["type"]:
            g["type"] = new_type
            if not new_fake or new_fake == g["fake"]:
                salt += 1
                chk = _checker(units, state, skip_gid=g["id"])
                g["fake"] = _generator(state, chk, salt).make(new_type, strip_possessive(_rep_form(g))[0])
        if new_fake is not None and new_fake != g["fake"]:
            new_fake = new_fake.strip()
            chk = _checker(units, state, skip_gid=g["id"])
            reason = chk.problem(new_fake, _rep_form(g))
            if reason and not (g["parent"] and reason == "fake overlaps another fake or original"):
                warnings.append(f"Fake for '{_rep_form(g)}' not changed: {reason}")
            else:
                g["fake"] = new_fake

    for t in edits.get("add_terms", []):
        text, type_ = (t.get("text") or "").strip(), t.get("type") or PERSON
        if not text:
            continue
        g = _find_by_key(state, norm_key(text))
        if g is not None:
            g["include"] = True
            g["suggested"] = False
            g["forms"].setdefault(text, 0)
            continue
        salt += 1
        g = _new_group(state, type_, "user")
        g["forms"][text] = 0
        chk = _checker(units, state, skip_gid=g["id"])
        g["fake"] = _generator(state, chk, salt).make(type_, strip_possessive(text)[0] if type_ in NER_TYPES else text)

    for c in edits.get("columns", []):
        col = next((x for x in state["columns"] if x["key"] == c.get("key")), None)
        if col is None:
            continue
        mode, type_ = c.get("mode", col["mode"]), c.get("type", col["type"])
        if mode == col["mode"] and type_ == col["type"]:
            continue
        _unset_column(state, col["key"])
        col["mode"], col["type"] = mode, type_
        if mode == "whole":
            salt += 1
            _set_column_whole(units, state, col, salt)

    state["edit_salt"] = salt
    recount(units, state)
    return warnings


def _find_by_key(state: dict, key: str, type_: str | None = None) -> dict | None:
    for g in state["groups"]:
        if type_ and g["type"] != type_:
            continue
        if any(norm_key(f) == key for f in g["forms"]):
            return g
    return None


def _unset_column(state: dict, key: str) -> None:
    keep = []
    for g in state["groups"]:
        if key in g["cols"]:
            g["cols"].remove(key)
            if g["source"] == "column" and not g["cols"]:
                continue
        keep.append(g)
    state["groups"] = keep


def _set_column_whole(units: list[Unit], state: dict, col: dict, salt: int) -> None:
    text_by_id = {u.id: u.text for u in units}
    index = {}
    for g in state["groups"]:
        for f in g["forms"]:
            index.setdefault(norm_key(f), g)
    chk = _checker(units, state)
    gen = _generator(state, chk, salt)
    for uid in col["unit_ids"]:
        v = text_by_id.get(uid, "").strip()
        k = norm_key(v)
        if not k:
            continue
        g = index.get(k)
        if g is None:
            g = _new_group(state, col["type"], "column")
            g["forms"][v] = 0
            g["fake"] = gen.make(col["type"], strip_possessive(v)[0] if col["type"] in NER_TYPES else v)
            index[k] = g
        else:
            g["forms"].setdefault(v, 0)
            g["include"] = True
            g["suggested"] = False
        if col["key"] not in g["cols"]:
            g["cols"].append(col["key"])


# ---------------------------------------------------------------------------
# public view for the API / UI
# ---------------------------------------------------------------------------

def public_view(state: dict) -> dict:
    groups = []
    for g in state["groups"]:
        groups.append({
            "id": g["id"], "type": g["type"], "include": g["include"], "suggested": g["suggested"],
            "forms": sorted(set(g["forms"]) | set(g["counts"])), "fake": g["fake"],
            "occurrences": g["total"], "detected": sum(g["forms"].values()),
            "score": round(g["score"], 2), "contexts": g["contexts"], "source": g["source"],
            "parent": g["parent"],
        })
    groups.sort(key=lambda g: (g["suggested"], g["type"], -g["occurrences"]))
    cols = [{k: v for k, v in c.items() if k != "unit_ids"} for c in state["columns"]]
    return {"groups": groups, "columns": cols}
