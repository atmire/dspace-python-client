# Access load test (`examples/access_load_test/`)

Generate realistic **read / access traffic** against a DSpace instance and find the point
where it starts to degrade. For most modern DSpace repositories the pressure is no longer
ingest, it is **access**: people using the Angular UI (which fans out into many REST calls
per page), well-behaved crawlers and OAI-PMH harvesters, and badly-behaved bots that ignore
`robots.txt` and crawl everything they can reach. This tool simulates all three and reports,
per request class, when and how the server gives way.

> [!WARNING]
> **This tool is designed to push a server toward failure. Run it only against a DSpace
> instance you own or have explicit written permission to load test.**
> It sends everything from one IP (per-IP rate limiters and WAFs will notice), pollutes the
> Solr statistics core with view events and downloads, and every simulated human is a real
> headless-browser tab that consumes real RAM and CPU on the machine running the test. The
> DSpace community demo and sandbox hosts (`*.dspace.org`) are **hard-blocked with no
> override**. Re-read the root [Important Safety Notice](../../README.md#important-safety-notice).

Every request carries the header `X-DSpace-Load-Test: <run id>` so operators can find and
filter this traffic in their logs.

---

## Contents

- [Scenarios](#scenarios)
- [Install](#install)
- [Quick start](#quick-start)
- [The two questions: users and think time](#the-two-questions-users-and-think-time)
- [Why throttle applies to actions, not the browser](#why-throttle-applies-to-actions-not-the-browser)
- [Why the library's adaptive throttle and concurrency are not used](#why-the-librarys-adaptive-throttle-and-concurrency-are-not-used)
- [What counts as "breaking"](#what-counts-as-breaking)
- [Realism details](#realism-details)
- [Generator host resources](#generator-host-resources)
- [Reports](#reports)
- [Reading the results](#reading-the-results)
- [Safety features](#safety-features)
- [Flag reference](#flag-reference)
- [Module map](#module-map)
- [Troubleshooting](#troubleshooting)

---

## Scenarios

| Scenario | Models | Needs a browser? |
|----------|--------|------------------|
| `human` | People using the Angular UI: real headless Chromium tabs that trigger the same server-side render, hydration and client-side REST fan-out a person's browser does | **yes (Playwright)** |
| `good-bots` | Well-behaved crawlers and OAI-PMH harvesters: self-identifying, fetch `robots.txt`, obey `Disallow`, crawl the sitemap; HTML only | no |
| `bad-bots` | Scrapers that ignore `robots.txt` and follow every link they find, breadth-first | no |
| `mix` | All three at once, in a configurable split | if the split includes humans |

The `good-bots` and `bad-bots` scenarios run with **no browser and no extra dependencies**.
Only `human` (and `mix` when it includes humans) needs Playwright; the tool tells you if it
is missing rather than failing obscurely.

---

## Install

```bash
pip install -e ".[loadtest]"     # adds Playwright to the base install
playwright install chromium      # one-time download of the browser binary
```

If you only ever run the bot scenarios, the base install is enough; skip the two commands
above and pass a bot scenario.

---

## Quick start

```bash
# Interactive. Prompts for URL, scenario, users, think time, duration, and a typed
# confirmation of the target hostname before anything is sent.
python examples/access_load_test/main.py

# Non-interactive (CI / scripted). --i-own-this-host must equal the target hostname.
python examples/access_load_test/main.py \
    --base-url https://repo.example.org --scenario good-bots \
    --users 10 --think-time 1 --duration 10m \
    --i-own-this-host repo.example.org

# See the plan, the discovered target pool and the robots.txt rules without sending load.
python examples/access_load_test/main.py \
    --base-url https://repo.example.org --scenario mix --users 20 \
    --think-time 2 --duration 10m --dry-run --i-own-this-host repo.example.org

# A ramping mix that adds users over time so you can watch the knee build, then holds.
python examples/access_load_test/main.py \
    --base-url https://repo.example.org --ui-url https://repo.example.org \
    --scenario mix --mix human=20,good=30,bad=50 --users 40 \
    --ramp start=5,step=5,every=45s --think-time 1 --duration 10m \
    --i-own-this-host repo.example.org
```

Pass `--ui-url` when the Angular UI and the REST backend are on different hosts. Targets
DSpace **7.6, 8.0, 9.0 and 10.0**; the human fan-out is modelled on the DSpace 7+ Angular UI.

---

## The two questions: users and think time

You configure the load with exactly two numbers, plus an optional ramp:

- **Number of parallel users** (`--users`). Each user is an independent worker: a browser
  tab for a human, an async crawler for a bot, each with its own session, cookies and user
  agent, so the server sees distinct visitors.
- **Think time** (`--think-time`, seconds). The mean pause between one user's **actions**.
  `0` is allowed (a user who acts the instant a page is ready).

Combined request rate is an **emergent** property, not a setting: many users each honouring
the think time still produce a high aggregate rate, and a single page action fans out into
many requests. The tool prints its estimate and the real achieved rate; you do not compute
it yourself.

`--ramp start=5,step=5,every=45s` starts with 5 users and adds 5 every 45 seconds up to
`--users`, then holds. A ramp is the easiest way to watch the load, latency and error curves
build and to read off the user count at which degradation began.

---

## Why throttle applies to actions, not the browser

The unit the think time throttles is the **user action** — "visit the home page", "open
this item", "run this search" — never the individual HTTP request.

A real browser fires many requests per second to assemble one page, sometimes several in the
same instant, and it does not pause between them. Throttling those would make the test
unrepresentative. So everything a page needs in order to render happens at full browser
speed; the think time is only the gap **between** top-level actions. The next action starts
no earlier than the think time after the previous one has **fully settled** — its key
element is visible and the network has been quiet briefly (with a hard cap so a page that
never settles cannot stall a user forever).

Because a user waits for one action to finish before starting the next, the workload is
**closed-loop**: a slower server makes each user act less often, so total offered load falls.
That is realistic for people, and the report shows **offered vs achieved** action rate so you
can see the server slowing its own callers down. Bad bots additionally support an
**open-loop** mode (`--bad-bot-open-loop`): new fetches launch on a fixed interval whether or
not earlier ones finished, modelling the connection pile-up a scraper *population* causes
when the server slows down.

---

## Why the library's adaptive throttle and concurrency are not used

`dspace_client` ships an adaptive throttle and an adaptive concurrency controller. Both
**back off when latency rises**. That is exactly the signal a load test exists to surface, so
putting either in the request path would hide the knee we are trying to find. This suite
therefore:

- keeps pacing **fixed** so degradation shows up purely in the measurements;
- offers an optional **ramp** that only ever *adds* users, so the report can name the load at
  which degradation began;
- uses adaptivity **only to stop, never to slow down**: an auto-stop ends the run at the
  breaking point (and optionally at first confirmed degradation) so you do not keep hammering
  a server that has already given way. Disable with `--no-auto-stop`.

---

## What counts as "breaking"

A load test is only useful if it distinguishes a server *failing* from a server *doing its
job under pressure*. This tool classifies every response into three buckets, and each drives
a different signal:

- **Server faults** — 5xx responses, connection errors, and request timeouts. These are the
  only thing the **breaking-point** verdict keys on, and only once they exceed a rate over a
  real sample (a couple of stray timeouts in thousands of requests will not stop the run; a
  genuine fault storm or a p95 blow-up will).
- **Rate limiting** — HTTP 429. This is the server (typically the Angular SSR layer or an
  edge/WAF) *deliberately shedding load*. It is reported as its own **capacity signal**, not
  a fault. On DSpace 7–9 the SSR routinely returns 429 to bot traffic on `/search` and
  `/items` by design, so treating it as a failure would be wrong.
- **Benign 4xx** — 401 (the Angular app's anonymous auth probes), 404 (crawlers hitting stale
  links), 400. Recorded and shown, but excluded from the breaking verdict.

Browser-cancelled requests (a navigation superseding an in-flight request) are recorded as a
non-error `aborted(browser)` outcome, not a failure.

**Client-side browser errors (human users).** The headless browsers also report events that
carry no HTTP status and are invisible to every server-side signal (SSR, Tomcat, Solr):
uncaught JavaScript exceptions, browser `console.error` messages, and page crashes. These are
captured per human user and summarised in the reports (`browser_errors`), so you can catch
front-end breakage even when every HTTP request returned 200 — the classic "the app broke in
the browser" case. They are reported for visibility and do **not** gate the breaking verdict
(a single stray `console.error` should not stop a run). The browser's console echo of a failed
network request is deliberately excluded, since that request is already captured on the wire.

Separately, the tool detects **degradation onset per request class**: it runs a short
single-user **baseline** first to learn each class's unloaded latency, then flags the first
sustained window where the rolling median latency rises above baseline by a factor
(`--onset-factor`, default 1.5). Because it is per class, you can see that `search` (Solr)
gave way before `ssr-item` (Node SSR) or `rest` (Tomcat), or that bitstream downloads slowed
first — the answer to "as soon as response time trend is going the wrong way, which part
is impacted?"

---

## Realism details

- **Unique search queries.** Terms are drawn from vocabulary harvested from the target itself
  (titles, authors, subjects seen during discovery) plus a built-in academic word list, and
  **no query repeats within a run across any user or thread**, so you measure Solr rather than
  its query cache. Two runs only resemble each other if you pass `--seed`. Sort order, page
  number and facet filters are varied too.
- **Downloads never touch disk.** When a persona downloads a bitstream the bytes are streamed
  and counted on the wire, then discarded. Per-download (`--max-download-mb`) and run-wide
  (`--max-total-download-gb`) caps bound the traffic, so the generator can never fill its own
  disk no matter how much download load you generate.
- **Humans stay on the target.** Requests a page makes to third parties (analytics,
  Altmetric, fonts, CDNs) are aborted in the browser and counted, so you never accidentally
  load-test someone else's service.
- **A realistic item mix.** The target pool is sampled with a Zipf-like popularity skew so a
  few items are hot, as in real traffic.
- **Good bots ignore `Crawl-delay`** (as the major crawlers do); the report states what
  `robots.txt` asked for versus the think time actually used.

---

## Generator host resources

Each simulated human is a real Chromium tab. Budget roughly **150 MB of RAM and about 0.3 of
a CPU core per active human**; tabs are grouped `--humans-per-browser` (default 10) into each
Chromium process. Before starting, the tool prints the estimated footprint against this
machine's RAM and core count and **refuses to start** if it would exceed a safe share, unless
you pass `--force-resources`. There is also a per-host cap (`--max-human-users`, default 60).

Split large human counts across several machines rather than forcing one — the run id header
lets you merge the logs afterwards. Throughout the run the tool watches its own event-loop
lag and, if the generator itself saturates, the report is flagged **unreliable**, because a
saturated generator's timings say nothing about the server. Bot scenarios are cheap and scale
to hundreds of users on a laptop.

---

## Reports

Written to `--output-dir` (default `access-load-reports/`) at the end of the run, and also on
a single Ctrl-C. Filenames follow the MegaSpace convention: a UTC timestamp, the scenario and
the hostname. All four are git-ignored.

| File | For |
|------|-----|
| `…-summary.md` | The verdict (healthy / degraded / breaking), degradation onset and breaking point with the exact load at that moment, a per-class table, and next steps. Read this first. |
| `…-extended.md` | The per-window time series, status-code breakdown, slowest URLs, top errors, **client-side browser errors** (JS exceptions / console errors / crashes, human users), robots.txt findings, per-persona stats and generator health. |
| `…-raw.json` | A schema-versioned machine payload with an explicit verdict object and `analysis_hints`, designed to be read by tooling or an AI. |
| `…-requests.jsonl` | One line per request (bounded by `--requests-log-max-mb`) for deep dives. |

Press **Ctrl-C once** to stop early and still get the reports; twice to abort.

---

## Reading the results

The verdict is one of:

- **HEALTHY** — ran to completion (or was stopped by you) with no sustained server faults.
- **DEGRADED** — latency trended up against the baseline for one or more request classes
  (see `onset` and `onset_by_class`), but the server was still answering.
- **BREAKING** — server faults, timeouts or extreme p95 crossed the thresholds; the run
  auto-stopped unless `--no-auto-stop` was set.

Key things to correlate:

- **Which request class degraded first.** `search` points at Solr (filter cache, GC, disk);
  `ssr-*` at the Node SSR workers; `rest` at Tomcat threads and the database pool;
  `bitstream` at the assetstore and I/O. The per-class table gives each one a
  load-vs-baseline ratio.
- **Rate limiting.** A high 429 count concentrated on `ssr-*` classes means the SSR is
  shedding bot load by design — expected and healthy, not a fault.
- **Offered vs achieved action rate** (in the windows). A widening gap means the server is
  slowing its own callers down — an early warning even before errors appear.
- **The run id and UTC window.** Use them to line the run up against your own Tomcat, Node
  (SSR), Solr and database dashboards.

---

## Safety features

- A red warning panel at startup, and matching warnings in this README and the module
  docstring.
- A built-in **blocklist**: any host under `*.dspace.org` is refused, with no override flag.
- A **typed-hostname confirmation** before any load is sent (or `--i-own-this-host HOST` for
  non-interactive runs, which must match the target).
- **Anonymous, read-only access only** — the tool never authenticates and never mutates data.
- Mandatory duration, a run-wide request/traffic budget, and a `--dry-run` that discovers the
  target and prints the plan without sending load.
- The `X-DSpace-Load-Test: <run id>` header on every request, and edge/WAF responses
  (Cloudflare, Akamai, and similar) are detected by their headers and reported separately
  from DSpace's own errors.

---

## Flag reference

```
Target & scenario
  --base-url URL              DSpace base URL, without /server (REST is at <base>/server/api)
  --ui-url URL               Angular UI URL if different from the base URL
  --scenario NAME            human | good-bots | bad-bots | mix
  --mix human=50,good=20,bad=30   persona split for the mix scenario (percentages, sum 100)
  --users N                  number of parallel simulated users
  --i-own-this-host HOST     non-interactive confirmation; must equal the target hostname

Load shape & pacing
  --think-time SEC           mean seconds between one user's actions (0 allowed)
  --fixed-think-time         constant think time instead of a lognormal spread
  --duration 10m             load phase length (e.g. 300, 90s, 10m, 1.5h)
  --baseline 30s             single-user baseline phase length (0 to skip)
  --window 10                metrics window in seconds
  --ramp start=2,step=2,every=60s   add users gradually up to --users, then hold

Downloads (disk-safe; bytes are streamed and discarded)
  --no-downloads             never download bitstreams
  --download-probability P   chance a page/crawl with a file link downloads it (default 0.25)
  --max-download-mb MB       per-download byte cap (0 = unlimited)
  --max-total-download-gb GB run-wide download budget (0 = unlimited)
  --max-inflight N           hard cap on concurrent httpx requests (safety valve)

Persona behaviour
  --bad-bot-open-loop        bad bots launch fetches on a fixed interval regardless of completion
  --bad-bot-max-depth N      crawl depth cap for bad bots
  --good-bot-ua "STRING"     override the good-bot user agent (e.g. a Googlebot string)
  --oai-share F              fraction of good bots that harvest OAI-PMH instead of crawling
  --no-view-events           block the browser's statistics view/search events
  --humans-per-browser N     browser contexts per Chromium process (default 10)
  --headed                   show the browsers (debugging only)
  --max-human-users N        per-host cap on simulated humans (default 60)
  --force-resources          ignore the generator-host RAM/CPU guard and the human cap

Detection & stopping
  --no-auto-stop             keep going past the breaking point
  --stop-at-onset            stop as soon as degradation onset is confirmed
  --onset-factor F           rolling p50 must exceed baseline * F to flag onset (default 1.5)

Output & misc
  --seed N                   RNG seed for reproducible runs (default: random, printed in the report)
  --output-dir DIR           where reports are written (default access-load-reports/)
  --no-requests-log          skip the per-request JSONL file
  --requests-log-max-mb MB   cap the per-request JSONL (default 200)
  --dry-run                  discover targets and print the plan; send no load
  --skip-version-check       skip the anonymous server-version probe
```

---

## Module map

| File | Role |
|------|------|
| `main.py` | CLI, warnings, prompts, the resource guard, orchestration wiring |
| `config.py` | `RunConfig`, validation, the host blocklist, resource estimation, flag parsers |
| `orchestrator.py` | Runs the setup, baseline and load phases; spawns users; graceful stop |
| `context.py` | Shared run state and the per-action measurement scope |
| `pool.py` | Target discovery: robots.txt, sitemaps, REST discovery; vocabulary harvest |
| `queries.py` | The unique-per-run search query generator |
| `robots.py` | robots.txt parsing with wildcard support |
| `html_links.py` | Same-site link and asset extraction from SSR HTML (stdlib only) |
| `http_user.py` | One httpx session per user; records every request into the metrics collector |
| `sink.py` | The byte-counting download sink and the run-wide traffic budget |
| `persona_good_bot.py` | Robots-obeying crawler and OAI-PMH harvester |
| `persona_bad_bot.py` | Robots-ignoring crawler, closed- and open-loop |
| `persona_human.py` | Playwright-driven human: real browser, request capture, disk-safe downloads |
| `pacing.py` | Think-time (lognormal) and user start scheduling |
| `metrics.py` | Request/action records, time windows, baseline, trend detection, verdict |
| `report.py` | Summary + extended Markdown and the raw JSON payload |
| `reqlog.py` | The bounded per-request JSONL writer |

---

## Troubleshooting

- **"The human persona needs Playwright."** Run `pip install -e ".[loadtest]"` then
  `playwright install chromium`. Bot-only scenarios need neither.
- **"No item URLs discovered."** The instance has no anonymously visible items: no sitemap
  and discovery returned nothing. Seed public content first (the `examples/seed/`
  `publication_page.py`, or `megaspace.py` with public read), and on a freshly seeded instance
  rebuild the Solr index (`dspace index-discovery`) so items appear in anonymous discovery.
- **Version check fails.** The library targets 7.6–10.0; pass `--skip-version-check` for a
  newer or unrecognised server, at your own risk.
- **The report says the generator was unreliable.** The load machine saturated (event-loop
  lag was high). Lower `--users`, especially humans, or split the run across machines.
- **Reports look empty / the run stopped almost immediately.** Check the verdict and stop
  reason at the top of the summary; a `breaking` verdict with a tiny load usually means the
  target refused connections or an edge/WAF returned errors — look at the status-code table.
