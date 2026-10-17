import pytest

from awesomebooks_pro.isbn import extract_isbn, isbn10_to_13, isbn13_is_valid, isbn_from_sku
from tests.conftest import ISBNS


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("9781639736058", "9781639736058"),
        ("978-1-63973-605-8", "9781639736058"),
        (" 978 1639 736058 ", "9781639736058"),
        ("BO-9781639736058", "9781639736058"),
        ("HM-9781639736058", "9781639736058"),
        ("9781639736058B", "9781639736058"),
        ("9781639736058.0", "9781639736058"),
        (9781639736058.0, "9781639736058"),
        ("0306406152", "9780306406157"),  # ISBN-10 upgraded to ISBN-13
        ("080442957X", "9780804429573"),
        ("9053-9780241003008", "9780241003008"),  # AwesomeBooks used-stock SKU
        ("4444-PEN-9781529961034", "9781529961034"),  # AwesomeBooks new-stock SKU
        ("ISBN-10: 0-241-00300-8", "9780241003008"),
    ],
)
def test_extract_isbn_accepts_real_world_formats(raw, expected):
    assert extract_isbn(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "hello", "9781639736059", "9.78164E+12", "12345", float("nan")])
def test_extract_isbn_rejects_garbage(raw):
    assert extract_isbn(raw) is None


def test_isbn10_conversion_and_checksum():
    assert isbn10_to_13("0306406152") == "9780306406157"
    assert isbn13_is_valid("9780306406157")
    assert not isbn13_is_valid("9780306406158")


def test_isbn_from_awesomebooks_sku():
    assert isbn_from_sku("9053-9780241003008") == "9780241003008"
    assert isbn_from_sku("4444-GRD-9780241003008") == "9780241003008"
    assert isbn_from_sku("") is None


def test_long_codes_never_yield_a_coincidental_isbn10():
    # 17 digits with no valid ISBN-13 inside: a 10-digit window must not be accepted by chance.
    assert extract_isbn("90530306406152000") is None


def test_fixture_isbns_are_valid():
    assert all(isbn13_is_valid(i) for i in ISBNS)
