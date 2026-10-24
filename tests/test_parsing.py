"""Parsers against real AwesomeBooks pages (trimmed copies saved in tests/fixtures)."""

import json

import pytest

from awesomebooks_pro import parsing
from tests.conftest import load_fixture

BASE = "https://www.awesomebooks.com"


def product(name):
    return parsing.parse_product_page(load_fixture(name), BASE)


def test_new_page_of_a_book_that_also_has_used_copies():
    p = product("product_new_with_used.html")
    assert (p.isbn13, p.country, p.currency) == ("9780241003008", "US", "USD")
    assert (p.condition, p.price, p.quantity, p.quantity_text) == ("New", 13.01, 1483, "20+")  # exact count, not "20+"
    assert p.in_stock and not p.out_of_stock
    assert [(o.label, o.selected) for o in p.options] == [("New", True), ("Used", False)]
    assert p.options[1].url == f"{BASE}/book/9780241003008/the-very-hungry-caterpillar/used"
    assert p.title == "The Very Hungry Caterpillar" and p.author == "Eric Carle"
    assert (p.format, p.page_count, p.publication_date) == ("Board book", 24, "1994-09-29")
    assert p.publisher == "Penguin Random House Children's UK" and p.dimensions == "135 x 190 x 20 mm"
    assert p.category.startswith("Children's > ")


def test_used_page_has_its_own_price_and_stock():
    p = product("product_used.html")
    assert (p.condition, p.price, p.rrp, p.quantity, p.quantity_text) == ("Used", 8.84, 10.63, 3, "3")
    assert [(o.label, o.selected) for o in p.options] == [("New", False), ("Used", True)]


def test_single_condition_pages_have_no_buttons():
    new_only = product("product_new_only.html")
    used_only = product("product_used_only.html")
    assert (new_only.condition, new_only.price, new_only.quantity, new_only.options) == ("New", 18.74, 990, [])
    assert (used_only.condition, used_only.price, used_only.quantity, used_only.options) == ("Used", 10.57, 7, [])


def test_sold_out_page():
    p = product("product_out_of_stock.html")
    assert p.isbn13 == "9780330258647" and p.title == "The Hitchhiker's Guide To The Galaxy"
    assert p.out_of_stock and not p.in_stock
    assert (p.condition, p.price, p.quantity, p.rrp) == (None, None, 0, 7.98)


def test_uk_page_is_recognised_as_uk():
    p = product("product_gb.html")
    assert (p.country, p.currency, p.price) == ("GB", "GBP", 16.34)


def test_locale_markers_on_every_page_type():
    assert parsing.page_locale(load_fixture("listing_used.html")) == ("US", "USD")
    assert parsing.page_locale(load_fixture("home.html")) == ("GB", "GBP")
    assert parsing.csrf_token(load_fixture("listing_used.html"))
    assert parsing.page_locale("<html>nothing</html>") == (None, None)


def test_home_page_is_not_a_product_page():
    assert not parsing.is_product_page(load_fixture("home.html"))
    assert parsing.is_product_page(load_fixture("product_used.html"))


def test_category_listing():
    page = parsing.parse_listing(load_fixture("listing_fiction.html"), f"{BASE}/books/category/303/fiction", BASE)
    assert (page.total, page.shown_page, len(page.cards)) == (699_808, 1, 20)
    assert {v for v, _ in page.facets["condition"]} == {"new", "used", "rare"}
    assert ("paperback", 610_969) in page.facets["format"]
    assert len(page.subcategories) == 16 and "/books/category/329/adventure-fiction" in page.subcategories
    card = next(c for c in page.cards if c.isbn == "9780571334650")
    assert (card.condition, card.title, card.author, card.price, card.rrp) == (
        "Used",
        "Normal People",
        "Sally Rooney",
        10.6,
        13.31,
    )
    assert card.url == f"{BASE}/book/9780571334650/normal-people"  # condition suffix removed
    assert not page.is_home_page


def test_used_listing_offers_category_filter():
    page = parsing.parse_listing(load_fixture("listing_used.html"), f"{BASE}/used-books", BASE)
    assert page.total == 931_426 and all(c.condition == "Used" for c in page.cards)
    assert len(page.facets["catid"]) == 33 and "condition" not in page.facets
    assert page.subcategories == []


def test_campaign_page_has_cards_but_no_counter():
    page = parsing.parse_listing(load_fixture("listing_campaign.html"), f"{BASE}/campaign/booker-prize-books", BASE)
    assert page.total is None and len(page.cards) == 12
    assert all(c.price for c in page.cards)


def test_home_page_answer_is_detected():
    page = parsing.parse_listing(load_fixture("home.html"), f"{BASE}/used-books?page=501", BASE)
    assert page.is_home_page and page.total is None


@pytest.mark.parametrize(
    "url, expected",
    [
        ("/book/9780241003008/the-very-hungry-caterpillar", ("9780241003008", "the-very-hungry-caterpillar", None)),
        ("https://www.awesomebooks.com/book/9780241003008/x/used", ("9780241003008", "x", "Used")),
        ("/book/9780241003008", None),
        ("/book/9780241003009/x", None),  # bad checksum
        ("/books/category/303/fiction", None),
    ],
)
def test_book_url_parts(url, expected):
    assert parsing.book_url_parts(url) == expected


def test_search_hits():
    hits, pages = parsing.parse_search_hits(json.loads(load_fixture("search_isbn.json")), BASE)
    assert pages == 1 and len(hits) == 1
    hit = hits[0]
    assert (hit.isbn, hit.new_available, hit.used_available) == ("9780241003008", True, True)
    assert hit.url == f"{BASE}/book/9780241003008/the-very-hungry-caterpillar"
    best, pages = parsing.parse_search_hits(json.loads(load_fixture("search_bestsellers.json")), BASE)
    assert pages == 50 and len(best) == 6


def test_money_and_condition_helpers():
    assert parsing.money("$1,234.50") == 1234.5
    assert parsing.money("£0.00") is None and parsing.money(None) is None
    assert parsing.condition_label(" USED ") == "Used" and parsing.condition_label("nope") is None


def test_listing_url_keeps_filters_and_omits_page_one():
    assert parsing.listing_url(f"{BASE}/used-books", {"format": "board book"}, 1) == f"{BASE}/used-books?format=board+book"
    assert parsing.listing_url(f"{BASE}/used-books", {}, 3) == f"{BASE}/used-books?page=3"


def test_delivery_window_is_read_per_condition():
    # Real pages (captured 2026-10-17): Used ships sooner than New; a sold-out page quotes no window.
    assert product("product_new_with_used.html").delivery == "Thu, October 29 - Tue, November 3"
    assert product("product_used.html").delivery == "Tue, October 27 - Fri, October 30"
    assert product("product_new_only.html").delivery == "Thu, October 29 - Tue, November 3"
    assert product("product_used_only.html").delivery == "Tue, October 27 - Fri, October 30"
    assert product("product_out_of_stock.html").delivery is None


def test_delivery_countdown_is_not_mistaken_for_the_window():
    page = (
        '<div class="mb-1"> Standard worldwide delivery by <span class="font-weight-bold"> Mon, November 9 - Thu, November 12 </span></div>'
        '<div class="remaining-time"><span>Order within</span> <span id="remainingMinutesStandardDelivery">20 hrs</span></div>'
    )
    assert parsing.parse_product_page(page, BASE).delivery == "Mon, November 9 - Thu, November 12"
    assert parsing.parse_product_page("<div>Delivery</div><span class='font-weight-bold'>Fast</span>", BASE).delivery is None
