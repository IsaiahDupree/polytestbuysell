#!/usr/bin/env node
/*
 * trade.js — Polymarket buy/sell via the OFFICIAL latest TS clob-client (5.8.1).
 *
 * The Python py_clob_client builds a V1 order the live exchange rejects with
 * "invalid order version". The TS client is the reference impl; this script uses
 * it natively (no monkeypatching) so order format is whatever Polymarket ships.
 *
 *   node trade.js check
 *   node trade.js book   --token <id>
 *   node trade.js buy    --token <id> --price 0.16 --size 5 [--neg-risk] [--execute]
 *   node trade.js sell   --token <id> --price 0.16 --size 5 [--neg-risk] [--execute]
 */
const path = require("path");
require("dotenv").config({ path: path.join(__dirname, "..", "tools", ".env") });
const { ethers } = require("ethers");
const { ClobClient, Side, OrderType, getContractConfig } = require("@polymarket/clob-client");

const HOST = "https://clob.polymarket.com";
const CHAIN = 137;

function parseArgs() {
  const a = process.argv.slice(3);
  const o = { execute: false, negRisk: false };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === "--token") o.token = a[++i];
    else if (a[i] === "--price") o.price = parseFloat(a[++i]);
    else if (a[i] === "--size") o.size = parseFloat(a[++i]);
    else if (a[i] === "--execute") o.execute = true;
    else if (a[i] === "--neg-risk") o.negRisk = true;
  }
  return o;
}

async function buildClient() {
  const pk = process.env.POLYMARKET_PK;
  const funder = process.env.POLYMARKET_FUNDER;
  const sigType = parseInt(process.env.POLYMARKET_SIG_TYPE || "1", 10);
  if (!pk) throw new Error("POLYMARKET_PK missing in tools/.env");
  const signer = new ethers.Wallet(pk);
  // L1 client (signer only) to derive creds
  const l1 = new ClobClient(HOST, CHAIN, signer, undefined, sigType, funder);
  const creds = await l1.createOrDeriveApiKey();
  const c = new ClobClient(HOST, CHAIN, signer, creds, sigType, funder);
  return { c, signer, funder, sigType, creds };
}

async function cmdCheck() {
  const { c, signer, funder, sigType, creds } = await buildClient();
  console.log("native config 137:", JSON.stringify(getContractConfig(CHAIN)));
  console.log("signer:", signer.address, "funder:", funder, "sigType:", sigType);
  console.log("derived api_key:", creds.key);
  const { AssetType } = require("@polymarket/clob-client");
  try {
    const ba = await c.getBalanceAllowance({ asset_type: AssetType.COLLATERAL });
    console.log("CLOB collateral balance/allowance:", JSON.stringify(ba));
  } catch (e) {
    console.log("getBalanceAllowance err:", e.message || e);
  }
}

async function cmdBook(o) {
  const { c } = await buildClient();
  const ob = await c.getOrderBook(o.token);
  console.log("asks:", (ob.asks || []).slice(-4));
  console.log("bids:", (ob.bids || []).slice(-4));
}

async function place(o, side) {
  const { c } = await buildClient();
  const order = await c.createOrder(
    { tokenID: o.token, price: o.price, side: side === "buy" ? Side.BUY : Side.SELL, size: o.size },
    { tickSize: "0.001", negRisk: o.negRisk }
  );
  const notional = o.price * o.size;
  console.log(`${side.toUpperCase()} ${o.size} @ ${o.price} (notional $${notional.toFixed(2)}) token ${o.token}`);
  console.log("signed order:", JSON.stringify(order.order || order));
  if (!o.execute) {
    console.log("DRY-RUN: not posted. add --execute to POST.");
    return;
  }
  try {
    const resp = await c.postOrder(order, OrderType.GTC);
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
    else console.log("usage: node trade.js check|book|buy|sell [...]");
  } catch (e) {
    console.error("FATAL:", e.message || e);
    process.exit(1);
  }
})();
