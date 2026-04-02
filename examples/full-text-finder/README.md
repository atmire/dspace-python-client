# Full-text finder example

Find open-access full-text PDFs for items that have a **DOI** but **no PDF** in the **ORIGINAL** bundle, using the same source order as the Google Apps Script project: **Unpaywall → OpenAlex → OpenAIRE → CORE**. Optionally upload the file to DSpace.

## Setup

From the repository root (with the project venv activated):

```bash
pip install -e "."
```

## Run

```bash
python examples/full-text-finder/main.py
python examples/full-text-finder/main.py <item-uuid>
python examples/full-text-finder/main.py --mode bulk --no-user-verify --max-items 10
```

### Prompt order (interactive)

1. Script attribution and the **Full Text Finder** intro panel (what the tool does).
2. **DSpace base URL** and credentials (and external API prompts from `config`).
3. **Mode** (single / bulk / item UUID) and optional **item UUID**, unless you passed `--mode` or a UUID on the command line.
4. **“Do you want to see each individual PDF before it gets added to the item?”** — Press **Enter** for **Yes** (open each PDF and confirm before upload; quickest path for careful review). Type **`No`** or **`n`** to add PDFs automatically without preview. Skipped if you passed **`--no-user-verify`** or **`--dry-run`**.
5. Connection to DSpace and processing.

### Modes

| Mode | Behavior |
|------|----------|
| **Single** | Discovery (newest first) until the first item with a DOI and no PDF in ORIGINAL; process that item and stop. |
| **Bulk** | Process all eligible items (use `--max-items` to cap). |
| **Item** | Pass a single item UUID (argument or `--mode item` + UUID). Validates DOI and absence of PDF in ORIGINAL. |

### Flags

| Flag | Meaning |
|------|---------|
| `--discovery-query` / `-q` | Lucene query for discovery (default: `dc.identifier.doi:*`). Adjust if your Solr field names differ. |
| `--max-items` | Maximum eligible items in bulk mode. |
| `--dry-run` | Resolve a URL only; log `DRY_RUN` lines; no download upload to DSpace. |
| `--no-user-verify` | Skip preview: upload each PDF without the interactive question (overrides Enter = preview default). |
| `--skip-open` | When preview is enabled: do not open the system PDF viewer (still prompt to upload each file). |
| `--courtesy-delay` | Seconds between DSpace API calls (default: `1.0`). |
| `--strict-versions` | Run `verify_server_version` after login. |

### Environment variables

**DSpace:** standard login prompts (or demo defaults for `demo.dspace.org`).

**External APIs:**

| Variable | Purpose |
|----------|---------|
| `FULLTEXT_UNPAYWALL_EMAIL` | Required; used for Unpaywall and OpenAlex `mailto`. |
| `FULLTEXT_CORE_API_KEY` | Optional; enables CORE discovery. |
| `FULLTEXT_OPENAIRE_PAT` | Optional **OpenAIRE Personal Access Token** (preferred over refresh token). |
| `FULLTEXT_OPENAIRE_REFRESH_TOKEN` | Optional; legacy OpenAIRE refresh token if you have no Personal Access Token. |
| `FULLTEXT_HTTP_TIMEOUT` | HTTP timeout for external APIs in seconds (default: `30`). |
| `FULLTEXT_FINDER_LOG_DIR` | Directory for audit logs (default: current directory). |

If `FULLTEXT_UNPAYWALL_EMAIL` is unset, the script prompts for it and optionally for CORE / OpenAIRE keys.

### Audit logs

Each run writes a timestamped file:

`full_text_finder_YYYY-MM-DD_HH-MM-SS.log`

The pattern `full_text_finder_*.log` is listed in the repo **`.gitignore`** so logs are not committed. Lines include uploads (`UPLOAD`), skips, failures, and `DRY_RUN` entries.

### Warnings

- **Automatic upload** (type **No** at the preview question, or use **`--no-user-verify`**) uploads whatever the resolver returns without opening files locally; verify licensing and correctness in DSpace afterward.
- Discovery query defaults may not match every DSpace/Solr configuration; tune `-q` for your site.

## Compatibility

Targets DSpace **7.x / 8.x / 9.x** (same as other examples).

**Atmire messaging:** When the DSpace session closes, an optional **session-end** thank-you panel may appear (non-blocking). To hide **all** Atmire output, set **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`** (as in automated tests).
