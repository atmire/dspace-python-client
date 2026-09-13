# Seed scenarios (`examples/seed/`)

This folder holds **end-to-end repository scenarios**: scripts that build realistic communities, collections, items, bitstreams, and (in MegaSpace) people, groups, and policies. They are aimed at **staging and demo servers**, **integration smoke tests**, and **exercising bulk client APIs** (batch item creation, concurrency, slow-request diagnostics)—not at replacing your production ingest pipeline.

If you have not installed the project yet, follow the root **[README → Running the Examples](../../README.md#running-the-examples)** (editable install plus **`pip install -e ".[examples]"`** for PyYAML). The notes below assume you already did that.

> Re-read the root **[Important Safety Notice](../../README.md#important-safety-notice)** before running against anything that is not disposable. These scripts create a lot of content; MegaSpace and MiniSpace can also **delete** large subtrees when you opt into cleanup.

## What each scenario is for

### MiniSpace (`minispace.py`)

**Goal:** Walk through the smallest complete “vertical slice” of the API in one sitting: **one community → one collection → one item → one PDF bitstream**, with **titles and item metadata** generated from the shared YAML seed pack (`seedpacks/default.yml`) via `DataFactory` in `seed_data.py`. Good for **quick validation** that authentication, version checks, and basic CRUD work on your server.

**Notable behaviour:** After a successful run it can **delete the whole community** (cascade) so you can repeat the demo on the same instance. It does **not** configure special Anonymous READ groups on the collection.

### MegaSpace (`megaspace.py`)

**Goal:** Stress a **DSpace 9.0 / 10.0** instance with **many collections, batch-created items, EPeople, groups, READ policies, view/statistics-style traffic**, and sequential **bulk bitstream uploads**. It uses **`BatchItemCreator`** and **`ConcurrencyConfig`** from the library and is meant for **performance testing**, **concurrency behaviour**, and **realistic “busy repository”** data—not for a minimal tutorial.

**Notable behaviour:** Configurable scale via CLI flags; optional **diagnostic export** (JSON + Markdown) and a **slow-request** summary; optional cleanup. See [Pacing and MegaSpace diagnostics](#pacing-and-megaspace-diagnostics).

**Item/bitstream READ access:** MegaSpace asks whether the created items and bitstreams should be **publicly readable (Anonymous READ)**—the **default**—or restricted to the site-wide **`MegaSpace Readers`** group. Public content is what you want for **read/access load tests, crawler tests, and open-access smoke tests** (anonymous clients can see it). Choosing restricted (`--restricted-read`) grants READ only to `MegaSpace Readers`, so anonymous callers see nothing—useful for testing access control and restricted-item behaviour. Skip the prompt with **`--public-read`** or **`--restricted-read`**. Note: on a freshly seeded instance, items only appear in **anonymous discovery search** after the server rebuilds its Solr index (`dspace index-discovery`).

### Publication Page (`publication_page.py`)

**Goal:** Create the **same graph as MiniSpace** (one of each), but drive **community/collection names and item metadata** from **`publication-page-config.json`** so runs are **repeatable and import-like** (your DC fields, authors, dates, etc.). It also attaches the built-in **Anonymous** group to the collection’s **item READ** and **bitstream READ** defaults so the record and PDF are **readable without logging in**—useful for **publication-page demos** and **open-access smoke tests**.

The YAML pack is still loaded only so a **generated sample PDF** can be produced when you omit `bitstream.path` in the config; it does **not** supply titles or item metadata for this script.

## Choosing a script

| You want… | Use |
|-----------|-----|
| Fast, guided CRUD demo with synthetic but coherent metadata; optional teardown | **MiniSpace** |
| Large-scale batch load, groups/EPeople, diagnostics, optional cleanup | **MegaSpace** |
| Fixed metadata from JSON + anonymous read on item/file | **Publication Page** |

All three target **DSpace 9.0 and 10.0**, call **`verify_server_version`** after login **by default**, and accept **`--skip-version-check`** for a faster run without the compatibility probe.

## Layout

| File | Role |
|------|------|
| `seedpacks/default.yml` | Large YAML corpus (disciplines, works, names) consumed by `seed_data.py` |
| `seed_data.py` | Load the pack + `DataFactory` helpers (titles, metadata, sample PDF bytes) |
| `seed_client.py` | Shared `connect_seed_client` helper (auth + `DSpaceClient` for these scripts) |
| `minispace.py` | Minimal four-object graph from the pack; optional delete |
| `publication_page.py` | Same graph from JSON config; Anonymous READ on items/bitstreams |
| `publication-page-config.json` | Default config for Publication Page (`base_url` optional; optional `bitstream`) |
| `megaspace.py` | Large scenario: batches, people, groups, policies, diagnostics |

## MiniSpace vs Publication Page

| | MiniSpace | Publication Page |
|---|-----------|------------------|
| **Names & item metadata** | From `DataFactory` + `default.yml` (rich, English-tagged values) | From JSON: `community.name`, `collection.name`, `item.metadata` as strings or lists of strings |
| **Item name** | Factory title | `item.name` or first `dc.title` |
| **Community / collection metadata** | `dc.title` and `dc.description` | `dc.title` only (mirrors configured name) |
| **Bitstream** | Generated PDF, `sample.pdf`, with bitstream metadata | Optional `bitstream.path` / `bitstream.name`; omit path → same generated PDF |
| **Anonymous access** | Not configured | Item + bitstream READ groups include **Anonymous** |
| **After success** | Optional **delete community** | No delete step |

### Publication Page config

The script always reads **`publication-page-config.json`** next to `publication_page.py` (no `--config` flag). Replace that file with another JSON file of the same shape if needed.

- **`base_url`** (optional): used unless you pass `--base-url`; if absent, you are prompted (empty → `http://localhost:8080`). MiniSpace’s prompt defaults to **`https://demo.dspace.org`** instead.
- **`community`** / **`collection`**: each requires **`name`**.
- **`item`**: optional **`name`**; **`metadata`**: keys map to a string or list of strings (normalized to REST metadata with `language` null, `authority` null, `confidence` -1). See the docstring at the top of `publication_page.py` for a full example.

## MegaSpace: minimum collections

**`--collections` must be at least 2.** The scenario assumes two owning collections (e.g. metadata-heavy vs bitstream-heavy placement and round-robin batch logic). **`--collections 1`** exits before interactive prompts.

## Authentication, version check, and client defaults

Login follows the usual REST flow: CSRF → `POST /authn/login` → `GET /authn/status`.

**Atmire promo panels:** summarised in the root **[Atmire Promotional Messages](../../README.md#atmire-promotional-messages-optional)** section. `connect_seed_client` still calls `show_atmire_promo_start` for API compatibility (currently a no-op). Set **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`** to disable end-of-session promo output.

**HTTP timeouts:** auth uses a **120s** read timeout; slow public demos may still hit **`ReadTimeout`** on login (network/server, not the scripts’ business logic).

## Pacing and MegaSpace diagnostics

`DSpaceClient` defaults to **`courtesy_delay=1.0`** s between calls. **`connect_seed_client`** uses **`courtesy_delay=0`** unless you override it, so MiniSpace and Publication Page are not artificially slowed.

**MegaSpace** prompts for **delay between REST requests** (default **1.0** s on Enter) or **`--courtesy-delay SEC`**. After a successful run it prints **Performance** stats and **slow requests** (over **2 s** by default). Before optional cleanup it can write **`YYYY-MM-DD-HH.MM-megaspace-{hostname}-raw.json`** and **`-readable.md`** (UTC time in the filename). The repo **`.gitignore`** ignores those patterns so they are not committed by mistake.

**Mega-bitstreams** uploads many PDFs **sequentially** with a progress bar; on slow hosts this phase can take a long time.

## Run

From the repository root:

```bash
python examples/seed/minispace.py

python examples/seed/publication_page.py
python examples/seed/publication_page.py --base-url http://localhost:8080

python examples/seed/megaspace.py --collections 2 --items-per-collection 2 --epeople 5

# Public items (default; skips the READ-access prompt):
python examples/seed/megaspace.py --collections 2 --items-per-collection 2 --public-read

# Restrict READ to the MegaSpace Readers group (anonymous clients cannot see the items):
python examples/seed/megaspace.py --collections 2 --items-per-collection 2 --restricted-read
```

## Maintainers: updating `default.yml`

The pack is vendored under `seedpacks/default.yml`. If you still track an external **dspace-seed** checkout, you can replace this file with its `seedpacks/default.yml` and run a quick MiniSpace smoke test; behaviour of the Python client does not depend on that project name.
