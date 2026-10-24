"""Shared fixtures: an in-memory fake of the AwesomeBooks storefront.

The fake behaves like the real site in every way the scraper depends on:

* every browser identity is its own visitor session, shown UK prices in GBP
  until it picks "Ship to US" (``/changeLocation/US``), which redirects back
  to the page it came from;
* book pages show one condition at a time; asking for a condition without
  stock redirects to the one that has it, a sold-out book shows "Out of stock",
  and an unknown ISBN sends the visitor to the home page;
* listings print "Showing <page> – 20 of N results", offer condition / format /
  category refinements, and stop paging at a cap (the home page after that);
* ``/psearch`` (the site's search proxy) demands the session's CSRF token.

So the whole stack (HTTP client, site client, job engine, SQLite and the Flask
API) is exercised end to end without touching the network.
"""

from __future__ import annotations

import html
import json
import math
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.awesomebooks.test"
GBP_RATE = 0.75  # UK visitors see different numbers: the scraper must never record them


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_isbn(n: int) -> str:
    core = f"978{n:09d}"
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


# Valid ISBN-13s used across the tests
ISBNS = [make_isbn(1000 + i) for i in range(10)]


@dataclass
class FakeResponse:
    status_code: int
    text: str
    headers: dict = field(default_factory=dict)


@dataclass
class Book:
    isbn: str
    title: str
    new: tuple[float, int] | None = (12.5, 40)  # (US price, exact stock)
    used: tuple[float, int] | None = None
    listed: bool = True
    author: str = "Jane Doe"
    format: str = "paperback"
    publisher: str = "Test House"
    category: int = 303
    sub: int = 11
    rank: int = 0
    rrp: float = 15.99
    # Delivery window the book page quotes for each condition (Used usually ships sooner).
    new_delivery: str = "Thu, November 5 - Tue, November 10"
    used_delivery: str = "Tue, November 3 - Fri, November 6"

    def offers(self) -> dict[str, tuple[float, int]]:
        out = {}
        if self.new and self.new[1] > 0:
            out["New"] = self.new
        if self.used and self.used[1] > 0:
            out["Used"] = self.used
        return out

    @property
    def slug(self) -> str:
        return "-".join(self.title.lower().split())


def make_books(n: int, start: int = 0) -> list[Book]:
    """A varied shelf: some with Used copies, some Used-only, a few formats and sub-categories."""
    books = []
    for i in range(start, start + n):
        new = (10.0 + (i % 7), 5 + i) if i % 9 != 4 else None
        used = (4.5 + (i % 5), 1 + i % 4) if i % 3 == 0 or new is None else None
        books.append(
            Book(
                isbn=make_isbn(5000 + i),
                title=f"Book {i}",
                new=new,
                used=used,
                format=("paperback", "hardback", "board book")[i % 3],
                sub=11 + i % 2,
                rank=i + 1,
            )
        )
    return books


class Session:
    """One visitor (one browser identity): its own country and CSRF token."""

    def __init__(self) -> None:
        self.country = "GB"
        self.token = uuid.uuid4().hex


SECTION_PATHS = {
    "/books/category/303/test-fiction": "category",
    "/books/category/11/sub-a": "sub",
    "/books/category/12/sub-b": "sub",
    "/used-books": "used",
    "/campaign/test-picks": "campaign",
}


class FakeAwesomeBooks:
    def __init__(self, books: list[Book]) -> None:
        self.books = {b.isbn: b for b in books}
        self.lock = threading.Lock()
        self.log: list[tuple[str, str, str]] = []  # (method, path, country of the session)
        self.fail_plan: list[int] = []  # status codes returned for the next requests
        self.page_cap = 500
        self.page_size = 20
        self.forget_locale_on: set[int] = set()  # request numbers at which a session "expires" back to GB
        self.locale_changes = 0
        self.search_calls = 0

    # -- routing ----------------------------------------------------------------
    def respond(
        self, session: Session, method: str, url: str, params: dict | None, body: str | None, headers: dict
    ) -> FakeResponse:
        params = dict(params or {})
        parsed = urlparse(url)
        params.update(dict(parse_qsl(parsed.query)))
        with self.lock:
            n = len(self.log)
            self.log.append((method, parsed.path, session.country))
            if n in self.forget_locale_on:
                session.country = "GB"
            if self.fail_plan:
                return FakeResponse(self.fail_plan.pop(0), "<html>Too Many Requests</html>", {"Retry-After": "0"})
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/changeLocation/"):
            session.country = path.rsplit("/", 1)[1].upper()
            with self.lock:
                self.locale_changes += 1
            back = headers.get("Referer") or f"{BASE}/"
            return self.respond_page(session, back)  # the site redirects back to the page the visitor was on
        if path == "/psearch" and method == "POST":
            return self._search(session, json.loads(body or "{}"), headers)
        return self.respond_page(session, url, params)

    def respond_page(self, session: Session, url: str, params: dict | None = None) -> FakeResponse:
        parsed = urlparse(url)
        params = {**dict(parse_qsl(parsed.query)), **(params or {})}
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["book"]:
            if len(parts) < 3:
                return FakeResponse(404, "<html>Not found</html>")
            return FakeResponse(200, self.product_page(session, parts[1], parts[3] if len(parts) > 3 else None))
        if path in SECTION_PATHS:
            return FakeResponse(200, self.listing_page(session, path, params))
        if path == "/books/bestsellers":
            return FakeResponse(200, self.page(session, "/books/bestsellers", "<div id='bestsellers'></div>"))
        return FakeResponse(200, self.home(session))

    # -- page chrome --------------------------------------------------------------
    def page(self, session: Session, canonical: str, body: str, extra_head: str = "") -> str:
        us = session.country == "US"
        symbol, currency = ("$", "USD") if us else ("£", "GBP")
        return (
            "<!doctype html><html><head>"
            f'<meta name="csrf-token" content="{session.token}">'
            f'<link rel="canonical" href="{BASE}{canonical if canonical != "/" else ""}"/>{extra_head}</head><body>'
            '<header><a class="dropdown-toggle ship-to" href="#" id="shipToDropdown">'
            f"<span>SHIP TO {session.country}</span></a>"
            '<a class="dropdown-toggle currency" href="#" id="currencyDropdown" data-toggle="dropdown">'
            f"<span>CURRENCY</span>\n<span>\n {symbol}\n {currency}\n </span></a></header>"
            f'<main id="content">{body}</main>'
            f'<script>window.shippingCountry = "{session.country}";</script>'
            "<script>a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
            "</body></html>"
        )

    def money(self, session: Session, usd: float) -> str:
        return f"${usd:.2f}" if session.country == "US" else f"£{usd * GBP_RATE:.2f}"

    def home(self, session: Session) -> str:
        cards = "".join(self.card(session, b, "New") for b in list(self.books.values())[:4])  # a carousel, like the real one
        return self.page(session, "/", f"<h2>Trending Reads</h2>{cards}")

    # -- book pages -----------------------------------------------------------------
    def product_page(self, session: Session, isbn: str, suffix: str | None) -> str:
        book = self.books.get(isbn)
        if book is None or not book.listed:
            return self.home(session)  # unknown ISBN: the site redirects to its home page
        offers = book.offers()
        wanted = "Used" if suffix == "used" else "New"
        shown = wanted if wanted in offers else next(iter(offers), None)  # redirect to a condition with stock
        base = f"{BASE}/book/{isbn}/{book.slug}"
        ld = {
            "@context": "https://schema.org",
            "@type": "Book",
            "name": book.title,
            "isbn": isbn,
            "author": {"@type": "Person", "name": book.author},
            "publisher": {"@type": "Organization", "name": book.publisher},
            "datePublished": "2024-05-01",
            "numberOfPages": 321,
            "image": f"https://img.test/{isbn}.jpg",
            "url": base,
        }
        head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        if shown is None:
            buy = (
                '<div class="product-view product-cart col-12"><div class="quantity-box">'
                f"Out of stock <span>List price : {self.money(session, book.rrp)}</span></div></div>"
                '<script>var ga4Items = {"product_availability":0};</script>'
            )
        else:
            price, qty = offers[shown]
            visible = "20+" if qty > 20 else str(qty)
            buy = (
                '<div class="product-view product-cart col-12"><div class="quantity-box">'
                f'<strong><span class="text-light">Condition:</span><span id="currentCondition"> {shown.upper()} </span></strong>'
                f'<div class="font-size-36 mr-4" id="price" data-price="{self.money(session, price)}">{self.money(session, price)}</div>'
                f'<div class="d-flex"><div class="text-red">RRP <del class="font-size-16">{self.money(session, book.rrp)}</del></div></div>'
                f'<div class="available-stock">Available <span id="availability"> {visible} </span> in stock</div>'
                f'<input type="hidden" name="sku" id="sku" value="9053-{isbn}"></div></div>'
                f'<script>var ga4Items = {{"item_id":"{isbn}-1","product_availability":{qty},"ISBN":"{isbn}"}};</script>'
            )
        buttons = ""
        if len(offers) > 1:
            for label in offers:
                href = base if label == "New" else f"{base}/used"
                checked = "checked" if label == shown else ""
                buttons += (
                    f'<div class="condition-options-item differentCondition col-md-4"><a href="{href}">'
                    f'<input type="radio" name="conditionitem" {checked}/><label>'
                    f'<span class="font-weight-bold"> {label.upper()} </span><span>{book.format}</span>'
                    '<span class="font-weight-bold" style="display: none">$0.01</span></label></a></div>'
                )
        details = (
            '<table class="table"><tr><td>Type</td><td>Book</td></tr>'
            f"<tr><td>ISBN13</td><td>{isbn}</td></tr><tr><td>ISBN10</td><td>{isbn[3:12]}X</td></tr>"
            "<tr><td>Number Of Pages</td><td>321</td></tr><tr><td>Item Weight</td><td>250 g</td></tr>"
            "<tr><td>Product Dimensions</td><td>129 x 198 x 20 mm</td></tr>"
            f"<tr><td>Publisher / Reseller</td><td>{book.publisher}</td></tr><tr><td>Format</td><td>{book.format}</td></tr></table>"
        )
        currency = "USD" if session.country == "US" else "GBP"
        script = f'<script>const currentObjectID = \'{isbn}\'; const currentCurrency = {{"symbol": "$", "currency": "{currency}"}};</script>'
        crumbs = '<nav class="breadcrumb"><a href="/books/category/303/fiction">Fiction</a><a href="#">Test fiction</a></nav>'
        delivery = ""
        if shown is not None:  # like the real page: only while the shown condition is in stock
            window = book.new_delivery if shown == "New" else book.used_delivery
            delivery = (
                '<div class="row mb-2 pb-2 border-bottom border-gray"><div class="col-md-10 font-size-14">'
                f'<div class="mb-1"> Standard worldwide delivery by <span class="font-weight-bold"> {window} </span></div>'
                '<div class="remaining-time font-weight-bold text-dark-green"><span class="font-weight-normal text-black">'
                'Order within</span> <span id="remainingMinutesStandardDelivery">0 </span></div></div></div>'
                "<script>let standardDeliveryCountDownMinutes = 1157;</script>"
            )
        return self.page(
            session,
            f"/book/{isbn}/{book.slug}",
            f"{script}{crumbs}<h1>{html.escape(book.title)}</h1>{buttons}{delivery}{buy}{details}",
            head,
        )

    # -- listings ---------------------------------------------------------------------
    def section_books(self, path: str, params: dict) -> list[Book]:
        kind = SECTION_PATHS[path]
        books = [b for b in self.books.values() if b.listed and b.offers()]
        if kind == "sub":
            books = [b for b in books if b.sub == int(path.split("/")[3])]
        if kind == "used" or params.get("condition") == "used":
            books = [b for b in books if "Used" in b.offers()]
        if params.get("condition") == "new":
            books = [b for b in books if "New" in b.offers()]
        if params.get("condition") == "rare":
            books = []
        if params.get("format"):
            books = [b for b in books if b.format == params["format"]]
        if params.get("catid"):
            books = [b for b in books if str(b.category) == params["catid"]]
        if params.get("sortBy") in ("price_low_high", "price_high_low"):
            books.sort(key=lambda b: min(p for p, _ in b.offers().values()), reverse=params["sortBy"] == "price_high_low")
        return books

    def listing_page(self, session: Session, path: str, params: dict) -> str:
        kind = SECTION_PATHS[path]
        page = int(params.pop("page", 1))
        books = self.section_books(path, params)
        if kind == "campaign":
            cards = "".join(self.card(session, b, "New" if "New" in b.offers() else "Used") for b in books[:30])
            return self.page(session, path, f"<h1>Picks</h1><ul class='row products-group'>{cards}</ul>")
        if page > self.page_cap or (page > 1 and (page - 1) * self.page_size >= len(books)):
            return self.home(session)  # past the last page: the site serves its home page
        chunk = books[(page - 1) * self.page_size : page * self.page_size]
        condition = "Used" if kind == "used" or params.get("condition") == "used" else None
        cards = "".join(self.card(session, b, condition or ("New" if "New" in b.offers() else "Used")) for b in chunk)
        facets = self.facets(path, params)
        showing = f'<div class="text-center">Showing {page} – {self.page_size}\n of {len(books):,}\n results</div>'
        return self.page(session, path, f"{facets}{showing}<ul class='row list-unstyled products-group'>{cards}</ul>")

    def facets(self, path: str, params: dict) -> str:
        kind = SECTION_PATHS[path]
        links = []

        def link(extra: dict, text: str) -> str:
            return f'<a href="{BASE}{path}?{urlencode({**params, **extra})}">{text}</a>'

        if kind in ("category", "sub") and "condition" not in params:
            links += [link({"condition": c}, c.title()) for c in ("new", "used", "rare")]
        if kind == "used" and "catid" not in params:
            links += [link({"catid": c}, f"Category {c}") for c in sorted({b.category for b in self.books.values()})]
        if "format" not in params:
            counts: dict[str, int] = {}
            for b in self.section_books(path, params):
                counts[b.format] = counts.get(b.format, 0) + 1
            links += [link({"format": f}, f"{f} ({n})") for f, n in counts.items()]
        side = ""
        if kind == "category":
            side = '<a href="/books/category/11/sub-a">Sub A</a><a href="/books/category/12/sub-b">Sub B</a>'
        return f'<aside class="u-sidebar u-sidebar--left">{side}{"".join(links)}</aside>'

    def card(self, session: Session, book: Book, condition: str) -> str:
        price = book.offers().get(condition, next(iter(book.offers().values()), (0, 0)))[0]
        suffix = "/used" if condition == "Used" else ""
        return (
            '<li class="col-6"><div class="product-item__body pb-xl-2 text-center">'
            f'<a href="{BASE}/book/{book.isbn}/{book.slug}{suffix}" class="d-block"><img src="https://img.test/{book.isbn}.jpg" alt="{book.title}"></a>'
            f'<h5 class="book_title"><a href="{BASE}/book/{book.isbn}/{book.slug}{suffix}">{book.title}</a></h5>'
            f'<h5 class="book_author"><a href="#">{book.author}</a></h5>'
            f'<span class="text-gray-6 mr-2">{book.format.title()}</span>'
            f'<div class="product-price"><span class="rrp">{self.money(session, book.rrp)}</span>'
            f'<span class="font-weight-bold">{self.money(session, price)}</span></div>'
            f'<div class="product-condation mb-2">{condition}</div></div></li>'
        )

    # -- search -------------------------------------------------------------------------
    def _search(self, session: Session, payload: dict, headers: dict) -> FakeResponse:
        if headers.get("X-CSRF-TOKEN") != session.token:
            return FakeResponse(419, '{"message": "CSRF token mismatch."}')
        with self.lock:
            self.search_calls += 1
        request = payload["requests"][0]
        params = request.get("params") or {}
        in_index = [b for b in self.books.values() if b.listed and b.offers()]  # the index holds titles in stock only
        query = request.get("query")
        if query:
            hits = [b for b in in_index if b.isbn == query]
            per_page, page, pages = 5, 0, 1
        else:
            in_index.sort(key=lambda b: b.rank)
            per_page, page = int(params.get("hitsPerPage", 20)), int(params.get("page", 0))
            pages = min(math.ceil(len(in_index) / per_page), 1000 // per_page)
            hits = in_index[page * per_page : (page + 1) * per_page] if page < pages else []
        result = {
            "nbHits": len(in_index),
            "nbPages": pages,
            "hitsPerPage": per_page,
            "page": page,
            "hits": [
                {
                    "objectID": b.isbn,
                    "isbn13": b.isbn,
                    "title": b.title,
                    "author": [b.author],
                    "format": b.format,
                    "link": f"{BASE}/book/{b.isbn}/{b.slug}",
                    "new_available": int("New" in b.offers()),
                    "used_available": int("Used" in b.offers()),
                    "newPrice": 1.0,  # the index holds UK base prices: never used for output
                    "image": f"https://img.test/{b.isbn}.jpg",
                }
                for b in hits
            ],
        }
        return FakeResponse(200, json.dumps({"results": [result]}))


class FakeTransport:
    """One browser identity = one visitor session on the fake site."""

    def __init__(self, site: FakeAwesomeBooks) -> None:
        self.site = site
        self.session = Session()

    def get(self, url, *, params, headers, timeout):
        if isinstance(params, list):
            params = dict(params)
        return self.site.respond(self.session, "GET", url, params, None, headers)

    def post(self, url, *, data, headers, timeout):
        return self.site.respond(self.session, "POST", url, None, data, headers)

    def close(self):
        pass


@pytest.fixture
def books() -> list[Book]:
    return [
        Book(ISBNS[0], "Both Conditions", new=(13.01, 1482), used=(8.84, 3), rank=3),
        Book(ISBNS[1], "New Only", new=(18.74, 990), rank=1),
        Book(ISBNS[2], "Used Only", new=None, used=(10.57, 7), rank=2),
        Book(ISBNS[3], "Sold Out", new=(9.99, 0), used=None, rank=9),
        Book(ISBNS[4], "Few Left", new=(25.48, 4), used=(11.0, 1), format="hardback", rank=4),
        Book(ISBNS[5], "Delisted", listed=False),
    ]


@pytest.fixture
def fake_site(books) -> FakeAwesomeBooks:
    return FakeAwesomeBooks(books)


def build_env(tmp_path, site: FakeAwesomeBooks):
    from awesomebooks_pro.config import Settings
    from awesomebooks_pro.jobs import JobManager
    from awesomebooks_pro.net import AdaptiveRateLimiter
    from awesomebooks_pro.storage import CatalogStore, InventoryStore
    from awesomebooks_pro.web import create_app

    settings = Settings(base_url=BASE, data_dir=tmp_path / "data", export_dir=str(tmp_path / "exports"))
    manager = JobManager(
        settings,
        CatalogStore(settings.catalog_db),
        InventoryStore(settings.inventory_db),
        transport_factory=lambda identity: FakeTransport(site),
        limiter_factory=lambda interval: AdaptiveRateLimiter(0.0, jitter=0.0, warning_cooldown=0.01, breaker_cooldown=0.02),
        backoff_scale=0.0,
    )
    app = create_app(settings, manager)
    app.testing = True
    app.test_client_class = DashboardClient
    return app, manager, site


@pytest.fixture
def app_env(tmp_path, fake_site):
    return build_env(tmp_path, fake_site)


from flask.testing import FlaskClient  # noqa: E402


class DashboardClient(FlaskClient):
    """Behaves like the dashboard's own scripts: always sends the anti-CSRF header."""

    def open(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("X-Requested-With", "awesomebooks-pro")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)
