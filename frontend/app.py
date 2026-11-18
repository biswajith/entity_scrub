"""Streamlit UI (SPEC §8).  Run:  streamlit run frontend/app.py"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
import api  # noqa: E402

TYPES = ["person", "organization", "location", "address", "email", "phone", "url", "ip_address",
         "iban", "credit_card", "ssn"]
LOCALES = ["en_US", "en_GB", "en_IN", "en_AU", "de_DE", "fr_FR", "es_ES", "it_IT", "nl_NL", "pt_BR",
           "ja_JP", "zh_CN"]

st.set_page_config(page_title="Entity Scrub", page_icon="🧽", layout="wide")


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------
def sidebar() -> None:
    h = api.health()
    with st.sidebar:
        st.title("Entity Scrub")
        if h is None:
            st.error(f"Backend not reachable at {api.BASE}")
            st.stop()
        st.caption(f"Model: `{h['model']}` · {h['device']} · "
                   f"{'loaded' if h['model_loaded'] else 'loading…'}")
        st.subheader("Recent jobs")
        for j in api.jobs()[:15]:
            c1, c2 = st.columns([4, 1])
            label = f"{j['filename']} · {j['status']}"
            if c1.button(label, key=f"open-{j['id']}", use_container_width=True):
                st.session_state.job_id = j["id"]
                st.session_state.pop("ent", None)
                st.rerun()
            if c2.button("🗑", key=f"del-{j['id']}", help="Delete this job and its files"):
                api.delete(j["id"])
                if st.session_state.get("job_id") == j["id"]:
                    st.session_state.pop("job_id")
                st.rerun()


# ---------------------------------------------------------------------------
# scrub page
# ---------------------------------------------------------------------------
def upload_form() -> None:
    st.header("Scrub a file")
    last = st.session_state.get("last_opts", {})
    f = st.file_uploader("CSV, Excel (.xlsx), Word (.docx) or PDF — up to 20 MB",
                         type=["csv", "tsv", "xlsx", "docx", "pdf"])
    with st.form("opts"):
        types = st.multiselect("Entity types", TYPES, default=last.get("types", TYPES))
        c1, c2, c3 = st.columns(3)
        threshold = c1.slider("NER confidence threshold", 0.1, 0.95, last.get("threshold", 0.5), 0.05)
        locale = c2.selectbox("Fake-name style (locale)", LOCALES,
                              index=LOCALES.index(last.get("locale", "en_US")))
        region = c3.text_input("Phone region (ISO code)", last.get("phone_region", "US"), max_chars=2)
        extra = st.text_input("Extra phone regions (comma-separated, optional)",
                              ",".join(last.get("extra_regions", [])))
        with st.expander("Advanced"):
            custom = st.text_area("Custom ID patterns, one per line as `name: regex`",
                                  "\n".join(f"{c['name']}: {c['pattern']}" for c in last.get("custom_patterns", [])),
                                  placeholder="account_no: ACC-\\d{8}")
            cc1, cc2 = st.columns(2)
            enc = cc1.text_input("CSV encoding override", "")
            delim = cc2.text_input("CSV delimiter override", "", max_chars=1)
            second = st.checkbox("Run a second detection pass on the output", last.get("second_pass", True))
        go = st.form_submit_button("Upload and detect", type="primary", disabled=False)
    if go:
        if f is None:
            st.warning("Choose a file first.")
            return
        patterns = []
        for line in custom.splitlines():
            if ":" in line:
                n, p = line.split(":", 1)
                patterns.append({"name": n.strip(), "pattern": p.strip()})
        opts = {"types": types, "threshold": threshold, "locale": locale,
                "phone_region": region.upper() or "US",
                "extra_regions": [r.strip().upper() for r in extra.split(",") if r.strip()],
                "custom_patterns": patterns, "csv_encoding": enc or None, "csv_delimiter": delim or None,
                "second_pass": second}
        st.session_state.last_opts = opts
        try:
            stt = api.create(f.name, f.getvalue(), opts)
        except api.ApiError as e:
            st.error(str(e))
            return
        st.session_state.job_id = stt["id"]
        st.session_state.pop("ent", None)
        st.rerun()


def progress_view(stt: dict) -> None:
    done, total, msg = stt["progress"]
    st.subheader(f"{stt['filename']} — {stt['status']}")
    st.progress(min(1.0, done / total) if total else 0.0, text=f"{msg} ({done}/{total})" if total else msg)
    time.sleep(1.0)
    st.rerun()


def _groups_df(groups: list[dict]) -> pd.DataFrame:
    rows = [{
        "include": g["include"], "type": g["type"], "original": " | ".join(g["forms"]), "fake": g["fake"],
        "occurrences": g["occurrences"], "confidence": g["score"],
        "suggested": "suggested variant" if g["suggested"] else ("linked" if g["parent"] else ""),
        "context": g["contexts"][0] if g["contexts"] else "", "id": g["id"],
    } for g in groups]
    return pd.DataFrame(rows, columns=["include", "type", "original", "fake", "occurrences", "confidence",
                                       "suggested", "context", "id"])


def review_view(job_id: str, stt: dict) -> None:
    st.subheader(f"Review — {stt['filename']}")
    info = stt.get("info", {})
    notes = []
    if info.get("revisions_accepted"):
        notes.append(f"{info['revisions_accepted']} tracked change(s) will be accepted before scrubbing.")
    if info.get("encoding"):
        notes.append(f"CSV read as {info['encoding']}, delimiter `{info['delimiter']}`, {info.get('newline')}.")
    for u in info.get("unsupported", []):
        notes.append(f"⚠ Unsupported: {u}")
    for n in notes:
        st.info(n)

    if "ent" not in st.session_state:
        st.session_state.ent = api.entities(job_id)
    ent = st.session_state.ent
    for w in st.session_state.pop("warnings", []):
        st.warning(w)

    # -- column view -------------------------------------------------------
    edits: dict = {"groups": [], "add_terms": [], "columns": []}
    if ent["columns"]:
        with st.expander("Columns (spreadsheets)", expanded=True):
            st.caption("Set a column to **whole** to replace every value in it as the chosen type, "
                       "without relying on per-cell detection.")
            cdf = pd.DataFrame([{
                "column": c["header"], "values": c["n"], "sample": " · ".join(c["sample"][:3]),
                "detected as": c["proposed_type"], "whole-cell hits": c["hit_rate"],
                "suggest whole": "✓" if c["suggest_whole"] and c["mode"] != "whole" else "",
                "mode": c["mode"], "type": c["type"], "key": c["key"]} for c in ent["columns"]])
            cedit = st.data_editor(
                cdf, hide_index=True, use_container_width=True, key="coled",
                disabled=["column", "values", "sample", "detected as", "whole-cell hits", "suggest whole", "key"],
                column_config={
                    "mode": st.column_config.SelectboxColumn(options=["ner", "whole"], required=True),
                    "type": st.column_config.SelectboxColumn(options=TYPES, required=True),
                    "key": None,
                })
            for (_, a), (_, b) in zip(cdf.iterrows(), cedit.iterrows()):
                if a["mode"] != b["mode"] or a["type"] != b["type"]:
                    edits["columns"].append({"key": a["key"], "mode": b["mode"], "type": b["type"]})

    # -- entity table ------------------------------------------------------
    groups = ent["groups"]
    present = sorted({g["type"] for g in groups})
    c1, c2, c3 = st.columns([3, 2, 2])
    show = c1.multiselect("Filter types", present, default=present)
    query = c2.text_input("Search", "")
    hide_sugg = c3.checkbox("Hide suggested variants", False)
    view = [g for g in groups if g["type"] in show and (not hide_sugg or not g["suggested"])
            and (not query or query.lower() in " ".join(g["forms"]).lower() or query.lower() in g["fake"].lower())]
    st.caption(f"{sum(g['include'] for g in groups)} of {len(groups)} entities selected · "
               f"{sum(g['occurrences'] for g in groups if g['include'])} replacements")
    df = _groups_df(view)
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, height=min(700, 38 + 35 * max(len(df), 3)), key="ented",
        disabled=["original", "occurrences", "confidence", "suggested", "context", "id"],
        column_config={
            "include": st.column_config.CheckboxColumn(width="small"),
            "type": st.column_config.SelectboxColumn(options=TYPES, required=True),
            "fake": st.column_config.TextColumn("fake (editable)"),
            "confidence": st.column_config.NumberColumn(format="%.2f", width="small"),
            "context": st.column_config.TextColumn(width="large"),
            "id": None,
        })
    for (_, a), (_, b) in zip(df.iterrows(), edited.iterrows()):
        e = {}
        if a["include"] != b["include"]:
            e["include"] = bool(b["include"])
        if a["type"] != b["type"]:
            e["type"] = b["type"]
        if a["fake"] != b["fake"]:
            e["fake"] = b["fake"]
        if e:
            e["id"] = a["id"]
            edits["groups"].append(e)

    b1, b2, b3 = st.columns(3)
    bulk_type = b1.selectbox("Bulk: type", present or TYPES, key="bulk_t")
    if b2.button(f"Exclude all '{bulk_type}'"):
        _save(job_id, {"groups": [{"id": g["id"], "include": False} for g in groups if g["type"] == bulk_type]})
    if b3.button(f"Include all '{bulk_type}'"):
        _save(job_id, {"groups": [{"id": g["id"], "include": True} for g in groups if g["type"] == bulk_type]})

    with st.form("addterm", clear_on_submit=True):
        st.markdown("**Add a missed term** — it will be replaced everywhere it appears")
        a1, a2, a3 = st.columns([4, 2, 1])
        term = a1.text_input("Text", label_visibility="collapsed", placeholder="e.g. jsmith or Project Falcon")
        ttype = a2.selectbox("Type", TYPES, label_visibility="collapsed")
        if a3.form_submit_button("Add") and term.strip():
            _save(job_id, {"add_terms": [{"text": term.strip(), "type": ttype}]})

    pending = bool(edits["groups"] or edits["columns"])
    if st.button("Save changes", type="primary" if pending else "secondary", disabled=not pending):
        _save(job_id, edits)

    st.divider()
    st.markdown("### Apply")
    st.caption("The mapping file is always encrypted. **If you lose the passphrase, the scrub cannot be "
               "reversed** (you still have your original file).")
    with st.form("apply"):
        p1 = st.text_input("Mapping passphrase (min 12 characters)", type="password")
        p2 = st.text_input("Repeat passphrase", type="password")
        go = st.form_submit_button("Apply and scrub", type="primary", disabled=pending)
    if pending:
        st.warning("You have unsaved changes in the tables — save them before applying.")
    if go:
        if len(p1) < 12:
            st.error("Passphrase must be at least 12 characters.")
        elif p1 != p2:
            st.error("Passphrases do not match.")
        else:
            try:
                api.apply(job_id, p1)
                st.session_state.pop("ent", None)
                st.rerun()
            except api.ApiError as e:
                st.error(str(e))


def _save(job_id: str, edits: dict) -> None:
    try:
        res = api.edit(job_id, edits)
    except api.ApiError as e:
        st.error(str(e))
        return
    st.session_state.ent = {"groups": res["groups"], "columns": res["columns"]}
    st.session_state.warnings = res.get("warnings", [])
    for k in ("ented", "coled"):
        st.session_state.pop(k, None)
    st.rerun()


def done_view(job_id: str, stt: dict) -> None:
    st.subheader(f"Done — {stt['filename']}")
    rep = api.report(job_id)
    m = st.columns(4)
    m[0].metric("Replacements", rep["replacements"])
    m[1].metric("Entities", rep["entities"])
    m[2].metric("Residual hits", len(rep["residual_hits"]))
    m[3].metric("2nd-pass warnings", len(rep["second_pass_warnings"]))
    if rep["replacements_by_type"]:
        st.bar_chart(pd.Series(rep["replacements_by_type"], name="replacements"))
    for n in rep.get("notes", []) + [f"Unsupported: {u}" for u in rep.get("unsupported", [])]:
        st.info(n)
    if rep.get("revisions_accepted"):
        st.info(f"{rep['revisions_accepted']} tracked change(s) were accepted before scrubbing.")

    h = api.hits(job_id)
    if h["hits"]:
        st.error("The output still contains approved originals in these places:")
        st.dataframe(pd.DataFrame(h["hits"]), hide_index=True, use_container_width=True)
        if not stt["acknowledged"]:
            if st.checkbox("I understand the output still contains these"):
                api.acknowledge(job_id)
                st.rerun()
    if h["warnings"]:
        with st.expander(f"Second pass found {len(h['warnings'])} possible misses (not blocking)"):
            st.dataframe(pd.DataFrame(h["warnings"]), hide_index=True, use_container_width=True)

    blocked = stt["blocking"] and not stt["acknowledged"]
    d1, d2, d3 = st.columns(3)
    if blocked:
        d1.button("Download scrubbed file", disabled=True)
    else:
        data, name = api.download(job_id, "output")
        d1.download_button("Download scrubbed file", data, file_name=name, type="primary")
    mdata, mname = api.download(job_id, "mapping")
    d2.download_button("Download mapping (encrypted)", mdata, file_name=mname)
    d3.download_button("Download report", json.dumps(rep, indent=1), file_name=f"{job_id}.report.json")
    st.caption("Keep the mapping file away from the scrubbed file when you share it.")
    if st.button("Scrub another file"):
        st.session_state.pop("job_id", None)
        st.rerun()


def scrub_page() -> None:
    job_id = st.session_state.get("job_id")
    if not job_id:
        upload_form()
        return
    try:
        stt = api.state(job_id)
    except api.ApiError:
        st.session_state.pop("job_id", None)
        st.rerun()
        return
    status = stt["status"]
    if status in ("queued", "detecting", "applying"):
        progress_view(stt)
    elif status == "review":
        review_view(job_id, stt)
    elif status == "done":
        done_view(job_id, stt)
    else:
        st.error(f"Job failed: {stt.get('error')}")
        if st.button("Start over"):
            st.session_state.pop("job_id", None)
            st.rerun()


# ---------------------------------------------------------------------------
# restore page
# ---------------------------------------------------------------------------
def restore_page() -> None:
    st.header("Restore originals")
    st.caption("Works for CSV, Excel and Word. PDF restore is not supported — keep your original PDF.")
    f = st.file_uploader("Scrubbed file", type=["csv", "tsv", "xlsx", "docx"])
    m = st.file_uploader("Mapping file (.mapping.enc)", type=["enc", "json"])
    p = st.text_input("Passphrase", type="password")
    if st.button("Restore", type="primary", disabled=not (f and m and p)):
        try:
            data, name, n = api.restore(f.name, f.getvalue(), m.getvalue(), p)
        except api.ApiError as e:
            st.error(str(e))
            return
        st.success(f"Restored {n} value(s).")
        st.download_button("Download restored file", data, file_name=name, type="primary")


sidebar()
nav = st.navigation([st.Page(scrub_page, title="Scrub", icon="🧽", default=True),
                     st.Page(restore_page, title="Restore", icon="↩️")])
nav.run()
