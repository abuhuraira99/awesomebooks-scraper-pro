"""Pure parsing functions: AwesomeBooks HTML / JSON -> plain Python objects.

Nothing here touches the network, so every rule is unit-tested against real
pages saved in ``tests/fixtures``.

What a product page carries (all server-rendered, no JavaScript needed):

* ``const currentObjectID = '978…'``: the ISBN-13 of the book;
* ``window.shippingCountry`` and ``currentCurrency``: the locale the prices are for;
* the buy box: ``#currentCondition`` (NEW / USED), ``#price``, the RRP and
  "Available ``#availability`` in stock" (capped at "20+" for shoppers);
* the GA4 data layer ``"product_availability": N``: the *exact* stock count of
  the condition being shown (equal to the visible number up to 20);
* the condition buttons (``.condition-options-item``): one link per condition
  the book is sold in. Opening a condition that has no stock redirects to one
  that has, so the condition that was actually served is always re-checked;
* the "More Details" table and a schema.org ``Book`` JSON-LD block.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .isbn import extract_isbn, isbn13_is_valid

LISTING_PAGE_SIZE = 20
CONDITIONS = ("New", "Used", "Rare")
FACET_KEYS = ("condition", "format", "catid")

_OBJECT_ID_RE = re.compile(r"currentObjectID\s*=\s*['\"](\d{13})['\"]")
_COUNTRY_RE = re.compile(r"window\.shippingCountry\s*=\s*['\"]([A-Z]{2})['\"]")
# The header's "CURRENCY $ USD" switcher is on every page; product pages also declare currentCurrency.
_HEADER_CURRENCY_RE = re.compile(
    r'id="currencyDropdown".{0,400}?<span>\s*CURRENCY\s*</span>\s*<span>[^<]*?\b([A-Z]{3})\s*</span>', re.S
)
_CURRENCY_RE = re.compile(r"currentCurrency\s*=\s*\{[^}]*?[\"']currency[\"']\s*:\s*[\"']([A-Z]{3})[\"']", re.S)
_AVAILABILITY_RE = re.compile(r"[\"']product_availability[\"']\s*:\s*(\d+)")
_CSRF_RE = re.compile(r'<meta\s+(?=[^>]*\bname="csrf-token")[^>]*\bcontent="([^"]+)"')
# "Showing 50 – 20 of 931,426 results" on page 50: the site prints the page number and the page size.
_SHOWING_RE = re.compile(r"Showing\s+([\d,]+)\s*\S\s*([\d,]+)\s+of\s+([\d,]+)\s+results", re.S)
_BOOK_PATH_RE = re.compile(r"^/book/(\d{13})/([^/?#]+)(?:/(new|used|rare))?/?$", re.I)
_MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_COUNT_RE = re.compile(r"\(([\d,]+)\)\s*$")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    value = " ".join(_html.unescape(str(text)).split())
    return value or None


def money(text: Any) -> float | None:
    """``"$1,234.50"`` -> ``1234.5``. ``None`` for anything without a positive amount."""
    if text is None:
        return None
    match = _MONEY_RE.search(str(text).replace("\xa0", " "))
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(amount, 2) if amount > 0 else None


def format_name(text: str | None) -> str | None:
    """``"paperback"`` / ``"PAPERBACK"`` -> ``"Paperback"`` (book pages and listing cards spell it differently)."""
    value = clean(text)
    return value[:1].upper() + value[1:].lower() if value else None


def condition_label(text: str | None) -> str | None:
    """``"USED"`` / ``"used"`` / ``"Used"`` -> ``"Used"``; anything unknown -> ``None``."""
    value = (clean(text) or "").lower()
    for label in CONDITIONS:
        if value == label.lower() or value.startswith(label.lower() + " "):
            return label
    return None


def page_locale(page: str) -> tuple[str | None, str | None]:
    """(shipping country, currency) the page's prices were rendered for."""
    country = _COUNTRY_RE.search(page)
    currency = _HEADER_CURRENCY_RE.search(page) or _CURRENCY_RE.search(page)
    return (country.group(1) if country else None, currency.group(1) if currency else None)


def csrf_token(page: str) -> str | None:
    match = _CSRF_RE.search(page)
    return match.group(1) if match else None


def is_product_page(page: str) -> bool:
    return bool(_OBJECT_ID_RE.search(page))


def book_url_parts(url: str) -> tuple[str, str, str | None] | None:
    """``/book/978…/slug/used`` -> ``(isbn, slug, "Used")``; ``None`` if it isn't a book link."""
    match = _BOOK_PATH_RE.match(urlparse(url).path)
    if not match or not isbn13_is_valid(match.group(1)):
        return None
    return match.group(1), match.group(2), condition_label(match.group(3)) if match.group(3) else None


def product_base_url(base_url: str, isbn: str, slug: str = "book") -> str:
    """The product's default page (it shows the New offer whenever one exists)."""
    return f"{base_url.rstrip('/')}/book/{isbn}/{slug}"


# ---------------------------------------------------------------------------
# product page
# ---------------------------------------------------------------------------


@dataclass
class ConditionOption:
    label: str  # "New" | "Used" | "Rare"
    url: str
    selected: bool


@dataclass
class ProductPage:
    isbn13: str | None
    country: str | None
    currency: str | None
    condition: str | None  # the condition this page's price and stock belong to
    price: float | None
    rrp: float | None
    quantity: int | None  # exact stock of ``condition`` (site data layer)
    quantity_text: str | None  # what shoppers see, e.g. "20+"
    out_of_stock: bool
    options: list[ConditionOption] = field(default_factory=list)
    title: str | None = None
    author: str | None = None
    format: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    page_count: int | None = None
    dimensions: str | None = None
    weight: str | None = None
    language: str | None = None
    category: str | None = None
    image_url: str | None = None
    url: str | None = None  # canonical product URL
    sku: str | None = None
    delivery: str | None = None  # "Thu, November 5 - Tue, November 10": the window quoted for ``condition``

    @property
    def in_stock(self) -> bool:
        """The served condition can be bought right now."""
        if self.out_of_stock or self.condition is None or self.price is None:
            return False
        return self.quantity is None or self.quantity > 0


def _json_ld_book(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") in ("Book", "Product"):
                return item
    return {}


def _names(value: Any) -> str | None:
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        names = [clean(v.get("name") if isinstance(v, dict) else v) for v in value]
        return ", ".join(dict.fromkeys(n for n in names if n)) or None
    return clean(value) if value else None


def _spec_table(soup: BeautifulSoup) -> dict[str, str]:
    """The "More Details" table: ``{"isbn13": "978…", "number of pages": "24", …}``."""
    specs: dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not any("isbn" in (r.get_text() or "").lower() for r in rows):
            continue
        for row in rows:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = (clean(cells[0].get_text(" ")) or "").rstrip(":").lower()
                value = clean(cells[1].get_text(" "))
                if key and value:
                    specs.setdefault(key, value)
    return specs


def _options(soup: BeautifulSoup, base_url: str) -> list[ConditionOption]:
    options: list[ConditionOption] = []
    for item in soup.select(".condition-options-item"):
        link = item.find("a", href=True)
        label_el = item.select_one("label .font-weight-bold") or item.find("label")
        label = condition_label(label_el.get_text(" ") if label_el else None)
        if not link or not label:
            continue
        radio = item.find("input")
        options.append(
            ConditionOption(label=label, url=urljoin(base_url, link["href"]), selected=bool(radio and radio.has_attr("checked")))
        )
    return options


_DELIVERY_LABEL_RE = re.compile(r"\bdelivery by\b", re.I)


def _delivery(soup: BeautifulSoup) -> str | None:
    """The delivery window under the title: "Standard worldwide delivery by <b>Thu, November 5 - Tue, November 10</b>".

    It is quoted for the condition being shown, and only while that condition is in stock. The
    "Order within …" countdown below it is the daily dispatch cut-off, so it is not kept.
    """
    for label in soup.find_all(string=_DELIVERY_LABEL_RE):
        window = label.parent.find(class_="font-weight-bold") if label.parent is not None else None
        text = clean(window.get_text(" ")) if window is not None else None
        if text and any(ch.isdigit() for ch in text):
            return text
    return None


def _breadcrumb(soup: BeautifulSoup) -> str | None:
    crumbs = [clean(a.get_text(" ")) for a in soup.select(".breadcrumb a, nav[aria-label=breadcrumb] a")]
    crumbs = [c for c in crumbs if c and c.lower() not in ("home", "awesome books")]
    return " > ".join(dict.fromkeys(crumbs)) or None


def parse_product_page(page: str, base_url: str = "https://www.awesomebooks.com") -> ProductPage:
    soup = BeautifulSoup(page, "html.parser")
    country, currency = page_locale(page)
    ld = _json_ld_book(soup)
    specs = _spec_table(soup)

    object_id = _OBJECT_ID_RE.search(page)
    isbn13 = (
        (object_id.group(1) if object_id and isbn13_is_valid(object_id.group(1)) else None)
        or extract_isbn(specs.get("isbn13"))
        or extract_isbn(ld.get("isbn"))
    )

    cond_el = soup.find(id="currentCondition")
    condition = condition_label(cond_el.get_text(" ") if cond_el else None)
    price_el = soup.find(id="price")
    price = money(price_el.get("data-price") or price_el.get_text(" ")) if price_el else None

    buy_box = soup.select_one(".product-cart .quantity-box") or soup.select_one(".product-cart")
    buy_text = (clean(buy_box.get_text(" ")) or "").lower() if buy_box else ""
    rrp_el = buy_box.select_one(".text-red del, del") if buy_box else None
    rrp = money(rrp_el.get_text(" ")) if rrp_el else None
    if rrp is None:
        list_price = re.search(r"list price\s*:?\s*([^\s]+)", buy_text)
        rrp = money(list_price.group(1)) if list_price else None

    qty_el = soup.find(id="availability")
    quantity_text = clean(qty_el.get_text(" ")) if qty_el else None
    exact = _AVAILABILITY_RE.search(page)
    quantity: int | None = int(exact.group(1)) if exact else None
    if quantity is None and quantity_text and quantity_text.isdigit():
        quantity = int(quantity_text)
    out_of_stock = "out of stock" in buy_text and price is None
    if out_of_stock:
        condition, quantity = None, 0

    sku_el = soup.find("input", id="sku")
    pages = specs.get("number of pages") or str(ld.get("numberOfPages") or "")
    canonical = soup.find("link", rel="canonical")
    image = ld.get("image")
    return ProductPage(
        isbn13=isbn13,
        country=country,
        currency=currency,
        condition=condition,
        price=price,
        rrp=rrp,
        quantity=quantity,
        quantity_text=quantity_text,
        out_of_stock=out_of_stock,
        options=_options(soup, base_url),
        title=clean(ld.get("name")) or clean((soup.find("h1") or soup.new_tag("x")).get_text(" ")),
        author=_names(ld.get("author")),
        format=format_name(specs.get("format") or str(ld.get("bookFormat") or "").rsplit("/", 1)[-1]),
        publisher=clean(specs.get("publisher / reseller") or specs.get("publisher")) or _names(ld.get("publisher")),
        publication_date=clean(ld.get("datePublished")) or clean(specs.get("publication date")),
        page_count=int(pages) if pages.isdigit() and int(pages) > 0 else None,
        dimensions=clean(specs.get("product dimensions")),
        weight=clean(specs.get("item weight")),
        language=clean(ld.get("inLanguage")) or clean(specs.get("language")),
        category=_breadcrumb(soup),
        image_url=image[0] if isinstance(image, list) and image else (image or None),
        url=canonical.get("href") if canonical is not None and canonical.get("href") else clean(ld.get("url")),
        sku=sku_el.get("value") if sku_el is not None else None,
        delivery=None if out_of_stock else _delivery(soup),
    )


# ---------------------------------------------------------------------------
# listing pages (categories, Used, Bargain, New & Trending, search, authors…)
# ---------------------------------------------------------------------------


@dataclass
class ListingCard:
    isbn: str
    url: str  # the product's default page (condition suffix removed)
    condition: str | None  # the condition this card is offering
    title: str | None
    author: str | None
    format: str | None
    price: float | None
    rrp: float | None
    image_url: str | None


@dataclass
class ListingPage:
    total: int | None  # "Showing 1 – 20 of N results"; None on pages without a counter
    shown_page: int | None  # the page number the site says it served
    cards: list[ListingCard]
    facets: dict[str, list[tuple[str, int | None]]]  # refinements offered for this listing
    subcategories: list[str] = field(default_factory=list)  # child category paths in the left sidebar
    canonical_path: str | None = None  # "/" when the site answered with its home page

    @property
    def is_home_page(self) -> bool:
        return self.canonical_path in ("", "/")


def _card(body: Any, base_url: str) -> ListingCard | None:
    link = None
    for a in body.find_all("a", href=True):
        if book_url_parts(urljoin(base_url, a["href"])):
            link = urljoin(base_url, a["href"])
            break
    if not link:
        return None
    isbn, slug, url_condition = book_url_parts(link)  # type: ignore[misc]
    title_el = body.select_one(".book_title a") or body.select_one(".book_title")
    authors = [clean(a.get_text(" ")) for a in body.select(".book_author a")] or [
        clean(el.get_text(" ")) for el in body.select(".book_author")
    ]
    fmt_el = body.select_one(".text-gray-6")
    price_el = body.select_one(".product-price .font-weight-bold, .product-price .font-weight-bolder")
    rrp_el = body.select_one(".product-price .rrp")
    cond_el = body.select_one(".product-condation, .product-condition")
    img = body.find("img")
    return ListingCard(
        isbn=isbn,
        url=product_base_url(base_url, isbn, slug),
        condition=url_condition or condition_label(cond_el.get_text(" ") if cond_el else None) or "New",
        title=clean(title_el.get_text(" ")) if title_el else clean(img.get("alt")) if img else None,
        author=", ".join(dict.fromkeys(a for a in authors if a)) or None,
        format=format_name(fmt_el.get_text(" ")) if fmt_el else None,
        price=money(price_el.get_text(" ")) if price_el else None,
        rrp=money(rrp_el.get_text(" ")) if rrp_el else None,
        image_url=img.get("src") or img.get("data-src") if img else None,
    )


def _query(url: str) -> dict[str, str]:
    return {k: v for k, v in parse_qsl(urlparse(url).query, keep_blank_values=False) if k != "page"}


def _facets(soup: BeautifulSoup, page_url: str, base_url: str) -> dict[str, list[tuple[str, int | None]]]:
    """Refinement links on this listing: same path, current filters plus one new condition/format/category."""
    here = urlparse(page_url)
    current = _query(page_url)
    found: dict[str, dict[str, int | None]] = {key: {} for key in FACET_KEYS}
    for a in soup.find_all("a", href=True):
        target = urlparse(urljoin(base_url, a["href"]))
        if target.path.rstrip("/").lower() != here.path.rstrip("/").lower():
            continue
        params = _query(target.geturl())
        added = [k for k in params if k not in current]
        if len(added) != 1 or added[0] not in FACET_KEYS:
            continue
        if any(params.get(k) != v for k, v in current.items()):
            continue
        count = _COUNT_RE.search(clean(a.get_text(" ")) or "")
        found[added[0]].setdefault(params[added[0]], int(count.group(1).replace(",", "")) if count else None)
    return {key: list(values.items()) for key, values in found.items() if values}


_CATEGORY_PATH_RE = re.compile(r"^/books/category/(\d+)/[^/]+$", re.I)


def _subcategories(soup: BeautifulSoup, page_url: str, base_url: str) -> list[str]:
    """Child categories a category page lists in its sidebar (they partition the category)."""
    here = _CATEGORY_PATH_RE.match(urlparse(page_url).path.rstrip("/"))
    if not here:
        return []
    side = soup.select_one("aside.u-sidebar") or soup.find("aside")
    if side is None:
        return []
    paths: list[str] = []
    for a in side.find_all("a", href=True):
        target = urlparse(urljoin(base_url, a["href"]))
        match = _CATEGORY_PATH_RE.match(target.path.rstrip("/"))
        if match and match.group(1) != here.group(1) and not target.query and target.path not in paths:
            paths.append(target.path)
    return paths


def _canonical_path(soup: BeautifulSoup) -> str | None:
    link = soup.find("link", rel="canonical")
    if link is None or not link.get("href"):
        return None
    return urlparse(link["href"]).path.rstrip("/") or "/"


def parse_listing(page: str, page_url: str, base_url: str = "https://www.awesomebooks.com") -> ListingPage:
    soup = BeautifulSoup(page, "html.parser")
    cards: list[ListingCard] = []
    seen: set[tuple[str, str | None]] = set()
    for body in soup.select(".product-item__body"):
        card = _card(body, base_url)
        if card and (card.isbn, card.condition) not in seen:
            seen.add((card.isbn, card.condition))
            cards.append(card)
    showing = _SHOWING_RE.search(" ".join(soup.get_text(" ").split()))
    return ListingPage(
        total=int(showing.group(3).replace(",", "")) if showing else None,
        shown_page=int(showing.group(1).replace(",", "")) if showing else None,
        cards=cards,
        facets=_facets(soup, page_url, base_url),
        subcategories=_subcategories(soup, page_url, base_url),
        canonical_path=_canonical_path(soup),
    )


def listing_url(section_url: str, params: dict[str, str], page: int) -> str:
    """The section's page ``page`` with ``params`` applied (page 1 carries no page number, like the site)."""
    parsed = urlparse(section_url)
    query = {**params}
    if page > 1:
        query["page"] = str(page)
    return parsed._replace(query=urlencode(query), fragment="").geturl()


# ---------------------------------------------------------------------------
# site search (the Algolia proxy the search box and Bestsellers page use)
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    isbn: str
    url: str
    title: str | None
    author: str | None
    format: str | None
    new_available: bool
    used_available: bool
    image_url: str | None


def parse_search_hits(data: dict[str, Any], base_url: str = "https://www.awesomebooks.com") -> tuple[list[SearchHit], int]:
    """``/psearch`` answer -> (hits, number of pages the index will serve)."""
    results = (data or {}).get("results") or []
    if not results:
        return [], 0
    result = results[0]
    hits: list[SearchHit] = []
    for hit in result.get("hits") or []:
        isbn = extract_isbn(hit.get("isbn13") or hit.get("objectID"))
        if not isbn:
            continue
        parts = book_url_parts(urljoin(base_url, hit.get("link") or ""))
        url = product_base_url(base_url, isbn, parts[1]) if parts else product_base_url(base_url, isbn)
        authors = hit.get("author")
        hits.append(
            SearchHit(
                isbn=isbn,
                url=url,
                title=clean(hit.get("title")),
                author=_names(authors) if authors else None,
                format=format_name(hit.get("format")),
                new_available=bool(hit.get("new_available")),
                used_available=bool(hit.get("used_available")),
                image_url=hit.get("image"),
            )
        )
    return hits, int(result.get("nbPages") or 0)
