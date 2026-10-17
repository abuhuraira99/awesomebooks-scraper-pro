"""AwesomeBooks storefront client: knows *what* to ask the site, not *how* to send it.

All traffic goes through :class:`~awesomebooks_pro.net.HttpClient`, so pacing,
identity rotation and block recovery apply to every call made here.

US pricing
    AwesomeBooks prices depend on the shipping country (a US shopper pays a
    different price than a UK one, not just a converted one), and a first-time
    visitor is shown UK prices plus a "Shipping Country" pop-up. The client only
    sells in the US, so every browser identity behaves like a US shopper who
    arrives, opens the *Ship to* menu and picks United States: the site stores
    that in the visitor's session and sends them back to the same page, now in
    US dollars. No pop-up is ever rendered: there is no browser, only the
    session the site itself keeps. Every page is then re-checked: a price is
    recorded only if the page says *Ship to US* and *USD*.

New and Used
    A book page shows one condition at a time. Its condition buttons are links;
    opening one shows that condition's own price and stock. Opening a condition
    that has no stock redirects to one that has, so the condition that was
    actually served is always read back from the page, never assumed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from . import parsing
from .net import FetchError, HttpClient, NotFoundError

SEARCH_PATH = "/psearch"  # the site's own proxy to its search index (search box, Bestsellers page)
SEARCH_INDEX = "books"
BESTSELLER_PATHS = ("/books/bestsellers",)
BESTSELLER_PAGE_SIZE = 100
LISTING_MAX_PAGES = 500  # the site stops paging at 500 (page 501 redirects to the home page)
LISTING_WINDOW = LISTING_MAX_PAGES * parsing.LISTING_PAGE_SIZE  # 10,000 results per listing query
LOCALE_ATTEMPTS = 4
ALLOWED_HOSTS = ("awesomebooks.com", "www.awesomebooks.com")


class InvalidSectionUrl(ValueError):
    pass


class LocaleError(FetchError):
    """The site kept serving prices for another country or currency."""


# ---------------------------------------------------------------------------
# section links
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    url: str  # normalised section address without page number
    path: str
    params: tuple[tuple[str, str], ...]  # filters already in the link (condition, format, sortBy, q…)
    kind: str  # "listing" | "bestsellers"

    @property
    def label(self) -> str:
        """Human-readable name, e.g. ``/books/category/8601/Holiday Bargains?format=paperback``."""
        query = "&".join(f"{k}={v}" for k, v in self.params)
        return unquote(self.path) + (f"?{query}" if query else "")

    def param_dict(self) -> dict[str, str]:
        return dict(self.params)


def section_from_url(url: str, base_url: str = "https://www.awesomebooks.com") -> Section:
    """``https://www.awesomebooks.com/used-books?page=3`` -> the Used section, any page."""
    value = (url or "").strip()
    if not value:
        raise InvalidSectionUrl("Paste an AwesomeBooks section link, e.g. https://www.awesomebooks.com/used-books")
    if "://" not in value:
        value = (
            base_url.rstrip("/") + "/" + value.lstrip("/")
            if not value.lower().startswith(("www.", "awesomebooks."))
            else "https://" + value
        )
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host and host not in ALLOWED_HOSTS and urlparse(base_url).hostname != host:
        raise InvalidSectionUrl(f"'{host}' is not an AwesomeBooks address.")
    path = "/" + "/".join(p for p in parsed.path.split("/") if p)
    if path.lower().endswith("/featured"):  # "/books/category/2766/childrens-books/featured" lists the same books
        path = path[: -len("/featured")]
    if parsing.book_url_parts(path):
        raise InvalidSectionUrl("That is a single book's page. Open a section (a category, Used, Bargain…) and copy its address.")
    if path in ("/", ""):
        raise InvalidSectionUrl(
            "That is the home page. Open a section (a category, Used, Bargain, New & Trending…) and copy its address."
        )
    params = tuple((k, v) for k, v in parse_qsl(parsed.query) if k != "page" and v != "")
    kind = "bestsellers" if path.lower() in BESTSELLER_PATHS else "listing"
    return Section(url=base_url.rstrip("/") + path, path=path, params=params, kind=kind)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class Offer:
    """One condition's live offer as the product page shows it to a US shopper."""

    price: float | None
    quantity: int | None  # exact stock; None only if the site published no number
    quantity_text: str | None
    url: str

    @property
    def available(self) -> bool:
        return self.price is not None and (self.quantity is None or self.quantity > 0)


@dataclass
class ProductRecord:
    isbn: str
    found: bool
    url: str | None = None
    title: str | None = None
    author: str | None = None
    format: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    page_count: int | None = None
    dimensions: str | None = None
    weight: str | None = None
    category: str | None = None
    image_url: str | None = None
    rrp: float | None = None
    new: Offer | None = None
    used: Offer | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return any(o is not None and o.available for o in (self.new, self.used))

    @property
    def quantity(self) -> int | None:
        """Units across New and Used. ``None`` if an available offer has no published number."""
        offers = [o for o in (self.new, self.used) if o is not None and o.available]
        if any(o.quantity is None for o in offers):
            return None
        return sum(o.quantity or 0 for o in offers)

    def fields(self) -> dict[str, Any]:
        """Flat columns for storage."""

        def offer(o: Offer | None, prefix: str) -> dict[str, Any]:
            return {
                f"{prefix}_available": 1 if o is not None and o.available else 0,
                f"{prefix}_price": o.price if o is not None and o.available else None,
                f"{prefix}_qty": (o.quantity if o.available else 0) if o is not None else 0,
            }

        return {
            "found": 1 if self.found else 0,
            "title": self.title,
            "author": self.author,
            "format": self.format,
            "publisher": self.publisher,
            "publication_date": self.publication_date,
            "page_count": self.page_count,
            "dimensions": self.dimensions,
            "weight": self.weight,
            "category": self.category,
            "image_url": self.image_url,
            "product_url": self.url,
            "rrp": self.rrp,
            **offer(self.new, "new"),
            **offer(self.used, "used"),
            "note": "; ".join(dict.fromkeys(self.notes)) or None,
        }


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class SessionStats:
    """Thread-safe counters shared by every session of a job (shown in the Traffic Guard panel)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.locale_switches = 0

    def bump_locale(self) -> None:
        with self._lock:
            self.locale_switches += 1


class AwesomeBooksClient:
    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        *,
        ship_to: str = "US",
        currency: str = "USD",
        stats: SessionStats | None = None,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ship_to = ship_to
        self.currency = currency
        self.stats = stats or SessionStats()
        self._locale_serial = -1  # identity serial the US locale was set up for
        self._csrf: str | None = None

    # -- locale -------------------------------------------------------------
    def _locale_ready(self) -> bool:
        _ = self.http.identity  # rotates an exhausted identity now, before we compare
        return self._locale_serial == self.http.identity_serial

    def _is_target_locale(self, page: str) -> bool:
        return parsing.page_locale(page) == (self.ship_to, self.currency)

    def page(self, url: str, *, referer: str | None = None) -> str:
        """A storefront page whose prices are guaranteed to be for ``ship_to`` / ``currency``."""
        referer = referer or f"{self.base_url}/"
        last = (None, None)
        for _ in range(LOCALE_ATTEMPTS):
            if self._locale_ready():
                page = self.http.get_text(url, referer=referer)
            else:
                # A new visitor: lands on the page, then picks the shipping country from the header.
                page = self.http.get_text(url, referer=referer)
                if not self._is_target_locale(page):
                    page = self._switch_locale(url)
                self._locale_serial = self.http.identity_serial
            if self._is_target_locale(page):
                self._csrf = parsing.csrf_token(page) or self._csrf
                return page
            last = parsing.page_locale(page)
            if last == (None, None):
                # Neither marker: not a normal storefront page (maintenance or an interstitial).
                raise FetchError(f"unexpected page for {url} (no storefront header)")
            self._locale_serial = -1  # session lost its country (expired or rotated): set it again
        raise LocaleError(
            f"AwesomeBooks kept showing {last[0] or '?'}/{last[1] or '?'} prices instead of {self.ship_to}/{self.currency}"
        )

    def _switch_locale(self, back_to: str) -> str:
        self.stats.bump_locale()
        page = self.http.get_text(f"{self.base_url}/changeLocation/{self.ship_to}", referer=back_to)
        country, currency = parsing.page_locale(page)
        if country == self.ship_to and currency not in (None, self.currency):
            page = self.http.get_text(f"{self.base_url}/changecurrency", {"currency": self.currency}, referer=back_to)
        return page

    # -- search ---------------------------------------------------------------
    def _search(self, params: dict[str, Any], query: str | None = None, *, referer: str | None = None) -> dict[str, Any]:
        if not self._locale_ready() or not self._csrf:
            self.page(f"{self.base_url}/", referer=f"{self.base_url}/")  # a visitor's first page (sets up the session)
        request: dict[str, Any] = {"indexName": SEARCH_INDEX, "params": params}
        if query is not None:
            request["query"] = query
        headers = {"X-CSRF-TOKEN": self._csrf or "", "Origin": self.base_url}
        try:
            return self.http.post_json(
                f"{self.base_url}{SEARCH_PATH}", {"requests": [request]}, referer=referer or f"{self.base_url}/", headers=headers
            )
        except FetchError as exc:
            if "HTTP 419" in str(exc):  # Laravel's "page expired": the session token went stale
                self._csrf = None
                self._locale_serial = -1
            raise

    def search_isbn(self, isbn: str) -> parsing.SearchHit | None:
        """What the site's search box finds for this ISBN (the index only holds titles in stock)."""
        data = self._search({"hitsPerPage": 5}, isbn)
        hits, _ = parsing.parse_search_hits(data, self.base_url)
        return next((h for h in hits if h.isbn == isbn), None)

    def bestsellers_page(self, page: int) -> tuple[list[parsing.SearchHit], int]:
        """One page (0-based) of the Bestsellers list, exactly as the site's Bestsellers page requests it."""
        data = self._search(
            {"hitsPerPage": BESTSELLER_PAGE_SIZE, "page": page, "facets": ["format", "new_available", "used_available"]},
            referer=f"{self.base_url}/books/bestsellers",
        )
        return parsing.parse_search_hits(data, self.base_url)

    # -- listings ---------------------------------------------------------------
    def listing_page(self, path: str, params: dict[str, str], page: int) -> parsing.ListingPage:
        """Page ``page`` of the listing at ``path`` with ``params`` (filters, sort order) applied."""
        section_url = self.base_url + path
        url = parsing.listing_url(section_url, params, page)
        referer = parsing.listing_url(section_url, params, page - 1) if page > 1 else f"{self.base_url}/"
        html = self.page(url, referer=referer)
        listing = parsing.parse_listing(html, url, self.base_url)
        if listing.is_home_page or (page > 1 and listing.total is None):
            # The site answers a page that doesn't exist with its home page (and its carousels of books).
            if page == 1:
                raise NotFoundError(f"{path} is not a section on AwesomeBooks (the site showed its home page)")
            return parsing.ListingPage(total=None, shown_page=None, cards=[], facets={})
        if listing.shown_page is not None and listing.shown_page != page:
            raise FetchError(f"{url} served page {listing.shown_page:,} instead of page {page}")
        return listing

    # -- products -----------------------------------------------------------------
    def product(self, isbn: str, url: str | None = None) -> ProductRecord:
        """Everything about one book for a US shopper: New and Used price and exact stock.

        Raises on network failure; never guesses. "Not listed" means the site sent the
        visitor to its home page instead of a book page.
        """
        start = url or parsing.product_base_url(self.base_url, isbn)
        try:
            html = self.page(start, referer=f"{self.base_url}/search?q={isbn}")
        except NotFoundError:
            return ProductRecord(isbn=isbn, found=False, notes=["Not listed on AwesomeBooks"])
        if not parsing.is_product_page(html):
            return ProductRecord(isbn=isbn, found=False, notes=["Not listed on AwesomeBooks"])
        first = parsing.parse_product_page(html, self.base_url)
        if first.isbn13 and first.isbn13 != isbn:
            raise FetchError(f"asked for {isbn} but the site showed {first.isbn13}")

        record = ProductRecord(
            isbn=isbn,
            found=True,
            url=first.url or start,
            title=first.title,
            author=first.author,
            format=first.format,
            publisher=first.publisher,
            publication_date=first.publication_date,
            page_count=first.page_count,
            dimensions=first.dimensions,
            weight=first.weight,
            category=first.category,
            image_url=first.image_url,
            rrp=first.rrp,
        )
        offers: dict[str, Offer] = {}
        visited = {first.condition} if first.condition else set()
        if first.condition:
            offers[first.condition] = Offer(first.price, first.quantity, first.quantity_text, start)
        if first.out_of_stock:
            record.notes.append("Out of stock on AwesomeBooks")

        for option in first.options:
            if option.label not in ("New", "Used") or option.label in visited:
                continue
            visited.add(option.label)
            other = parsing.parse_product_page(self.page(option.url, referer=start), self.base_url)
            if other.condition == option.label:
                offers[option.label] = Offer(other.price, other.quantity, other.quantity_text, option.url)
                record.rrp = record.rrp or other.rrp
            # Otherwise the site redirected away: that condition has no stock right now.

        rare_only = any(o.label == "Rare" for o in first.options) or first.condition == "Rare"
        record.new = offers.get("New")
        record.used = offers.get("Used")
        if rare_only and not record.in_stock:
            record.notes.append("Only offered as a rare / collectable copy")
        for label, offer in (("New", record.new), ("Used", record.used)):
            if offer is not None and offer.available and offer.quantity is None:
                record.notes.append(f"{label}: in stock, exact quantity not published")
        return record

    def lookup_stock(self, isbn: str) -> ProductRecord:
        """Definitive stock answer for one ISBN, as a shopper would get it.

        Like a shopper, it first types the ISBN into the search box; the result links
        to the book. The search index only lists titles in stock, so when it finds
        nothing the book page is opened directly to tell "sold out" from "not listed".
        """
        hit = self.search_isbn(isbn)
        record = self.product(isbn, hit.url if hit else None)
        if not hit and record.found and record.in_stock:
            record.notes.append("In stock although the site search did not list it")
        return record
