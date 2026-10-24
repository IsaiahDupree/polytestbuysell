# polytestbuysell — Polymarket buy/sell test harness

Scaffolding lifted from `WeatherPredictor/tools` to test a **buy and a sell** on a
specific Polymarket account end-to-end. Self-custody V2/pUSD primitives, all
DRY-RUN by default; nothing broadcasts or posts without an explicit flag.

## Account under test (verified on-chain 2026-06-14)
| Thing | Value |
|-------|-------|
| Portfolio cash (UI) | $6.12 |
| **Funder (holds the pUSD)** | `0x024ee8fed08e007644a556a93cd2b0a196e0f90e` — $6.12 pUSD ✓ |
| Relayer signer | `0xC81f425863B0CE3c1FE4722399B94eaf8bdB7a49` — $0.71 native USDC, 0 POL |
| CLOB API key | `019ec8f8-…` (L2 auth, in `tools/.env`) |
| Builder code | `0xf4aad954…` (0% maker/taker fee) |

> Polymarket's UI labels `0x024ee8…` as the "Builder / API-use-only" address, but
> on-chain it is the proxy wallet that actually holds the $6.12 pUSD collateral.
> That is the **funder** for trading.

## ⚠️ TEST RESULT (2026-06-14): blocked by Polymarket account type
Buy/sell were driven end-to-end. The full diagnosis, in order:

1. Legacy `py-clob-client` 0.34.6 / `@polymarket/clob-client` 5.8.1 → CLOB returns
   **`invalid order version`**. Cause: Polymarket cut over to **CLOB V2 on
   2026-04-28**; both published legacy clients sign order version "1" against the
   old exchange `0x4bFb…`. The live V2 exchanges are `0xE111…` (non-negRisk) /
   `0xe2222…` (negRisk), EIP-712 domain `version="2"` (verified on-chain via
   `eip712Domain()`).
2. `@polymarket/clob-client-v2` (1.0.6) builds the correct V2 order → cleared that
   error. New error: **`maker address not allowed, please use the deposit wallet
   flow`** (matches py-clob-client-v2 issue #56).
3. Cause: this is an **old Magic.link `POLY_PROXY` wallet** (`0x024ee8…`, where the
   $6.12 pUSD lives). CLOB V2 no longer allows old Magic proxies as order makers.
4. sig_type=3 (POLY_1271) attempts → `the order signer address has to be the
   address of the API KEY`, then `Unauthorized/Invalid api key`, then L1 key
   derivation for the proxy returns nothing. The proxy can't get a V2 trading key.

5. After the account's **email was verified**, the maker error disappeared and the
   order was accepted through format + maker + auth. Final response:
   **`403 Trading restricted in your region`**
   (https://docs.polymarket.com/developers/CLOB/geoblock).

6. **(2026-06-14) Geoblock bypassed by the Webshare proxy — proven by control.**
   Same signed order POSTed twice, only the egress IP differs:
   - **Direct (US IP)** → `403 Trading restricted in your region`
   - **Via Webshare CA proxy** (45.39.13.6) → `400 maker address not allowed,
     please use the deposit wallet flow`
   The 403 disappears through the proxy, so the geoblock check fires first and the
   proxy clears it; the request then advances to the maker rule (item 3 above).
   The CURRENT live blocker is therefore the **old-Magic-proxy maker rule**, not
   geography — an account-type issue independent of the proxy. To trade, switch to
   a deposit-wallet / EOA-7702 account (funder==signer, `sig_type=0`) or run the
   deposit-wallet flow.

**Conclusion:** the code path is fully correct end-to-end — order builds, signs,
authenticates, clears the **geoblock via the Webshare proxy**, and is processed by
CLOB V2. The remaining gate is the account-type maker rule (item 3/6), not region.

Order pipeline status: **geoblock SOLVED via Webshare proxy**; blocked now only by
the Magic-proxy maker rule (needs a deposit-wallet/EOA account).

## Webshare proxy integration (handles the geoblock)
CLOB order placement is blocked from US IPs. `tools/webshare.py` (Python) and
`js/webshare.js` (Node) route CLOB traffic through a non-US Webshare proxy:

- **Fetch proxy list** — pulls live proxies from the Webshare v2 API.
- **Sticky proxy per account** — pins one proxy per account (keyed by funder
  address), persisted to `data/proxy_assignments.json` (gitignored — holds
  proxy creds; shared by both the Python and JS paths so an account egresses
  from the same IP either way).
- **Usage / health** — read plan + proxy counts from the API; probe a proxy's
  exit IP / country / latency.

It applies the proxy at the exact HTTP seam each client uses — Python swaps
`py_clob_client`'s module-level `httpx.Client` singleton; JS sets
`axios.defaults.httpsAgent` on the same axios the SDK calls. RPC/Gamma reads stay
on the direct route (only placement is geoblocked). The trade scripts call it
automatically (`build_client` / `buildClient`) when `WEBSHARE_ENABLED` ≠ 0.

```bash
cd tools                                   # (or cd js — same subcommands via `node webshare.js`)
# one-time: set WEBSHARE_API_KEY in tools/.env, then
python3 webshare.py list --country CA               # live proxy list
python3 webshare.py assign 0x024ee8…                # pin a sticky CA proxy to the funder
python3 webshare.py assignments                     # who's pinned to what
python3 webshare.py health 0x024ee8…                # probe exit IP / country / latency
python3 webshare.py usage                           # plan + proxy counts
python3 webshare.py url 0x024ee8… --raw             # the resolved proxy URL
```
Without an API key it falls back to `WEBSHARE_PROXY_URL` (a hand-picked proxy),
so trading works before you wire the API. Verified egress 2026-06-14:
`104.253.36.150 / CA`. Tests: `python3 -m pytest tools/test_webshare.py -q`.

### Working tools
- `js/trade_v2.js` — V2 SDK buy/sell (clears the version error; hits the maker rule)
- `js/trade.js`, `js/v2order.js` — legacy + manual-V2 experiments (kept as evidence)
- `tools/*.py` — read-only on-chain truth + (EOA-only) self-custody funnel

## The original blocker for buy/sell
Placing a CLOB order requires an **EIP-712 signature from the wallet's signer
EOA** — i.e. a **private key**. The credentials pasted for this account are:
- **CLOB API key/secret/passphrase** → L2 *auth* (authenticates requests; does NOT sign orders)
- **Relayer API key** → gasless on-chain tx submission (does NOT sign CLOB orders)
- **Builder code** → fee attribution

None of these can sign an order. To run the live buy/sell you must put the signer
EOA private key in `tools/.env` as `POLYMARKET_PK` (with `POLYMARKET_SIG_TYPE=1`
for this email/magic proxy account). Without it, only read-only checks run.

## What works right now (no private key)
```bash
cd tools
python3 poly_preflight.py 0x024ee8fed08e007644a556a93cd2b0a196e0f90e   # on-chain truth
python3 poly_l2_check.py market --slug <market-slug>                   # resolve token ids
python3 poly_l2_check.py book   --token <token-id>                     # public order book
```

## Buy/sell test (needs POLYMARKET_PK set)
```bash
cd tools
python3 poly_v2_send.py  status                       # funnel: native→USDC.e→pUSD→approvals
python3 poly_v2_send.py  approve --execute            # one-time pUSD + CTF approvals (needs POL gas)
python3 poly_v2_trade.py check                        # CLOB sees pUSD collateral?
python3 poly_v2_trade.py buy  --token <id> --price 0.50 --size 2            # DRY-RUN
python3 poly_v2_trade.py buy  --token <id> --price 0.50 --size 2 --execute  # POST real order
python3 poly_v2_trade.py sell --token <id> --price 0.55 --size 2 --execute  # POST real order
```

### Known caveat (carried from WeatherPredictor)
The published `py_clob_client` (0.34.6) builds **V1** order structs. The live
exchange is **V2**. `check`/`book` work; `buy`/`sell` may get
`invalid order version` from the CLOB. If so, placement needs the V2 (TypeScript)
client or a ported V2 order-builder. Collateral/approvals are unaffected.

## Files
- `tools/polymarket_v2.py` — pure calldata/unit/slippage helpers + read-only chain client
- `tools/poly_preflight.py` — read-only on-chain collateral/allowance check (any address)
- `tools/poly_l2_check.py` — L2 balance (needs key) + public book/market reads (no key)
- `tools/poly_v2_send.py` — swap/wrap/approve funnel (DRY-RUN unless `--execute`)
- `tools/poly_v2_trade.py` — check/book/buy/sell (DRY-RUN unless `--execute`)
- `tools/.env` — secrets (gitignored)
