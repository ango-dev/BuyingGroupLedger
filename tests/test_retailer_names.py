"""One spelling for every retailer (models/retailers.py), the profile model taking
any spelling, the proxy switch, and the one-off that rewrites config.json."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models.profile import ProfileConfig  # noqa: E402
from models.retailers import KEYS, NAMES, key_of, name_of  # noqa: E402
from scripts.standardize_retailers import plan  # noqa: E402
from test_web_settings import client, config  # noqa: E402,F401


class TestTheOneSpelling:
    def test_any_spelling_gives_the_key_and_the_name(self):
        for spelling in ("Best Buy", "best buy", "bestbuy", "best-buy", "BESTBUY", " Best  Buy "):
            assert key_of(spelling) == "bestbuy" and name_of(spelling) == "Best Buy"
        assert key_of("Amazon Business") == "amazon-business" and name_of("amazon-business") == "Amazon Business"
        assert key_of("amazon_business") == "amazon-business"
        assert key_of("Amazn") == "amazn" and name_of("walmart") == "Walmart"  # unknown: visible, Title Cased
        assert set(KEYS) == set(NAMES) == {"amazon", "amazon-business", "bestbuy", "costco"}


class TestTheProfileModel:
    def test_retailers_and_auth_keys_load_from_any_spelling_as_keys(self):
        p = ProfileConfig(label="p", retailers=["Best Buy", "amazon-business", "bestbuy", "Costco"],
                          auth={"Best Buy": {"method": "password", "username": "u", "password": "x"}})
        assert p.retailers == ["bestbuy", "amazon-business", "costco"]
        assert list(p.auth) == ["bestbuy"]

    def test_a_switched_off_proxy_is_handed_to_nobody(self):
        p = ProfileConfig(label="p", proxy={"host": "h", "port": 1, "username": "u", "password": "pw", "enabled": False})
        assert p.proxy is None
        on = ProfileConfig(label="p", proxy={"host": "h", "port": 1})
        assert on.proxy is not None and on.proxy.enabled is True


class TestTheProxySwitch:
    def test_the_summary_switch_flips_the_proxy_in_place(self, client, config):
        from test_web_settings import config_value

        config(profiles=[{"label": "p1", "profile_id": "", "retailers": ["Costco"],
                          "proxy": {"host": "h", "port": 1, "username": "u", "password": "pw"}}])
        body = client.get("/settings").text
        assert 'hx-post="/settings/section/profiles/entry/0/proxy"' in body and ">Proxy On<" in body
        off = client.post("/settings/section/profiles/entry/0/proxy", headers={"HX-Request": "true"})
        assert off.status_code == 200 and off.text.lstrip().startswith('<details class="entry-card" data-key="profiles:p1"')  # that profile's card alone (2026-09-20)
        assert ">Proxy Off<" in off.text and "Proxy off for profile p1" in off.text
        assert config_value("profiles")[0]["proxy"] == {"host": "h", "port": 1, "username": "u", "password": "pw", "enabled": False}
        on = client.post("/settings/section/profiles/entry/0/proxy", headers={"HX-Request": "true"})
        assert ">Proxy On<" in on.text and "enabled" not in config_value("profiles")[0]["proxy"]
        # the card's own tick does the same on save
        client.post("/settings/section/profiles/entry/0", data={"label": "p1", "retailers": "Costco", "proxy_host": "h",
                                                                "proxy_port": "1", "proxy_username": "u", "proxy_password": "",
                                                                "proxy_form": "1"},  # the page's form, box unticked
                    follow_redirects=False)
        assert config_value("profiles")[0]["proxy"]["enabled"] is False
        assert 'name="proxy_form" value="1"' in client.get("/settings").text
        missing = client.post("/settings/section/profiles/entry/7/proxy", headers={"HX-Request": "true"})
        assert missing.status_code == 400


class TestTheOneOff:
    def test_the_plan_rewrites_every_spelling_by_name_and_nothing_else(self):
        config = {"profiles": [{"label": "a", "retailers": ["bestbuy", "amazon-business", "Best Buy"],
                                "auth": {"bestbuy": {"username": "u"}, "amazon": {"username": "v"}},
                                "proxy": {"host": "h", "password": "secret"}}],
                  "cards": [{"name": "c", "retailer_rates": {"Best Buy": "3%", "amazon": "5%"},
                             "caps": [{"retailers": ["amazon", "amazon-business"], "spend_limit": 1}]}]}
        changes = plan(config)
        assert config["profiles"][0]["retailers"] == ["Best Buy", "Amazon Business"]
        assert list(config["profiles"][0]["auth"]) == ["Best Buy", "Amazon"]
        assert config["profiles"][0]["proxy"]["password"] == "secret"
        assert config["cards"][0]["retailer_rates"] == {"Best Buy": "3%", "Amazon": "5%"}
        assert config["cards"][0]["caps"][0]["retailers"] == ["Amazon", "Amazon Business"]
        assert len(changes) == 4 and plan(config) == []  # a second pass finds nothing
