<div align="center">

<img src="docs/assets/logo.svg" width="64" height="64" alt="AwesomeBooks Scraper Pro logo" />

# AwesomeBooks Scraper Pro

**Catalog extraction and live New & Used stock monitoring for [awesomebooks.com](https://www.awesomebooks.com), priced for US shipping. Resumable, observable, and built not to get blocked.**

[![CI](https://github.com/abuhuraira99/awesomebooks-scraper-pro/actions/workflows/ci.yml/badge.svg)](https://github.com/abuhuraira99/awesomebooks-scraper-pro/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.14-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/flask-3.x-000?logo=flask)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![No browser](https://img.shields.io/badge/selenium-not%20required-1f8a63)
![Tests](https://img.shields.io/badge/tests-130-b88a3c)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

<img src="docs/assets/catalog.png" alt="Catalog Extractor" width="100%" />

</div>

---

Two tools in one self-hosted dashboard:

| | **Catalog Extractor** | **Stock Monitor** |
|---|---|---|
| **Input** | Any AwesomeBooks section link: a category, Used, Bargain, New & Trending, Bestsellers, an author or a search | A CSV / Excel file of ISBNs (ISBN-13, ISBN-10, or AwesomeBooks SKUs) |
| **Does** | Reads every page of the section, including sections far larger than the 10,000 results the site will page through, then opens each book as **New** and as **Used** | Searches each ISBN the way a shopper would and reports **In Stock + exact quantity**, or **Out of Stock** (sold out / not listed) |
| **Output** | `.xlsx` with one row per ISBN-13: New Available · New Price · New Qty · Used Available · Used Price · Used Qty, plus title, author, format, publisher, pages, size, category | `.xlsx` in the same row order as the upload, with the same New / Used columns and a total Quantity |
| **When done** | Saved to the PC's **Downloads** folder automatically | Same |

Only one job runs at a time across both modules. They share one IP and one politeness budget.

## What makes AwesomeBooks different, and how it's handled

| The site… | The scraper… |
|---|---|
| …asks every new visitor for a **shipping country** (pop-up, defaulting to UK) and prices differ by country, not just by currency (£16.34 in the UK is **$25.54** for a US shopper, not $21.78) | …behaves like a US shopper: each browser identity opens a page, picks *Ship to United States* from the header, and is sent back to the same page in USD. **Every page is re-checked for "SHIP TO US" and "USD" before a price is recorded.** A page in any other locale is discarded and the locale set again; prices are never converted or guessed. |
| …shows a **20% OFF pop-up** and a cookie banner | …never renders a page, so pop-ups don't exist for it. No Selenium, no clicks, nothing on screen. |
| …shows **two ISBNs** per book (ISBN-13 and ISBN-10) | …outputs **ISBN-13 only**. Uploaded ISBN-10s are converted to their ISBN-13. |
| …has **NEW / USED buttons**: clicking one turns it green and changes the price and stock, and Used copies also appear on "new" books | …opens each condition's own page (the buttons are links), reads its price and stock, and confirms which condition the site actually served: asking for a condition with no stock silently redirects to the other one, so availability is decided by the page served, never assumed. |
| …tells shoppers **"20+ in stock"** | …reports the **exact** count the page carries in its own e-commerce data (`product_availability`), e.g. 1,482. Up to 20 it equals the visible number. |
| …pages through at most **500 pages (10,000 results)** per listing | …splits bigger sections into slices by condition, category filter, sub-category and format using the site's own exact counts, and reads slices that can't be narrowed in all four sort orders. The run reports exactly how many books it reached against how many the section lists. |
| …answers unknown pages with its **home page** (not a 404) | …recognises the home page by its canonical link, so a carousel of trending books is never mistaken for results. |
| …is behind **Cloudflare**, which injects its detection script into every page | …does not mistake that script for a challenge, but treats a real interstitial or any 403 as a block: everyone pauses, identities rotate, and the pace eases. |

## Highlights

- **No headless browser.** AwesomeBooks renders its pages on the server, so the engine reads the same HTML a browser receives, using about 40 MB of RAM instead of gigabytes of Chrome. Bestsellers, which the site draws with JavaScript, is read from the same search endpoint the page itself calls.
- **Exact, US-only numbers.** US-dollar prices for New and Used separately, exact stock per condition, ISBN-13 only. Nothing is estimated: if the site doesn't publish a value, the cell is empty.
- **Built not to get blocked.** A global adaptive scheduler, real browser TLS fingerprints (`curl_cffi`), short-lived browser identities that behave like real shoppers, Retry-After-aware cool-downs and a circuit breaker. See [Traffic Guard](#traffic-guard-how-it-avoids-blocks).
- **Never guesses.** A network failure is never written as "out of stock". "Not listed" means the site itself sent the visitor to its home page; "out of stock" means the book page says so.
- **Crash-proof and self-resuming.** Every result is committed to SQLite as it arrives. After a crash, reboot or power cut, the interrupted job resumes automatically from the exact page or row where it stopped. Internet outages are waited out without failing a single item.
- **Downloads itself.** A finished run writes its Excel file straight into Downloads, even if nobody is watching.
- **Tiny footprint.** Two SQLite files (`catalog.db`, `inventory.db`), no database server. Setup on Windows is a double-click.
- **A dashboard people enjoy using.** Live progress, ETA, a Traffic Guard panel, an activity feed, searchable results with New / Used chips, and an editorial *Emerald & Gold* design in dark and light themes.

<p align="center"><img src="docs/assets/results.png" alt="Results with New and Used offers" width="100%" /></p>

## Quick start

### Windows (one click)

1. Download or clone this repository into a short path such as `C:\AwesomeBooks Scraper Pro`.
2. Double-click **`setup.bat`** once. It installs Python if it's missing, creates a private virtual environment (`.venv`) and installs the dependencies.
3. Double-click **`run.bat`** whenever you want to use it. A console window opens (keep it open while a job runs) and the dashboard opens at <http://127.0.0.1:7642>.

Closing the browser tab does **not** stop a job; closing the console window does. Running `run.bat` while the app is already open simply reopens the dashboard.

### macOS / Linux

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m awesomebooks_pro --open
```

### Docker

```bash
docker build -t awesomebooks-scraper-pro .
docker run -p 7642:7642 -v "$PWD/data:/app/data" \
  -e ABP_AUTH_USER=admin -e ABP_AUTH_PASSWORD=change-me awesomebooks-scraper-pro
```

## Using it

### Catalog Extractor

1. Open any section on awesomebooks.com and copy its address, e.g. `https://www.awesomebooks.com/used-books` or `https://www.awesomebooks.com/books/category/303/fiction?condition=used`. The quick-pick chips fill in common sections.
2. Choose **Sessions** (parallel shoppers sharing one pace) and **Pace** (*Careful ≈ 30 / Balanced ≈ 55 / Fast ≈ 100 requests per minute*).
3. Options:
   - **New & Used prices · exact stock** (default on) opens every book as New and as Used. Off, a section is read from its listing pages alone: ISBN, title, author, format and the price of the condition the listing shows, about 20 times faster. The quantity columns are then left out of the file instead of being exported empty.
   - **Max books** (optional) stops after that many books. Leave it empty to read the whole section.
4. Press **Start extraction**. When it completes, the Excel file appears in Downloads automatically.

> Speed: about one book every two seconds at *Balanced* (two pages per book, New and Used). That's deliberate. AwesomeBooks sections are big (Used lists ~930,000 books), so use **Max books** or a narrower section when you need part of one.

### Stock Monitor

1. Drag a `.csv`, `.xlsx` or `.txt` file onto the upload area. The easiest start is the **[upload template](awesomebooks_pro/static/ISBN_Upload_Template.xlsx)**, a single `ISBN` column pre-formatted as text so Excel keeps every digit. Headers aren't case-sensitive and other columns are ignored; without an *ISBN / EAN / barcode / SKU* header, the column with the most valid ISBNs is used.
2. Messy input is normalised: `978-1-529-96103-4`, ISBN-10s, and AwesomeBooks SKUs like `9053-9780241003008` or `4444-PEN-9781529961034` all work. Duplicates are checked once. Cells Excel turned into `9.78164E+12` are flagged with an explanation.
3. Pick Sessions and Pace and press **Start stock check**. The finished Excel file is saved to Downloads automatically.

<p align="center"><img src="docs/assets/inventory.png" alt="Stock Monitor in the light theme" width="100%" /></p>

## Output columns

Every value comes from AwesomeBooks itself, as a US shopper sees it.

<details open>
<summary><b>Catalog export</b></summary>

| Column | Exact source |
|---|---|
| ISBN-13 | The book page's own product id, checksum-validated (ISBN-10 is never output) |
| Title, Author | The book page's schema.org data |
| Format | The *More Details* table |
| Availability | *In Stock* when New or Used can be bought now |
| **New Available / New Price (USD) / New Qty** | The New offer's page: price shown to a US shopper (the site states it includes shipping) and the exact stock |
| **Used Available / Used Price (USD) / Used Qty** | The Used offer's page, read the same way |
| RRP (USD) | The crossed-out list price, when the site shows one |
| Publisher, Publication Date, Pages, Dimensions, Weight | *More Details* table and schema.org data |
| Category | The page's breadcrumb trail |
| Note | e.g. *Out of stock on AwesomeBooks*, *Only offered as a rare / collectable copy* |
| Product URL, Image URL | Links to the book and its cover |

</details>

<details open>
<summary><b>Stock check export</b></summary>

| Column | Meaning |
|---|---|
| SKU | Exactly as it appeared in your file |
| ISBN-13 | Normalised ISBN-13 |
| Status | **In Stock** or **Out of Stock** (sold out or not listed), *Invalid ISBN*, or *Check failed* (a network problem, retryable; never guessed) |
| Quantity | New Qty + Used Qty. `0` when out of stock or not listed |
| New / Used Available, Price (USD), Qty | As in the catalog export |
| Title, Author, Format, RRP | As listed on AwesomeBooks |
| Listed on AwesomeBooks | *No* only when the site sends a shopper to its home page instead of a book page |
| Note, Product URL, Checked At | Context, link and timestamp of the check |

</details>

> **Prices move by a few cents.** AwesomeBooks converts its prices to dollars with an exchange rate that differs slightly between its servers (e.g. $15.15 on one visit, $15.18 on the next). Each row holds exactly what the site showed at the moment it was checked.

## Traffic Guard: how it avoids blocks

Politeness is the core of the design, not a feature added later:

| Layer | What it does |
|---|---|
| **Global scheduler** | Every session draws from one shared schedule, so adding sessions never raises the request rate. Each gap is randomly jittered (±35%) so the traffic has no machine-regular rhythm. |
| **Adaptive pacing** | On any warning (HTTP 403/429/503, a Cloudflare interstitial, or HTML where JSON was expected) the gap doubles and all sessions pause together, honouring `Retry-After`. After a clean streak the pace eases back, never faster than the pace you chose. |
| **Circuit breaker** | Three warnings in a row pause the whole engine for four minutes before it tries again. |
| **Real browser fingerprints** | Requests go through [`curl_cffi`](https://github.com/lexiforest/curl_cffi), whose TLS and HTTP/2 handshakes match real Chrome, Safari and Firefox builds, not the telltale Python handshake. |
| **Shopper-like identities** | Each session is an ordinary visitor with its own cookie jar, browser build and language. It arrives, picks *Ship to US*, browses with matching referrers (search box → book page → its New/Used button), and retires after a random 35–90 requests, or immediately after any warning. |
| **Optional proxies** | One proxy URL per line in a file, set `ABP_PROXY_FILE`, and identities round-robin through them for IP diversity. |

The **Traffic Guard** panel shows all of this live: effective pace, cool-downs, shoppers used, slow-down signals, network retries and locale switches. Finished runs keep their report.

## Security

Built to be safe on a client's everyday PC:

- **Not reachable from the network.** It listens on `127.0.0.1` only and opens no firewall port.
- **Protected against browser-based attacks.** A foreign `Host` header is refused (DNS rebinding), and state-changing calls need a header only the dashboard's own scripts send (CSRF). A strict Content-Security-Policy, `X-Frame-Options: DENY` and `nosniff` are set on every response.
- **Safe with uploaded files.** Uploads are parsed in memory and never stored or executed. Size and row limits apply, Excel files are read through `defusedxml`, and text starting with `=` is written to Excel as plain text so it can never become a live formula.
- **No secrets, no telemetry.** It stores no passwords or keys and talks to nobody except awesomebooks.com.
- **Going beyond one PC** (a VPS): set `ABP_HOST=0.0.0.0` with `ABP_AUTH_USER` / `ABP_AUTH_PASSWORD`, and list the server's name in `ABP_ALLOWED_HOSTS`.

## Configuration

All settings are optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `ABP_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` on a VPS. |
| `ABP_PORT` | `7642` | Dashboard port (if it is busy, the next free port is used) |
| `ABP_SHIP_TO` / `ABP_CURRENCY` | `US` / `USD` | Shipping country and currency every price is verified against |
| `ABP_AUTH_USER` / `ABP_AUTH_PASSWORD` | *(off)* | HTTP Basic Auth for the dashboard |
| `ABP_DATA_DIR` | `./data` | Where the SQLite databases live |
| `ABP_MAX_SESSIONS` | `6` | Upper limit for the Sessions control |
| `ABP_PROXY_FILE` | *(none)* | Text file with one `http://user:pass@host:port` per line |
| `ABP_MAX_REQUEST_ATTEMPTS` | `7` | Retries for a single request before it counts as a failure |
| `ABP_MAX_ITEM_ATTEMPTS` | `3` | Times a failed item is re-queued before it's marked *Failed* |
| `ABP_REQUEST_TIMEOUT` | `40` | Seconds per request |
| `ABP_EXPORT_DIR` | *(Downloads)* | Where finished runs are saved automatically |
| `ABP_AUTO_RESUME` | `1` | Resume an interrupted job on start-up (`0` to turn off) |
| `ABP_ALLOWED_HOSTS` | *(none)* | Extra host names accepted when serving beyond localhost |

## Architecture

```mermaid
flowchart LR
    UI["Dashboard<br/>(vanilla JS)"] -- REST / polling --> API["Flask API<br/>web.py"]
    API --> JM["JobManager<br/>one job at a time"]
    JM --> SP["Scan planner<br/>scan.py"]
    JM --> W1["Shopper 1"] & W2["Shopper 2"] & W3["Shopper N"]
    W1 & W2 & W3 --> SC["AwesomeBooksClient<br/>site.py · US locale · New/Used"]
    SC --> HC["HttpClient<br/>identities · retries · block detection"]
    HC --> RL["AdaptiveRateLimiter<br/>shared schedule · cool-downs · breaker"]
    HC -->|curl_cffi| AB[("awesomebooks.com<br/>behind Cloudflare")]
    JM <--> DB1[("catalog.db")]
    JM <--> DB2[("inventory.db")]
    API --> XL["Excel export<br/>openpyxl"]
```

The research behind every rule (what the site serves, where each number comes from, and the edge cases found in the browser) is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

```
awesomebooks_pro/
├── net/              # client.py · identity.py · ratelimit.py: the anti-block HTTP layer
├── site.py           # what to ask AwesomeBooks: US locale, books, New/Used, search, listings
├── parsing.py        # pure parsers for book pages, listings and search results
├── scan.py           # slicing sections larger than the site's 10,000-result window
├── jobs.py           # job engine, worker sessions, single-job lock
├── storage.py        # SQLite stores (catalog.db, inventory.db)
├── inputs.py         # CSV / XLSX / TXT upload parsing & ISBN column detection
├── isbn.py           # ISBN-10/13 validation and normalisation
├── export.py         # styled Excel workbooks
├── web.py            # Flask API
└── static/           # dashboard (HTML/CSS/JS, no build step)
tests/                # 130 tests: real-page parsing, a fake storefront end to end, failure injection, live
```

## Development

```bash
pip install -r requirements-dev.txt
pytest                         # offline suite: unit + end-to-end against a fake storefront
ABP_LIVE=1 pytest -m live -v   # read-only smoke tests against the real site
ruff check . && ruff format --check .
python packaging/build_zip.py  # the client ZIP
```

The parser tests run against real AwesomeBooks pages saved in `tests/fixtures`. The end-to-end tests drive the real Flask API, job engine, HTTP client, SQLite stores and Excel exporter against an in-memory fake of the site. Every fake visitor starts with UK prices, and the fake injects 403/429/503 responses and sessions that "forget" their country mid-run, to prove that throttling is absorbed and that a non-US price can never reach a spreadsheet.

## Lineage

The fifth tool in a family built for the same client: *Hamilton Scraper Pro* (URL-driven catalog extraction), *Oldies Scraper Pro* (strict rate limiting), *Amazon & Hamilton Inventory Scraper* (CSV stock checks) and *BookOutlet Scraper Pro* (browserless engine, Traffic Guard, auto-resume). This one adds shipping-country handling, per-condition offers, exact stock and listing slicing.

## Responsible use

This tool reads public product information at a pace comparable to a person browsing. Keep the default pacing, run one job at a time, and respect AwesomeBooks' Terms and any instructions the site gives. It is not affiliated with or endorsed by AwesomeBooks.

## License

[MIT](LICENSE) © Abu Huraira
