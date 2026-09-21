"""Spend caps on a card's cashback (ledger/cashback_caps.py, models.card.CashbackCap). The cap is decided by SPEND: what the card was charged, in Order Date order, per
period, with returns credited back at their Return Date and outside-ledger spend taken off first."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import cashback_caps as caps  # noqa: E402
from ledger import sync as ledger_sync  # noqa: E402
from ledger.sync import HEADER  # noqa: E402
from models.card import Card, CashbackCap  # noqa: E402
from models.order import FIELDNAMES, OrderItem  # noqa: E402


def cap(**kw) -> CashbackCap:
    base = {"retailers": ["Amazon", "Amazon Business"], "spend_limit": 1000, "fallback_rate": "1%"}
    base.update(kw)
    return CashbackCap.model_validate(base)


def card(**kw) -> Card:
    base = {"last4": "5555", "name": "ABP", "cashback_rate": "1%",
            "retailer_rates": {"Amazon": "5%", "Amazon Business": "5%"},
            "caps": [{"retailers": ["Amazon", "Amazon Business"], "spend_limit": 1000, "fallback_rate": "1%"}]}
    base.update(kw)
    return Card.model_validate(base)


def row(**values) -> dict:
    base = {"retailer": "Amazon", "order_id": "A1", "order_date": "2026-03-01", "item_name": "Widget",
            "shipment": "1", "status": "paid", "card_last4": "5555", "profile_label": "p",
            "cost_per_item": "100", "total_cost": "100"}
    base.update({k: str(v) for k, v in values.items()})
    return base


def values(*rows: dict) -> list[list]:
    """A get_all_values() grid from field-name dicts."""
    return [list(HEADER)] + [[r.get(f, "") for f in FIELDNAMES] for r in rows]


class TestTheModel:
    def test_a_cap_normalises_its_retailers_limit_rate_and_offsets(self):
        c = cap(retailers="Amazon, best-buy", spend_limit="$150,000", fallback_rate="1%",
                outside_spend="2026: 4,000; 2027: 0")
        assert c.retailers == ["amazon", "bestbuy"] and c.spend_limit == 150000.0
        assert c.fallback_rate == 0.01 and c.outside_spend == {"2026": 4000.0, "2027": 0.0}

    def test_resets_is_calendar_year_never_or_a_date(self):
        assert cap().resets == "calendar-year"
        assert cap(resets="never").resets == "never"
        assert cap(resets="03-15").resets == "03-15"
        with pytest.raises(ValueError, match="resets"):
            cap(resets="yearly")
        with pytest.raises(ValueError, match="resets"):
            cap(resets="13-01")

    def test_a_cap_refuses_a_zero_limit_a_bad_fallback_and_negative_offsets(self):
        with pytest.raises(ValueError, match="spend limit"):
            cap(spend_limit=0)
        with pytest.raises(ValueError, match="outside 0-1"):
            cap(fallback_rate=2)
        with pytest.raises(ValueError, match="negative"):
            cap(outside_spend={"2026": -1})

    def test_a_card_routes_a_retailer_to_its_cap_else_the_catch_all(self):
        c = card(caps=[{"retailers": ["Amazon"], "spend_limit": 100, "fallback_rate": "1%"},
                       {"retailers": [], "spend_limit": 25000, "fallback_rate": "1%"}])
        assert c.cap_for("amazon") is c.caps[0]
        assert c.cap_for("Best Buy") is c.caps[1] and c.cap_for("Costco") is c.caps[1]
        assert card().cap_for("Costco") is None  # no catch-all: no cap there

    def test_a_retailer_in_two_caps_or_two_catch_alls_is_refused(self):
        with pytest.raises(ValueError, match="two cashback caps"):
            card(caps=[{"retailers": ["Amazon"], "spend_limit": 1, "fallback_rate": 0.01},
                       {"retailers": ["amazon"], "spend_limit": 2, "fallback_rate": 0.01}])
        with pytest.raises(ValueError, match="catch-all"):
            card(caps=[{"spend_limit": 1, "fallback_rate": 0.01}, {"spend_limit": 2, "fallback_rate": 0.01}])


class TestPeriodsAndEvents:
    def test_period_keys(self):
        assert caps.period_key(cap(), "2026-03-01") == "2026"
        assert caps.period_key(cap(resets="never"), "2026-03-01") == "all"
        anniversary = cap(resets="03-15")
        assert caps.period_key(anniversary, "2026-03-14") == "2025"
        assert caps.period_key(anniversary, "2026-03-15") == "2026"

    def test_the_basis_is_the_cogs_basis_never_below_zero(self):
        assert caps.basis(row(total_cost="100", shipping="5", sales_tax="8", gift_card="20", rewards_used="3")) == 90.0
        assert caps.basis(row(total_cost="10", gift_card="50")) == 0.0
        assert caps.basis(row(total_cost="$1,259.99")) == 1259.99

    def test_a_return_is_an_event_at_its_return_date_or_the_order_date(self):
        events = caps.events_of(row(return_quantity="1", cost_per_item="40", return_date="2026-04-02"))
        assert [(e.when, e.amount, e.rank[0]) for e in events] == [("2026-03-01", 100.0, 0), ("2026-04-02", -40.0, 1)]
        undated = caps.events_of(row(return_quantity="1", cost_per_item="40"))
        assert undated[1].when == "2026-03-01"

    def test_cancelled_superseded_and_cardless_rows_are_no_events(self):
        assert caps.events_of(row(status="cancelled")) == []
        assert caps.events_of(row(status="superseded")) == []
        assert caps.events_of(row(card_last4="")) == []

    def test_rows_by_field_reads_a_get_all_values_grid(self):
        rows = caps.rows_by_field(values(row(order_id="X9", total_cost="12.5")))
        assert rows[0]["order_id"] == "X9" and rows[0]["total_cost"] == "12.5"


class TestCappedRate:
    def test_under_over_and_the_blend(self):
        c = cap(spend_limit=1000, fallback_rate="1%")
        assert caps.capped_rate(0.05, c, used=0, amount=999) == 0.05
        assert caps.capped_rate(0.05, c, used=1000, amount=50) == 0.01
        assert caps.capped_rate(0.05, c, used=1200, amount=50) == 0.01
        # 400 of room at 5%, 600 past it at 1%: (400*0.05 + 600*0.01) / 1000 = 0.026
        assert caps.capped_rate(0.05, c, used=600, amount=1000) == 0.026
        assert caps.capped_rate(0.05, c, used=600, amount=0) == 0.05

    def test_spend_before_counts_the_scope_the_period_and_the_offset(self):
        c = card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%",
                        "outside_spend": {"2026": 250}}])
        the_cap = c.caps[0]
        events = caps.events_from_rows([
            row(order_id="A0", order_date="2026-01-05", total_cost="100"),
            row(order_id="A1", order_date="2026-02-01", total_cost="200"),
            row(order_id="B1", order_date="2026-02-02", total_cost="500", retailer="Best Buy"),  # out of scope
            row(order_id="Z1", order_date="2025-12-30", total_cost="900"),                        # last period
            row(order_id="A2", order_date="2026-02-01", total_cost="50", card_last4="1111"),      # another card
            row(order_id="A3", order_date="2026-02-03", total_cost="70"),                          # after
        ])
        upto = next(e for e in events if e.key[0] == "A3")
        assert caps.spend_before(events, [c], c, the_cap, upto) == 250 + 100 + 200

    def test_a_virtual_number_pools_with_its_card(self):
        real = card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%"}])
        virtual = Card(last4="9999", name="ABP virtual", virtual_of="5555", retailer_rates={"Amazon": "5%"})
        cards = [real, virtual]
        events = caps.events_from_rows([
            row(order_id="V1", order_date="2026-01-05", total_cost="700", card_last4="9999"),
            row(order_id="A1", order_date="2026-02-01", total_cost="100"),
        ])
        upto = next(e for e in events if e.key[0] == "A1")
        assert caps.spend_before(events, cards, real, real.caps[0], upto) == 700.0
        # and a purchase on the virtual number is capped by the real card's allowance
        items = [item(order_id="V2", order_date="2026-03-01", total_cost=500, card_last4="9999")]
        caps.apply_to_items(items, cards, [row(order_id="A0", order_date="2026-01-01", total_cost="900")])
        assert items[0].cashback_rate == round((100 * 0.05 + 400 * 0.01) / 500, 4)

    def test_a_return_gives_its_spend_back_in_its_own_period(self):
        c = card()
        the_cap = c.caps[0]
        events = caps.events_from_rows([
            row(order_id="A0", order_date="2025-12-01", total_cost="900", return_quantity="1",
                cost_per_item="900", return_date="2026-01-10"),  # bought last year, returned this year
            row(order_id="A1", order_date="2026-02-01", total_cost="100"),
        ])
        upto = next(e for e in events if e.key[0] == "A1")
        assert caps.spend_before(events, [c], c, the_cap, upto) == -900.0


def item(**kw) -> OrderItem:
    base = dict(retailer="Amazon", order_id="N1", order_date="2026-03-05", item_name="Thing", shipment="1",
                status="ordered", card_last4="5555", profile_label="p", quantity=1, cost_per_item=100,
                total_cost=100, shipping=0, sales_tax=0, gift_card=0, rewards_used=0, cashback_rate=0.05)
    if "total_cost" in kw:  # an item derives Total Cost from quantity x unit cost
        kw.setdefault("cost_per_item", kw["total_cost"])
    base.update(kw)
    return OrderItem(**base)


class TestScrapeTime:
    def test_the_batch_is_placed_among_the_ledger_rows_and_capped(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%"}])]
        ledger = [row(order_id="A0", order_date="2026-01-05", total_cost="700")]
        items = [item(order_id="N2", order_date="2026-03-06", total_cost=500),   # 200 of room left: blend
                 item(order_id="N1", order_date="2026-03-05", total_cost=100)]   # earlier: under the cap
        changes = caps.apply_to_items(items, cards, ledger)
        by_id = {it.order_id: it.cashback_rate for it in items}
        assert by_id["N1"] == 0.05
        assert by_id["N2"] == round((200 * 0.05 + 300 * 0.01) / 500, 4)
        assert [(c[0].order_id, c[1]) for c in changes] == [("N2", 0.05)]

    def test_a_rows_older_self_on_the_ledger_is_not_counted_twice(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%"}])]
        ledger = [row(order_id="N1", order_date="2026-03-05", item_name="Thing", total_cost="900")]  # the same key, re-scraped
        items = [item(order_id="N1", order_date="2026-03-05", total_cost=900)]
        caps.apply_to_items(items, cards, ledger)
        assert items[0].cashback_rate == 0.05

    def test_order_level_amounts_are_shared_by_total_cost_and_a_cardless_row_is_left(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 300, "fallback_rate": "1%"}])]
        items = [item(order_id="N1", shipment="1", total_cost=100, shipping=30, gift_card=0),
                 item(order_id="N1", shipment="2", item_name="Other", total_cost=200, shipping=30),
                 item(order_id="N3", card_last4="", cashback_rate=None)]
        caps.apply_to_items(items, cards, [])
        # shipment 1: 100 + 10 of shipping = 110, under; shipment 2: 200 + 20 = 220 with 190 of room
        assert items[0].cashback_rate == 0.05
        assert items[1].cashback_rate == round((190 * 0.05 + 30 * 0.01) / 220, 4)
        assert items[2].cashback_rate is None

    def test_no_caps_configured_touches_nothing(self):
        items = [item()]
        assert caps.apply_to_items(items, [card(caps=[])], []) == [] and items[0].cashback_rate == 0.05


class TestRecompute:
    def test_rows_past_shipped_follow_the_spend_and_open_rows_do_not(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%"}])]
        grid = values(
            row(order_id="A0", order_date="2026-01-05", total_cost="700", cashback_rate="0.05", status="paid"),
            row(order_id="A1", order_date="2026-02-01", total_cost="500", cashback_rate="0.05", status="delivered"),  # should blend
            row(order_id="A2", order_date="2026-02-02", total_cost="100", cashback_rate="0.05", status="paid"),       # past: fallback
            row(order_id="A3", order_date="2026-02-03", total_cost="100", cashback_rate="0.05", status="shipped"),    # open: left alone
            row(order_id="A4", order_date="2026-02-04", total_cost="100", cashback_rate="0.05", status="cancelled"),
        )
        changes = caps.recompute(grid, cards, {}, default_rate=0.02)
        assert changes == [(3, round((300 * 0.05 + 200 * 0.01) / 500, 4), 0.05), (4, 0.01, 0.05)]

    def test_a_hand_edited_rate_cell_and_an_uncapped_card_are_skipped(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 100, "fallback_rate": "1%"}]),
                 Card(last4="1111", name="Flat", cashback_rate="2%")]
        grid = values(
            row(order_id="A0", order_date="2026-01-05", total_cost="700", cashback_rate="0.05", status="paid"),
            row(order_id="A1", order_date="2026-02-01", total_cost="50", cashback_rate="0.05", status="paid"),
            row(order_id="F1", order_date="2026-02-01", total_cost="900", cashback_rate="0.02", status="paid", card_last4="1111"),
        )
        protected = {("A1", "2026-02-01", "Widget", "1"): {"cashback_rate"}}
        changes = caps.recompute(grid, cards, protected, default_rate=0.02)
        # A0 crosses the line on its own (100 of room, 600 past it); A1 is protected; F1 has no cap
        assert changes == [(2, round((100 * 0.05 + 600 * 0.01) / 700, 4), 0.05)]

    def test_a_percent_text_rate_reads_as_a_number(self):
        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 1000, "fallback_rate": "1%"}])]
        grid = values(row(order_id="A0", order_date="2026-01-05", total_cost="700", cashback_rate="5%", status="paid"))
        assert caps.recompute(grid, cards, {}) == []


class TestTheSyncRecomputes:
    """After a sync, rows past shipped on a capped card are rewritten (ledger.sync._recompute_cashback_caps)."""

    def test_the_rate_cells_are_rewritten_and_the_activity_says_so(self, monkeypatch, tmp_path):
        from test_ledger_sync import FakeWorksheet, write_csv_file

        ws = FakeWorksheet()
        ws.rows = [list(HEADER)] + [[r.get(f, "") for f in FIELDNAMES] for r in (
            row(order_id="A0", order_date="2026-01-05", total_cost="700", cashback_rate="0.05", status="paid"),
            row(order_id="A2", order_date="2026-02-02", total_cost="100", cashback_rate="0.05", status="paid"),
        )]
        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: ws)
        monkeypatch.setattr("config.cards.load_cards", lambda: [
            card(caps=[{"retailers": ["Amazon"], "spend_limit": 500, "fallback_rate": "1%"}])])
        recorded = []
        monkeypatch.setattr("diagnostics.activity.record", lambda kind, summary, details=None, **kw: recorded.append((kind, summary)))
        path = write_csv_file(tmp_path, dict(retailer="Amazon", order_id="N1", order_date="2026-03-01", item_name="New",
                                             shipment="1", status="ordered", card_last4="5555"))
        ledger_sync.sync_csv_to_ledger(path)
        rate = FIELDNAMES.index("cashback_rate")
        by_id = {r[FIELDNAMES.index("order_id")]: r[rate] for r in ws.data_rows()}
        assert float(by_id["A0"]) == round((500 * 0.05 + 200 * 0.01) / 700, 4)  # crosses the line
        assert float(by_id["A2"]) == 0.01                                          # past it
        assert any("re-tiered" in summary for _kind, summary in recorded)

    def test_no_capped_card_means_no_read_and_no_write(self, monkeypatch):
        calls = []

        class Silent:
            def get_all_values(self):
                calls.append("read")
                return [list(HEADER)]

            def batch_update(self, *a, **k):
                calls.append("write")

        monkeypatch.setattr("config.cards.load_cards", lambda: [Card(last4="1111", name="Flat", cashback_rate="2%")])
        ledger_sync._recompute_cashback_caps(Silent())
        assert calls == []


class TestTheRunOrder:
    """main._tag_cards: the card's rate, then the cap, then the Amazon promo on top."""

    def test_the_promo_rides_on_top_of_the_capped_rate(self, monkeypatch):
        import main

        cards = [card(caps=[{"retailers": ["Amazon"], "spend_limit": 100, "fallback_rate": "1%"}])]
        monkeypatch.setattr(main, "_cards", lambda: cards)
        monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, amazon_promo_cashback_enabled=True))

        class Ledger:
            def get_all_values(self):
                return [list(HEADER)]

        monkeypatch.setattr(ledger_sync, "_get_worksheet", lambda: Ledger())
        it = item(order_id="N1", total_cost=500, cashback_rate=None)
        it._promo_cashback_rate = 0.01
        main._tag_cards([it], "Amazon [p]")
        capped = round((100 * 0.05 + 400 * 0.01) / 500, 4)
        assert it.cashback_rate == round(capped + 0.01, 4) and it.card_name == "ABP"

    def test_an_unreadable_ledger_leaves_the_uncapped_rate(self, monkeypatch):
        import main

        monkeypatch.setattr(main, "_cards", lambda: [card()])
        monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, amazon_promo_cashback_enabled=False))

        def boom():
            raise OSError("locked")

        monkeypatch.setattr(ledger_sync, "_get_worksheet", boom)
        it = item(order_id="N1", total_cost=5000, cashback_rate=None)
        main._tag_cards([it], "Amazon [p]")
        assert it.cashback_rate == 0.05
