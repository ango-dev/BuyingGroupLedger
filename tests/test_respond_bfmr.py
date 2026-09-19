"""The orchestrator half of the BFMR combined-package auto-reply: dry-run discipline (no send,
no flag, NO browser fee), the live serial fetch on --apply, idempotency (the \\Answered flag
plus the .state.json backstop), and loud needs_manual paths. IMAP/SMTP/ledger/serials are all
injected or patched — nothing here touches a network."""

import pytest

import main as main_module
import respond_bfmr
from config.loader import load_state, save_state
from ledger.sync import HEADER

TRACKING = "999900001111"
MESSAGE_ID = "<synthetic-0002@buyformeretail.com>"
#: Per-package, as the live fetch returns them: {order: {tracking: [serials]}}.
SERIALS = {"BBY01-1": {TRACKING: ["SERIAL01", "SERIAL02"]}}


def _email(subject=f"Action Needed: Combined Best Buy Package {TRACKING}",
           body="combined Best Buy order — please reply with serials and your receipt.") -> bytes:
    return (
        "From: BFMR Support <support@buyformeretail.com>\r\n"
        "To: Jane Fixture <jane.fixture@example.com>\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {MESSAGE_ID}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n" + body
    ).encode()


def _row(**cells) -> list:
    row = [""] * len(HEADER)
    for name, value in cells.items():
        row[HEADER.index(name)] = value
    return row


def _sheet():
    return [list(HEADER), _row(**{
        "Order ID": "BBY01-1", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
        "Quantity": 2, "Tracking Number": TRACKING, "Status": "delivered",
        "Retailer": "Best Buy", "Profile": "profile-a",
        "Receipt Link": "https://objectstorage.example.com/PARSECRET/BBY01-1.pdf",
    })]


class FakeMailbox:
    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.answered: list[str] = []

    def search_unanswered(self, sender_domains, since_days=30):
        return [uid for uid in self.messages if uid not in self.answered]

    def fetch(self, uid):
        return self.messages[uid]

    def mark_answered(self, uid):
        self.answered.append(uid)


class FakeSerialFetch:
    """Records calls; returns a canned PER-PACKAGE answer ({order: {tracking: [serials]}}).
    Injected as fetch_serials(profile, order_ids)."""

    def __init__(self, serials=None):
        self.serials = SERIALS if serials is None else serials
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, profile_label, order_ids):
        self.calls.append((profile_label, list(order_ids)))
        return {oid: self.serials.get(oid, {}) for oid in order_ids}


@pytest.fixture(autouse=True)
def _pin_mail_settings(monkeypatch):
    """These tests must not inherit the developer's live config.json (settings freeze at import
    from the REAL file): the reply CC and the From address are pinned to fixture values."""
    import dataclasses

    monkeypatch.setattr(respond_bfmr, "settings",
                        dataclasses.replace(respond_bfmr.settings,
                                            bfmr_combined_package_reply_cc="",
                                            bfmr_combined_package_gmail_address="",
                                            bfmr_combined_package_gmail_app_password="",
                                            gmail_address="jane.fixture@example.com",
                                            gmail_app_password="fixture-pw"))


@pytest.fixture
def sent(monkeypatch):
    """Records every reply that would leave the machine (on top of conftest's global stub)."""
    calls = []
    monkeypatch.setattr("alerts.notifier.send_message",
                        lambda msg, recipients, account=None: calls.append((msg, recipients, account)))
    return calls


@pytest.fixture
def alerts(monkeypatch):
    calls = []
    monkeypatch.setattr("respond_bfmr.alert",
                        lambda subject, message: calls.append((subject, message)))
    return calls


def _run(mailbox, apply=False, serial_fetch=None, **kwargs):
    return respond_bfmr.run(
        apply=apply, mailbox=mailbox,
        fetch_pdf=lambda url: b"%PDF-1.4 fake receipt",
        fetch_serials=serial_fetch if serial_fetch is not None else FakeSerialFetch(),
        sheet_values=_sheet(), **kwargs)


def test_dry_run_sends_nothing_flags_nothing_fetches_nothing(sent, alerts, capsys):
    """A dry run must not spend the CDP fee either: the serial fetch is --apply-only, and the
    preview says so instead of showing serials it doesn't have."""
    fetch = FakeSerialFetch()
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=False, serial_fetch=fetch)

    assert outcome["skipped"] == [(TRACKING, "dry run")]
    assert sent == [] and alerts == [] and mailbox.answered == [] and fetch.calls == []
    assert load_state().get("bfmr_email_replies") is None
    out = capsys.readouterr().out
    assert TRACKING in out and "serials not shown" in out


def test_apply_fetches_serials_sends_marks_answered_and_records_state(sent, alerts):
    fetch = FakeSerialFetch()
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True, serial_fetch=fetch)

    assert outcome["replied"] == [TRACKING]
    assert fetch.calls == [("profile-a", ["BBY01-1"])]
    assert len(sent) == 1
    msg, recipients, account = sent[0]
    assert recipients == ["support@buyformeretail.com"]
    assert account == ("jane.fixture@example.com", "fixture-pw")  # the alerts account, by default
    assert msg["From"] == "jane.fixture@example.com"
    assert msg["In-Reply-To"] == MESSAGE_ID
    body = msg.get_body(("plain",)).get_content()
    assert "SERIAL01" in body and "SERIAL02" in body
    assert mailbox.answered == ["1"]
    assert load_state()["bfmr_email_replies"][MESSAGE_ID]["tracking"] == TRACKING
    assert alerts == []


def test_answered_message_is_never_refetched(sent):
    mailbox = FakeMailbox({"1": _email()})
    _run(mailbox, apply=True)

    outcome = _run(mailbox, apply=True)

    assert outcome == {"replied": [], "skipped": [], "needs_manual": [], "failed": []}
    assert len(sent) == 1  # only the first run's send


def test_state_record_blocks_resend_when_flag_write_failed(sent, alerts):
    """Sent before, but the \\Answered write never landed: the message still shows unanswered,
    and the .state.json record is what stops a second reply going out."""
    save_state({"bfmr_email_replies": {MESSAGE_ID: {"tracking": TRACKING, "sent_at": "x"}}})
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True)

    assert sent == []
    assert outcome["skipped"] == [(TRACKING, "already replied per .state.json")]
    assert mailbox.answered == ["1"]  # the flag is re-attempted so it stops resurfacing
    assert any("already sent" in s for s, _ in alerts)


def test_no_serials_found_leaves_message_unanswered_and_alerts_once(sent, alerts):
    """The site not showing serials for a shipped order is the expected first-run outcome —
    it must be LOUD and actionable, never a guess or a silent skip."""
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True, serial_fetch=FakeSerialFetch(serials={}))

    assert sent == [] and mailbox.answered == []
    assert len(outcome["needs_manual"]) == 1
    assert len(alerts) == 1
    subject, message = alerts[0]
    assert subject.startswith("ACTION NEEDED") and TRACKING in subject
    assert "no serial numbers found" in message and "--serials" in message


def test_serial_count_mismatch_blocks_the_reply(alerts):
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True,
                   serial_fetch=FakeSerialFetch(serials={"BBY01-1": {TRACKING: ["ONLYONE"]}}))

    assert len(outcome["needs_manual"]) == 1
    assert "1 serial(s) attributed" in alerts[0][1] and "2 unit(s)" in alerts[0][1]


def test_serial_fetch_failure_fails_loudly_and_stays_unanswered(sent, alerts):
    def boom(profile_label, order_ids):
        raise RuntimeError("Best Buy session is logged out")
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True, serial_fetch=boom)

    assert sent == [] and mailbox.answered == []
    assert len(outcome["failed"]) == 1
    assert any("Reading serials from Best Buy failed" in m for _s, m in alerts)


def test_a_non_pdf_receipt_fails_loudly_and_stays_unanswered(alerts):
    mailbox = FakeMailbox({"1": _email()})

    outcome = respond_bfmr.run(apply=True, mailbox=mailbox,
                               fetch_pdf=lambda url: b"<html>viewer chrome</html>",
                               fetch_serials=FakeSerialFetch(),
                               sheet_values=_sheet())

    assert len(outcome["failed"]) == 1 and mailbox.answered == []
    assert any("auto-reply failed" in s for s, _ in alerts)


def test_serials_override_skips_the_live_fetch(sent):
    fetch = FakeSerialFetch()
    mailbox = FakeMailbox({"1": _email()})

    outcome = _run(mailbox, apply=True, serial_fetch=fetch,
                   serials_override="OVR1, OVR2")

    assert outcome["replied"] == [TRACKING]
    assert fetch.calls == []  # the hand-given list replaced the browser session
    body = sent[0][0].get_body(("plain",)).get_content()
    assert "OVR1" in body and "OVR2" in body


def test_serials_override_refused_when_the_box_holds_two_orders():
    sheet = _sheet()
    sheet.append(_row(**{
        "Order ID": "BBY01-2", "Order Date": "2026-09-05", "Item Name": "MacBook Air",
        "Quantity": 1, "Tracking Number": TRACKING, "Status": "delivered",
        "Retailer": "Best Buy", "Profile": "profile-a",
        "Receipt Link": "https://objectstorage.example.com/PARSECRET/BBY01-2.pdf",
    }))
    mailbox = FakeMailbox({"1": _email()})

    with pytest.raises(SystemExit, match="2 order"):
        respond_bfmr.run(apply=False, mailbox=mailbox, serials_override="A,B",
                         fetch_pdf=lambda url: b"%PDF-1.4",
                         fetch_serials=FakeSerialFetch(), sheet_values=sheet)


def test_tracking_filter_selects_only_that_box(sent):
    other = _email(subject="Action Needed: Combined Best Buy Package 888800002222")
    mailbox = FakeMailbox({"1": _email(), "2": other})

    outcome = _run(mailbox, apply=True, tracking=TRACKING)

    assert outcome["replied"] == [TRACKING]
    assert mailbox.answered == ["1"]  # the other request was not touched


def test_disabled_master_switch_is_inert(monkeypatch):
    """main's hook mirrors run_buying_group_sync: switch off means respond_bfmr never runs.
    The flag is forced off here rather than trusting the ambient default — settings freeze at
    import from the developer's real config.json, where the flag may genuinely be on."""
    import dataclasses

    monkeypatch.setattr("respond_bfmr.run",
                        lambda **kwargs: pytest.fail("run() called with the switch off"))
    monkeypatch.setattr(main_module, "settings",
                        dataclasses.replace(main_module.settings,
                                            bfmr_combined_package_autoreply_enabled=False))

    main_module.run_bfmr_email_autoreply()


def test_enabled_master_switch_runs_with_apply(monkeypatch):
    import dataclasses

    calls = []
    monkeypatch.setattr("respond_bfmr.run", lambda apply: calls.append(apply))
    monkeypatch.setattr(main_module, "settings",
                        dataclasses.replace(main_module.settings,
                                            bfmr_combined_package_autoreply_enabled=True))

    main_module.run_bfmr_email_autoreply()

    assert calls == [True]


class TestReplyAccountChoice:
    """The mailbox is the alerts account UNLESS the feature's own pair is set — resolved in one
    place (settings.bfmr_reply_account) so IMAP and SMTP can never use different accounts."""

    @staticmethod
    def _settings(**overrides):
        import dataclasses
        return dataclasses.replace(respond_bfmr.settings, **overrides)

    def test_both_blank_falls_back_to_the_alerts_account(self):
        s = self._settings(gmail_address="alerts@example.com", gmail_app_password="alerts-pw",
                           bfmr_combined_package_gmail_address="",
                           bfmr_combined_package_gmail_app_password="")
        assert s.bfmr_reply_account() == ("alerts@example.com", "alerts-pw")

    def test_both_set_uses_the_separate_account(self):
        s = self._settings(gmail_address="alerts@example.com", gmail_app_password="alerts-pw",
                           bfmr_combined_package_gmail_address="replies@example.com",
                           bfmr_combined_package_gmail_app_password="replies-pw")
        assert s.bfmr_reply_account() == ("replies@example.com", "replies-pw")

    def test_half_a_pair_is_refused_never_mixed(self):
        s = self._settings(bfmr_combined_package_gmail_address="replies@example.com",
                           bfmr_combined_package_gmail_app_password="")
        with pytest.raises(RuntimeError, match="TOGETHER"):
            s.bfmr_reply_account()

    def test_a_configured_separate_account_reads_and_sends_as_itself(self, monkeypatch, sent):
        import dataclasses
        monkeypatch.setattr(respond_bfmr, "settings",
                            dataclasses.replace(respond_bfmr.settings,
                                                bfmr_combined_package_gmail_address="replies@example.com",
                                                bfmr_combined_package_gmail_app_password="replies-pw"))
        mailbox = FakeMailbox({"1": _email()})

        outcome = _run(mailbox, apply=True)

        assert outcome["replied"] == [TRACKING]
        msg, _recipients, account = sent[0]
        assert account == ("replies@example.com", "replies-pw")
        assert msg["From"] == "replies@example.com"
