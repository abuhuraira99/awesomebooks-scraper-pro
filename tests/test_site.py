"""Site client against the fake storefront: US locale, New/Used switching, search, listings."""

import random

import pytest

from awesomebooks_pro.net import AdaptiveRateLimiter, FetchError, HttpClient, IdentityFactory, NotFoundError
from awesomebooks_pro.site import AwesomeBooksClient, InvalidSectionUrl, LocaleError, section_from_url
from tests.conftest import BASE, ISBNS, FakeTransport


def client(site, budget=(1000, 1000)):
    http = HttpClient(
        AdaptiveRateLimiter(0.0, jitter=0.0, warning_cooldown=0.0, breaker_cooldown=0.0),
        IdentityFactory(rng=random.Random(1), budget_range=budget),
        transport_factory=lambda identity: FakeTransport(site),
        backoff_scale=0.0,
    )
    return AwesomeBooksClient(http, BASE)


# -- section links ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, label, kind",
    [
        ("https://www.awesomebooks.com/used-books", "/used-books", "listing"),
        ("https://www.awesomebooks.com/used-books?page=7", "/used-books", "listing"),
        ("awesomebooks.com/books/bargain?format=paperback&page=2", "/books/bargain?format=paperback", "listing"),
        ("/books/category/303/fiction?condition=used", "/books/category/303/fiction?condition=used", "listing"),
        (
            "https://www.awesomebooks.com/books/category/2766/childrens-books/featured",
            "/books/category/2766/childrens-books",
            "listing",
        ),
        ("https://www.awesomebooks.com/books/bestsellers", "/books/bestsellers", "bestsellers"),
        ("https://www.awesomebooks.com/search?q=GCSE", "/search?q=GCSE", "listing"),
        ("new-books", "/new-books", "listing"),
    ],
)
def test_section_links(url, label, kind):
    section = section_from_url(url)
    assert (section.label, section.kind) == (label, kind)
    assert section.url.startswith("https://www.awesomebooks.com/")


@pytest.mark.parametrize(
    "url, message",
    [
        ("", "Paste"),
        ("https://www.bookoutlet.com/collections/x", "not an AwesomeBooks address"),
        ("https://www.awesomebooks.com/book/9780241003008/the-very-hungry-caterpillar/used", "single book"),
        ("https://www.awesomebooks.com/", "home page"),
    ],
)
def test_bad_section_links(url, message):
    with pytest.raises(InvalidSectionUrl, match=message):
        section_from_url(url)


# -- US locale ------------------------------------------------------------------------------


def test_every_visitor_switches_to_us_like_a_shopper(fake_site):
    site = client(fake_site)
    record = site.product(ISBNS[1])
    assert record.new.price == 18.74  # the US price, never the UK one (14.06)
    # Arrived (UK prices, discarded), picked "Ship to US", came back to the same page.
    assert [(m, p) for m, p, _ in fake_site.log[:2]] == [("GET", f"/book/{ISBNS[1]}/book"), ("GET", "/changeLocation/US")]
    assert fake_site.locale_changes == 1 and site.stats.locale_switches == 1


def test_new_identity_sets_the_locale_again(fake_site):
    site = client(fake_site, budget=(2, 2))  # rotate identities very often
    records = [site.product(isbn) for isbn in ISBNS[:3]]
    assert fake_site.locale_changes >= 2
    assert (records[0].new.price, records[0].used.price) == (13.01, 8.84)  # US prices only, never the UK ones
    assert (records[1].new.price, records[2].used.price) == (18.74, 10.57)


def test_session_losing_its_country_is_detected_and_fixed(fake_site):
    site = client(fake_site)
    site.product(ISBNS[1])
    fake_site.forget_locale_on = {len(fake_site.log)}  # the very next request comes back in GBP
    record = site.product(ISBNS[0])
    assert (record.new.price, record.used.price) == (13.01, 8.84)
    assert fake_site.locale_changes == 2


def test_gives_up_rather_than_recording_foreign_prices(fake_site, monkeypatch):
    site = client(fake_site)
    monkeypatch.setattr(fake_site, "respond_page", lambda session, url, params=None: _always_gb(fake_site, session, url))
    with pytest.raises(LocaleError):
        site.product(ISBNS[1])


def _always_gb(fake_site, session, url):
    session.country = "GB"
    from tests.conftest import FakeResponse

    return FakeResponse(200, fake_site.home(session))


# -- products -------------------------------------------------------------------------------


def test_both_conditions_are_read_from_their_own_pages(fake_site):
    record = client(fake_site).product(ISBNS[0])
    assert (record.new.price, record.new.quantity) == (13.01, 1482)
    assert (record.used.price, record.used.quantity) == (8.84, 3)
    assert record.quantity == 1485 and record.in_stock
    assert record.new.delivery == "Thu, November 5 - Tue, November 10"  # each condition's own window
    assert record.used.delivery == "Tue, November 3 - Fri, November 6"
    fields = record.fields()
    assert (fields["new_delivery"], fields["used_delivery"]) == (record.new.delivery, record.used.delivery)
    assert record.title == "Both Conditions" and record.publisher == "Test House" and record.page_count == 321


def test_single_condition_books(fake_site):
    site = client(fake_site)
    new_only, used_only = site.product(ISBNS[1]), site.product(ISBNS[2])
    assert new_only.used is None and new_only.new.quantity == 990
    assert used_only.new is None and (used_only.used.price, used_only.used.quantity) == (10.57, 7)


def test_sold_out_and_unlisted(fake_site):
    site = client(fake_site)
    sold_out = site.product(ISBNS[3])
    assert sold_out.found and not sold_out.in_stock and sold_out.quantity == 0
    assert sold_out.fields()["new_delivery"] is None and sold_out.fields()["used_delivery"] is None
    assert "Out of stock" in sold_out.notes[0]
    missing = site.product(ISBNS[5])
    assert not missing.found and missing.notes == ["Not listed on AwesomeBooks"]


def test_stock_lookup_searches_first_then_opens_the_book(fake_site):
    site = client(fake_site)
    record = site.lookup_stock(ISBNS[0])
    assert record.in_stock and record.quantity == 1485
    assert fake_site.search_calls == 1
    paths = [p for _, p, _ in fake_site.log]
    assert f"/book/{ISBNS[0]}/both-conditions" in paths  # followed the search result's real link


def test_stock_lookup_of_sold_out_book_opens_the_page_directly(fake_site):
    record = client(fake_site).lookup_stock(ISBNS[3])
    assert record.found and not record.in_stock  # search had nothing (index = in stock only), page says sold out


def test_stale_search_token_is_renewed(fake_site):
    site = client(fake_site)
    site.lookup_stock(ISBNS[1])
    site._csrf = "stale"
    with pytest.raises(FetchError, match="419"):
        site.search_isbn(ISBNS[0])
    assert site.search_isbn(ISBNS[0]).isbn == ISBNS[0]  # next call sets the session up again


# -- listings ---------------------------------------------------------------------------------


def test_listing_pages_and_end_of_listing(fake_site):
    fake_site.page_size = 2
    site = client(fake_site)
    first = site.listing_page("/books/category/303/test-fiction", {}, 1)
    assert first.total == 4 and len(first.cards) == 2 and first.shown_page == 1  # listings show books in stock only
    second = site.listing_page("/books/category/303/test-fiction", {}, 2)
    assert len(second.cards) == 2 and second.shown_page == 2
    past = site.listing_page("/books/category/303/test-fiction", {}, 3)  # the site answers with its home page
    assert past.cards == [] and past.total is None


def test_unknown_section_is_not_found(fake_site):
    with pytest.raises(NotFoundError):
        client(fake_site).listing_page("/books/category/999/nothing", {}, 1)


def test_bestsellers_come_from_the_search_index(fake_site):
    hits, pages = client(fake_site).bestsellers_page(0)
    assert pages == 1 and [h.title for h in hits][:3] == ["New Only", "Used Only", "Both Conditions"]
