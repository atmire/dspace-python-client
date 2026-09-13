# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **MegaSpace READ access choice**: `examples/seed/megaspace.py` now asks whether the created items and bitstreams should be **publicly readable (Anonymous READ)** — the new **default** — or restricted to the site-wide `MegaSpace Readers` group. Previously items were always restricted to that group, so anonymous clients (browsers, crawlers, read-only scripts, the new access load test) saw nothing. New flags `--public-read` / `--restricted-read` skip the prompt; the choice is recorded in the diagnostics export and the run summary.
- **Access load test example** (`examples/access_load_test/`): simulate access/read traffic and find where a server degrades. Four scenarios (`human`, `good-bots`, `bad-bots`, `mix`); humans are real headless Chromium tabs (Playwright, new optional `loadtest` extra) that reproduce the Angular UI's REST fan-out at full browser speed, while bots are pure-httpx crawlers (robots-obeying and robots-ignoring) plus optional OAI-PMH harvesters. Per-user think time throttles **actions**, never the browser's own requests; adaptive throttle/concurrency are deliberately not in the request path (they would hide the knee), with an optional user **ramp** and an adaptive **auto-stop** at the breaking point instead. Unique-per-run search queries defeat Solr caches; bitstream downloads generate real traffic through a byte-counting sink that never writes to disk; a per-request-class **degradation-onset** detector compares against an unloaded baseline. Prominent warnings, a hard block on `*.dspace.org`, a typed-hostname confirmation, and a generator-host RAM/CPU guard. Reports: human summary, extended, AI-readable JSON, and a bounded per-request JSONL.
- **DSpace 10 support**: Added `10.0` to the supported-version registry (`dspace_client/versions.py`), mapped to the `dspace-10_x` RestContract branch, and declared `10.0` in the `TARGET_VERSIONS` of all applicable example scripts. Verified end-to-end against `demo.dspace.org` (now DSpace 10.1): authentication, full CRUD (community/collection/item/bundle/bitstream), statistics view events, discovery search, item/bundle/bitstream/format reads, the submitter endpoint, and group/EPerson management all work unchanged. The DSpace 9 -> 10 REST contract diff is additive only (new `auditevents`, `edititems`, `edititemmodes`, `securitysettings` endpoints and new `findEditAuthorized`/`findAddAuthorized`/`findByCustomURL` search methods); no endpoint our examples use changed.
- **Licensing**: Atmire copyright notice in `LICENSE`; new `NOTICE` and `THIRD_PARTY_LICENSES.md` for attribution and dependency license tracking.

### Changed

- **RestContract branch mapping**: Re-pointed `9.0` from `main` to the stable `dspace-9_x` branch, since `main` now tracks DSpace 10 after its release. `bleeding-edge` continues to track `main`.

### Fixed

- **`dspace-docs fetch 8.0`**: Re-pointed `8.0` from the non-existent `dspace-8.0` ref to the `dspace-8_x` branch (the RestContract repo renamed its 8.x branch), so fetching DSpace 8 docs works again.

### Fixed

- **Adaptive concurrency**: `AdaptiveSemaphore` ramp-up now releases pre-held permits; `BatchItemCreator` reuses the live adaptive semaphore; `should_ramp_down` computes throughput from timestamps; removed broken `AdaptiveDelayController` context manager.
- **Version validation**: `_request` passes explicit method names so the compatibility matrix is consulted correctly.
- **Request retries**: Tenacity retries now use `retry_if_exception`, honor `max_retries`, and retry on retryable `DSpaceAPIError` status codes (429/502/503/504).
- **Error handling**: Failed API responses log truncated bodies via `logger.warning` instead of dumping response headers to the console; `is_session_valid` no longer catches bare `Exception`.
- **Docs fetcher**: Cache directory is anchored to the project root; git subprocess calls use timeouts; shallow clones update with `git fetch --depth 1`; duplicate `get_repo_status` removed; timestamps use UTC.
- **Version lists**: `dspace_client/versions.py` is the single source of truth for supported versions and RestContract branch mapping (includes 7.1–7.5 keys).
- **Persistent caches**: CSV and `last_until.json` writes are atomic (temp file + `os.replace`).
- **OAI parsing**: Uses `defusedxml` for safer XML parsing of OAI-PMH responses.
- **`create_validated_client`**: Forwards `timeout` to both auth and REST clients; adds opt-in `fetch_docs` and `show_atmire_promo` flags (promo no longer shown by default).

### Added

- **`managed_client`**: Async context manager that authenticates and always closes the auth session in `finally`.
- **`CONTRIBUTING.md`**: Setup, test, lint, and PR expectations.

### Changed

- **Examples**: `basic_usage.py`, `bulk_import.py`, and `advanced_auth.py` use `managed_client` or `try/finally` for reliable session cleanup; full-text-finder demo credential check uses exact hostname match.
- **`search_items`**: Validates filter tuple shape with a clear `TypeError`; `sort=None` omits the sort parameter.
- **Tooling**: Library `NullHandler`, removed obsolete pytest `event_loop` fixture, updated `pyproject.toml` URLs, ruff auto-fixes across library/tests/examples.

### Changed (continued)

- **Atmire promo**: Opt-in only via `show_atmire_promo=True`; `auth.close()` shows the thank-you panel only after successful authentication when promo was enabled.
- **`link_author_authorities`**: Split into `examples/link_author_authorities/` package (`orcid`, `scoring`, `state`, `session`, `process`, `main`); root script is a thin runner.
- **Tests**: Added `test_docs.py`, `test_init.py`; expanded factory and docs fetcher coverage.

- **BatchItemCreator** (`create_items_batch`): optional **`on_metrics_sample`** callback — invoked whenever batch progress metrics are printed (every 50 completed items and at the end), with `(completed, total, PerformanceMetrics)` for time-series / degradation reporting.
- **examples/seed** — **MegaSpace** (`megaspace.py`): declares **DSpace 9.0**; **`verify_server_version`** runs **by default** (use **`--skip-version-check`** to skip); **courtesy delay** between REST calls (prompt default 1.0 s, or **`--courtesy-delay`**); **slow-request** logging (threshold 2 s) with end-of-run table; **Rich** progress for sequential mega-bitstream uploads; optional **diagnostics export** to `YYYY-MM-DD-HH.MM-megaspace-{hostname}-raw.json` and `-readable.md` (UTC time in filename; payload includes config, samples, degradation hints); **`.gitignore`** patterns for those exports.
- **examples/seed** — **MiniSpace** / **`connect_seed_client`**: target **9.0**, version check on by default, **`--skip-version-check`**; **`connect_seed_client`** accepts **`courtesy_delay`** and **`slow_request_*`** passthrough to **`DSpaceClient`**.
- **dspace_client.auth**: Failure-only structured logging (`WARNING` / optional `DEBUG` on `dspace_client.auth`) for CSRF, JWT refresh, login, and verify failures.
- **tests**: `test_link_author_orcid_normalize.py`; auth tests for `refresh_jwt`, `ensure_session`, and CSRF cookie fallback.
- **.gitignore**: ignore `link_author_authorities_*.log` and `link_author_authorities_state.jsonl`.

### Fixed
- **DSpaceAuthClient**: Proactive session refresh prefers JWT refresh (`POST /authn/login` with `Authorization: Bearer` + `X-XSRF-TOKEN`) before full CSRF + password login, avoiding fragile `GET /security/csrf` on long runs when proxies strip `DSPACE-XSRF-TOKEN`. `ensure_session` treats “no prior auth” as `_last_auth_time is None` (not falsy `0.0`).
- **DSpaceAuthClient**: If `GET /security/csrf` omits the header, CSRF value may be taken from `DSPACE-XSRF-COOKIE` in the httpx jar.
- **examples/link_author_authorities**: ORCID mode — parse checksum `X`, `www.orcid.org` URLs, and vocabulary metadata `person.identifier.orcid` / `dc.identifier.uri`; resolve via vocabulary `entryID`, hyphenated + compact filters, then first-four-digit pagination; fetch entry detail only when list metadata lacks ORCID; dim progress during broad scan.

### Changed

- **dspace_client.promo**: **Session-end only** — one non-blocking Rich panel when **`DSpaceAuthClient.close()`** runs (thank-you line, rotating **Did you know**, **https://www.atmire.com**). Session-start messaging and the session-end **browser** prompt are removed; **`DSPACE_CLIENT_DISABLE_ATMIRE_BROWSER_PROMPT`** and **`is_atmire_browser_prompt_disabled`** are removed. **`show_atmire_promo_start`** remains exported as a no-op for compatibility. Disable all promo output with **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`**.
- **examples/seed/megaspace.py**: **`--collections`** must be **at least 2** (argparse validation with a clear error). The full MegaSpace scenario assumes two collections (e.g. mega-metadata vs mega-bitstreams owning collections).
- **docs/API_GOTCHAS.md**: Notes on session refresh behavior and enabling auth diagnostics.

### Features
- **DSpaceAuthClient**: Complete authentication flow (CSRF → Login → JWT)
- **DSpaceClient**: Main API client with version validation
- **BatchItemCreator**: High-performance bulk operations
- **ConcurrencyController**: Adaptive concurrency control
- **RestContractFetcher**: Git-based documentation management
- **VersionCompatibility**: Multi-version compatibility checking

### API Coverage
- Communities (create, delete)
- Collections (create, delete)
- Items (create, delete)
- Bundles (create)
- Bitstreams (upload, delete)
- EPeople (create, delete, add to groups)
- Groups (create, delete, add subgroups)
- Collection default groups (item read, bitstream read)
- Statistics (view events)

### Documentation
- Comprehensive README with examples
- Quick start guide
- API reference
- Error handling guide
- Version compatibility documentation

### Examples
- Basic usage example
- Bulk import example
- Advanced authentication example

### Testing
- Unit tests for authentication
- Unit tests for core client
- Test fixtures and configuration
- Mock-based testing for HTTP operations

## [0.1.0] - 2024-01-XX

### Added
- Initial development release
- Core package structure
- Basic functionality implementation
- Documentation and examples
- Test suite foundation

### Technical Details
- Python 3.11+ support
- Async/await throughout
- Type hints for better IDE support
- Rich console output
- Git-based documentation fetching
- Version compatibility validation
- Comprehensive error handling
- Adaptive concurrency control
- Batch operations support
