# About dspace-python-client

## Table of contents

- [Introduction](#introduction)
- [Main reasons this tool exists](#main-reasons-this-tool-exists)
  - [1. Help Atmire SaaS clients despite the limitation that we cannot customize the DSpace code for clients in SaaS](#1-help-atmire-saas-clients-despite-the-limitation-that-we-cannot-customize-the-dspace-code-for-clients-in-saas)
  - [2. Support DSpace Open Source development](#2-support-dspace-open-source-development)
- [Installation & other technical documentation](#installation--other-technical-documentation)
- [FAQ](#faq)
  - [What categories of problems can `dspace-python-client` be used for?](#what-categories-of-problems-can-dspace-python-client-be-used-for)
  - [Why Python and not Java (or another toolset)?](#why-python-and-not-java-or-another-toolset)
  - [`dspace-seed`: instead of generating all this content, wouldn't it be better to share a big DB or assetstore that people can just deploy?](#dspace-seed-instead-of-generating-all-this-content-wouldnt-it-be-better-to-share-a-big-db-or-assetstore-that-people-can-just-deploy)

---

## Introduction

`dspace-python-client` is developed by Atmire and published open source at
[https://github.com/atmire/dspace-python-client](https://github.com/atmire/dspace-python-client).

It includes a number of powerful examples of what can be achieved when developing
directly against the DSpace REST API for various actions that read and write data
directly to DSpace, bypassing the Angular UI.

Aside from the included examples, it has robust infrastructure to make it easy for
developers to write additional examples/scripts.

---

## Main reasons this tool exists

### 1. Help Atmire SaaS clients despite the limitation that we cannot customize the DSpace code for clients in SaaS

When supporting clients on **DSpace Express** or **Open Repository**, Atmire staff is
often very limited in what they can or can't do to address a client's requirement or
find a solution for their problem. This is because, in contrast to Tier 3,
customizations to the DSpace Java code are not allowed in DSpace Express or Open
Repository — or at least they should be extremely exceptional.

Whenever problems or use cases emerge from multiple clients, we tend to address them
by providing solutions in future versions of our SaaS platforms. This can take very
long. And even then, if the problem or question from the client is so client-specific,
it's possible that we cannot justify adding or modifying code on our platforms to
address it.

This tool offers a solution path by allowing custom code solutions to be created
**completely outside of the DSpace codebase**, so that they do not affect the future
upgrade path for SaaS clients.

> ⚠️ **Caveat:** This solution path is not perfect. High (recurring!) reliance on
> `dspace-python-client` scripts — which could also break between upgrades — is a
> form of client-specific "technical debt" that should be treated carefully.
>
> But for **one-off solutions** to problems (e.g., when there is no need to keep
> running these scripts on a regular basis), it is a very viable path.

**Examples of client SaaS support tickets that led to example scripts in
`dspace-python-client` and to effective solutions:**

- **Sciensano** — Link ORCID authorities to items
- **Galway** — Find full text for metadata-only items that *do* have a DOI
  (first developed in Google Sheets, now also in `dspace-python-client`)
- **Tilburg** — Extract item metadata by year, to cope with the limitation that the
  CSV exporter has an item limit cap

### 2. Support DSpace Open Source development

This reason ties specifically to the `dspace-seed` example scripts.

**Problem statement:** In DSpace Open Source development, developers often test with
very small, often empty installations of DSpace. The main demo instance only contains
547 items ([demo.dspace.org](https://demo.dspace.org/search?spc.page=1)). Companies
like Atmire, who need to deploy DSpace at scale (often **100k or more items**),
regularly encounter performance or other related errors that have not been uncovered
in development, because developers do not test at scale.

**How `dspace-seed` helps:** These scripts make it very easy to generate a lot of
content in a DSpace repository, including:

- Communities
- Collections
- Items
- Bitstreams
- EPeople
- Statistics

---

## Installation & other technical documentation

See `README.md` at
[https://github.com/atmire/dspace-python-client/blob/main/README.md](https://github.com/atmire/dspace-python-client/blob/main/README.md).

---

## FAQ

### What categories of problems can `dspace-python-client` be used for?

The following aspects can be reasons to consider building a solution with
`dspace-python-client`:

- **One-off** — If you know the problem or client question is one-off in nature and
  not recurring, you have a stronger case. For recurring needs/problems, the general
  preference is building something in the DSpace codebase.
- **Client-unique** — If the problem is unlikely to appear in the same form for other
  clients, it makes less sense to build solutions in the DSpace code or feature
  branches, as the chance for reuse may be very limited.
- **Rapid prototyping** — If you're not exactly sure yet in which direction you want
  to go, making something quick in `dspace-python-client` can help to (in)validate a
  particular solution. You can always consider later building the "real" final thing
  in the DSpace code.
- **Small amounts of data** — Whenever we talk to the REST API, we want to avoid
  creating performance problems by leaving time between requests. That means the more
  data you want to consult or touch, the longer the script will need to execute — up
  to the point where it might need to run for multiple hours or even days to complete.
  Long run times are a reliable sign that you might either need to optimise *how* you
  talk to the REST API (which endpoints, sequence of calls, etc.) or consider doing it
  entirely differently — for example with direct queries against the database.

So within these criteria, typical fits include:

- Very specific, one-off reports
- Very specific changes to repository content
- …

### Why Python and not Java (or another toolset)?

Honestly, this is a fairly arbitrary choice, and a Java equivalent would have been a
reasonable call too. Python won out for a few practical reasons:

- **No compile step.** You edit a `.py` file and run it. There is no build pipeline
  between a change and a working script.
- **Less tooling to install.** Python plus `pip` is enough to run everything. There is
  no Maven (or Gradle) process for dependency resolution, no JDK version juggling, and
  no packaging format beyond "a folder of files".
- **Lower entry threshold for ad-hoc scripts.** Quick HTTP calls, JSON parsing, and
  file I/O tend to be short and readable in Python, which matters a lot for the
  one-off scripting use cases this tool is designed for.
- **Good async HTTP story.** Talking to DSpace at scale benefits from batching and
  concurrent requests, and Python's `httpx` + `asyncio` make adaptive concurrency
  reasonably pleasant to write.

That said, a lightweight Java library that talks directly to the DSpace REST API
would still have real merit:

- Most Atmire developers and DSpace committers already live in the Java ecosystem, so
  Java code can sit closer to existing DSpace tooling.
- Strong static typing catches a class of bugs earlier, which matters when scripts
  touch production repositories.
- Reusing DSpace model classes or utility code from the core project may occasionally
  be worth the extra setup overhead.

So the choice here is *"Python, because it's quick to pick up and quick to iterate
in"*, not *"Python, because Java is wrong"*. If a Java equivalent shows up later and
fills gaps this tool does not, that is a good outcome — not a competing one.

### `dspace-seed`: instead of generating all this content, wouldn't it be better to share a big DB or assetstore that people can just deploy?

It is indeed a limitation that `dspace-seed` needs to run for many hours in order to
create millions of objects. However, sharing a big DB or big assetstore also has its
challenges:

- Storing and downloading big volumes needs a lot of storage and bandwidth.
- If the content was not created in the repository itself — and especially if a
  different version of DSpace is used than the one where this sample content was
  initially created — there could be compatibility problems.

That said, if a big DB or big assetstore is readily available somewhere for a
particular test instance of DSpace, downloading and using it may be quicker than
generating it with `dspace-seed`.
