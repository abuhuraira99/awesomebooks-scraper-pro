"""Slicing a section that is bigger than the 10,000 results the site pages through."""

from awesomebooks_pro import scan
from awesomebooks_pro.parsing import ListingCard, ListingPage


def listing(total=50_000, facets=None, subs=None, cards=1):
    card = ListingCard("9780241003008", "u", "New", "t", None, None, 1.0, None, None)
    return ListingPage(total=total, shown_page=1, cards=[card] * cards, facets=facets or {}, subcategories=subs or [])


def piece(**params):
    return {"path": "/books/category/303/fiction", "params": params, "page": 0, "total": None}


def test_condition_first():
    parts, dim = scan.split(piece(), listing(facets={"condition": [("new", None), ("used", None), ("rare", None)]}))
    assert dim == "condition" and [p["params"] for p in parts] == [
        {"condition": "new"},
        {"condition": "used"},
        {"condition": "rare"},
    ]


def test_then_category_filter_then_subcategory_then_format():
    facets = {"catid": [("1", None), ("2", None)], "format": [("paperback", 9), ("hardback", 3)]}
    parts, dim = scan.split(
        piece(condition="used"), listing(facets=facets, subs=["/books/category/11/a", "/books/category/12/b"])
    )
    assert dim == "category" and parts[0]["params"] == {"condition": "used", "catid": "1"}

    parts, dim = scan.split(
        piece(condition="used", catid="1"), listing(facets=facets, subs=["/books/category/11/a", "/books/category/12/b"])
    )
    assert dim == "sub-category" and [p["path"] for p in parts] == ["/books/category/11/a", "/books/category/12/b"]
    assert all(p["params"] == {"condition": "used", "catid": "1"} and p["subcategory"] for p in parts)

    sub = parts[0]
    parts, dim = scan.split(sub, listing(facets=facets, subs=["/books/category/99/c", "/books/category/98/d"]))
    assert dim == "format"  # one level of sub-categories only, so siblings can never loop
    assert [p["params"]["format"] for p in parts] == ["paperback", "hardback"]


def test_nothing_left_to_split_reads_every_sort_order():
    parts, dim = scan.split(piece(condition="used", format="paperback"), listing())
    assert (parts, dim) == ([], None)
    orders = scan.every_order({**piece(condition="used"), "total": 70_000})
    assert [p["params"]["sortBy"] for p in orders] == list(scan.SLICE_SORTS)
    assert all(p["leaf"] and p["total"] == 70_000 for p in orders)


def test_zero_count_facets_are_skipped():
    parts, dim = scan.split(
        piece(condition="new"), listing(facets={"format": [("paperback", 30_000), ("other", 0), ("hardback", 9)]})
    )
    assert dim == "format" and [p["params"]["format"] for p in parts] == ["paperback", "hardback"]


def test_last_page_rules():
    p = piece()
    assert scan.is_last_page(p, 3, listing(total=50, cards=10))
    assert not scan.is_last_page(p, 2, listing(total=50, cards=20))
    assert scan.is_last_page(p, scan.MAX_PAGES, listing(total=99_999, cards=20))
    assert scan.is_last_page(p, 1, listing(total=None, cards=30))  # curated page: a single page
    assert scan.is_last_page(p, 1, listing(total=50, cards=0))


def test_describe():
    assert scan.describe(piece(condition="used")) == "/books/category/303/fiction · condition=used"
    leaf = {**piece(sortBy="price_high_low"), "leaf": True}
    assert scan.describe(leaf) == "/books/category/303/fiction · price high to low"
