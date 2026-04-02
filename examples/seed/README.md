# Seed examples (dspace-seed–style)

These scripts mirror the **MiniSpace** and **MegaSpace** scenarios from the **dspace-seed** project,
but use **`dspace_client` only** for HTTP and API calls. The YAML loader and `DataFactory` live in `seed_data.py` (adapted from dspace-seed’s
generators).

## Setup

From the repository root:

```bash
pip install -e ".[examples]"
```

`[examples]` pulls in **PyYAML** for `seedpacks/default.yml`.

## Files

| File | Role |
|------|------|
| `seedpacks/default.yml` | Full seed pack (same idea as dspace-seed’s default pack; large file) |
| `seed_data.py` | Load YAML + deterministic titles/metadata + helpers used by both scripts |
| `minispace.py` | One community → collection → item → PDF bitstream; optional delete |
| `megaspace.py` | Groups, EPeople, policies, batch items, statistics; optional cleanup |

## MegaSpace: minimum collections

**`--collections` must be at least 2.** MegaSpace is built around two collections (e.g. mega-metadata vs mega-bitstreams owning collections, and round-robin batch placement). If you pass **`--collections 1`**, the script exits immediately with an error before the interactive prompts.

## Login and version check

Login is the same in both projects: CSRF → `POST /authn/login` → `GET /authn/status`.

**Atmire messaging:** [`create_validated_client`](../../dspace_client/__init__.py) and **`connect_seed_client`** call **`show_atmire_promo_start`** (a no-op). When the auth client closes, an optional end panel may appear. Set **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`** to disable all Atmire output.

**Server version:** Both examples declare compatibility with **DSpace 9.0** and, **by default**, run **`verify_server_version`** after login (several HTTP GETs to detect the server version). Pass **`--skip-version-check`** to skip that probe (faster, closer to the old dspace-seed CLI, but you lose the explicit compatibility check).

## Pacing and MegaSpace diagnostics

`DSpaceClient` defaults to **`courtesy_delay=1.0`** second between API calls. **`connect_seed_client`** in `seed_client.py` defaults to **`courtesy_delay=0`** so light scripts are not artificially paced unless you pass a value.

**MegaSpace** prompts for **delay between REST requests** (default **1.0** s if you press Enter). You can set it non-interactively with **`--courtesy-delay SEC`**. At the end of a successful run it prints a **Performance** panel (wall time, courtesy delay, slow-request threshold, scale) and a **table of slow requests** (over 2 s by default), so you can share logs as REST performance diagnostics.

Before the optional cleanup step, MegaSpace can **save diagnostics** to the current directory as a pair of files: **`YYYY-MM-DD-HH.MM-megaspace-{hostname}-raw.json`** (time is **UTC hour and minute** when the file is saved) (full payload: config, samples over time, slow requests, degradation hints) and **`-readable.md`** (Markdown summary plus embedded JSON for LLM use). The repository **`.gitignore`** ignores `*-megaspace-*-raw.json` and `*-megaspace-*-readable.md` so these exports are not committed by accident.

The **mega-bitstreams** step uploads many PDFs **one at a time** (sequential `upload_bitstream` calls) and shows a **progress bar**; on slow public demos this phase can take minutes.

If you still see **`ReadTimeout`** on login, that is usually the public demo or the network, not the
above — the scripts use a **120s** HTTP timeout on the auth client.

## Run

```bash
# MiniSpace (interactive)
python examples/seed/minispace.py

# MegaSpace (defaults are small; override flags as needed)
python examples/seed/megaspace.py --collections 2 --items-per-collection 2 --epeople 5
```

## Syncing `default.yml`

If the upstream dspace-seed pack changes, replace `seedpacks/default.yml` with the version from
`dspace-seed/seedpacks/default.yml` and re-run a quick smoke test.
