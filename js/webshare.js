#!/usr/bin/env node
/*
 * webshare.js — Webshare proxy integration for the JS (clob-client-v2) trade path.
 *
 * Mirror of tools/webshare.py for the TypeScript SDK side, and it SHARES the same
 * sticky-assignment file (../data/proxy_assignments.json) so an account egresses
 * through the same IP whether you trade via Python or Node.
 *
 * The geoblock that blocks CLOB order placement (README: "403 Trading restricted
 * in your region") is the reason this exists. @polymarket/clob-client-v2 issues
 * every request through the global `axios.default(...)` with no `proxy:false`, so
 * applyToAxios() sets `axios.defaults.httpsAgent` (an https-proxy-agent) on that
 * same axios module — every CLOB call then tunnels through the proxy. We resolve
 * axios/https-proxy-agent from THIS package's node_modules (the same copy the SDK
 * loads) so the defaults we set are the ones the SDK reads.
 *
 * Config: see tools/.env (WEBSHARE_API_KEY, WEBSHARE_ENABLED, WEBSHARE_COUNTRY,
 * WEBSHARE_PROXY_URL). Same keys as the Python module.
 */
const fs = require("fs");
const path = require("path");
const http = require("http");
const https = require("https");

const API_BASE = "https://proxy.webshare.io/api/v2";
const IP_ECHO = "https://ipv4.webshare.io/";
const ASSIGN_PATH = path.join(__dirname, "..", "data", "proxy_assignments.json");

// ----------------------------- pure helpers ----------------------------- //

function buildProxyUrl(p) {
  const host = p.proxy_address || p.host;
  const port = p.port;
  if (!host || !port) throw new Error(`proxy record missing host/port: ${JSON.stringify(p)}`);
  let auth = "";
  if (p.username) {
    auth = encodeURIComponent(String(p.username));
    if (p.password != null) auth += ":" + encodeURIComponent(String(p.password));
    auth += "@";
  }
  return `http://${auth}${host}:${port}`;
}

function redact(url) {
  if (!url) return url;
  return url.replace(/^(https?:\/\/)[^@]*@/, "$1");
}

function normalizeAccount(account) {
  const a = (account || "").trim();
  return a.startsWith("0x") ? a.toLowerCase() : a;
}

/** Deterministic pick: valid+country+fresh, then relax constraints in order. */
function selectProxy(proxies, { preferCountry = null, usedIds = new Set() } = {}) {
  const cc = preferCountry ? preferCountry.toUpperCase() : null;
  const valid = (p) => p.valid !== false;
  const countryOk = (p) => !cc || String(p.country_code || "").toUpperCase() === cc;
  const fresh = (p) => !usedIds.has(String(p.id));
  const tiers = [
    (p) => valid(p) && countryOk(p) && fresh(p),
    (p) => valid(p) && countryOk(p),
    (p) => valid(p) && fresh(p),
    (p) => valid(p),
    () => true,
  ];
  for (const pred of tiers) {
    const hit = proxies.find(pred);
    if (hit) return hit;
  }
  return null;
}

// ----------------------------- assignment store ------------------------- //

function emptyStore() { return { version: 1, assignments: {} }; }

function loadAssignments(p = ASSIGN_PATH) {
  if (!fs.existsSync(p)) return emptyStore();
  try {
    const data = JSON.parse(fs.readFileSync(p, "utf8"));
    if (!data || typeof data !== "object" || !data.assignments) return emptyStore();
    return data;
  } catch {
    return emptyStore();
  }
}

function saveAssignments(store, p = ASSIGN_PATH) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
  fs.renameSync(tmp, p);
}

function getAssignment(account, p = ASSIGN_PATH) {
  return loadAssignments(p).assignments[normalizeAccount(account)] || null;
}

function setAssignment(account, proxy, p = ASSIGN_PATH) {
  const store = loadAssignments(p);
  const rec = {
    assigned_at: Math.floor(Date.now() / 1000),
    country_code: proxy.country_code,
    host: proxy.proxy_address || proxy.host,
    password: proxy.password,
    port: proxy.port,
    proxy_id: String(proxy.id != null ? proxy.id : ""),
    username: proxy.username,
  };
  store.assignments[normalizeAccount(account)] = rec;
  saveAssignments(store, p);
  return rec;
}

function clearAssignment(account, p = ASSIGN_PATH) {
  const store = loadAssignments(p);
  const k = normalizeAccount(account);
  if (store.assignments[k]) { delete store.assignments[k]; saveAssignments(store, p); return true; }
  return false;
}

// ----------------------------- API client ------------------------------- //

const axios = require("axios");

class WebshareError extends Error {}

class WebshareClient {
  constructor(apiKey = process.env.WEBSHARE_API_KEY, { timeout = 20000 } = {}) {
    if (!apiKey) throw new WebshareError("WEBSHARE_API_KEY not set (the API token from dashboard.webshare.io)");
    this.apiKey = apiKey;
    this.timeout = timeout;
  }

  async _get(p, params = {}) {
    try {
      const r = await axios.get(`${API_BASE}${p}`, {
        headers: { Authorization: `Token ${this.apiKey}` },
        params,
        timeout: this.timeout,
        proxy: false, // the API itself is reached directly, never through a proxy
      });
      return r.data;
    } catch (e) {
      const status = e.response && e.response.status;
      if (status === 401) throw new WebshareError("401 Unauthorized — bad WEBSHARE_API_KEY");
      throw new WebshareError(`GET ${p} failed: ${status || e.message}`);
    }
  }

  async listProxies({ validOnly = true, countryCode = null, mode = "direct", pageSize = 100, maxPages = 20 } = {}) {
    let out = [];
    const params = { mode, page: 1, page_size: pageSize };
    if (countryCode) params.country_code__in = countryCode.toUpperCase();
    for (let i = 0; i < maxPages; i++) {
      const data = await this._get("/proxy/list/", params);
      out = out.concat(data.results || []);
      if (!data.next) break;
      params.page += 1;
    }
    return validOnly ? out.filter((p) => p.valid !== false) : out;
  }

  config() { return this._get("/proxy/config/"); }

  async subscription() {
    for (const p of ["/subscription/", "/subscription/plan/", "/payment/subscription/"]) {
      try { return await this._get(p); } catch { /* try next spelling */ }
    }
    return null;
  }
}

// ----------------------------- resolution ------------------------------- //

function isEnabled(env = process.env) {
  if (String(env.WEBSHARE_ENABLED || "").trim() === "0") return false;
  return Boolean(env.WEBSHARE_API_KEY || env.WEBSHARE_PROXY_URL || env.HTTPS_PROXY);
}

async function proxyUrlFor(account, { env = process.env, autoAssign = true, p = ASSIGN_PATH } = {}) {
  const rec = getAssignment(account, p);
  if (rec && rec.host) return buildProxyUrl(rec);

  if (autoAssign && env.WEBSHARE_API_KEY) {
    try {
      const client = new WebshareClient(env.WEBSHARE_API_KEY);
      const country = env.WEBSHARE_COUNTRY;
      const proxies = await client.listProxies({ validOnly: true, countryCode: country });
      const usedIds = new Set(Object.values(loadAssignments(p).assignments).map((a) => a.proxy_id));
      const chosen = selectProxy(proxies, { preferCountry: country, usedIds });
      if (chosen) return buildProxyUrl(setAssignment(account, chosen, p));
    } catch (e) {
      process.stderr.write(`[webshare] API auto-assign failed: ${e.message}\n`);
    }
  }

  const staticUrl = env.WEBSHARE_PROXY_URL || env.HTTPS_PROXY;
  if (staticUrl) return staticUrl.startsWith("http") ? staticUrl : `http://${staticUrl}`;
  return null;
}

// ----------------------------- apply ------------------------------------ //

/** Route the SDK's axios traffic through `proxyUrl`. Sets httpsAgent/httpAgent
 *  on the shared axios defaults and disables axios's own env-proxy handling so
 *  the agent is authoritative. Idempotent. Returns true when applied. */
function applyToAxios(proxyUrl) {
  if (!proxyUrl) return false;
  const { HttpsProxyAgent } = require("https-proxy-agent");
  const agent = new HttpsProxyAgent(proxyUrl);
  axios.defaults.httpsAgent = agent;
  axios.defaults.httpAgent = agent;
  axios.defaults.proxy = false;
  return true;
}

/** Trade-path convenience: if enabled, resolve + apply + log, return the URL. */
async function maybeApply(account, env = process.env) {
  if (!isEnabled(env)) return null;
  const url = await proxyUrlFor(account, { env });
  if (url && applyToAxios(url)) {
    console.log(`[webshare] CLOB egress via ${redact(url)}`);
    return url;
  }
  return null;
}

// ----------------------------- health probe ----------------------------- //

function healthCheck(proxyUrl, { timeout = 15000 } = {}) {
  return new Promise((resolve) => {
    let agent;
    try {
      const { HttpsProxyAgent } = require("https-proxy-agent");
      agent = new HttpsProxyAgent(proxyUrl);
    } catch (e) {
      return resolve({ ok: false, error: e.message });
    }
    const t0 = Date.now();
    const lib = IP_ECHO.startsWith("https") ? https : http;
    const req = lib.get(IP_ECHO, { agent, timeout }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        const ms = Date.now() - t0;
        let ip = body.trim();
        try { ip = JSON.parse(body).ip || ip; } catch { /* plain text */ }
        resolve({ ok: true, ip, ms, error: null });
      });
    });
    req.on("timeout", () => { req.destroy(); resolve({ ok: false, error: "timeout" }); });
    req.on("error", (e) => resolve({ ok: false, error: e.message }));
  });
}

module.exports = {
  buildProxyUrl, redact, normalizeAccount, selectProxy,
  loadAssignments, saveAssignments, getAssignment, setAssignment, clearAssignment,
  WebshareClient, WebshareError, isEnabled, proxyUrlFor,
  applyToAxios, maybeApply, healthCheck, ASSIGN_PATH,
};

// ----------------------------- CLI -------------------------------------- //

async function cli() {
  require("dotenv").config({ path: path.join(__dirname, "..", "tools", ".env") });
  const [cmd, ...rest] = process.argv.slice(2);
  const arg = (flag) => { const i = rest.indexOf(flag); return i >= 0 ? rest[i + 1] : undefined; };
  const has = (flag) => rest.includes(flag);
  const positional = rest.find((r) => !r.startsWith("--"));

  if (cmd === "list") {
    const client = new WebshareClient();
    const proxies = await client.listProxies({ validOnly: !has("--all"), countryCode: arg("--country") });
    console.log(`${proxies.length} proxies${has("--all") ? " (incl. invalid)" : " (valid)"}`);
    for (const p of proxies) {
      console.log(`  [${p.valid === false ? "DEAD" : "ok  "}] ${String(p.id).padStart(10)}  ${p.proxy_address}:${p.port}  ${p.country_code || "??"}  ${p.city_name || ""}`);
    }
  } else if (cmd === "assign") {
    const client = new WebshareClient();
    const country = arg("--country") || process.env.WEBSHARE_COUNTRY;
    const proxies = await client.listProxies({ validOnly: true, countryCode: country });
    let chosen;
    if (arg("--proxy-id")) chosen = proxies.find((p) => String(p.id) === String(arg("--proxy-id")));
    else {
      const usedIds = new Set(Object.values(loadAssignments().assignments).map((a) => a.proxy_id));
      chosen = selectProxy(proxies, { preferCountry: country, usedIds });
    }
    if (!chosen) { console.error("no proxy available to assign"); process.exit(1); }
    const rec = setAssignment(positional, chosen);
    console.log(`assigned ${normalizeAccount(positional)} -> ${rec.host}:${rec.port} (${rec.country_code}) id=${rec.proxy_id}`);
  } else if (cmd === "assignments") {
    const store = loadAssignments();
    const keys = Object.keys(store.assignments);
    if (!keys.length) return console.log("no assignments yet");
    for (const k of keys) {
      const r = store.assignments[k];
      console.log(`  ${k}  ->  ${r.host}:${r.port}  ${r.country_code || "??"}  id=${r.proxy_id}`);
    }
  } else if (cmd === "unassign") {
    console.log(clearAssignment(positional) ? "removed" : "no assignment for that account");
  } else if (cmd === "url") {
    const url = await proxyUrlFor(positional);
    console.log(url ? (has("--raw") ? url : redact(url)) : "(no proxy configured)");
  } else if (cmd === "health") {
    const store = loadAssignments();
    const targets = positional ? [positional] : Object.keys(store.assignments);
    if (!targets.length) return console.log("no assignments to probe");
    for (const acct of targets) {
      const url = await proxyUrlFor(acct, { autoAssign: false });
      if (!url) { console.log(`  ${normalizeAccount(acct)}: (no proxy)`); continue; }
      const h = await healthCheck(url);
      console.log(h.ok
        ? `  ${normalizeAccount(acct)}: OK  ip=${h.ip}  ${h.ms}ms  via ${redact(url)}`
        : `  ${normalizeAccount(acct)}: FAIL  ${h.error}  via ${redact(url)}`);
    }
  } else if (cmd === "usage") {
    const client = new WebshareClient();
    const cfg = await client.config();
    console.log("proxy config:");
    for (const k of ["proxy_count", "countries", "valid_proxies"]) if (k in cfg) console.log(`  ${k}: ${JSON.stringify(cfg[k])}`);
    const sub = await client.subscription();
    if (sub) console.log("subscription:", JSON.stringify(sub).slice(0, 400));
  } else {
    console.log("usage: node webshare.js list|assign|assignments|unassign|url|health|usage [account] [--country CC] [--proxy-id ID] [--all] [--raw]");
  }
}

if (require.main === module) cli().catch((e) => { console.error("FATAL:", e.message); process.exit(1); });
