# PDF accessibility audit for DSpace: design

Status: implemented on branch `pdf-accessibility` (2026-10-03). Notes marked **As built** record
where the implementation differs from, or settled, the original design.

## Context

US public entities must meet WCAG 2.1 AA for their web content under the ADA Title II rule.
For DSpace repositories, the hardest part is the PDFs in item bitstreams. Institutions need to know:

1. Which PDFs are inaccessible, where in the file, and how to fix them.
2. What to do with each one. That splits in two:
   - **New content** (workspace and workflow items): the author can usually still be reached and asked
     for an accessible version.
   - **Existing content** (archived items): the realistic step is often to record that the item falls
     under an exception for pre-existing documents.

So this example has two separate entry points: **detection** (`detect.py`) and **remediation**
(`remediate.py`). The only thing connecting them is a results file.

### Non-goals

- Deciding legal compliance. A PASS means "no machine-detectable problems". About ten WCAG criteria,
  such as contrast and meaningful reading order, always need a person, and every report says so.
- Deciding whether an exception legally applies. The tool records a decision the institution has made.
- Changing PDF files. Nothing in this example rewrites or replaces a bitstream.

## Checking engine: outloud

[outloud](https://github.com/visionably/outloud) (Apache-2.0), installed through a new optional extra:

```toml
a11y = ["outloud>=0.2,<0.3"]
```

Why outloud:

- **Pure Python API, no Java.** `outloud.check(path) -> Result` never raises on a bad file; a file it
  cannot open gets the verdict `unreadable`.
- **Findings that are ready to act on:**
  - rule ID, severity, message and evidence for each finding;
  - a 1-based `page`, plus `boxes` listing every occurrence with its page;
  - a `count` that folds repeats together, so an untagged document gives a handful of findings,
    not thousands.
- **A catalogue entry for each rule** (`rules/catalogue.yaml`) with:
  - ISO 14289-1 clause and Matterhorn checkpoint;
  - WCAG 2.2 criteria;
  - `layer` (conformance or semantic);
  - a one-sentence `fix`.
- **Criteria results:** each Matterhorn checkpoint and each WCAG A/AA criterion gets
  pass, fail, not applicable or "manual" (needs a person).
- **`check(new, source=old)`** compares a remediated file with its original (rules SEM-001 to SEM-003).
  The verify-replacement track uses this.

Known gaps and how we cover them:

| Gap | Handling |
|---|---|
| No rule for image-only scans (no text layer). Such files get "tag the document" advice, which can't be done before OCR. | Our own `OCR-001` check (see below). Reported upstream as [outloud#4](https://github.com/visionably/outloud/issues/4); remove ours if it lands upstream. |
| Severity can't be configured (only `--only` and `--skip`). | Our policy layer on top (see "Severity policy"). |
| Version 0.x, so rule IDs and output may change. | Pinned to `<0.3`. The outloud version is stored with every result. |
| CPU-bound and synchronous. | Run in worker processes driven from asyncio. |

Licensing: outloud is Apache-2.0. Its dependency PyMuPDF is AGPL-3.0. Both are compatible with this
project's GPL-3.0, and the README must say so.

Phase 0 (below) decides whether outloud holds up on real repository content. If it doesn't, only
`engine.py` changes: the policy is keyed on rule IDs, Matterhorn checkpoints and WCAG criteria, and
veraPDF or another engine can be mapped onto those.

## Detection flow (`detect.py`)

```
login ─▶ admin check ─▶ choose scope ─▶ choose output ─▶ enumerate PDFs ─▶ skip if cached
      ─▶ download (async, concurrent) ─▶ check (process pool) ─▶ apply policy ─▶ write results/CSV/metadata
```

1. **Login** (`session.py`), then `verify_server_version`. `TARGET_VERSIONS = ["9.0", "10.0"]`
   (DSpace 9 and later only). **As built:** the login loop is local rather than
   `prompt_and_authenticate`, because a long run must keep the password that worked: it needs it
   to log in again when refreshing the token fails.
2. **Admin check.** Call `GET /api/authz/authorizations/search/object?uri=<site>&feature=administratorOf`.
   If the account is not a site administrator, abort with a clear message. Only a site admin can read
   every bitstream, including embargoed ones and those of in-progress items.
3. **Scope** (interactive menu; every choice can also be passed as a CLI flag):

   | Mode | Input | Source |
   |---|---|---|
   | Bitstream | bitstream UUID | `core/bitstreams/{id}` → `/bundle/item` |
   | Item | item UUID | ORIGINAL bundle → bitstreams |
   | Collection | collection UUID | `discover/search/objects?scope=<uuid>&dsoType=item`, paged |
   | Workspace | sub-choice: **mine** or **all users** | `submission/workspaceitems/search/findBySubmitter?uuid=<me>`, or `submission/workspaceitems` (admin only) |
   | Workflow | sub-choice: **all workflow items** or **my tasks** | `workflow/workflowitems` (admin only), or `workflow/pooltasks` + `workflow/claimedtasks` for the current user |

   Collection mode depends on the Discovery index. The README advises reindexing first if the index
   may be stale.
4. **Output:** CSV, item metadata, or both. `results.jsonl` is always written.
5. **Enumerate** only the ORIGINAL bundle. A bitstream counts as a PDF if its format is
   `application/pdf`; if the format is unknown, we fall back to a `%PDF` check on the first bytes.
6. **Cache.** Skip a bitstream when `results.jsonl` already holds a result with the same
   `(bitstream uuid, checksum, outloud version, policy hash)`. Re-runs are cheap, and changing the
   policy or upgrading outloud triggers a re-check.
7. **Download** to a temporary directory with bounded concurrency and stream to disk. Delete each
   file after its check.
   - Files above the size cap (default 250 MB) are not checked and get the verdict `SKIPPED_TOO_LARGE`.
   - Requests use a distinctive User-Agent (`dspace-a11y-audit/<version>`). The README tells admins to
     add it to `config/spiders/agents`, so the audit doesn't inflate download statistics.
8. **Check** in worker processes (`engine.py`):
   - **As built:** one long-lived process per worker slot, not a `ProcessPoolExecutor`. On timeout
     (default 120 s) only the stuck worker is killed and replaced; checks running in the other
     workers carry on. Workers are recycled after 200 files. A worker that dies (out of memory, a
     crash in PDF parsing) gives `CHECK_FAILED` for that file only.
   - If any rule run reports `status == "crashed"`, the file can no longer PASS. **As built:** a file
     with errors is still `FAIL`; without errors it becomes `INCOMPLETE`.
9. **Apply the policy**, render the advice text, and write the outputs.

### Verdicts

`PASS`, `REVIEW` (warnings only), `FAIL` (at least one effective error), `INCOMPLETE` (no errors, but a
rule crashed), `UNREADABLE` (corrupt or password-protected), `CHECK_FAILED` (timeout or worker
crash), `SKIPPED_TOO_LARGE`, `DOWNLOAD_FAILED`, `NOT_PDF` (named `.pdf`, but not a PDF), and, in the
CSV only, `NO_PDF`. `CHECK_FAILED` and `DOWNLOAD_FAILED` are never cached, so the next run retries them.

### OCR-001: image-only pages

Our own check, built on PyMuPDF (already installed as an outloud dependency).

- **Trigger:** a page has image content, no text that can be extracted, and its images cover most of
  the page.
- **Reported per page**, so mixed files (a born-digital body with scanned appendices) are caught.
- **Severity:** error. WCAG 1.1.1 and 1.4.5.
- **When it fires on most pages, it comes first in the report.** Its advice replaces the DOC-002
  "tag the document" advice: *"Run OCR (e.g. OCRmyPDF) to add a text layer, then tag the document."*

## Severity policy

Shipped as `policy_default.yml`. Institutions copy it and pass `--policy`.

```yaml
standard: wcag21-aa       # only WCAG 2.1 A/AA criteria count toward the verdict
inherit: outloud          # start from outloud's severity for each rule
no_wcag_mapping: info     # PDF/UA housekeeping (e.g. DOC-012 "no pdfuaid"): reported, not counted
layers:
  semantic: warning       # heuristic checks: reported with advice, at most REVIEW, never FAIL on their own
matterhorn: {}            # override a whole checkpoint, e.g. {"07": warning}
rules:                    # override single rules
  HDG-002: warning        # "first heading is not H1": common in journal articles
  OCR-001: error
verdict:
  fail_on: error
  review_on: warning
```

**How a rule's effective severity is resolved:**

1. An explicit `rules` entry wins.
2. Otherwise a `matterhorn` checkpoint override applies.
3. Otherwise:
   - start from outloud's own severity;
   - cap semantic rules at `layers.semantic`;
   - downgrade a rule to `no_wcag_mapping` when none of its WCAG criteria are in the chosen standard.

   Criteria only in WCAG 2.2 (2.4.11, 2.4.12, 2.5.7, 2.5.8, 3.2.6, 3.3.7, 3.3.8) don't count under
   `wcag21-aa`.

The verdict is recalculated from effective severities and does not use outloud's own verdict.
`policy_hash` is the SHA-256 of the normalised policy file.

`standard` values: `wcag21-aa` (default), `wcag22-aa`, `pdfua1` (outloud's severities unchanged).

## Advice text

The same text goes into the CSV's last column and the metadata value:

```
FAIL: 3 errors, 1 warning · outloud 0.2.0 · policy wcag21-aa/7c1e02 · 2026-10-03
[ERROR] Figure has no alternative text (FIG-001), pages 1, 2 (2×) · WCAG 1.1.1
  Fix: Add alt text describing what the image shows and why; mark decorative images as artifacts.
  Who: author
[ERROR] Heading level is skipped: H1 → H3 "Methods" (HDG-001), page 2 · WCAG 1.3.1
  Fix: Use the next level down (H2 after H1), or promote the deeper heading.
  Who: author
[WARNING] ...
Needs human review: 10 WCAG criteria (e.g. 1.4.3 contrast, 1.3.2 meaningful sequence).
```

- The fix text comes from outloud's catalogue `fix` field.
- `advice_overrides.yml` replaces it for the roughly ten most common findings in repository content
  (DOC-002, DOC-010, DOC-013, FIG-001, TAG-001, HDG-001, TBL-001, TXT-002, OCR-001). The overrides use
  author-facing wording for Word, LibreOffice, LaTeX and Acrobat, and add a `who` field
  (`author` or `repository staff`).
- Pages are taken from `boxes`, deduplicated and sorted. They are shown as ranges ("pages 3–7, 12")
  and capped at 20 with "…". A finding with no page shows "whole document".

## Outputs

### `results.jsonl` (always written; the contract with `remediate.py`)

One line per bitstream:

```json
{"item_uuid": "...", "item_title": "...",
 "item_state": "archived|withdrawn|workspace|workflow|in-progress",
 "workspaceitem_id": null, "workflowitem_id": null,
 "bitstream_uuid": "...", "bitstream_name": "...", "checksum": "md5:...", "size": 123,
 "verdict": "FAIL", "errors": 3, "warnings": 1, "pages": 12, "error": null,
 "analysis": {"verdict": "FAIL", "findings": [{"rule": "FIG-001", "severity": "error",
               "engine_severity": "error", "layer": "conformance", "pages": [1, 2], "count": 2,
               "message": "...", "evidence": "...", "wcag": ["1.1.1"], "matterhorn": "13",
               "fix": "..."}],
              "manual_criteria": ["1.4.3", "..."], "crashed_rules": [],
              "engine": {"outloud": "0.2.0"}, "mostly_scanned": false, "...": "..."},
 "engine_version": "0.2.0", "policy_hash": "...", "checked_at": "2026-10-03T12:00:00Z"}
```

The file lives at `pdf-a11y-reports/<host>-results.jsonl`, is append-only, and the latest line
for a bitstream wins. This also lets an interrupted run resume (same pattern as
`link_author_authorities/state.py`). **As built:** `item_state` is `in-progress` when the item was
reached through the bitstream or item scope and is not archived; those scopes do not look up
whether it is in workspace or workflow. Reports are re-rendered from the stored `analysis` each
time, so edits to the advice overrides also apply to cached results.

### CSV

Columns: `item_uuid, item_title, bitstream_uuid, bitstream_filename, verdict, errors, warnings, pages, analysis`.

- `item_title` is the first `dc.title` value.
- `analysis` is the full advice text, multi-line and quoted.
- One row per bitstream.
- Items without a PDF get one row with verdict `NO_PDF` and empty bitstream columns. That makes it
  clear the item was looked at.

### Item metadata

**What gets written:** the field name is asked at run time; the suggested default is
`local.accessibility.report`. Each checked bitstream gets **one value containing the full report**,
prefixed with a marker:

```
[a11y:<bitstream-uuid>] <bitstream name>
FAIL: 3 errors, 1 warning · ...
```

**Field and schema handling:**

- The **schema must already exist**. If it doesn't, abort with a message saying which schema is
  missing. The script never creates schemas.
- If the **field** is missing, create it with `POST core/metadatafields?schemaId=`, after
  retype-to-confirm.

**Re-runs:** a re-run replaces only values whose marker matches a bitstream it just checked. Values
for other bitstreams and other fields are not touched. If a bitstream is gone from the item, its old
value is left alone, and the run summary reports it.

**Item states:**

- Archived items are written via `PATCH core/items/{uuid}`.
- Workspace and workflow items use the same endpoint. **As built:** confirmed for workspace items on
  DSpace 11.0-SNAPSHOT, the local test instance. Not yet checked on 9 and 10, or on workflow items.
- **As built:** each new value is appended with its own `add /metadata/<field>/-` operation. An
  `add /metadata/<field>` with a list stores only the first value when the field is empty; this
  was found live and is recorded in `docs/API_GOTCHAS.md`.

**Dry-run is the default.** It prints the planned patches and changes nothing. Writing needs
`--apply` and a final confirmation.

**Visibility:** the README tells admins to hide the field with
`metadata.hide.local.accessibility.report = true`. Without that, the report appears in the full item
view and in OAI.

## Remediation flow (`remediate.py`)

Reads `results.jsonl`. Three tracks, chosen from a menu. Every track starts in dry-run.

### 1. Exception tagging (existing content, writes metadata)

**Candidates:** results with verdict `FAIL` (optionally also `REVIEW`) for archived items.

**Eligibility date:**

1. `dc.date.issued`, if present and a legal ISO-8601 date (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or a full
   timestamp).
2. Otherwise `dc.date.accessioned`.
3. A partial date is read as the **latest moment it could mean**: `2027` becomes 2027-12-31 and
   `2027-04` becomes 2027-04-30. An ambiguous date therefore never qualifies.

The **cutoff date** is asked at run time. There is no built-in default, because it depends on the
institution's compliance date.

**What gets written:** an item qualifies when its eligibility date is before the cutoff. Field and
value template come from `exceptions.yml`, with a neutral default:

```yaml
field: local.accessibility.exception
value: "preexisting-document; cutoff {cutoff}; basis: {basis_field} {basis_value}; recorded {today}"
```

The README says plainly that the wording, and whether the exception applies, are for the
institution's legal or compliance office to decide.

**Safety:**
- Dry-run lists qualifying items, items that don't qualify (and why), and items without a usable date.
- Writing needs `--apply` and retype-to-confirm.
- Items that already have a value in the exception field are skipped, never overwritten.

### 2. Author contact list (new content, read-only)

**Candidates:** results with verdict `FAIL` or `REVIEW` for workspace and workflow items.

**Output:** a CSV with one row per submitter and item. Columns: submitter name and email, item
title, item state, the bitstreams and their verdicts, and the advice text.

The submitter comes from `get_item_submitter` (`core/items/{uuid}/submitter`, DSpace 9+).

Sending the emails is up to the institution. The script sends nothing.

### 3. Verify replacement (read-only)

**Purpose:** confirm that a fixed PDF uploaded to an item actually solves the problem and lost nothing.

**Pairing is automatic and conservative:**

- **Originals:** bitstreams in `results.jsonl` with verdict `FAIL` or `REVIEW` whose item still has them.
- **Candidates:** PDFs in the same item's ORIGINAL bundle that are not in `results.jsonl` for that
  item, i.e. uploaded after the detection run.
- **Matching:** normalise file names by dropping the extension, case and common suffixes
  (`_accessible`, `-remediated`, `_ua`, `_v2`, `(1)`, …). An original and a candidate are paired only
  when they match **exactly one-to-one**. If an item has exactly one original and one candidate, they
  are paired even when the names differ.
- **Anything ambiguous or unmatched** goes in an "unpaired" section of the report. Nothing is guessed.

**Each pair** is checked with `outloud.check(candidate, source=original)`, then the policy is applied.
The pair passes when:
- the candidate's verdict is PASS (or REVIEW, if configured); and
- SEM-001, SEM-002 and SEM-003 (lost structure, lost text, changed appearance) find nothing.

**Output:** a CSV of pairs with the original verdict, the new verdict and the regression findings.
An item where the original is gone but a new PDF exists is reported as `ORIGINAL_MISSING`.
**As built:** updating the report metadata from a verified pair is not implemented. Instead, run
`detect.py` on the item afterwards; it records the new file and, in metadata mode, writes its report.

## Library additions (`dspace_client/core.py`)

| Method | Endpoint |
|---|---|
| `get_authn_status()`, `get_current_eperson()` | `authn/status` |
| `get_site()`, `has_feature()`, `is_site_admin()` | `core/sites`, `authz/authorizations/search/object?feature=administratorOf&eperson=` |
| `iter_pages(endpoint, key, ...)` | any paginated HAL list |
| `get_bitstream(uuid)`, `get_bitstream_item(uuid)` | `core/bitstreams/{id}`, `/bundle/item` |
| `download_bitstream(uuid, dest, max_bytes)` | `core/bitstreams/{id}/content` (streamed) |
| `search_items(..., scope=None)` | adds `scope` to `discover/search/objects` |
| `iter_workspace_items(submitter_uuid=None)` | `submission/workspaceitems[/search/findBySubmitter]` |
| `iter_workflow_items()`, `iter_pool_tasks()`, `iter_claimed_tasks()` | `workflow/workflowitems`, `workflow/pooltasks/search/findByUser`, `workflow/claimedtasks/search/findByUser` |
| `find_metadata_schema(prefix)`, `find_metadata_field(name)`, `create_metadata_field(...)` | `core/metadataschemas`, `core/metadatafields/search/byFieldName`, `POST core/metadatafields?schemaId=` |

Each has a unit test (`tests/test_core_a11y_methods.py`; the download uses respx). All are
registered in the version compatibility table.

## Module layout

```
examples/pdf_accessibility/
  DESIGN.md
  README.md
  detect.py              # entry point
  remediate.py           # entry point
  session.py             # login, version check, admin check, session refresh, User-Agent
  scope.py               # scope modes → items; PDFs of an item
  engine.py              # killable outloud workers, timeout, OCR-001
  analysis.py            # raw result + policy → findings + verdict
  policy.py              # severity resolution
  policy_default.yml
  advice.py              # report text rendering
  advice_overrides.yml
  sinks.py               # CSV + metadata patch planning
  fields.py              # field check / creation with retype-to-confirm
  state.py               # results.jsonl read/write, cache lookup
  eligibility.py         # exception eligibility dates
  exceptions.yml         # exception field + value template
  pairing.py             # verify-replacement pairing
```

## What can change the repository

| Action | Where | Reversible? | Safeguard |
|---|---|---|---|
| Create a metadata field | detect.py, metadata mode | Deleting the field later also deletes all its values | Dry-run default; retype-to-confirm |
| Write report values | detect.py, metadata mode | Yes, values carry a marker and can be removed | Dry-run default; `--apply`; only touches values with its own marker |
| Write exception values | remediate.py, track 1 | Yes, but this records a compliance decision | Dry-run default; `--apply` + retype-to-confirm; never overwrites existing values |
| Download every PDF | detect.py | Not a change, but inflates download statistics unless the UA is configured as a spider | Distinctive User-Agent; README step |

Everything else is read-only. Nothing creates, replaces or deletes a bitstream.

## Phases

**As built:** phases 1 to 5 are done. Each was tested live against a local DSpace 11.0-SNAPSHOT
(with `--skip-version-check`), using a test collection that has archived items (tagged, untagged
and scanned PDFs), a workspace item and replacement uploads. Phase 0 results are under "Phase 0
results" below.

0. **Engine check (go/no-go for outloud).**
   - Build a corpus of 100–300 openly licensed PDFs from a few public DSpace repositories. Download
     them anonymously, at a polite rate, into a gitignored folder. Aim for a mix: older scans, theses,
     Word and LaTeX exports, publisher PDFs.
   - Run `engine.py` with `OCR-001` over the corpus. Cross-check the conformance verdicts with veraPDF.
   - Keep outloud if all of these hold:
     - fewer than 1% `UNREADABLE` or `INCOMPLETE` results on files that open in a normal reader;
     - page attribution correct in a 20-finding manual sample;
     - at least 95% agreement with veraPDF on conformance-layer pass/fail;
     - under 2 s per file on average.
1. **Library methods** plus tests, including the admin PATCH check on in-progress items.
2. **Detection:** CSV output and `results.jsonl`.
3. **Detection:** metadata output.
4. **Remediation:** exception tagging, author contact list, verify replacement.
5. **README** with setup, the spider User-Agent, field hiding, licence notes and the limits of what a
   PASS means.

## Phase 0 results (2026-10-03)

**Corpus:** 240 PDFs, 60 each from four public US DSpace repositories: MIT DSpace, Virginia Tech
VTechWorks, Ohio State Knowledge Bank and UMN Conservancy. They were downloaded anonymously at
one request per second per host and kept in the gitignored `corpus/` folder. The mix covers the
oldest items (19th-century scans), the newest items, and items in between: 12,713 pages in total,
with a median of 23 pages per file. Every file was checked with `engine.py` (outloud 0.2.0 +
OCR-001, 4 workers) and with veraPDF 1.30.2 (`-f ua1`).

| Criterion | Target | Result | |
|---|---|---|---|
| Files outloud could not finish | < 1% | **10 / 240 (4.2%)**, all one bug (below) | not met |
| Page attribution (FIG-001 sample) | correct | 27 / 27 reported pages hold an image or drawing (automated proxy, not a manual check) | met |
| Conformance pass/fail agreement with veraPDF | ≥ 95% | 221 / 230 (96.1%) | met |
| Time per file | < 2 s | mean 2.10 s per worker (median 0.51 s, p95 11.5 s, max 34.5 s); with 4 workers 144 s wall for 240 files, i.e. 0.6 s per file | met for throughput |

**The robustness failure is a single outloud bug.** Structure-element attribute arrays are
converted with `float()`, but `/BorderColor` on table cells can be an array of four per-side
arrays, or contain `null`. That is legal PDF, and common in Word-generated tables. outloud then
stops part-way. Its document-level findings survive, but its structure-tree rules do not. veraPDF
processed all 10 files.
- **Handled here:** such a file is no longer reported as `UNREADABLE`. It becomes `FAIL` when it
  already has errors, otherwise `INCOMPLETE`, with "outloud stopped part-way" in the report.
- **Upstream:** reported with a minimal reproduction as [outloud#5](https://github.com/visionably/outloud/issues/5).

**Per-check agreement with veraPDF:**

| Check | Both flag | Only outloud | Only veraPDF |
|---|---|---|---|
| DOC-001 not marked as tagged | 141 | 0 | 0 |
| DOC-002 no structure tree | 130 | 0 | 0 |
| DOC-010 no dc:title | 169 | 1 | 2 |
| DOC-011 DisplayDocTitle | 169 | 0 | 0 |
| DOC-012 no pdfuaid | 196 | 5 | 0 |
| DOC-030 optional content | 20 | 0 | 0 |
| FIG-001 figure without alt | 7 | 0 | 0 |
| HDG-001 skipped heading level | 4 | 0 | 3 |
| TXT-001 font not embedded | 41 | 50 | 1 |
| TXT-002 no Unicode mapping | 8 | 1 | 3 |
| TXT-005 .notdef glyph | 18 | 0 | 41 |
| TXT-007 CIDToGIDMap | 21 | 0 | 0 |

The two large gaps both come from **invisible OCR text** (render mode 3):
- **TXT-005, 41 files only veraPDF flags.** 40 of the 41 have invisible text layers, and all of
  them were OCR'd with Adobe Acrobat's Paper Capture (non-embedded Type 1 base fonts plus a
  `HiddenHorzOCR` Type 0 font). outloud's TXT-005 only counts character code 0 in 2-byte
  (composite) fonts. veraPDF flags any reference to `.notdef` "regardless of text rendering mode".
  Screen readers do read OCR text layers, so the gap matters. Reported as
  [outloud#6](https://github.com/visionably/outloud/issues/6).
- **TXT-001, 50 files only outloud flags.** veraPDF exempts fonts used only for invisible text
  from embedding; outloud does not. That makes outloud stricter here, and harmless: TXT-001 has no
  WCAG mapping, so the default policy reports it as a note.

**OCR-001 earns its place.** 41 files are mostly scans: 38 MIT items from 1853–1877 and 3 from
UMN. For these, outloud alone would have advised "tag the document". Neither tool flags them as
scans; veraPDF only reports untagged content.

**Verdicts under the default policy:** 211 FAIL, 15 REVIEW, 4 PASS, 10 UNREADABLE (before the
classification fix above; now INCOMPLETE or FAIL). veraPDF found 6 of the 240 files PDF/UA-1
compliant.

**Decision:** stay on outloud. Agreement and speed are good. The robustness gap and the
invisible-text gap are each one specific, reproducible outloud bug, worth reporting upstream.
`detect.py` already degrades gracefully on both. Re-run phase 0 when a fixed outloud release is
out. If the bugs are not fixed, `engine.py` is the only module to replace.

## OCR remediation track (added 2026-10-03)

Phase 0 found 41 of 240 repository PDFs to be mostly scanned, many of them handwritten 19th-century
theses. outloud can only advise "tag the document" for those; they need a text layer first.

**Requirement:** pip-only, FOSS. No Tesseract or Ghostscript system packages.

**Engine: RapidOCR** (Apache-2.0), running PaddleOCR PP-OCRv6 models on ONNX Runtime (MIT).
The models ship inside the wheel. The extra adds about 300 MB, mostly OpenCV.
- EasyOCR and docTR were also considered. Both are pip-only too, but pull in PyTorch
  (gigabytes).
- OCRmyPDF + Tesseract is the mature alternative, but needs system packages.

**Trial on the phase-0 corpus:**
- *Printed pages.* On 7 printed 1860s–70s UMN pages, RapidOCR's text matched Acrobat Paper
  Capture's OCR at 0.88 on average. Several differences were Acrobat's errors ("ra71ujey,
  goverrwr" against RapidOCR's correct "ramsey, governor").
- *Handwriting.* On handwritten MIT theses it is mostly readable, with errors.
- *Speed and size.* 0.4–1.2 s per page on a laptop CPU. Ten scanned files (448 pages) all
  went from image-only to text, and grew 4–7% in size.
- *Checks on the output.* outloud found no new problems and no visual change (SEM-003).
  veraPDF reported no font or language failures.

**Text layer (`ocr.py`):**
- Lines are written with PyMuPDF as invisible text (render mode 3), each stretched to the width
  of its recognised line.
- The font is generated with fontTools: one empty glyph per character used, like Tesseract's
  GlyphLessFont. That makes it embedded, gives no `.notdef` references (compare outloud#6), and
  extracts correctly.
- PyMuPDF leaves out `CIDToGIDMap` (outloud TXT-007, veraPDF 7.21.3.2-1) and `/Lang` (veraPDF
  7.2-34), so pikepdf adds both. `/Lang` comes from `dc.language.iso`.
- Two things were found in the trial and fixed:
  - one glyph shared by all characters made text extract as "÷÷÷";
  - a derotation step misplaced text on rotated pages, because PyMuPDF's `insert_text` already
    works in displayed coordinates.

  Both are covered by tests.

**Track (`ocr_track.py`), as built:**
- *Candidates* are results with OCR-001. Only image-only pages are OCR'd.
- *Validation* runs before any placement: no SEM-001..003, no new TXT-* findings, and mean
  confidence ≥ `--min-confidence` (0.80). A file that fails is `NEEDS_REVIEW` and stays local.
- *Placement* is the repository manager's choice:
  - `local` (default): saved for review;
  - `alongside`: uploaded as `<name>_ocr.pdf`;
  - `replace`: uploaded under the original name, the original moved to `PRESERVATION`, and the
    primary flag moved with it.

  Nothing is deleted. If the move fails after the upload, the result is `PARTIAL` and nothing is
  rolled back.
- *Access rule:* uploads happen only when the original is downloadable by Anonymous
  (`anonymous_can_download`, the `canDownload` feature asked without credentials). A new upload
  inherits bundle policies, not the original file's, so embargoed or restricted originals become
  `SAVED_RESTRICTED`.
- *Audit trail:* `dc.description` on the new bitstream and `dc.description.provenance` on the item.
- *Re-runs* skip originals that already have an `_ocr` copy (`ALREADY_DONE`). A replaced original
  is no longer in ORIGINAL, so it is not picked up again.
- *Not supported:* DSpace item versioning as a placement. A new version starts as a workspace
  item that has to go through submission, which is out of scope for a batch tool.
- *Tested live* on DSpace 11.0-SNAPSHOT with `local`, `alongside` and `replace`, including re-runs.
  Afterwards detection went from 6 to 4 errors for the OCR'd files: OCR-001 and the missing
  language were gone, and untagged and no-title remained.

**Known limits:** OCR adds text, not tags; files still fail DOC-001/DOC-002 until tagged. OCR
errors are read out as-is by screen readers. The README says to review handwritten and
low-confidence material.
