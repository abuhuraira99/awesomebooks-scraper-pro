"""Planning the scan of a section larger than the site will page through.

AwesomeBooks lists 20 books per page and stops at page 500: any listing query
reaches at most its first 10,000 results (page 501 redirects to the home
page). Sections are far bigger (Used holds ~930,000 books), so a big section is
split into *slices*, each a listing query of its own that holds at most
10,000 results:

1. by condition (``?condition=new|used|rare``),
2. by the site's category filter (``?catid=…``, on Used, Bargain, New & Trending…),
3. by sub-category (the child categories a category page lists in its sidebar),
4. by format (``?format=paperback|hardback|…``).

The counts come from the site itself ("Showing 1 – 20 of N results"), so every
decision is exact. A slice that still holds more than 10,000 after all four is
read in each of the site's four sort orders (price low→high and high→low,
publication date new→old and old→new), which reaches up to 40,000 different
books. The run reports exactly how many books it extracted against how many the
section lists, so a shortfall is never silent.

The plan is plain JSON stored with the job, so a scan resumes on the exact page
where it stopped after a crash, reboot or Stop.
"""

from __future__ import annotations

from typing import Any

from .parsing import LISTING_PAGE_SIZE, ListingPage

WINDOW = 10_000  # results one listing query will page through
MAX_PAGES = WINDOW // LISTING_PAGE_SIZE
SLICE_SORTS = ("price_low_high", "price_high_low", "pubdate_high_low", "pubdate_low_high")
SORT_LABELS = {
    "price_low_high": "price low to high",
    "price_high_low": "price high to low",
    "pubdate_high_low": "newest first",
    "pubdate_low_high": "oldest first",
}
CONDITION_VALUES = ("new", "used", "rare")

Piece = dict[str, Any]  # {"path", "params", "page": last page read, "total", "leaf"}
Plan = dict[str, Any]  # {"queue": [Piece], "listed": section total, "unreachable": int}


def new_plan(path: str, params: dict[str, str]) -> Plan:
    return {"queue": [{"path": path, "params": dict(params), "page": 0, "total": None}], "listed": None, "unreachable": 0}


def describe(piece: Piece) -> str:
    parts = [piece["path"]]
    params = {k: v for k, v in piece["params"].items() if k != "sortBy"}
    if params:
        parts.append(", ".join(f"{k}={v}" for k, v in params.items()))
    if piece["params"].get("sortBy") and piece.get("leaf"):
        parts.append(SORT_LABELS.get(piece["params"]["sortBy"], piece["params"]["sortBy"]))
    return " · ".join(parts)


def split(piece: Piece, listing: ListingPage) -> tuple[list[Piece], str | None]:
    """Narrower slices that together cover ``piece``, using the refinements its first page offers.

    Returns ``([], None)`` when nothing is left to split by.
    """
    params = piece["params"]
    facets = listing.facets
    if "condition" not in params:
        values = [v for v, count in facets.get("condition", []) if v in CONDITION_VALUES and count != 0]
        if len(values) > 1:
            return [_child(piece, params={**params, "condition": v}) for v in values], "condition"
    if "catid" not in params:
        values = [v for v, count in facets.get("catid", []) if count != 0]
        if len(values) > 1:
            return [_child(piece, params={**params, "catid": v}) for v in values], "category"
    if listing.subcategories and not piece.get("subcategory"):
        children = [_child(piece, path=path) for path in listing.subcategories]
        for child in children:
            child["subcategory"] = True
        if len(children) > 1:
            return children, "sub-category"
    if "format" not in params:
        values = [v for v, count in facets.get("format", []) if count != 0]
        if len(values) > 1:
            return [_child(piece, params={**params, "format": v}) for v in values], "format"
    return [], None


def every_order(piece: Piece) -> list[Piece]:
    """Read a slice that can't be split any further in each of the site's sort orders."""
    total = int(piece.get("total") or 0)
    return [{**_child(piece, params={**piece["params"], "sortBy": order}), "leaf": True, "total": total} for order in SLICE_SORTS]


def is_last_page(piece: Piece, page: int, listing: ListingPage) -> bool:
    if not listing.cards or page >= MAX_PAGES:
        return True
    if listing.total is None:  # curated pages (campaigns) have one page and no counter
        return True
    return page * LISTING_PAGE_SIZE >= min(listing.total, WINDOW)


def _child(piece: Piece, *, path: str | None = None, params: dict[str, str] | None = None) -> Piece:
    child = {
        "path": path or piece["path"],
        "params": dict(params if params is not None else piece["params"]),
        "page": 0,
        "total": None,
    }
    if piece.get("subcategory"):
        child["subcategory"] = True
    return child
