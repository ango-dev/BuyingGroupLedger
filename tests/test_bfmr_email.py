"""The pure half of the BFMR combined-package auto-reply: recognising the email, resolving the
box against the ledger, the serial-count policy, and building the reply. Everything offline;
the fixture email is SYNTHETIC by ruling (nothing from any real BFMR email is committed)."""

from pathlib import Path

from buying_groups.bfmr_email import (
    attribute_serials, build_reply, parse_request, resolve_box, split_serials,
)
from sheets.ledger_sync import HEADER

FIXTURE = Path(__file__).parent / "fixtures" / "bfmr_combined_package_email.eml"
DOMAINS = ["buyformeretail.com", "bfmr.com"]

#: What the live fetch returns: serials PER PACKAGE. Both orders here ship whole in the box.
FETCHED = {"BBY01-1": {"999900001111": ["SERIAL01", "SERIAL02"]},
           "BBY01-2": {"999900001111": ["SERIAL03"]}}
SERIALS = {"BBY01-1": ["SERIAL01", "SERIAL02"], "BBY01-2": ["SERIAL03"]}


def _email(from_addr="BFMR Support <support@buyformeretail.com>",
           subject="Action Needed: Combined Best Buy Package 999900001111",
           body="We received your package as part of a combined Best Buy order: 999900001111. "
                "Please reply to this email with serial numbers and your receipt.",
           reply_to=None, content_type="text/plain") -> bytes:
    headers = [
        f"From: {from_addr}",
        "To: Jane Fixture <jane.fixture@example.com>",
        f"Subject: {subject}",
        "Message-ID: <synthetic-0002@buyformeretail.com>",
        "MIME-Version: 1.0",
        f'Content-Type: {content_type}; charset="utf-8"',
    ]
    if reply_to:
        headers.append(f"Reply-To: {reply_to}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


def _row(**cells) -> list:
    """A sheet row by display column name; everything else blank."""
    row = [""] * len(HEADER)
    for name, value in cells.items():
        row[HEADER.index(name)] = value
    return row


def _grid():
    """A combined box: two Best Buy orders under one tracking number, plus a bystander."""
    return [
        _row(**{"Order ID": "BBY01-1", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
                "Quantity": 2, "Tracking Number": "999900001111", "Status": "delivered",
                "Retailer": "Best Buy", "Profile": "profile-a",
                "Receipt Link": "https://objectstorage.example.com/PARSECRET/receipts/bestbuy/2026-09/BBY01-1.pdf"}),
        _row(**{"Order ID": "BBY01-2", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
                "Quantity": 1, "Tracking Number": "999900001111", "Status": "delivered",
                "Retailer": "Best Buy", "Profile": "profile-a",
                "Receipt Link": "https://objectstorage.example.com/PARSECRET/receipts/bestbuy/2026-09/BBY01-2.pdf"}),
        _row(**{"Order ID": "BBY01-3", "Order Date": "2026-09-01", "Item Name": "Unrelated",
                "Quantity": 1, "Tracking Number": "888800002222", "Status": "delivered",
                "Retailer": "Best Buy", "Profile": "profile-a"}),
    ]


# --- parse_request --------------------------------------------------------------------------


def test_parses_the_synthetic_fixture():
    request = parse_request(FIXTURE.read_bytes(), uid="7", sender_domains=DOMAINS)
    assert request is not None
    assert request.tracking_number == "999900001111"
    assert request.message_id == "<fixture-combined-0001@buyformeretail.com>"
    assert request.reply_to == "support@buyformeretail.com"
    assert request.uid == "7"


def test_ignores_our_own_action_needed_alerts():
    """The repo's own alerts land in the SAME inbox with subjects that name tracking numbers —
    the sender domain, never the subject, is what admits a mail."""
    raw = _email(from_addr="buyer.track@example.com",
                 subject="ACTION NEEDED — BFMR combined package 999900001111: cannot auto-reply",
                 body="combined package 999900001111 blocked")
    assert parse_request(raw, uid="1", sender_domains=DOMAINS) is None


def test_ignores_non_bfmr_senders():
    raw = _email(from_addr="Best Buy <bestbuyinfo@emailinfo.bestbuy.com>")
    assert parse_request(raw, uid="1", sender_domains=DOMAINS) is None


def test_ignores_bfmr_conversation_replies():
    """Proven necessary live: BFMR's Zendesk acknowledges an auto-reply with a
    ticket receipt whose subject still says "Combined Best Buy Package" — answering it would
    loop with their ticket system (and each extra reply re-queues the ticket). A message
    carrying In-Reply-To/References is a conversation, never a fresh request."""
    headers_body = _email().decode()
    threaded = headers_body.replace(
        "MIME-Version: 1.0",
        "In-Reply-To: <ours@mx.google.com>\r\nReferences: <ours@mx.google.com>\r\nMIME-Version: 1.0")
    assert parse_request(threaded.encode(), uid="1", sender_domains=DOMAINS) is None


def test_ignores_bfmr_mail_that_is_not_a_combined_request():
    raw = _email(subject="Your BFMR payout was processed",
                 body="Your payout of $1,262.00 was sent. Deal 999900001111.")
    assert parse_request(raw, uid="1", sender_domains=DOMAINS) is None


def test_tracking_read_from_html_body_when_subject_lacks_it():
    raw = _email(subject="Action Needed: Combined Best Buy Package",
                 body="<html><body><p>combined Best Buy order:</p>"
                      "<p><b>999900001111</b></p></body></html>",
                 content_type="text/html")
    request = parse_request(raw, uid="1", sender_domains=DOMAINS)
    assert request.tracking_number == "999900001111"


def test_ambiguous_body_numbers_yield_no_tracking_not_a_guess():
    raw = _email(subject="Action Needed: Combined Best Buy Package",
                 body="combined Best Buy order: 999900001111 or 999900002222")
    request = parse_request(raw, uid="1", sender_domains=DOMAINS)
    assert request is not None and request.tracking_number is None


def test_reply_to_wins_over_from():
    raw = _email(reply_to="Combined Desk <combined@buyformeretail.com>")
    request = parse_request(raw, uid="1", sender_domains=DOMAINS)
    assert request.reply_to == "combined@buyformeretail.com"


# --- resolve_box ----------------------------------------------------------------------------


def _request(tracking="999900001111"):
    raw = _email(subject=f"Action Needed: Combined Best Buy Package {tracking}")
    return parse_request(raw, uid="1", sender_domains=DOMAINS)


def test_bare_email_number_matches_bare_ledger_number():
    box = resolve_box(_request(), HEADER, _grid())
    assert sorted(box.orders) == ["BBY01-1", "BBY01-2"]
    assert box.missing == []


def test_suffixed_email_number_matches_bare_ledger_number():
    """BFMR appends a letter to de-duplicate a carton; the ledger keeps the bare number."""
    box = resolve_box(_request("999900001111B"), HEADER, _grid())
    assert sorted(box.orders) == ["BBY01-1", "BBY01-2"]
    assert box.missing == []


def test_multi_order_box_groups_rows_per_order_with_profile():
    box = resolve_box(_request(), HEADER, _grid())
    assert box.orders["BBY01-1"].items == [("MacBook Air", 2)]
    assert box.orders["BBY01-2"].items == [("MacBook Air", 1)]
    assert box.orders["BBY01-1"].quantity() == 2 and box.orders["BBY01-2"].quantity() == 1
    assert box.orders["BBY01-1"].profile_label == "profile-a"
    assert box.orders["BBY01-1"].rows == [2] and box.orders["BBY01-2"].rows == [3]


def test_no_matching_rows_is_reported_missing():
    box = resolve_box(_request("777700000000"), HEADER, _grid())
    assert not box.orders
    assert any("no ledger row" in m for m in box.missing)


def test_a_non_bestbuy_match_is_reported_not_replied_from():
    """Only Best Buy has combined packages; a hit on another retailer's row is a wrong match,
    and replying from it would attach the wrong receipt."""
    grid = _grid()
    grid[0][HEADER.index("Retailer")] = "Amazon"
    box = resolve_box(_request(), HEADER, grid)
    assert "BBY01-1" not in box.orders
    assert any("Best Buy only" in m for m in box.missing)


def test_missing_receipt_reported_missing():
    grid = _grid()
    grid[1][HEADER.index("Receipt Link")] = ""
    box = resolve_box(_request(), HEADER, grid)
    assert any("Receipt Link is blank" in m for m in box.missing)


def test_non_pdf_receipt_reported_missing():
    grid = _grid()
    grid[1][HEADER.index("Receipt Link")] = "https://objectstorage.example.com/PAR/x.png"
    box = resolve_box(_request(), HEADER, grid)
    assert any("not a PDF" in m for m in box.missing)


def test_superseded_rows_never_claim_the_box():
    """A superseded row's number is dead — its live twin owns the box now."""
    grid = _grid()
    grid[0][HEADER.index("Status")] = "superseded"
    box = resolve_box(_request(), HEADER, grid)
    assert sorted(box.orders) == ["BBY01-2"]


def test_ambiguous_request_resolves_to_manual():
    request = _request()
    request.tracking_number = None
    box = resolve_box(request, HEADER, _grid())
    assert box.missing and "by hand" in box.missing[0]


def test_split_serials_tolerates_hand_typed_separators():
    assert split_serials("A1,B2; C3\nD4") == ["A1", "B2", "C3", "D4"]


# --- attribute_serials ----------------------------------------------------------------------


def test_attributed_serials_with_matching_counts_have_no_gaps():
    box = resolve_box(_request(), HEADER, _grid())
    serials, gaps = attribute_serials(box, FETCHED)
    assert gaps == [] and serials == SERIALS


def test_no_serials_found_is_a_gap_naming_the_order():
    box = resolve_box(_request(), HEADER, _grid())
    serials, gaps = attribute_serials(box, {"BBY01-1": FETCHED["BBY01-1"], "BBY01-2": {}})
    assert len(gaps) == 1 and "BBY01-2" in gaps[0] and "no serial numbers found" in gaps[0]
    assert "BBY01-2" not in serials


def test_attributed_count_mismatch_is_a_gap_never_a_guess():
    box = resolve_box(_request(), HEADER, _grid())
    fetched = {"BBY01-1": {"999900001111": ["A1", "B2", "C3"]}, "BBY01-2": FETCHED["BBY01-2"]}
    _serials, gaps = attribute_serials(box, fetched)
    assert len(gaps) == 1 and "3 serial(s) attributed" in gaps[0] and "2 unit(s)" in gaps[0]


def _split_grid():
    """A 2+1 split: ONE order, two tracking numbers, only the qty-2 package in this box."""
    return [
        _row(**{"Order ID": "BBY01-1", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
                "Quantity": 2, "Tracking Number": "999900001111", "Status": "delivered",
                "Retailer": "Best Buy", "Profile": "profile-a",
                "Receipt Link": "https://objectstorage.example.com/PARSECRET/BBY01-1.pdf"}),
        _row(**{"Order ID": "BBY01-1", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
                "Quantity": 1, "Shipment": 2, "Tracking Number": "999900002222",
                "Status": "delivered", "Retailer": "Best Buy", "Profile": "profile-a",
                "Receipt Link": "https://objectstorage.example.com/PARSECRET/BBY01-1.pdf"}),
    ]


def test_a_split_order_takes_only_its_own_packages_serials():
    """The live case: the order surfaced 3 serials; per-package attribution picks
    the 2 belonging to this box's tracking number."""
    box = resolve_box(_request(), HEADER, _split_grid())
    assert box.orders["BBY01-1"].other_tracking == {"999900002222"}
    fetched = {"BBY01-1": {"999900001111": ["A1", "B2"], "999900002222": ["C3"]}}
    serials, gaps = attribute_serials(box, fetched)
    assert gaps == [] and serials == {"BBY01-1": ["A1", "B2"]}


def test_unattributed_serials_are_refused_on_a_split_order():
    """3 loose serials, two packages: which 2 are in this box is unknowable — block, name the
    other package, point at --serials."""
    box = resolve_box(_request(), HEADER, _split_grid())
    fetched = {"BBY01-1": {"": ["A1", "B2", "C3"]}}
    serials, gaps = attribute_serials(box, fetched)
    assert serials == {} and len(gaps) == 1
    assert "999900002222" in gaps[0] and "--serials" in gaps[0]


def test_unattributed_serials_are_accepted_when_the_order_is_one_package():
    """No other tracking number on the sheet = the whole order is this box, so page-scanned
    serials with the right count are safe."""
    box = resolve_box(_request(), HEADER, _grid())
    fetched = {"BBY01-1": {"": ["A1", "B2"]}, "BBY01-2": {"": ["C3"]}}
    serials, gaps = attribute_serials(box, fetched)
    assert gaps == [] and serials == {"BBY01-1": ["A1", "B2"], "BBY01-2": ["C3"]}


def test_unattributed_count_mismatch_still_blocks_on_a_one_package_order():
    box = resolve_box(_request(), HEADER, _grid())
    fetched = {"BBY01-1": {"": ["A1"]}, "BBY01-2": FETCHED["BBY01-2"]}
    _serials, gaps = attribute_serials(box, fetched)
    assert len(gaps) == 1 and "1 serial(s) for 2 unit(s)" in gaps[0]


# --- build_reply ----------------------------------------------------------------------------


def _built():
    request = _request()
    box = resolve_box(request, HEADER, _grid())
    pdfs = {"BBY01-1": b"%PDF-1.4 fake one", "BBY01-2": b"%PDF-1.4 fake two"}
    return build_reply(request, box, pdfs, SERIALS, from_addr="jane.fixture@example.com",
                       cc="audit@example.com")


def test_reply_has_threading_headers():
    msg = _built()
    assert msg["To"] == "support@buyformeretail.com"
    assert msg["Subject"].startswith("Re: ")
    assert msg["In-Reply-To"] == "<synthetic-0002@buyformeretail.com>"
    assert "<synthetic-0002@buyformeretail.com>" in msg["References"]
    assert msg["Cc"] == "audit@example.com"


def test_reply_attaches_one_pdf_per_order():
    msg = _built()
    attachments = {p.get_filename(): p.get_payload(decode=True)
                   for p in msg.iter_attachments()}
    assert attachments == {"BBY01-1.pdf": b"%PDF-1.4 fake one",
                           "BBY01-2.pdf": b"%PDF-1.4 fake two"}
    assert all(p.get_content_type() == "application/pdf" for p in msg.iter_attachments())


def test_reply_body_puts_a_serials_block_below_the_receipt_line():
    """The user's chosen format (2026-09-11): receipt line first, then "Serials:" with one bare
    serial per line."""
    body = _built().get_body(("plain",)).get_content()
    assert "999900001111" in body
    assert "BBY01-1" in body and "BBY01-2" in body
    lines = body.splitlines()
    first_receipt = lines.index("Receipt: attached as BBY01-1.pdf")
    serial_header = lines.index("Serials:", first_receipt)
    assert serial_header > first_receipt
    assert lines[serial_header + 1:serial_header + 3] == ["SERIAL01", "SERIAL02"]
    second_serials = lines.index("Serials:", serial_header + 1)
    assert lines[second_serials + 1] == "SERIAL03"


def test_par_url_never_appears_in_the_reply():
    """The Receipt Link embeds the bucket-level PAR secret; the mail leaves the machine, so the
    URL must never — bytes are attached instead. Checked over the WHOLE serialized message."""
    raw = _built().as_string()
    assert "PARSECRET" not in raw
    assert "objectstorage.example.com" not in raw
