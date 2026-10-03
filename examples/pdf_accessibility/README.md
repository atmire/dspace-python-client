# PDF accessibility audit for DSpace

Find out which PDF bitstreams in a DSpace repository are inaccessible, where in each file the
problems are, and how to fix them. Then act on the results: record exceptions for pre-existing
content, contact the authors of new submissions, verify replacement files, and add an OCR text
layer to scanned PDFs.

Built for the ADA Title II rule, which requires US public entities to meet **WCAG 2.1 AA**.
Supports **DSpace 9 and 10**.

> **Read this first.**
> - Run against a test or staging copy before production. Item metadata changes are
>   **dry runs unless you pass `--apply`**, and creating a metadata field is not easily undone.
> - **A PASS is not a compliance certificate.** It means "no machine-detectable problems".
>   About ten WCAG criteria (colour contrast, meaningful reading order, alt text that actually
>   describes the image, ...) always need a person, and every report says so.
> - **The scripts never decide whether an exception applies.** They record a decision your
>   institution's legal or compliance office has made.
> - Only the `ocr` remediation track adds files to DSpace. It does so only with `--apply` and
>   the placement you choose. It never deletes a bitstream, and it skips originals that are
>   not publicly downloadable. Everything else leaves bitstreams alone.

## How it works

```
detect.py   ─▶  pdf-a11y-reports/<host>-results.jsonl  ─▶  remediate.py
   │                                                          ├─ exceptions  (writes metadata, dry run by default)
   ├─ <host>-<time>-<scope>.csv                               ├─ contacts    (CSV for the institution to send)
   └─ item metadata (optional, dry run by default)            ├─ verify      (checks replacement PDFs)
                                                              └─ ocr         (adds a text layer to scans)
```

Every PDF is checked with **[outloud](https://github.com/visionably/outloud)**: PDF/UA-1
conformance rules (calibrated against veraPDF) plus semantic checks that a validator cannot
make, each mapped to WCAG 2.2 success criteria and the Matterhorn Protocol. This example adds
**OCR-001**, which flags pages that are images with no text layer (scans without OCR). outloud has
no rule for that yet ([outloud#4](https://github.com/visionably/outloud/issues/4)).

A **severity policy** decides what counts. Each PDF gets a verdict:

| Verdict | Meaning |
|---|---|
| `PASS` | No error or warning under the policy |
| `REVIEW` | Warnings only |
| `FAIL` | At least one error |
| `INCOMPLETE` | No errors found, but at least one rule crashed, so a pass cannot be claimed |
| `UNREADABLE` | The file could not be opened (corrupt, password-protected) |
| `CHECK_FAILED` | The check did not finish (timeout, crash); retried on the next run |
| `SKIPPED_TOO_LARGE` | Over `--max-size-mb` |
| `DOWNLOAD_FAILED` | The file could not be downloaded; retried on the next run |
| `NOT_PDF` | Named `.pdf` but is not a PDF |
| `NO_PDF` | (CSV only) the item has no PDF in its ORIGINAL bundle |

Each report lists every finding with its page numbers, what was found, how to fix it and who
can (author or repository staff):

```
FAIL: 2 error(s), 0 warning(s), 1 note(s) · outloud 0.2.0 · policy wcag21-aa/54f271 · 2026-10-03
[ERROR] Pages are images with no text layer (OCR-001): pages 1-2 (2×) · WCAG 1.1.1, 1.4.5
  Found: 2 of 2 page(s) are images with no text a reader can get at
  Fix: These pages are pictures of text (a scan without OCR), so a screen reader has nothing to
  read. Run OCR on the file (Acrobat Pro: Scan & OCR > Recognize text; free: OCRmyPDF), ...
  Who: author or repository staff
[ERROR] Figure has no alternative text (FIG-001): pages 1, 2 (2×) · WCAG 1.1.1
  ...
Needs a person: 9 WCAG criteria cannot be checked automatically (1.3.3, 1.4.1, 1.4.3, 1.4.4
and 5 more), e.g. colour contrast and a meaningful reading order.
```

## Setup

```bash
source venv/bin/activate
pip install -e ".[a11y]"
```

The `a11y` extra installs outloud and its dependencies. One of them, PyMuPDF, is AGPL-3.0;
see `THIRD_PARTY_LICENSES.md`.

For the `ocr` remediation track, also install `pip install -e ".[a11y-ocr]"`. It needs pip only,
with no Tesseract or Ghostscript, and adds about 300 MB (mostly OpenCV).

**A site administrator account is required.** After login the script checks the
`administratorOf` authorization on the Site and stops if the account isn't a site admin. Only a
site admin can read every bitstream (including embargoed files and in-progress submissions),
list every workspace and workflow item, and add metadata fields.

### Recommended DSpace configuration

1. **Keep the audit out of download statistics.** Every check downloads the PDF through
   `/api/core/bitstreams/{uuid}/content`, which DSpace counts as a download. The scripts send the
   User-Agent `dspace-a11y-audit/0.1 (+https://github.com/atmire/dspace-python-client)`. Add a
   line `dspace-a11y-audit` to a file in `[dspace]/config/spiders/agents/` so DSpace treats it
   as a robot.
2. **Hide the report and exception fields from the public** before using metadata mode.
   Otherwise the reports appear on the full item page and in OAI-PMH. In `local.cfg`:
   ```
   metadata.hide.local.accessibility.report = true
   metadata.hide.local.accessibility.exception = true
   ```
3. **Reindex Discovery before collection runs** if the index may be stale. Collection scope
   finds items through Discovery.

## Detection: `detect.py`

```bash
python examples/pdf_accessibility/detect.py                     # interactive menus
python examples/pdf_accessibility/detect.py --base-url https://repo.example.org \
    --username admin@example.org --scope collection --uuid <collection-uuid> --output csv
```

The password is always prompted.

**Scope** (`--scope`):

| Scope | Input | What is checked |
|---|---|---|
| `bitstream` | `--uuid` of a bitstream | that one file |
| `item` | `--uuid` of an item | every PDF in the item's ORIGINAL bundle |
| `collection` | `--uuid` of a collection | every PDF of every item Discovery finds in it |
| `workspace` | `--which mine` or `all` | in-progress submissions: yours, or every user's |
| `workflow` | `--which all` or `mine` | every item in workflow, or only tasks in your pool/claimed by you |

**Output** (`--output`):
- `csv`: one row per PDF. Columns: `item_uuid, item_title, bitstream_uuid, bitstream_filename,
  verdict, errors, warnings, pages, analysis`. The full report is in `analysis`.
- `metadata` / `both`: the CSV, plus one value per PDF in an item metadata field (default
  `local.accessibility.report`, set with `--field`).
  - Each value starts with `[a11y:<bitstream-uuid>] <file name>`. A re-run replaces only values
    with a matching marker; other values and fields are untouched.
  - The field's schema must already exist; the script creates fields, never schemas.
  - Without `--apply`, nothing is written. The planned operations go to
    `<host>-<time>-planned-patches.jsonl` for review.
  - With `--apply`, you confirm by retyping the field name. If the field doesn't exist yet,
    the same confirmation creates it.

**Re-runs are cheap.** Every result is appended to `pdf-a11y-reports/<host>-results.jsonl`. A PDF
is checked again only when its checksum, the outloud version or the policy changed, or when its
last check did not finish. `--recheck` forces a fresh check.

**Other options:**

| Option | Default | Purpose |
|---|---|---|
| `--policy FILE` | `policy_default.yml` | Severity policy (see below) |
| `--workers N` | CPUs − 1, max 4 | Parallel checks (and items) |
| `--timeout S` | 120 | Seconds allowed per PDF; a stuck check is killed, not the run |
| `--max-size-mb N` | 250 | Larger PDFs are skipped |
| `--delay S` | 0.1 | Pause between REST requests |
| `--limit N` | none | Stop after N items (trial runs) |
| `--report-dir DIR` | `pdf-a11y-reports` | Where all output goes |
| `--skip-version-check` | off | Skip the DSpace version check |

## The severity policy

`policy_default.yml` is the default policy. Copy it, adjust it, and pass `--policy`.

```yaml
standard: wcag21-aa       # wcag21-aa | wcag22-aa | pdfua1
no_wcag_mapping: info     # rules with no WCAG criterion in the standard (e.g. DOC-012 "no pdfuaid")
layers:
  semantic: warning       # heuristic checks: at most REVIEW, never FAIL on their own
matterhorn: {}            # override a whole checkpoint, e.g. {"07": warning}
rules:                    # override single rules
  HDG-002: warning
  OCR-001: error
verdict: {fail_on: error, review_on: warning}
```

A rule's effective severity is decided like this:
1. A `rules` entry wins.
2. Otherwise a `matterhorn` entry applies.
3. Otherwise outloud's severity applies, with two limits:
   - semantic rules are capped at `layers.semantic`;
   - rules with no WCAG criterion in the chosen standard are lowered to `no_wcag_mapping`.

`ignore` removes a rule from reports entirely. Run `outloud --list-rules` to see every rule ID,
its severity, Matterhorn checkpoint and WCAG mapping.

Fix advice comes from outloud's catalogue. `advice_overrides.yml` rewrites it for the findings
most common in repository content, for authors who use Word, LibreOffice, LaTeX or Acrobat.
Edits there also apply to cached results the next time a report is written.

## Remediation: `remediate.py`

All tracks read `results.jsonl` from detect.py, so run detection first.

### `exceptions`: existing content (writes metadata)

```bash
python examples/pdf_accessibility/remediate.py --track exceptions --cutoff 2027-04-26 [--apply]
```

For archived items whose PDFs FAIL (add `--include-review` for REVIEW too):
- **Eligibility date:** `dc.date.issued` if it's a valid date, otherwise `dc.date.accessioned`.
  A partial date counts as the latest day it could mean: `2027` is 2027-12-31 and `2027-04` is
  2027-04-30. An ambiguous date never qualifies by accident.
- **Cutoff:** an item qualifies when its date is before the cutoff. There is no default cutoff,
  because it depends on your institution's compliance date.
- **What is written:** field and value template come from `exceptions.yml`. The default is
  `local.accessibility.exception` = `preexisting-document; cutoff …; basis: …; recorded …`.
  Adapt the wording with your compliance office.
- **Safeguards:**
  - Items that already have a value in the field are skipped, never overwritten.
  - Without `--apply` this is a dry run.
  - With `--apply` you retype the field name to confirm. If the field is missing, it is created
    after the same confirmation.
  - Every decision (qualifies, does not qualify, no usable date, skipped, written) goes to
    `<host>-<time>-exceptions.csv`.

### `contacts`: new content (read-only)

For workspace and workflow items whose PDFs FAIL (or REVIEW with `--include-review`), writes
`<host>-<time>-author-contacts.csv`. Columns: submitter name and email, item, files, and the full
reports. Nothing is sent; how to contact authors is up to the institution.

### `verify`: replacement files (read-only)

When someone uploads a fixed PDF next to the original, this track checks that the new file
passes and that nothing was lost.

**Pairing.** Originals are PDFs that had FAIL or REVIEW; candidates are PDFs added to the item
after detection ran.
- File names are compared without extension, case and suffixes like `_accessible`,
  `-remediated`, `_ua`, `_v2` and `(1)`.
- An item with exactly one original and one candidate is paired even if the names differ.
- Anything ambiguous is reported as `UNPAIRED`; nothing is guessed.

**Checking.** Each pair is checked with outloud's source comparison
(`check(new, source=original)`). `VERIFIED` means:
- the replacement's verdict is PASS (or REVIEW with `--accept-review`); and
- outloud found no lost structure, lost text or changed page appearance (SEM-001 to SEM-003).

The results go to `<host>-<time>-verify.csv`. Afterwards, run `detect.py` on the item to record
the new file in the results.

### `ocr`: add a text layer to scanned PDFs (can add files to DSpace)

```bash
python examples/pdf_accessibility/remediate.py --track ocr --placement local
python examples/pdf_accessibility/remediate.py --track ocr --placement alongside --item <uuid> --apply
```

This track works on PDFs whose detection result has OCR-001 (pages that are images without
text).

**How the OCR is done.** OCR uses [RapidOCR](https://github.com/RapidAI/RapidOCR) (Apache-2.0;
PaddleOCR models on ONNX Runtime, shipped in the wheel). Only the image-only pages are OCR'd;
pages that already have text are left alone. Each recognised line is written as invisible
text over the page image, so the page looks exactly as before. The text uses a generated
font that is embedded and maps every character correctly. The document language is set from
the item's `dc.language.iso` (or `--lang`).

**Every result is checked before it can be uploaded.** outloud compares it with the original:
- no lost structure or text and no visual change (SEM-001 to SEM-003);
- no new text or font problems;
- a mean OCR confidence of at least `--min-confidence` (default 0.80).

Results that fall short are kept locally as `NEEDS_REVIEW`.

**What happens with the result is the manager's choice (`--placement`):**

| Placement | Effect |
|---|---|
| `local` (default) | Saved under `pdf-a11y-reports/<host>-ocr/<item>/` for review. Nothing changes in DSpace. |
| `alongside` | Uploaded as `<name>_ocr.pdf` next to the original in ORIGINAL. |
| `replace` | Uploaded under the original name to ORIGINAL. The original is moved to the `PRESERVATION` bundle (`--keep-bundle`), and the primary-bitstream flag moves to the new file. Nothing is deleted. |

**Safeguards for `alongside` and `replace`:**
- They upload only with `--apply`, after you type the placement name to confirm. Without
  `--apply` you get a dry run (`WOULD_UPLOAD`, `WOULD_REPLACE`).
- They only touch originals that **Anonymous can download**. A new upload takes on its
  bundle's access policies, not the original file's, so an embargoed or restricted original
  must not get a more open copy. Such files are saved locally as `SAVED_RESTRICTED`, to upload
  by hand with the right policies.
- Every upload gets a `dc.description` on the new file and a `dc.description.provenance` entry
  on the item, saying what was done, by whom and with which engine.
- Re-runs are safe. An original that already has a `_ocr` copy is skipped (`ALREADY_DONE`),
  and a replaced original is no longer in ORIGINAL.
- `--item <uuid>` limits the run to one item, to try it out first.

**What OCR does not do: it adds text, not tags.** A screen reader goes from silence to reading
the page, but the file still fails the tagging checks (DOC-001/DOC-002) until it is tagged.
OCR quality also varies. In trials, printed 19th-century pages matched or beat Acrobat's
Paper Capture OCR, and handwritten pages were mostly readable but contained errors, which a
screen reader reads out as-is. Review handwritten and low-confidence material.

Run `detect.py` on the items afterwards to record the new files. Details of every file go to
`<host>-<time>-ocr.csv`.

## Files

| File | Purpose |
|---|---|
| `detect.py`, `remediate.py` | Entry points |
| `engine.py` | outloud in killable worker processes; OCR-001 |
| `policy.py`, `policy_default.yml` | Severity policy |
| `analysis.py` | Raw result + policy → findings and verdict |
| `advice.py`, `advice_overrides.yml` | Report text |
| `scope.py` | The five scopes; PDFs of an item |
| `sinks.py`, `fields.py` | CSV, metadata patches, field creation |
| `state.py` | `results.jsonl` cache |
| `eligibility.py`, `exceptions.yml` | Exception dates and wording |
| `pairing.py` | Replacement pairing |
| `ocr.py`, `ocr_track.py` | Pip-only OCR text layer; the `ocr` track and its placements |
| `session.py` | Login, version and admin checks, session refresh |
| `DESIGN.md` | Design and decisions |

Tests: `tests/test_pdf_accessibility.py` and `tests/test_core_a11y_methods.py`.
