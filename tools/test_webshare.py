"""
test_webshare.py — tests for the Webshare proxy integration.

Pure logic (URL build, redaction, selection) + the assignment store exercised
against a REAL temp JSON file on disk (no mock objects, per repo rule). The one
network test that hits the live Webshare API is auto-skipped unless
WEBSHARE_API_KEY is set, so the suite runs offline.
"""
import json
import os
import tempfile

import pytest

import webshare as ws


# ----------------------------- pure helpers ----------------------------- #

def test_build_proxy_url_api_shape():
    p = {"proxy_address": "1.2.3.4", "port": 5588, "username": "u", "password": "p"}
    assert ws.build_proxy_url(p) == "http://u:p@1.2.3.4:5588"


def test_build_proxy_url_store_shape_and_encoding():
    # persisted-assignment shape + a password needing percent-encoding
    p = {"host": "9.9.9.9", "port": 80, "username": "user", "password": "a:b@c"}
    assert ws.build_proxy_url(p) == "http://user:a%3Ab%40c@9.9.9.9:80"


def test_build_proxy_url_no_auth():
    assert ws.build_proxy_url({"host": "5.5.5.5", "port": 8080}) == "http://5.5.5.5:8080"


def test_build_proxy_url_missing_host_raises():
    with pytest.raises(ValueError):
        ws.build_proxy_url({"port": 80})


def test_redact_strips_credentials():
    assert ws.redact("http://u:secret@1.2.3.4:5588") == "http://1.2.3.4:5588"
    assert ws.redact("http://1.2.3.4:5588") == "http://1.2.3.4:5588"


def test_normalize_account_lowercases_hex_only():
    assert ws.normalize_account("0xABCdef") == "0xabcdef"
    assert ws.normalize_account("  0xABC ") == "0xabc"
    assert ws.normalize_account("MyAccount") == "MyAccount"


# ----------------------------- selection -------------------------------- #

PROXIES = [
    {"id": 1, "proxy_address": "1.1.1.1", "port": 1, "valid": True, "country_code": "US"},
    {"id": 2, "proxy_address": "2.2.2.2", "port": 2, "valid": False, "country_code": "CA"},
    {"id": 3, "proxy_address": "3.3.3.3", "port": 3, "valid": True, "country_code": "CA"},
    {"id": 4, "proxy_address": "4.4.4.4", "port": 4, "valid": True, "country_code": "CA"},
]


def test_select_prefers_valid_country_fresh():
    chosen = ws.select_proxy(PROXIES, prefer_country="CA", used_ids=set())
    assert chosen["id"] == 3  # first valid CA, not the invalid #2


def test_select_skips_used_when_fresh_available():
    chosen = ws.select_proxy(PROXIES, prefer_country="CA", used_ids={"3"})
    assert chosen["id"] == 4  # 3 is used, 4 is the next fresh valid CA


def test_select_falls_back_to_any_valid_when_country_exhausted():
    only_us = [PROXIES[0]]
    chosen = ws.select_proxy(only_us, prefer_country="CA", used_ids=set())
    assert chosen["id"] == 1  # no CA -> any valid


def test_select_none_when_empty():
    assert ws.select_proxy([], prefer_country="CA") is None


# ----------------------------- assignment store (real temp file) -------- #

@pytest.fixture()
def store_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)  # start absent so we test creation
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_load_missing_returns_empty(store_path):
    store = ws.load_assignments(store_path)
    assert store == {"version": 1, "assignments": {}}


def test_set_get_roundtrip_and_persistence(store_path):
    proxy = {"id": 42, "proxy_address": "7.7.7.7", "port": 700,
             "username": "u", "password": "p", "country_code": "CA"}
    rec = ws.set_assignment("0xABC", proxy, store_path)
    assert rec["proxy_id"] == "42" and rec["host"] == "7.7.7.7"

    # persisted to disk as real JSON, keyed by normalized account
    with open(store_path) as f:
        raw = json.load(f)
    assert "0xabc" in raw["assignments"]

    got = ws.get_assignment("0xabc", store_path)
    assert ws.build_proxy_url(got) == "http://u:p@7.7.7.7:700"


def test_assignment_is_sticky_across_resolution(store_path):
    proxy = {"id": 9, "proxy_address": "8.8.8.8", "port": 80,
             "username": "u", "password": "p", "country_code": "CA"}
    ws.set_assignment("0xDEAD", proxy, store_path)
    # no API key -> resolution must return the stored sticky proxy, not re-fetch
    url = ws.proxy_url_for("0xDEAD", env={}, path=store_path)
    assert url == "http://u:p@8.8.8.8:80"


def test_clear_assignment(store_path):
    ws.set_assignment("0xFEED", {"id": 1, "host": "1.1.1.1", "port": 1}, store_path)
    assert ws.clear_assignment("0xFEED", store_path) is True
    assert ws.get_assignment("0xFEED", store_path) is None
    assert ws.clear_assignment("0xFEED", store_path) is False


# ----------------------------- resolution fallbacks --------------------- #

def test_proxy_url_for_static_fallback(store_path):
    url = ws.proxy_url_for("0xNEW", env={"WEBSHARE_PROXY_URL": "http://u:p@h:1"}, path=store_path)
    assert url == "http://u:p@h:1"


def test_proxy_url_for_bare_hostport_fallback_gets_scheme(store_path):
    url = ws.proxy_url_for("0xNEW", env={"HTTPS_PROXY": "1.2.3.4:8080"}, path=store_path)
    assert url == "http://1.2.3.4:8080"


def test_proxy_url_for_none_when_unconfigured(store_path):
    assert ws.proxy_url_for("0xNEW", env={}, path=store_path) is None


def test_is_enabled_logic():
    assert ws.is_enabled({"WEBSHARE_API_KEY": "x"}) is True
    assert ws.is_enabled({"WEBSHARE_PROXY_URL": "http://h:1"}) is True
    assert ws.is_enabled({"WEBSHARE_API_KEY": "x", "WEBSHARE_ENABLED": "0"}) is False
    assert ws.is_enabled({}) is False


# ----------------------------- apply (real monkeypatch) ----------------- #

def test_apply_to_pyclob_swaps_httpx_singleton():
    pytest.importorskip("py_clob_client")
    import httpx
    import py_clob_client.http_helpers.helpers as helpers
    before = helpers._http_client
    applied = ws.apply_to_pyclob("http://u:p@1.2.3.4:5588")
    assert applied is True
    assert helpers._http_client is not before
    assert isinstance(helpers._http_client, httpx.Client)


def test_apply_to_pyclob_noop_on_empty():
    assert ws.apply_to_pyclob(None) is False
    assert ws.apply_to_pyclob("") is False


# ----------------------------- live API (gated) ------------------------- #

@pytest.mark.skipif(not os.environ.get("WEBSHARE_API_KEY"),
                    reason="set WEBSHARE_API_KEY to run the live Webshare API test")
def test_live_list_proxies():
    client = ws.WebshareClient(os.environ["WEBSHARE_API_KEY"])
    proxies = client.list_proxies(valid_only=True)
    assert isinstance(proxies, list)
    if proxies:
        p = proxies[0]
        assert p.get("proxy_address") and p.get("port")
        assert ws.build_proxy_url(p).startswith("http://")
