# Access load test (`examples/access_load_test/`)

Simulate **read/access traffic** against a DSpace instance and find the point where it
starts to degrade. Three traffic types, plus a combination:

| Scenario | What it models | Browser? |
|----------|----------------|----------|
| `human` | People using the Angular UI: real headless Chromium tabs that trigger the same REST fan-out, SSR and hydration a person's browser does | **yes (Playwright)** |
| `good-bots` | Well-behaved crawlers and OAI-PMH harvesters: self-identifying, read `robots.txt`, obey `Disallow`, crawl the sitemap | no |
| `bad-bots` | Scrapers that ignore `robots.txt` and follow every link, breadth-first | no |
| `mix` | All three at once, in a configurable split | if any humans |

> [!WARNING]
> **This tool generates real load and is designed to push a server toward failure.**
> Run it **only** against a DSpace instance you own or have **explicit written permission**
> to load test. It sends everything from one IP (rate limiters and WAFs will notice), it
> pollutes the Solr statistics core with view events and downloads, and every simulated
> human is a Chromium tab that needs real RAM and CPU on the machine running the test.
> The DSpace community demo and sandbox hosts (`*.dspace.org`) are **hard-blocked** with no
> override. Re-read the root [Important Safety Notice](../../README.md#important-safety-notice).

Every request carries the header `X-DSpace-Load-Test: <run id>` so operators can find and
filter this traffic in their logs.

## Install

```bash
pip install -e ".[loadtest]"     # adds Playwright
playwright install chromium      # one-time browser download
```

The `good-bots` and `bad-bots` scenarios run **without** Playwright. Only `human` and
`mix` (when it includes humans) need it; the tool tells you if it is missing.

## Run

```bash
# Interactive (prompts for URL, scenario, users, think time, duration, and a typed
# confirmation of the target hostname):
python examples/access_load_test/main.py

# Non-interactive (CI / scripted). --i-own-this-host must equal the target hostname:
python examples/access_load_test/main.py \
    --base-url https://repo.example.org --scenario good-bots \
    --users 10 --think-time 1 --duration 10m \
    --i-own-this-host repo.example.org

# See the plan and the target pool without sending any load:
python examples/access_load_test/main.py --base-url https://repo.example.org \
    --scenario mix --users 20 --think-time 2 --duration 10m --dry-run \
    --i-own-this-host repo.example.org
```

If the Angular UI and the REST backend are on different hosts, pass `--ui-url`.

## The two questions, and why throttle is not the whole story

You are asked for two things: the **think time** (mean seconds between one user's actions)
and the **number of parallel users**. Each user honours the think time; combined, many
users can still produce a high request rate. The tool prints the resulting estimate before
you confirm.

**Throttle applies to actions, never to the browser.** A real browser fires many requests
per second to build one page, sometimes several in the same instant, and it does not pause
between them. Throttling those would make the test unrepresentative. So the think time is
the gap **between top-level actions** (a page visit, a crawl fetch, a search). Everything a
page needs in order to render happens at full browser speed. The next action starts no
earlier than the think time after the previous one has **fully settled** (key element
visible and the network quiet briefly, capped).

**Why the library's adaptive throttle and adaptive concurrency are deliberately not used
here.** Both back off when latency rises. That is exactly the signal a load test exists to
surface, so putting them in the request path would hide the knee we are looking for. This
suite instead:

- keeps pacing **fixed** so degradation shows up purely in the measurements;
- offers an optional **ramp** (`--ramp start=2,step=2,every=60s`) that only ever *adds*
  users, so the report can name the user count and request rate at which degradation began;
- uses adaptivity **only to stop**, not to slow down: an auto-stop aborts the run at the
  breaking point (and optionally at first confirmed degradation) so you do not keep
  hammering a server that has already given way. Disable with `--no-auto-stop`.

Bots are **closed-loop** by default (fetch, wait for the response, then wait the think
time), which models a single well-behaved-but-rude crawler. `--bad-bot-open-loop` launches
fetches on a fixed interval regardless of whether earlier ones finished, modelling the
connection pile-up a scraper *population* causes when the server slows down.

## Realism details

- **Unique queries.** Search terms are drawn from vocabulary harvested from the target
  itself plus a built-in word list, and no query repeats within a run across any thread, so
  you measure Solr rather than its query cache. Two runs only repeat each other if you pass
  `--seed`.
- **Downloads never touch disk.** When a persona downloads a bitstream, the bytes are
  streamed and counted on the wire and immediately discarded. Per-download
  (`--max-download-mb`) and run-wide (`--max-total-download-gb`) caps bound the traffic.
- **Humans stay on the target.** Requests a page makes to third parties (analytics,
  Altmetric, fonts) are aborted in the browser and counted, so you never load-test someone
  else's service by accident.
- **Good bots ignore `Crawl-delay`** (as the big crawlers do); the report states what
  `robots.txt` asked for versus the think time actually used.

## Generator host resources

Each simulated human is a Chromium tab. Budget roughly **150 MB RAM and ~0.3 CPU core per
active human**; tabs are grouped `--humans-per-browser` (default 10) per Chromium process.
Before starting, the tool prints the estimated footprint against this machine's RAM and
core count and **refuses** if it exceeds a safe share, unless you pass `--force-resources`.
Split large human counts over several machines rather than forcing one; the run id header
lets you merge the logs afterwards. If the generator itself saturates, the report flags the
run as **unreliable** (it watches event-loop lag), because a saturated generator says
nothing about the server.

## Reports

Written to `--output-dir` (default `access-load-reports/`) at the end, and also on a single
Ctrl-C. Filenames follow the MegaSpace convention with a UTC timestamp, scenario and
hostname. All four are git-ignored.

| File | For |
|------|-----|
| `…-summary.md` | The verdict (healthy / degraded / breaking), onset and breaking point with the load at that moment, per-class table, next steps |
| `…-extended.md` | Per-window time series, status codes, slowest URLs, top errors, robots findings, per-persona stats, generator health |
| `…-raw.json` | Schema-versioned machine payload with an explicit verdict object and analysis hints, for tooling and AI |
| `…-requests.jsonl` | One line per request (bounded by `--requests-log-max-mb`) for deep dives |

**Degradation is caught before errors appear.** The tool runs a short single-user
**baseline** first to learn each request class's unloaded latency, then flags **onset** as
the first sustained window where the rolling median latency rises above baseline by a factor
(`--onset-factor`, default 1.5). It reports onset **per request class**, so you can see, for
example, that `search` (Solr) gave way before `ssr-item` (Node) or `rest` (Tomcat). It also
tracks **offered vs achieved** action rate: when users slow down because the server did, the
gap is itself an early warning.

## Key flags

```
--scenario human|good-bots|bad-bots|mix   --users N   --think-time SEC (0 allowed)
--duration 10m   --baseline 30s   --ramp start=2,step=2,every=60s   --window 10
--mix human=50,good=20,bad=30   --fixed-think-time
--no-downloads --download-probability 0.25 --max-download-mb 50 --max-total-download-gb 5
--bad-bot-open-loop   --oai-share 0.2   --no-view-events   --good-bot-ua "<string>"
--humans-per-browser 10   --headed   --max-human-users 60   --force-resources
--no-auto-stop --stop-at-onset --onset-factor 1.5
--seed 12345   --dry-run   --skip-version-check
--i-own-this-host HOST   --output-dir DIR   --no-requests-log
```

Targets DSpace **7.6, 8.0, 9.0 and 10.0**; the human fan-out is modelled on the DSpace 7+
Angular UI. Note that on DSpace 8 and 9 the server-side rendered search and browse pages
render no results by default, so a crawler legitimately finds fewer links there.
