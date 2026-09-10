"""restore_cells writes single cells RAW from a plan; the one value decision it makes is the TYPE.

Found live while backfilling Package ID: `_typed` turned every plan value that looked like
a number into one, so a Costco carton id "00009999990181363453" would have been written as the int
9999990181363452 -- leading zeros gone AND the last digit wrong, because a 20-digit integer does not
survive the float round trip. The same path would strip "0315" from Card Last 4. Coercion is now
limited to the columns that are numbers or checkboxes on the sheet.
"""

from scripts.restore_cells import _typed


def test_a_text_column_keeps_its_digits_verbatim():
    assert _typed("00009999990181363453", "Package ID") == "00009999990181363453"
    assert _typed("0315", "Card Last 4") == "0315"
    assert _typed("1Z999AA10123456784", "Tracking Number") == "1Z999AA10123456784"
    assert _typed(766, "Card Last 4") == "766"  # a plan may carry a bare number; the cell is still text


def test_a_numeric_column_is_coerced_to_a_real_number():
    assert _typed("2847", "Total Cost") == 2847
    assert _typed("949.5", "Cost Per Item") == 949.5
    assert _typed("3", "Quantity") == 3
    assert _typed(True, "Tracking Submitted") is True


def test_an_unknown_column_falls_back_to_the_old_behaviour():
    assert _typed("12", None) == 12
    assert _typed("free text", None) == "free text"
