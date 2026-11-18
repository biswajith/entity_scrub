# Entity Scrub — Spec (v0.3, open questions resolved)

## 1. Goal

A local tool that takes CSV / Excel / Word / PDF files, uses **GLiNER** (plus regex) to find real
entities (people, organizations, locations/addresses, contact details and IDs), lets the user
**review** what was found, and writes back a copy of the file in the **same format** with each
entity replaced by a **consistent fake value**. A **mapping file** is produced so the scrub can
be reversed later.

Runs entirely on one machine (target: M4 Mac, CPU-only by default). No data leaves the machine;
the only network use is the one-time model download.

### Non-goals (v1)
- Scanned / image-only PDFs, and PDFs with no usable text layer (no OCR). They are detected
  and reported as unsupported.
- **Restoring PDFs.** Keep the original PDF. The PDF mapping is still produced, so text taken
  from the scrubbed PDF can be un-scrubbed.
- `.xls`, `.doc`, macro files (`.xlsm`/`.docm`), multi-user use, auth.
- Mappings shared across files (see §12).

## 2. Architecture

```
┌──────────────────────┐   HTTP (127.0.0.1)   ┌──────────────────────────────────────┐
│  Frontend: Streamlit │ ───────────────────► │  Backend: FastAPI (thin layer)       │
│  scrub / review /    │ ◄─────────────────── │        │                             │
│  download / restore  │                      │        ▼                             │
└──────────────────────┘                      │  scrub/ core library (no web deps)   │
                                              │  ├─ formats: csv/xlsx/docx/pdf       │
                                              │  ├─ detect: GLiNER + regex           │
                                              │  ├─ mapping + fake generator         │
                                              │  └─ apply / restore / leak check     │
                                              └──────────────────────────────────────┘
```

- **The core is a plain Python library** (`scrub/`), so it can be tested and scripted without
  the web layer. FastAPI wraps it, and Streamlit calls FastAPI.
- **One job runs at a time** on a single background worker thread, which holds one GLiNER
  model in memory.
- **Storage**: `~/.entity-scrub/jobs/<job_id>/` holds `state.json`, the original file,
  `units.jsonl`, `entities.json`, the output, `mapping.enc` and `report.json`. There's no
  database.
  - **The plaintext mapping is never written to disk.** It lives in memory and is saved only
    in encrypted form.
  - When a job reaches `done` or `failed`, the original, `units.jsonl` and `entities.json` are
    deleted. Only the output, `mapping.enc` and the report are kept.
  - At startup, unfinished jobs older than 24 hours and all jobs older than 7 days are purged.
    Jobs can also be deleted from the UI.
  - The folder is created with `chmod 700`, and the README recommends turning on FileVault.
- **Packaging**: `uv` + `pyproject.toml`. `make dev` starts both processes. The `gliner`,
  `torch`, `pymupdf` and `python-docx` versions are pinned.

## 3. Workflow

```
Upload ─► Extract ─► Detect ─► REVIEW ─► Apply ─► Leak check ─► Download (file + mapping + report)
                                                                  │
                         Restore (CSV/XLSX/DOCX only: scrubbed file + mapping) ─► original
```

1. **Upload**: the user drops a file (≤ 20 MB) and picks the options. Defaults are prefilled
   from the last job:
   - entity types and confidence threshold,
   - **fake-name locale** (default `en_US`),
   - **phone region** plus optional extra regions.

   The app also shows file-specific facts. For CSV, the detected encoding and delimiter, which
   can be overridden. For DOCX, the number of tracked revisions that will be accepted.
2. **Extract**: the file is split into **text units**, each with a locator (§6). Every text
   location in the file is listed, including metadata, so that apply and the leak check cover
   the same ground.
3. **Detect** (§4): progress is reported as units processed out of the total.
4. **Review**. There are two parts:
   - **Column view (spreadsheets only)**: for each column, the header, a sample of values, the
     proposed type and the hit rate. The user can mark a column **"replace whole value as
     \<type\>"**, which treats every value in it as that entity with no reliance on per-cell
     NER. Columns mostly made of names, IDs or emails get this suggested.
   - **Entity table**: `include ☑ | type | surface forms | proposed fake | occurrences (incl.
     propagated) | sample context | confidence`. The user can untick false positives, edit the
     fake, change the type, and add a missed term. Suggested variants (e.g., a standalone
     surname) appear **unticked** by default. Grouping or merging is done by editing the fake
     so that two rows share it, with no separate merge UI.
5. **Apply** (§5.3) using the matching rule below.
   At Apply, the user enters the **mapping passphrase twice** (at least 12 characters). It is
   held in memory only.
6. **Leak check** (§11) runs automatically. Residual hits come in two kinds:
   - An **approved original still present** in the output, including an entity in an XLSX
     sheet name. This **blocks download** until the user ticks "I understand the output still
     contains these".
   - A **new entity found by the second detection pass**. This is a warning only and doesn't
     block.
7. **Download**: the scrubbed file, `mapping.enc` (always encrypted) and `report.json`. The
   report holds only locators, types and counts, never original strings, which are shown in
   the UI only.
8. **Restore** (CSV/XLSX/DOCX): upload a scrubbed file + its mapping (+ passphrase) to get the
   original back.

## 4. Detection

### 4.1 GLiNER (people, organizations, locations, addresses)
- Default model: `urchade/gliner_multi_pii-v1`, configurable. The `gliner` package version is
  pinned, and the inference call (`batch_predict_entities` or its successor in that version) is
  wrapped in one adapter function.
- Labels: `person`, `organization`, `location`, `address` (configurable).
- **Device**: CPU by default. MPS is opt-in (`SCRUB_DEVICE=mps`) and falls back to CPU on
  errors; DeBERTa on MPS is often not faster.
- **Chunking**: windows are counted with **GLiNER's own word splitter** (punctuation counts as
  tokens). Each window is capped at 300 tokens with a 50-token overlap, and a sentence boundary
  is preferred when splitting. An **assert fires if any input would be truncated**, so nothing
  is silently skipped. Offsets are mapped back to the unit, and overlap duplicates are removed.
- **Spreadsheet context**: a cell is sent as `"<header>: <value>"` (e.g., `"Customer: Smith"`),
  and offsets are shifted back afterwards. This gives the model context for lone names.
- **Speed**: (header, value) pairs are de-duplicated before inference, inference is batched,
  and units under 2 characters are skipped. Columns marked "replace whole value" skip NER
  entirely.
- Threshold: default 0.5, adjustable.

### 4.2 Regex (contact details and IDs)
`email`, `phone` (`phonenumbers`. The **primary region is set per job**: it's prefilled from
the last job, or from the macOS locale on first run, else `US`. There's an optional
extra-regions list, empty by default, and `+`-prefixed international numbers always parse), `url`,
`ip_address`, `iban`, `credit_card` (Luhn-checked) and `ssn`, plus **user-defined patterns**.
All patterns are wrapped in word boundaries. Regex also runs on **numeric cells**, which are
formatted the way Excel displays them, so phone numbers or IDs stored as numbers are caught.

### 4.3 Grouping and variants
- Overlapping spans:
  - **A span that fully contains another wins.** For example, a GLiNER address span containing
    a regex-matched postcode is kept as the address.
  - **On a partial overlap, regex wins** and the GLiNER span is trimmed to the part that
    doesn't overlap. The trimmed span is dropped if it ends up shorter than 2 characters.
- An entity **group** = one type + one fake + a set of **exact surface forms** (e.g., `John
  Smith`, `JOHN SMITH`, `John Smith's`). Forms are grouped by a normalized key for display, but
  each exact string is kept (§5).
- Suggested variants, all unticked by default: standalone surname or first name, `Mr./Ms.
  <surname>`, possessives, org names without their suffix (`Acme` for `Acme Corp`). Usernames
  like `jsmith` are not inferred in v1; the user can add them as terms.

## 5. Mapping, fakes and apply

### 5.1 Fake generation
- `Faker` with a seed per job, using **one locale per job** chosen from a dropdown (default
  `en_US`). The locale is never guessed per name.
- Fakes match the type and shape of the original: person names keep first/last/title/initials,
  companies keep their suffix, phone numbers and IDs keep the same digit count and separators,
  and cards stay Luhn-valid.
- **Emails**: an email is linked to a person only when its local part contains a detected
  person's first or last name (e.g., `john.smith@acme.com`). It then becomes
  `<fake first>.<fake last>@example.com`, following the original's pattern. Otherwise it gets a
  random fake `@example.com` address.
- Each surface form gets a fake with matching case: `JOHN SMITH` → `PETER WALKER`, and
  `John Smith` → `Peter Walker`, from the same group.
- **Collision rules** (needed for restore). A fake is rejected and regenerated if:
  - it appears anywhere in the source text (case-insensitive, word-bounded),
  - it equals or is a word-bounded substring of another fake or original (or contains one),
  - another surface form already uses it.

### 5.2 Mapping file (`mapping.enc`, plaintext structure shown)
```json
{ "version": 1, "job_id": "...", "source": {"name": "...", "sha256": "..."},
  "output_sha256": "...", "model": "...", "gliner_version": "...",
  "groups": [ {"type": "person", "forms": [{"original": "John Smith", "fake": "Peter Walker"}, ...]} ],
  "occurrences": [ {"locator": "...", "start": 10, "end": 20, "original": "...", "fake": "..."} ] }
```
The map is **exact string → exact string**. The occurrence log lets restore work by position
when the scrubbed file is unchanged (its SHA matches `output_sha256`); otherwise restore falls
back to string replacement of fake → original.

**Always encrypted**: the passphrase (at least 12 characters) goes through scrypt to make a
Fernet key. The passphrase only ever travels in a POST body, never in a URL. **If the
passphrase is lost, restore is impossible.** The UI says so at Apply, and the user still has
their original file.

### 5.3 Matching rule for apply (same for every format)
1. Collect every approved surface form from all groups and added terms.
2. In **every** text unit, find all occurrences of every form: **exact case, then a
   case-insensitive pass**, matched with **Unicode word boundaries** (`(?<!\w)…(?!\w)`), longest
   form first, with no overlapping matches. A missed "John Smith" in another cell or paragraph
   still gets replaced, and "Ann" never matches inside "Annual".
   A **case variant found only at apply** (e.g., `JOHN SMITH` when only `John Smith` was
   reviewed) is added to its group as a new surface form, with a case-matched fake checked
   against the collision rules. That way the exact-string mapping stays complete.
3. Columns marked "replace whole value" replace the full cell value through the mapping.
4. The review table shows **propagated counts**, so the user can see what step 2 will touch
   before applying.

## 6. Formats

Every writer edits the file at the **XML / content level** and leaves untouched parts
byte-identical.

| Format | Approach | Text locations covered |
|---|---|---|
| **CSV** | `csv` module with the detected (or overridden) encoding, delimiter, quote character and line ending. Writes a BOM if the input had one | All fields. The guarantee is **same dialect, equal values**, not byte-identical output (per-field quoting may change) |
| **XLSX** | **zip/XML-level rewrite** (lxml), not openpyxl save. All other parts are copied byte-for-byte, so charts, shapes, pivots, sparklines and formatting survive | `sharedStrings.xml` (including rich-text runs), inline strings, cell comments/notes, header/footer, hyperlink targets and display text, data-validation lists, string literals in formulas, chart titles and cached labels, docProps (core/app/custom). Sheets stay **unrenamed**: a sheet name holding an entity counts as a **blocking residual hit** (§3 step 6). **Pivot caches** are scrubbed (cache records + shared items) with `refreshOnLoad` set. Hidden sheets are processed. External-link caches are cleared |
| **DOCX** | **XML-level**: walks every part (`document`, `header*`, `footer*`, `footnotes`, `endnotes`, `comments`, text boxes inside the document) per paragraph, joining all `w:t` runs, including those inside hyperlinks, content controls and insertions. Replacement text goes into the first run of a span and the rest is trimmed from the following runs | Body text, tables, text boxes, headers/footers, foot/endnotes, comments (text + author + initials), `w:delText` and `w:instrText` (field codes), hyperlink targets in `.rels` (`mailto:`), image alt text, docProps core/app/custom (author, company, manager…), `customXml`. **Tracked changes are always accepted before scrubbing**, at the XML level: `w:del` and `w:moveFrom` are dropped, `w:ins` and `w:moveTo` are unwrapped, `*PrChange` is dropped, and paragraphs whose paragraph mark was deleted are merged. The revision count is shown at upload and in the report |
| **PDF** | **PyMuPDF**. Characters are extracted with `get_text("rawdict")`, the §5.3 matching runs over the page text, and each match is mapped to **per-character bboxes** (one rect per line when an entity wraps; the fake is split across those rects). Redaction uses explicit `images=PDF_REDACT_IMAGE_NONE` and `graphics=` none, with rects slightly shrunk so neighbouring glyphs are kept. The fake is then drawn at the original size and position (Helvetica fallback, shrunk to fit) | Page text, metadata + XMP, TOC/bookmarks, link URIs, annotation contents/authors (scrubbed, or removed if unparseable), form-field values, embedded files (removed and reported), JavaScript (removed). Saved with `garbage=4, clean=True, deflate=True`, **never incrementally**. Pages whose text extracts as garbage (no ToUnicode map) or that have no text are flagged as unsupported |

Licensing note: PyMuPDF is AGPL. That's fine for internal use, but it matters if the tool is
ever distributed.

## 7. Backend API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Upload a file plus options. Returns `job_id` and starts extract + detect |
| `GET` | `/jobs/{id}` | Status (`detecting / review / applying / done / failed`), progress, and errors |
| `GET` | `/jobs/{id}/columns` | Column view (spreadsheets) |
| `GET` / `PUT` | `/jobs/{id}/entities` | Get, or save review edits (include, fake, type, added terms, column modes) |
| `POST` | `/jobs/{id}/apply` | Apply + leak check. The body includes the mapping passphrase |
| `POST` | `/jobs/{id}/acknowledge` | Acknowledge blocking residual hits, which unlocks download |
| `GET` | `/jobs/{id}/output` | Scrubbed file. Returns 409 while blocking hits are unacknowledged |
| `GET` | `/jobs/{id}/mapping` | `mapping.enc` |
| `GET` | `/jobs/{id}/report` | Counts by type, residual leak hits, unsupported items, timings |
| `DELETE` | `/jobs/{id}` | Delete the job folder |
| `POST` | `/restore` | Scrubbed file + `mapping.enc` + passphrase → original (CSV/XLSX/DOCX) |
| `GET` | `/health` | Model loaded, device, versions |

The server binds to `127.0.0.1`, and uploads are capped at 20 MB (configurable).

## 8. Frontend (Streamlit)

- **Scrub page**: upload + options → progress bar → (column view) → entity table
  (`st.data_editor`, with a type filter, "exclude all of type X" and an add-term form) →
  passphrase (entered twice) + Apply → leak-check results (acknowledge if blocking) →
  download buttons + report.
- **Restore page**: upload a scrubbed file + `mapping.enc` + passphrase → download the
  original.
- **Sidebar**: recent jobs (open / delete) and defaults (types, threshold, custom regex, phone
  region(s), fake-name locale).

## 9. Performance (M4, CPU) — targets and worst case

- Model load is about 10 s; the model is ~1 GB+ on disk after the first download.
- About 20–60 short units per second on CPU.
- Typical 20 MB CSV (~200k cells, heavy repetition, name columns in "replace whole value"
  mode): **under ~10 minutes**.
- **Worst case**: a free-text column with ~200k unique long cells could take **1–2 hours** of
  NER. The UI shows an ETA after the first 500 units, and per-column choices let the user cut
  it down.
- 100-page text PDF: ~1–2 minutes.
- `scripts/bench.py` measures these numbers on synthetic data; the results go in the README.

## 10. Project layout

```
entity-scrub/
  pyproject.toml  Makefile  README.md  SPEC.md
  scrub/                      # core library
    units.py  matching.py  apply.py  restore.py  leakcheck.py  config.py
    detect/  gliner_adapter.py  regex_detector.py  chunking.py  grouping.py
    fake/    generator.py  mapping.py  crypto.py
    formats/ csv_fmt.py  xlsx_fmt.py  docx_fmt.py  pdf_fmt.py
  backend/  app.py  worker.py
  frontend/ app.py  pages/1_Scrub.py  pages/2_Restore.py
  tests/    fixtures/ (synthetic only)  test_*.py
  scripts/  make_fixtures.py  bench.py
```

## 11. Testing and leak check

- **Leak check (runs in-app on every job and in tests)**:
  - Re-extract the output with an **independent path**. For OOXML: unzip and parse the text of
    every XML part. For PDF: `pdfminer.six` text, plus metadata, annotations, TOC and links.
  - Grep for every approved original, case-insensitive and word-bounded. Hits **block**
    download (§3 step 6).
  - For DOCX, assert that **no revision elements remain** (`w:del`, `w:ins`, `w:moveFrom`,
    `w:moveTo`, `*PrChange`).
  - Run a **second detection pass** on the output with the same model, threshold and phone
    regions. Report any new entities that aren't known fakes as non-blocking warnings, since
    they may be misses.
- **Round trip**: scrub then restore must give back the original exactly (cell/paragraph text
  and formatting) for XLSX and DOCX, and equal values for CSV. For DOCX with tracked changes,
  the baseline is the **revisions-accepted** document, not the original.
- **Mapping**: a wrong passphrase fails cleanly, and no plaintext mapping file ever appears in
  the job folder.
- **Fixtures** (generated, synthetic) cover the tricky cases: an entity split across DOCX runs,
  hyperlinks, text boxes, comments, tracked changes, an XLSX pivot + chart + data validation +
  rich text, phone numbers stored as numbers, "Ann" vs "Annual", a PDF entity wrapping across a
  line, PDF annotations/bookmarks, and fakes that collide with text in the source.
- Unit tests use a mocked GLiNER, and a small set of tests is marked `@slow` to run the real
  model.

## 12. Later

- A persistent mapping across files (opt-in "project").
- OCR, `.pptx`, `.txt`, JSON, and PDF restore.
- Inferring usernames and email local parts from names.

## 13. Decisions (formerly open questions)

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Mapping security | **Always encrypted.** The passphrase is at least 12 characters, and the plaintext mapping never touches disk. Originals are deleted from the job folder when the job finishes | The mapping undoes the whole scrub, and the likeliest leak is it travelling next to the output. A lost passphrase costs little because you still have the original |
| 2 | Language / fake style | **Multilingual model. One Faker locale per job** (dropdown, default `en_US`), never guessed per name | A missed non-English name is a leak. Guessing per name is unreliable and hints at the real person's origin |
| 3 | Phone region | **Set per job**: prefilled from the last job, then the macOS locale, then `US`. Optional extra regions. `+`-prefixed numbers always parse | A wrong region silently misses local numbers, and trying every region floods review with false positives |
| 4 | DOCX tracked changes | **Always accept all revisions, then scrub**, and the report says so | Refusing only makes you do the same thing in Word. Scrubbing inside revisions is fragile and leaves deleted text in the file |

Other gaps resolved at the same time:
- The size cap is 20 MB.
- Overlapping spans follow a containment rule (§4.3).
- Case variants found at apply get their own mapping entries (§5.3).
- Leak-check gating is defined (§3 step 6).
- The report holds no original strings.
- An entity in a sheet name is a blocking hit.
- The rule for linking an email to a person is defined (§5.1).

## Appendix: changes from v0.2
- §13 questions resolved with Fable as advisor (see the table).
- The mapping is always encrypted, and the job folder is cleaned up when a job finishes.
- Fake-name locale and phone region are set per job.
- DOCX revisions are always accepted before scrubbing.
- Leak-check hits now block download until acknowledged.
- Upload cap is 20 MB.
- Overlap and case-variant rules are defined.

## Appendix: changes from v0.1 after review
- §5.3: one matching rule for every format, with word boundaries and propagation to all units.
- The mapping stores exact surface forms plus an occurrence log, and the collision rules are
  stricter.
- XLSX and DOCX are edited at the XML level, with a much wider list of text locations; sheet
  renaming and `.xls` are dropped.
- PDF uses per-character bboxes, explicit redaction options, full scrubbing of non-page text,
  and a non-incremental save. PDF restore is cut.
- Chunking uses GLiNER's own token count with a truncation assert. Spreadsheet cells get header
  context, there's a column-level "replace whole value" mode, and regex runs on numeric cells.
- CPU is the default and MPS is opt-in. The model size estimate went up and the worst-case
  timing is now stated.
- The leak check now re-extracts the output independently and runs a second detection pass.
- Simplified: no SQLite, queue or Jobs page, the core is a library, and the passphrase moved out
  of the URL.
