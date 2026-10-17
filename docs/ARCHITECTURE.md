# Architecture

This document explains how AwesomeBooks Scraper Pro works and, above all, *why*: what awesomebooks.com serves, where every number in the spreadsheets comes from, and which edge cases shaped the design. Everything below was established by reading the live site with plain HTTP and confirming it in a real browser (Chrome DevTools) before any code was written.

## 1. What the site is

AwesomeBooks is a Laravel application behind Cloudflare. Pages are rendered on the server, so the HTML a browser receives already contains every price, stock figure and book detail; no JavaScript has to run. Plain requests with a real browser's TLS fingerprint get the same `200` pages a shopper gets. That is why the engine needs no Selenium or Playwright: it reads the same documents a browser would, roughly 50 times lighter.

The one page drawn by JavaScript is **Bestsellers**. It calls the site's own search proxy, `POST /psearch` (Algolia under the hood, protected by the visitor's Laravel CSRF token), and the engine makes the same call.

## 2. US pricing is a session setting

A first-time visitor is treated as a UK shopper: `SHIP TO GB`, `CURRENCY £ GBP`, plus a *Shipping Country* modal and a *20% OFF* pop-up. The modal's **Confirm** posts `shippingCountryIso2=US` to `/shippingCountry`; the header's *Ship to* menu links to `GET /changeLocation/US`. Both store the country in the visitor's session (`awesomebooks_session` cookie), switch the currency to USD, and redirect back to the page the visitor was on (the `Referer`).

The country changes the **price itself**, not just the currency: *Make Me Well* is £16.34 for the UK and **$25.54** for the US (16.34 × 1.33 would be $21.78), because US prices include tracked USPS shipping. Converting is never an option.

What the engine does:

1. Each browser identity (cookie jar) opens its first page, sees UK prices, and calls `/changeLocation/US` with that page as `Referer`, exactly what a shopper using the header menu produces. The site answers with the same page in USD.
2. **Every page is verified before use.** `parsing.page_locale()` reads `window.shippingCountry` and the header's currency switcher, which are present on every page type (listing pages have no `currentCurrency` object, so the header is the reliable marker). Only `("US", "USD")` passes.
3. A page in any other locale means the session lost its country (expired, or the identity rotated mid-request after a warning). The page is discarded, the locale is set again, and the page is fetched again. After four failed attempts the item fails with `LocaleError`. A foreign price is never stored.

The pop-ups are never a concern because nothing is rendered.

## 3. A book page, condition by condition

`/book/{isbn13}/{slug}` shows the **New** offer when one exists; `/book/{isbn13}/{slug}/used` shows the **Used** offer. The slug is ignored by the server. The page carries:

| Data | Where |
|---|---|
| ISBN-13 | `const currentObjectID = '978…'`, also the *ISBN13* row of *More Details* |
| Condition shown | `#currentCondition` (`NEW` / `USED`) |
| Price for this condition | `#price[data-price]`, e.g. `$8.84` |
| RRP | `.text-red del` (hidden when the price is above it); `List price :` on sold-out pages |
| Visible stock | `#availability`: the exact number up to 20, then `20+` |
| **Exact stock** | GA4 data layer: `"product_availability": 1482` (equals the visible number up to 20) |
| Conditions sold | `.condition-options-item` links: one per condition, the selected one has a checked radio |
| Details | *More Details* table (ISBN13, ISBN10, pages, weight, dimensions, publisher, format), schema.org `Book` JSON-LD, breadcrumb |

**The NEW / USED buttons are links.** Clicking one loads that condition's page, and the site styles the selected button green. Each condition has its own price *and* its own stock (*The Very Hungry Caterpillar*: New $13.01 × 1,482, Used $8.84 × 3). The buttons also contain hidden (`display:none`) prices that do **not** match what the shopper sees after clicking, so they are ignored.

**Unavailable conditions redirect.** Asking for `/used` on a book without used stock returns the New page, and vice versa. The condition actually served is therefore always read back from `#currentCondition` and compared with the one requested; a mismatch means *not available*, never a misattributed price.

Other outcomes:

| Page | Meaning |
|---|---|
| Book page, no condition, buy box says *Out of stock* | Listed, sold out (`product_availability: 0`) |
| The home page (canonical link is the bare domain) | ISBN not listed: the site redirects unknown books home |
| `/book/{isbn}` without a slug | 404, which is why a slug is always sent |

`site.AwesomeBooksClient.product()` opens the default page, records its offer, then opens every other New/Used condition linked from the buttons. That is one page for single-condition books and two for books sold both ways.

## 4. Stock checks start at the search box

For an uploaded ISBN, `lookup_stock()` behaves like a shopper: it searches the ISBN through `/psearch` (the request the site's search box sends), follows the result's real link, then reads the book as above. The search index holds **in-stock titles only**, so an ISBN it doesn't know is opened directly to tell *sold out* (a book page saying so) from *not listed* (the home page). Search results are used only for navigation; index prices are UK base prices and are never output.

## 5. Listings and the 10,000-result window

Category, Used, Bargain, New & Trending, author, search and campaign pages share one card layout (`.product-item__body`), 20 books per page via `?page=N`, and a counter. The counter reads `Showing <page> – <page size> of <N> results`; its first number is the **page number**, not a result index, which the engine uses to confirm it received the page it asked for.

Paging stops at **page 500** (10,000 results); page 501 silently redirects to the home page, whose carousels would look like results. Every listing answer is therefore checked by its canonical link, and a home-page answer means *end of listing* (or, on page 1, *no such section*).

Sections are much larger than that (Used ≈ 931,000, Fiction ≈ 700,000, New & Trending ≈ 5.4 million), so `scan.py` slices them using refinements the listing itself offers, with exact counts:

1. `condition` (new / used / rare), on category-style pages;
2. `catid`, the category filter on Used, Bargain, New & Trending and similar pages;
3. sub-categories, the child category pages in a category's sidebar (one level, so sibling links can never cause a loop);
4. `format` (paperback, hardback, …).

A slice that still exceeds 10,000 after all four is read in each of the site's four sort orders (price ↑/↓, publication date ↑/↓), reaching up to 40,000 different books. Books are de-duplicated by ISBN-13. At the end the run reports how many books it reached against the count the section advertises, so a shortfall is visible, never silent. The whole plan is JSON stored with the job, so discovery resumes on the exact slice and page after a crash or Stop.

## 6. Traffic Guard

Unchanged in spirit from BookOutlet Scraper Pro, adjusted for Cloudflare:

- **One shared schedule** (`AdaptiveRateLimiter`) for all sessions, jittered ±35%.
- **Block detection** that knows the site: every normal page includes Cloudflare's passive script at `/cdn-cgi/challenge-platform/scripts/jsd/main.js`, so the word *challenge-platform* alone is **not** a block. Interstitials are recognised by `cf-chl` / `_cf_chl_opt`, the `/challenge-platform/h/` orchestrator, Turnstile or a *Just a moment…* title. Any 403 is a firewall verdict.
- On a warning: all sessions pause (honouring `Retry-After`), the gap doubles, the identity is retired; three in a row trip a 4-minute circuit breaker.
- **Identities** (`IdentityFactory`) are short-lived shoppers: real Chrome/Safari/Firefox TLS + HTTP/2 fingerprints via `curl_cffi`, their own cookie jar and language, 35–90 requests each. A new identity sets the US locale like a new visitor would (§2).
- Outages of *this* PC's internet are told apart from site refusals and waited out without spending an item's attempts.

## 7. Persistence and jobs

Two SQLite files in WAL mode: `catalog.db` and `inventory.db`. Workers claim rows atomically, write each result as it arrives, and move on. At start-up, a job that was running when the process died becomes *interrupted*, its in-flight rows go back to the queue, and it resumes automatically. A manual Stop waits for Resume. Starting a new run on a page clears that page's previous run (its Excel file was already saved).

Only one job runs at a time across both modules: they share the IP and the politeness budget.

## 8. Testing strategy

| Suite | What it proves |
|---|---|
| `test_parsing.py` | The parsers against **real AwesomeBooks pages** (trimmed copies in `tests/fixtures`): both conditions, single-condition, sold out, UK locale, listings, campaign page, home page, search JSON |
| `test_site.py` | The client against a fake storefront: the US switch, re-switching after a session loses its country, refusing to record foreign prices, condition redirects, search token renewal, end of listing |
| `test_scan.py` | Slice planning: dimension order, zero-count facets, sort-order fallback, last-page rules |
| `test_app.py` | End to end through the Flask API with throttling injection, locale loss mid-run, sections bigger than the window, Bestsellers, Stop/Resume without duplicates, uploads, Excel columns, security headers, formula-injection guard |
| `test_net.py` | Rate limiter, identity rotation, Cloudflare detection (and *not* flagging normal pages), outages, POST |
| `test_live.py` | Opt-in read-only checks against the real site to catch a redesign early |
