# Entity Scrub

Entity Scrub is a local tool. It finds real names, companies, places, addresses, emails, phone
numbers and IDs in **CSV, Excel (.xlsx), Word (.docx) and PDF** files, and replaces them with
consistent fake values. Detection uses [GLiNER](https://github.com/urchade/GLiNER) plus regex
rules. You review what was found before anything is rewritten. An encrypted mapping file lets
you undo the scrub later (CSV, Excel and Word).

Everything runs on your machine. The only network access is the one-time model download from
Hugging Face. The design is in [SPEC.md](SPEC.md).

## Quick start (macOS / Linux)

```bash
./start.sh
```

On the first run, the script creates `.venv` and installs everything. It uses
[uv](https://docs.astral.sh/uv/) if you have it; otherwise it falls back to `python3.12` or
`python3.11` with `venv` and pip. It then starts:
- the API on `127.0.0.1:8765`,
- the UI on http://127.0.0.1:8501, which opens in your browser on macOS.

Ctrl-C stops both. Use `./start.sh --reinstall` to rebuild the venv. The ports can be changed
with `API_PORT=… UI_PORT=… ./start.sh`.

The first start downloads `urchade/gliner_multi_pii-v1` (~1.2 GB) into the Hugging Face cache;
after that it loads in seconds.

### With make (if you use uv)

```bash
uv sync
make dev        # API + UI; or `make api` / `make ui` separately
```

### Using it
1. **Scrub page**: upload a file and pick the entity types, the fake-name locale and the phone
   region.
2. **Review** what was found:
   - Untick false positives.
   - Edit a fake value.
   - Change an entity's type.
   - Add missed terms.
   - For spreadsheets, set a column to **whole**, which replaces every value in it as one
     type.

   Suggested variants (a standalone surname, a company without its "Inc") start unticked.
3. **Apply**: enter a passphrase of at least 12 characters, twice.
4. **Download**:
   - The scrubbed file. If the leak check finds an approved name still in the output, you have
     to acknowledge it before you can download.
   - The **encrypted** mapping.
   - The report.
5. **Restore page**: upload a scrubbed file with its mapping and passphrase to get the original
   back. PDF restore isn't supported; keep your original PDF.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SCRUB_HOME` | `~/.entity-scrub` | Job folders |
| `SCRUB_MODEL` | `urchade/gliner_multi_pii-v1` | Any GLiNER model on Hugging Face |
| `SCRUB_DEVICE` | `cpu` | `mps` (Apple GPU, opt-in) or `cuda` |
| `SCRUB_MAX_MB` | `20` | Upload limit |
| `SCRUB_PRELOAD` | `1` | Load the model when the API starts |
| `SCRUB_API` | `http://127.0.0.1:8765` | Where the UI finds the API |

## Security notes

- The API listens on `127.0.0.1` only.
- The **mapping is always encrypted** (scrypt → Fernet). If you lose the passphrase, the scrub
  cannot be reversed.
- While a job is in review, its folder (`~/.entity-scrub/jobs/<id>`, mode 700) holds your
  **original file and the detected entities in plaintext**. They are deleted as soon as the job
  finishes or fails. What's kept is the output, the encrypted mapping, the report (which has no
  real names), and `review_hits.json`. That last file holds the residual hits shown in the UI,
  so it can contain originals.
- Jobs are purged at startup: unfinished ones after 24 hours, all jobs after 7 days. You can
  also delete a job from the sidebar.
- Turn on FileVault.
- Never share the mapping file together with the scrubbed file.

## How the replacement works

- **Detection**: regex finds emails, phone numbers (via `phonenumbers` with your region), URLs,
  IPs, IBANs (checksum-validated), cards (Luhn-checked) and US SSNs, plus any custom patterns
  you add. GLiNER finds person, organization, location and address. Spreadsheet cells are sent
  as `"<header>: <value>"` for context. Identical inputs are de-duplicated, and long text is
  chunked under GLiNER's token limit, with an assert that nothing is truncated.
- **Matching**: every approved form is replaced everywhere in the file, as whole words only.
  "Ann" never matches inside "Annual". Case variants are replaced with the fake in the same
  case ("JANE DOE" becomes "KATHRYN SCOTT"). Names are not matched when the text is all
  lowercase, so the first name "Will" never replaces the word "will".
- **Fakes**: Faker is seeded per job and uses one locale per job. Fakes keep the original's
  shape (titles, initials, company suffix, phone format, Luhn-valid cards) and are chosen close
  in length to the original. A fake is rejected if it:
  - appears anywhere in the file,
  - overlaps another fake or a real value,
  - reuses a word from a real name.
- **Formats**:
  - **CSV**: keeps the encoding, BOM, delimiter and line endings.
  - **XLSX / DOCX**: edited at the XML level, and every untouched part is copied byte-for-byte.
    That covers shared strings and rich text, comments, headers and footers, charts, text
    boxes, hyperlinks, data validation, pivot caches, docProps and VML.
  - **DOCX**: tracked changes are always accepted first.
  - **PDF**: each name is blacked out with a true text redaction (images and vector art are
    kept) and the fake is drawn at the same spot. Metadata, bookmarks, links, annotations and
    form fields are scrubbed too, and JavaScript, embedded files and XMP are removed.
- **Leak check**: the output is re-read independently (raw XML of every part, or pdfminer for
  PDFs) and searched for every approved original. An optional second detection pass flags
  anything new as a non-blocking warning.

## Tests

```bash
make test        # fast tests with a stand-in NER model
make test-slow   # also runs the real GLiNER model
make bench       # detection speed on synthetic data
```

## Known limitations

- Scanned PDFs (no text layer) and PDFs whose text can't be extracted are reported as
  unsupported, not scrubbed. There's no OCR.
- In PDFs the fake is drawn in Helvetica, shrunk to fit (down to 70% of the original size).
  It's close to the original but not pixel-perfect.
- Excel sheet names are never renamed. A name in a sheet tab shows up as a blocking leak-check
  hit, and you have to rename the tab yourself.
- Pivot caches are scrubbed and set to refresh on open, but no automated test covers this,
  because openpyxl can't create pivots.
- Restore swaps each fake string back to its original. This is exact because of the collision
  rules, but if you edit the scrubbed file and type one of the fake names yourself, restore will
  turn it into the real one.
