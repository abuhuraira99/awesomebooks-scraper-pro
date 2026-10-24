# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-10-24

### Added
- **Delivery window per condition**: two new Excel columns, **New Delivery** and **Used Delivery**, in both the Catalog Extractor and the Stock Monitor export. They hold the "Standard worldwide delivery by …" window the book page quotes a US shopper for that condition (e.g. *Thu, November 5 - Tue, November 10*), exactly as the site writes it. Used copies often ship sooner than New, so each condition is read from its own page. A condition that isn't in stock has no window. The "Order within …" countdown is the daily dispatch cut-off and is not saved.

### Changed
- Nothing else. The new columns are appended after the last existing column, so every v1.0 column keeps its position. Databases from v1.0 gain the two columns automatically on start-up; existing runs, including one that was interrupted, carry on unchanged.

## [1.0.0] - 2026-10-17

### Added
- **Catalog Extractor** for any awesomebooks.com section (categories, Used, Bargain, New & Trending, Bestsellers, authors, searches, campaigns): every page is read, then each book is opened as New and as Used.
- **Stock Monitor**: upload CSV / XLSX / TXT, detect the ISBN column automatically, normalise ISBN-10s and AwesomeBooks SKUs (`9053-978…`, `4444-PEN-978…`) to ISBN-13, and report In Stock with exact quantity, or Out of Stock (sold out / not listed).
- **US pricing guaranteed**: every browser identity picks *Ship to United States* like a shopper, and every page is verified as `SHIP TO US` / `USD` before a price is recorded. Pages in any other locale are discarded and the locale set again.
- **New / Used offers side by side**: New Available, New Price (USD), New Qty, Used Available, Used Price (USD), Used Qty. Each condition is read from its own page, and the condition the site actually served is always checked (unavailable conditions redirect).
- **Exact stock** from the page's own e-commerce data instead of the visible "20+".
- **ISBN-13 only** in every export.
- **Sections larger than the site's 10,000-result window** are sliced by condition, category filter, sub-category and format, with exact counts; leftovers are read in all four sort orders, and coverage is reported.
- **Bestsellers** via the site's own search endpoint (the top 1,000, as the page shows).
- Optional **Max books** limit and a quick listing-only mode.
- **Traffic Guard** from BookOutlet Scraper Pro, adapted to Cloudflare: Cloudflare's passive script on every page is not mistaken for a challenge; real interstitials and any 403 trigger a shared cool-down and a new identity.
- Resumable jobs in two SQLite databases, auto-save to Downloads, auto-resume after a crash, offline detection.
- New *Emerald & Gold* dashboard with dark and light themes.
- 130 tests: parsers against real AwesomeBooks pages, a fake storefront end to end (UK-first visitors, throttling and session-loss injection), and opt-in live checks.

### Lineage
Builds on *BookOutlet Scraper Pro* v1.2.0 (browserless engine, Traffic Guard, auto-save, auto-resume, port fallback), which built on *Hamilton Scraper Pro*, *Oldies Scraper Pro* and *Amazon & Hamilton Inventory Scraper*.
