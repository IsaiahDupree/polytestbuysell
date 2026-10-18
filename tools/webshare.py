#!/usr/bin/env python3
"""
webshare.py — Webshare proxy integration for the Polymarket buy/sell harness.

WHY: Polymarket geoblocks CLOB ORDER PLACEMENT from US IPs (the README's final
blocker: `403 Trading restricted in your region`). Order submission must egress
through a non-US IP. Webshare gives us a pool of proxies behind one API; this
module turns that API into three concrete capabilities the harness needs:

  1. Fetch proxy list   — pull live proxies from the Webshare v2 API (no more
                          hand-pasting from a benchmark file).
  2. Assign per account — give each Polymarket account (keyed by its funder
                          address) a STICKY proxy, persisted to
                          ../data/proxy_assignments.json, so the same account
                          always egresses from the same IP. Sticky IPs avoid
                          tripping Polymarket's "account moved countries" checks.
  3. Usage / health     — read the account's plan + bandwidth from the API and
                          actively probe a proxy's exit IP / latency.

It then APPLIES the resolved proxy at the one seam that matters: py_clob_client
talks to clob.polymarket.com through a module-level `httpx.Client` singleton, so
`apply_to_pyclob()` swaps that singleton for one carrying `proxy=`. RPC and Gamma
calls (urllib in polymarket_v2 / poly_l2_check) are NOT geoblocked and are left
on the direct US route — proxying them would only waste residential bandwidth.

Design split (mirrors the rest of the repo): the parsing / selection / store
logic is PURE and unit-tested (test_webshare.py); only the WebshareClient and
the apply_* helpers touch the network / global import state.

Config (tools/.env, gitignored):
  WEBSHARE_API_KEY=...        # the API TOKEN from dashboard.webshare.io (NOT the
                              #   per-proxy username/password). Enables the API.
  WEBSHARE_ENABLED=1          # default on when a key/static proxy is present;
                              #   set 0 to force the direct route.
  WEBSHARE_COUNTRY=CA         # preferred exit country for auto-assignment.
  WEBSHARE_PROXY_URL=http://user:pass@host:port   # static fallback when no API
                              #   key is set (degrades to the polymarket-weather
                              #   hand-picked-proxy approach).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

API_BASE = "https://proxy.webshare.io/api/v2"
# Cheap, neutral exit-IP echo used for health probes (returns {"ip": "..."}).
IP_ECHO = "https://ipv4.webshare.io/"
IP_GEO = "http://ip-api.com/json/{ip}?fields=country,countryCode,query"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSIGN_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "proxy_assignments.json"))


# --------------------------------------------------------------------------- #
# Pure helpers (no network, no global state) — fully unit-testable
# --------------------------------------------------------------------------- #

def build_proxy_url(p: dict) -> str:
    """Webshare proxy record -> `http://user:pass@host:port`.

    Accepts both the API shape (proxy_address/port/username/password) and the
    persisted-assignment shape (host/port/username/password)."""
    host = p.get("proxy_address") or p.get("host")
    port = p.get("port")
    user = p.get("username")
    pw = p.get("password")
    if not (host and port):
        raise ValueError(f"proxy record missing host/port: {p!r}")
    auth = ""
    if user:
        auth = urllib.parse.quote(str(user), safe="")
        if pw is not None:
            auth += ":" + urllib.parse.quote(str(pw), safe="")
        auth += "@"
    return f"http://{auth}{host}:{port}"


def redact(url: str) -> str:
    """Drop `user:pass@` so a proxy URL is safe to print/log."""
    if not url:
        return url
    return urllib.parse.urlsplit(url)._replace(
        netloc=urllib.parse.urlsplit(url).netloc.split("@")[-1]
    ).geturl()


def normalize_account(account: str) -> str:
    """Account identity key. Funder addresses are case-insensitive hex."""
    a = (account or "").strip()
    return a.lower() if a.startswith("0x") else a


def select_proxy(proxies: list[dict], *, prefer_country: str | None = None,
                 used_ids: set | None = None) -> dict | None:
    """Pick one proxy from a list. Preference order:
      1. valid + matches prefer_country + not already used by another account
      2. valid + matches prefer_country
      3. valid (any country) + not already used
      4. valid (any country)
      5. anything
    Deterministic (first match) so repeated calls are stable."""
    used_ids = used_ids or set()
    cc = (prefer_country or "").upper() or None

    def valid(p):  # Webshare marks dead proxies valid=False
        return p.get("valid", True)

    def country_ok(p):
        return cc is None or str(p.get("country_code", "")).upper() == cc

    def fresh(p):
        return str(p.get("id")) not in used_ids

    for pred in (
        lambda p: valid(p) and country_ok(p) and fresh(p),
        lambda p: valid(p) and country_ok(p),
        lambda p: valid(p) and fresh(p),
        lambda p: valid(p),
        lambda p: True,
    ):
        for p in proxies:
            if pred(p):
                return p
    return None


# --------------------------------------------------------------------------- #
# Assignment store (real JSON file on disk — shared with js/webshare.js)
# --------------------------------------------------------------------------- #

def _empty_store() -> dict:
    return {"version": 1, "assignments": {}}


def load_assignments(path: str = _ASSIGN_PATH) -> dict:
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "assignments" not in data:
            return _empty_store()
        return data
    except (ValueError, OSError):
        return _empty_store()


def save_assignments(store: dict, path: str = _ASSIGN_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic


def get_assignment(account: str, path: str = _ASSIGN_PATH) -> dict | None:
    return load_assignments(path)["assignments"].get(normalize_account(account))


def set_assignment(account: str, proxy: dict, path: str = _ASSIGN_PATH) -> dict:
    """Persist a sticky proxy for an account. `proxy` is an API record; we store
    only what's needed to rebuild the URL + audit it."""
    store = load_assignments(path)
    rec = {
        "proxy_id": str(proxy.get("id", "")),
        "host": proxy.get("proxy_address") or proxy.get("host"),
        "port": proxy.get("port"),
        "username": proxy.get("username"),
        "password": proxy.get("password"),
        "country_code": proxy.get("country_code"),
        "assigned_at": int(time.time()),
    }
    store["assignments"][normalize_account(account)] = rec
    save_assignments(store, path)
    return rec


def clear_assignment(account: str, path: str = _ASSIGN_PATH) -> bool:
    store = load_assignments(path)
    if normalize_account(account) in store["assignments"]:
        del store["assignments"][normalize_account(account)]
        save_assignments(store, path)
        return True
    return False


# --------------------------------------------------------------------------- #
# Webshare API client (network)
# --------------------------------------------------------------------------- #

class WebshareError(RuntimeError):
    pass


class WebshareClient:
    def __init__(self, api_key: str | None = None, *, timeout: int = 20):
        self.api_key = api_key or os.environ.get("WEBSHARE_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise WebshareError("WEBSHARE_API_KEY not set (the API token from dashboard.webshare.io)")

    def _get(self, path: str, params: dict | None = None) -> dict:
        import requests  # local import: only needed when actually hitting the API
        url = f"{API_BASE}{path}"
        try:
            r = requests.get(url, headers={"Authorization": f"Token {self.api_key}"},
                             params=params or {}, timeout=self.timeout)
        except requests.RequestException as e:
            raise WebshareError(f"GET {path} failed: {e}") from e
        if r.status_code == 401:
            raise WebshareError("401 Unauthorized — bad WEBSHARE_API_KEY")
        if r.status_code != 200:
            raise WebshareError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def list_proxies(self, *, valid_only: bool = True, country_code: str | None = None,
                     mode: str = "direct", page_size: int = 100, max_pages: int = 20) -> list[dict]:
        """All proxies on the account, following pagination. `direct` mode gives
        per-proxy host:port + username:password (what we proxy through)."""
        out: list[dict] = []
        params = {"mode": mode, "page": 1, "page_size": page_size}
        if country_code:
            params["country_code__in"] = country_code.upper()
        for _ in range(max_pages):
            data = self._get("/proxy/list/", params)
            out.extend(data.get("results", []))
            if not data.get("next"):
                break
            params["page"] += 1
        if valid_only:
            out = [p for p in out if p.get("valid", True)]
        return out

    def config(self) -> dict:
        """Account-wide proxy config: rotating endpoint, counts, allowed IPs."""
        return self._get("/proxy/config/")

    def subscription(self) -> dict | None:
        """Plan + bandwidth limits. Endpoint name has shifted across API versions;
        try the known spellings and return the first that answers."""
        for path in ("/subscription/", "/subscription/plan/", "/payment/subscription/"):
            try:
                return self._get(path)
            except WebshareError:
                continue
        return None


# --------------------------------------------------------------------------- #
# Resolution: account -> proxy URL (store -> API auto-assign -> static fallback)
# --------------------------------------------------------------------------- #

def is_enabled(env: dict | None = None) -> bool:
    env = env if env is not None else os.environ
    if str(env.get("WEBSHARE_ENABLED", "")).strip() == "0":
        return False
    return bool(env.get("WEBSHARE_API_KEY") or env.get("WEBSHARE_PROXY_URL")
                or env.get("HTTPS_PROXY"))


def proxy_url_for(account: str, *, env: dict | None = None, auto_assign: bool = True,
                  path: str = _ASSIGN_PATH) -> str | None:
    """Resolve the proxy URL an account should egress through.

    Order: (1) sticky assignment on disk, (2) fetch list + auto-assign via the
    API, (3) static WEBSHARE_PROXY_URL / HTTPS_PROXY fallback. Returns None when
    nothing is configured (caller then uses the direct route)."""
    env = env if env is not None else os.environ

    rec = get_assignment(account, path)
    if rec and rec.get("host"):
        return build_proxy_url(rec)

    if auto_assign and env.get("WEBSHARE_API_KEY"):
        try:
            client = WebshareClient(env["WEBSHARE_API_KEY"])
            country = env.get("WEBSHARE_COUNTRY")
            proxies = client.list_proxies(valid_only=True, country_code=country)
            used = {a.get("proxy_id") for a in load_assignments(path)["assignments"].values()}
            chosen = select_proxy(proxies, prefer_country=country, used_ids=used)
            if chosen:
                rec = set_assignment(account, chosen, path)
                return build_proxy_url(rec)
        except WebshareError as e:
            print(f"[webshare] API auto-assign failed: {e}", file=sys.stderr)

    static = env.get("WEBSHARE_PROXY_URL") or env.get("HTTPS_PROXY")
    if static:
        return static if static.startswith("http") else f"http://{static}"
    return None


# --------------------------------------------------------------------------- #
# Apply: route py_clob_client's CLOB traffic through the proxy
# --------------------------------------------------------------------------- #

def apply_to_pyclob(proxy_url: str | None) -> bool:
    """Swap py_clob_client's module-level httpx singleton for one bound to the
    proxy. Idempotent. Returns True if a proxy was applied.

    py_clob_client/http_helpers/helpers.py keeps `_http_client = httpx.Client(
    http2=True)` and every CLOB request goes through it, so replacing that one
    object reroutes all CLOB calls without touching the SDK's call sites."""
    if not proxy_url:
        return False
    import httpx
    import py_clob_client.http_helpers.helpers as helpers
    helpers._http_client = httpx.Client(http2=True, proxy=proxy_url, timeout=30.0)
    return True


def maybe_apply(account: str, env: dict | None = None) -> str | None:
    """Convenience for the trade scripts: if Webshare is enabled, resolve the
    account's proxy, apply it to py_clob_client, log the (redacted) egress, and
    return the URL. No-op + None when disabled."""
    env = env if env is not None else os.environ
    if not is_enabled(env):
        return None
    url = proxy_url_for(account, env=env)
    if url and apply_to_pyclob(url):
        print(f"[webshare] CLOB egress via {redact(url)}")
        return url
    return None


# --------------------------------------------------------------------------- #
# Health probe (network)
# --------------------------------------------------------------------------- #

def health_check(proxy_url: str, *, timeout: int = 15) -> dict:
    """Probe a proxy: exit IP, country, round-trip ms. {ok, ip, country, ms,
    error}."""
    import requests
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    try:
        r = requests.get(IP_ECHO, proxies=proxies, timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        ip = (r.json().get("ip") if r.headers.get("content-type", "").startswith("application/json")
              else r.text.strip())
    except Exception as e:  # noqa: BLE001 — report any transport failure verbatim
        return {"ok": False, "ip": None, "country": None, "ms": None, "error": str(e)[:160]}
    country = None
    try:
        g = requests.get(IP_GEO.format(ip=ip), timeout=timeout).json()
        country = g.get("countryCode") or g.get("country")
    except Exception:  # noqa: BLE001 — geo is best-effort
        pass
    return {"ok": True, "ip": ip, "country": country, "ms": ms, "error": None}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_dotenv() -> dict:
    """Read tools/.env so the CLI works standalone, falling back to os.environ."""
    env = dict(os.environ)
    path = os.path.join(_HERE, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip()) if k.strip() in os.environ else env.__setitem__(k.strip(), v.strip())
    return env


def main():
    ap = argparse.ArgumentParser(description="Webshare proxy ops for the Polymarket harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list proxies from the Webshare API")
    p_list.add_argument("--country", help="filter by country code (e.g. CA)")
    p_list.add_argument("--all", action="store_true", help="include invalid proxies")

    p_assign = sub.add_parser("assign", help="assign a sticky proxy to an account")
    p_assign.add_argument("account", help="account key (funder address)")
    p_assign.add_argument("--proxy-id", help="force a specific proxy id")
    p_assign.add_argument("--country", help="preferred country code")

    sub.add_parser("assignments", help="show all account->proxy assignments")

    p_unassign = sub.add_parser("unassign", help="remove an account's assignment")
    p_unassign.add_argument("account")

    p_url = sub.add_parser("url", help="print the resolved proxy URL for an account")
    p_url.add_argument("account")
    p_url.add_argument("--raw", action="store_true", help="show credentials (default: redacted)")

    p_health = sub.add_parser("health", help="probe assigned proxies' exit IP/latency")
    p_health.add_argument("account", nargs="?", help="one account, or all if omitted")

    sub.add_parser("usage", help="account plan + proxy counts from the API")

    args = ap.parse_args()
    env = _load_dotenv()

    if args.cmd == "list":
        client = WebshareClient(env.get("WEBSHARE_API_KEY"))
        proxies = client.list_proxies(valid_only=not args.all, country_code=args.country)
        print(f"{len(proxies)} proxies"
              + (f" in {args.country.upper()}" if args.country else "")
              + (" (incl. invalid)" if args.all else " (valid)"))
        for p in proxies:
            flag = "ok " if p.get("valid", True) else "DEAD"
            print(f"  [{flag}] {p.get('id'):>10}  {p.get('proxy_address')}:{p.get('port')}  "
                  f"{p.get('country_code','??')}  {p.get('city_name','')}")

    elif args.cmd == "assign":
        client = WebshareClient(env.get("WEBSHARE_API_KEY"))
        country = args.country or env.get("WEBSHARE_COUNTRY")
        proxies = client.list_proxies(valid_only=True, country_code=country)
        if args.proxy_id:
            chosen = next((p for p in proxies if str(p.get("id")) == str(args.proxy_id)), None)
            if not chosen:
                print(f"proxy id {args.proxy_id} not found in valid list"); sys.exit(1)
        else:
            used = {a.get("proxy_id") for a in load_assignments()["assignments"].values()}
            chosen = select_proxy(proxies, prefer_country=country, used_ids=used)
        if not chosen:
            print("no proxy available to assign"); sys.exit(1)
        rec = set_assignment(args.account, chosen)
        print(f"assigned {normalize_account(args.account)} -> "
              f"{rec['host']}:{rec['port']} ({rec['country_code']}) id={rec['proxy_id']}")

    elif args.cmd == "assignments":
        store = load_assignments()
        if not store["assignments"]:
            print("no assignments yet"); return
        for acct, rec in store["assignments"].items():
            print(f"  {acct}  ->  {rec['host']}:{rec['port']}  "
                  f"{rec.get('country_code','??')}  id={rec.get('proxy_id')}")

    elif args.cmd == "unassign":
        print("removed" if clear_assignment(args.account) else "no assignment for that account")

    elif args.cmd == "url":
        url = proxy_url_for(args.account, env=env)
        if not url:
            print("(no proxy configured)"); return
        print(url if args.raw else redact(url))

    elif args.cmd == "health":
        store = load_assignments()
        targets = ([args.account] if args.account
                   else list(store["assignments"].keys()))
        if not targets:
            print("no assignments to probe"); return
        for acct in targets:
            url = proxy_url_for(acct, env=env, auto_assign=False)
            if not url:
                print(f"  {normalize_account(acct)}: (no proxy)"); continue
            h = health_check(url)
            if h["ok"]:
                print(f"  {normalize_account(acct)}: OK  ip={h['ip']}  "
                      f"country={h['country']}  {h['ms']}ms  via {redact(url)}")
            else:
                print(f"  {normalize_account(acct)}: FAIL  {h['error']}  via {redact(url)}")

    elif args.cmd == "usage":
        client = WebshareClient(env.get("WEBSHARE_API_KEY"))
        cfg = client.config()
        print("proxy config:")
        for k in ("proxy_count", "countries", "valid_proxies", "preset_distribution"):
            if k in cfg:
                print(f"  {k}: {cfg[k]}")
        sub_info = client.subscription()
        if sub_info:
            data = sub_info.get("results", [sub_info]) if isinstance(sub_info, dict) else sub_info
            print("subscription:")
            for item in (data if isinstance(data, list) else [data]):
                for k in ("plan", "proxy_count", "monthly_bandwidth", "bandwidth_used",
                          "automatic_refresh_frequency", "on_demand_refreshes_available"):
                    if isinstance(item, dict) and k in item:
                        print(f"  {k}: {item[k]}")


if __name__ == "__main__":
    main()
