#!/usr/bin/env node
/*
 * v2order.js — place a Polymarket order on the live V2 exchange.
 *
 * Why this exists: the live CLOB migrated to V2 exchanges
 *   non-negRisk: 0xE111180000d2663C0091e4f400237545B87B996B
 *   negRisk    : 0xe2222d279d744050d28e00520010520000310F59
 * whose EIP-712 domain is {name:"Polymarket CTF Exchange", version:"2"} (verified
 * on-chain via eip712Domain()). Both published clients (py 0.34.6, ts 5.8.1) still
 * sign version "1" against the OLD exchange 0x4bFb -> CLOB returns
 * "invalid order version". We let the client build the order (correct amounts /
 * salt / fee), then RE-SIGN the identical struct with the V2 domain, swap the
 * signature, and POST through the client (so HMAC + body match exactly).
 *
 *   node v2order.js buy  --token <id> --price 0.16 --size 5 [--neg-risk] [--execute]
 *   node v2order.js sell --token <id> --price 0.16 --size 5 [--neg-risk] [--execute]
 */
const path = require("path");
require("dotenv").config({ path: path.join(__dirname, "..", "tools", ".env") });
const { ethers } = require("ethers");
const { ClobClient, Side, OrderType } = require("@polymarket/clob-client");

const HOST = "https://clob.polymarket.com";
const CHAIN = 137;
const V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B";
const V2_NEGRISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59";

const ORDER_TYPES = {
  Order: [
    { name: "salt", type: "uint256" },
    { name: "maker", type: "address" },
    { name: "signer", type: "address" },
    { name: "taker", type: "address" },
    { name: "tokenId", type: "uint256" },
    { name: "makerAmount", type: "uint256" },
    { name: "takerAmount", type: "uint256" },
    { name: "expiration", type: "uint256" },
    { name: "nonce", type: "uint256" },
    { name: "feeRateBps", type: "uint256" },
    { name: "side", type: "uint8" },
    { name: "signatureType", type: "uint8" },
  ],
};

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

async function main() {
  const side = process.argv[2];
  const o = parseArgs();
  const pk = process.env.POLYMARKET_PK;
  const funder = process.env.POLYMARKET_FUNDER;
  const sigType = parseInt(process.env.POLYMARKET_SIG_TYPE || "1", 10);
  const signer = new ethers.Wallet(pk);

  const l1 = new ClobClient(HOST, CHAIN, signer, undefined, sigType, funder);
  const creds = await l1.createOrDeriveApiKey();
  const c = new ClobClient(HOST, CHAIN, signer, creds, sigType, funder);

  // 1) let the client build the order (amounts, salt, fee, struct)
  const base = await c.createOrder(
    { tokenID: o.token, price: o.price, side: side === "buy" ? Side.BUY : Side.SELL, size: o.size },
    { tickSize: "0.001", negRisk: o.negRisk }
  );

  // 2) re-sign the identical struct with the V2 domain (version "2") + V2 exchange
  const verifyingContract = o.negRisk ? V2_NEGRISK_EXCHANGE : V2_EXCHANGE;
  const domain = { name: "Polymarket CTF Exchange", version: "2", chainId: CHAIN, verifyingContract };
  const message = {
    salt: base.salt, maker: base.maker, signer: base.signer, taker: base.taker,
    tokenId: base.tokenId, makerAmount: base.makerAmount, takerAmount: base.takerAmount,
    expiration: base.expiration, nonce: base.nonce, feeRateBps: base.feeRateBps,
    side: base.side, signatureType: base.signatureType,
  };
  const signature = await signer._signTypedData(domain, ORDER_TYPES, message);
  const signed = { ...base, signature };

  const notional = o.price * o.size;
  console.log(`${side.toUpperCase()} ${o.size} @ ${o.price} (notional $${notional.toFixed(2)})`);
  console.log(`  exchange=${verifyingContract} domain.version=2 feeRateBps=${base.feeRateBps}`);
  console.log(`  re-signed: ${signature.slice(0, 22)}…`);

  if (!o.execute) {
    console.log("  DRY-RUN: not posted. add --execute to POST.");
    return;
  }
  try {
    const resp = await c.postOrder(signed, OrderType.GTC);
    console.log("  CLOB RESPONSE:", JSON.stringify(resp));
  } catch (e) {
    console.log("  CLOB ERROR:", e.response ? JSON.stringify(e.response.data) : (e.message || e));
  }
}

main().catch((e) => { console.error("FATAL:", e.message || e); process.exit(1); });
