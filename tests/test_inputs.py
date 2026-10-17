import io

import pytest
from openpyxl import Workbook

from awesomebooks_pro.inputs import UploadError, parse_upload


def test_csv_with_header_and_messy_values():
    data = (
        b"Title,ISBN,Qty\n"
        b"A,978-1-63973-605-8,1\n"
        b"B,BO-9780593486108,2\n"
        b"C,0306406152,1\n"
        b"D,not-an-isbn,1\n"
        b"E,9781639736058,1\n"
        b"F,9.78164E+12,1\n"
    )
    parsed = parse_upload("list.csv", data)
    assert parsed.column_label == "ISBN"
    assert [r.isbn for r in parsed.rows] == ["9781639736058", "9780593486108", "9780306406157", None, "9781639736058", None]
    assert parsed.valid == 4 and parsed.invalid == 2 and parsed.duplicates == 1 and parsed.unique == 3
    assert parsed.rows[0].row_no == 2
    assert "scientific" in parsed.rows[5].problem


def test_headerless_single_column_txt():
    parsed = parse_upload("isbns.txt", b"9781639736058\n9780593486108\n\n")
    assert parsed.valid == 2 and parsed.rows[0].row_no == 1


def test_column_detection_by_content_semicolon():
    data = b"name;code\nfoo;9781639736058\nbar;9780593486108\n"
    parsed = parse_upload("x.csv", data)
    assert parsed.valid == 2


def test_sku_header_amazon_style():
    data = b"sku,price,quantity\nBO-9781639736058,1,1\nBO-9780593486108,2,2\n"
    parsed = parse_upload("amazon.csv", data)
    assert parsed.column_label == "sku"
    assert [r.raw_sku for r in parsed.rows] == ["BO-9781639736058", "BO-9780593486108"]


def test_xlsx_numeric_cells():
    wb = Workbook()
    ws = wb.active
    ws.append(["EAN"])
    ws.append([9781639736058])
    ws.append(["9780593486108"])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_upload("list.xlsx", buf.getvalue())
    assert [r.isbn for r in parsed.rows] == ["9781639736058", "9780593486108"]


@pytest.mark.parametrize("name,data", [("a.csv", b""), ("a.csv", b"hello\nworld\n"), ("a.xls", b"\xd0\xcf")])
def test_rejects_unusable_files(name, data):
    with pytest.raises(UploadError):
        parse_upload(name, data)
