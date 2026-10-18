#!/usr/bin/env node
/*
 * trade_v2.js — Polymarket buy/sell via @polymarket/clob-client-v2 (post-V2 migration).
 *
 * Polymarket cut over to CLOB V2 on 2026-04-28. The legacy clob-client signs
 * order version "1" and the server rejects it with "invalid order version".
 * This V2 SDK polls /version and builds the correct order version automatically.
 *
 *   node trade_v2.js check
 *   node trade_v2.js book   --token <id>
 *   node trade_v2.js buy    --token <id> --price 0.16 --size 5 [--execute]
 *   node trade_v2.js sell   --token <id> --price 0.16 --size 5 [--execute]
 */
const path = require("path");
require("dotenv").config({ path: path.join(__dirname, "..", "tools", ".env") });
const { ethers } = require("ethers");
const { ClobClient, Side, OrderType, SignatureTypeV2, AssetType } = require("@polymarket/clob-client-v2");
const webshare = require("./webshare");

const HOST = "https://clob.polymarket.com";
const CHAIN = 137;

function parseArgs() {
  const a = process.argv.slice(3);
  const o = { execute: false };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === "--token") o.token = a[++i];
    else if (a[i] === "--price") o.price = parseFloat(a[++i]);
    else if (a[i] === "--size") o.size = parseFloat(a[++i]);
    else if (a[i] === "--execute") o.execute = true;
  }
  return o;
}

async function buildClient() {
  const pk = process.env.POLYMARKET_PK;
  const funder = process.env.POLYMARKET_FUNDER;
  const sigType = parseInt(process.env.POLYMARKET_SIG_TYPE || "1", 10);
  if (!pk) throw new Error("POLYMARKET_PK missing in tools/.env");
  // CLOB order placement is geoblocked from US IPs (see README). If Webshare is
  // configured, route this account's CLOB traffic through its sticky proxy
  // (account keyed by funder) before the SDK makes any request.
  await webshare.maybeApply(funder);
  const signer = new ethers.Wallet(pk);
  const builderCode = process.env.BUILDER_CODE;
  const l1 = new ClobClient({ host: HOST, chain: CHAIN, signer, signatureType: sigType, funderAddress: funder });
  const creds = await l1.createOrDeriveApiKey();
  const c = new ClobClient({
    host: HOST, chain: CHAIN, signer, creds, signatureType: sigType, funderAddress: funder,
    builderConfig: builderCode ? { builderCode } : undefined,
  });
  return { c, signer, funder, sigType, creds };
}

async function cmdCheck() {
  const { c, signer, funder, sigType, creds } = await buildClient();
  const version = await c.getVersion();
  console.log("CLOB version:", version, "| signer:", signer.address, "| funder:", funder, "| sigType:", sigType);
  console.log("derived api_key:", creds.key);
  try {
    const ba = await c.getBalanceAllowance({ asset_type: AssetType.COLLATERAL });
    console.log("balance/allowance:", JSON.stringify(ba));
  } catch (e) { console.log("balance err:", e.message || e); }
}

async function cmdBook(o) {
  const { c } = await buildClient();
  const ob = await c.getOrderBook(o.token);
  console.log("asks:", (ob.asks || []).slice(-4));
  console.log("bids:", (ob.bids || []).slice(-4));
}

async function place(o, side) {
  const { c } = await buildClient();
  const signed = await c.createOrder({ tokenID: o.token, price: o.price, side: side === "buy" ? Side.BUY : Side.SELL, size: o.size });
  const notional = o.price * o.size;
  console.log(`${side.toUpperCase()} ${o.size} @ ${o.price} (notional $${notional.toFixed(2)}) token ${o.token}`);
  console.log("signed order:", JSON.stringify(signed).slice(0, 400));
  if (!o.execute) { console.log("DRY-RUN: not posted. add --execute to POST."); return; }
  try {
    const resp = await c.postOrder(signed, OrderType.GTC);
    console.log("CLOB RESPONSE:", JSON.stringify(resp));
  } catch (e) {
    console.log("CLOB ERROR:", e.response ? JSON.stringify(e.response.data) : (e.message || e));
  }
}

(async () => {
  const cmd = process.argv[2];
  const o = parseArgs();
  try {
    if (cmd === "check") await cmdCheck();
    else if (cmd === "book") await cmdBook(o);
    else if (cmd === "buy") await place(o, "buy");
    else if (cmd === "sell") await place(o, "sell");
    else console.log("usage: node trade_v2.js check|book|buy|sell [...]");
  } catch (e) { console.error("FATAL:", e.message || e); process.exit(1); }
})();
