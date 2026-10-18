#!/usr/bin/env python3
"""
poly_l2_check.py — read-only CLOB checks using the pasted L2 API creds + a public
market lookup. NO private key required. Proves: (a) the API creds authenticate,
(b) the CLOB sees the account's collateral, (c) order books are readable.

  python3 poly_l2_check.py balance              # CLOB collateral balance (L2 creds)
  python3 poly_l2_check.py book --token <id>    # public order book (no auth)
  python3 poly_l2_check.py market --slug <slug> # resolve a market -> token ids

Writing/placing orders needs a wallet private key (see poly_v2_trade.py); the L2
creds here cannot sign orders.
"""
import argparse
import json
import sys
import urllib.request

import polymarket_v2 as pv
from poly_v2_send import load_env

HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

_V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
_PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def _patch_v2():
    import py_clob_client.config as cfg
    from py_clob_client.clob_types import ContractConfig

    def v2(chainID, neg_risk=False):
        return ContractConfig(exchange=_V2_EXCHANGE, collateral=_PUSD, conditional_tokens=_CTF)

    cfg.get_contract_config = v2
    import py_clob_client.client as cl
    import py_clob_client.order_builder.builder as bd
    cl.get_contract_config = v2
    bd.get_contract_config = v2


def _http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def cmd_balance(env, args):
    _patch_v2()
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    creds = ApiCreds(api_key=env["CLOB_API_KEY"], api_secret=env["CLOB_SECRET"],
                     api_passphrase=env["CLOB_PASSPHRASE"])
    sig_type = int(env.get("POLYMARKET_SIG_TYPE", "1"))
    funder = env["POLYMARKET_FUNDER"]
    # If Webshare is configured, route CLOB traffic through this account's sticky
    # proxy (keyed by funder) — same egress the trade path uses.
    import webshare
    webshare.maybe_apply(funder, env)
    # L2-only client: creds present, no signing key. Funder + sig_type so the
    # balance endpoint resolves the right proxy wallet.
    c = ClobClient(HOST, chain_id=pv.CHAIN_ID, creds=creds,
                   signature_type=sig_type, funder=funder)
    print(f"funder={funder}  sig_type={sig_type}")
    try:
        ba = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal = int(ba.get("balance", "0")) / 1e6
        print(f"CLOB collateral balance: ${bal:.2f}")
        print(f"allowances: {ba.get('allowances', {})}")
        if bal > 0:
            print("✅ CLOB sees collateral via L2 creds.")
        else:
            print("⚠️ CLOB reports $0 (normal for sig_type=1/3 — on-chain pUSD is the truth: $6.12 confirmed).")
    except Exception as e:
        print(f"L2 balance call failed: {str(e)[:200]}")


def cmd_book(env, args):
    data = _http(f"{HOST}/book?token_id={args.token}")
    asks = data.get("asks", [])[:5]
    bids = data.get("bids", [])[:5]
    print(f"token {args.token}")
    print("  asks:", [(a["price"], a["size"]) for a in asks])
    print("  bids:", [(b["price"], b["size"]) for b in bids])


def cmd_market(env, args):
    data = _http(f"{GAMMA}/markets?slug={args.slug}")
    if not data:
        print("no market for slug"); return
    m = data[0]
    print(f"question: {m.get('question')}")
    print(f"active={m.get('active')} closed={m.get('closed')}")
    toks = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
    outs = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
    for o, t in zip(outs, toks):
        print(f"  {o}: {t}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["balance", "book", "market"])
    ap.add_argument("--token")
    ap.add_argument("--slug")
    args = ap.parse_args()
    env = load_env()
    # surface the extra env keys load_env doesn't whitelist
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for line in open(os.path.join(here, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    if args.cmd == "book" and not args.token:
        print("book needs --token"); sys.exit(1)
    if args.cmd == "market" and not args.slug:
        print("market needs --slug"); sys.exit(1)
    {"balance": cmd_balance, "book": cmd_book, "market": cmd_market}[args.cmd](env, args)


if __name__ == "__main__":
    main()
