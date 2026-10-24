"""Read-only smoke tests against the real awesomebooks.com (opt in: ``ABP_LIVE=1 pytest -m live``).

They catch a site redesign early: if AwesomeBooks changes its markup, these fail
before a client run silently degrades. A handful of polite requests in total.
"""

import os
import random
import re

import pytest

from awesomebooks_pro.net import AdaptiveRateLimiter, HttpClient, IdentityFactory
from awesomebooks_pro.site import AwesomeBooksClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("ABP_LIVE") != "1", reason="set ABP_LIVE=1 to run against the real site"),
]

BASE = "https://www.awesomebooks.com"
CATERPILLAR = "9780241003008"  # a perennial bestseller, sold both New and Used


@pytest.fixture(scope="module")
def site():
    http = HttpClient(AdaptiveRateLimiter(1.5), IdentityFactory(rng=random.Random()), timeout=40)
    client = AwesomeBooksClient(http, BASE)
    yield client
    http.close()


def test_book_page_in_us_dollars_with_both_conditions(site):
    record = site.product(CATERPILLAR)
    assert record.found and record.title and "Caterpillar" in record.title
    assert record.new is not None and record.new.price and record.new.quantity and record.new.quantity > 0
    assert record.used is None or (record.used.price and record.used.price < record.new.price * 3)
    assert site.stats.locale_switches >= 1  # switched to "Ship to US" like a shopper
    assert re.fullmatch(r"\w{3}, \w+ \d{1,2}( - \w{3}, \w+ \d{1,2})?", record.new.delivery or "")  # delivery window


def test_search_finds_an_isbn(site):
    hit = site.search_isbn(CATERPILLAR)
    assert hit is not None and hit.isbn == CATERPILLAR and "/book/" + CATERPILLAR in hit.url


def test_listing_page_has_cards_counter_and_refinements(site):
    page = site.listing_page("/books/category/303/fiction", {}, 1)
    assert page.total and page.total > 10_000 and len(page.cards) == 20
    assert "condition" in page.facets and page.subcategories


def test_bestsellers_page(site):
    hits, pages = site.bestsellers_page(0)
    assert len(hits) == 100 and pages == 10


def test_unknown_isbn_is_not_listed(site):
    assert not site.lookup_stock("9798888888889").found  # valid checksum, not sold here
