// GET /api/rpc?account=<contract>&method=<view method>&args=<base64url JSON>
//
// Read-only NEAR testnet view-call proxy for The Commons observatory.
// Browsers calling the public RPC mirrors directly get multi-second
// answers and connections cut mid-body, so the observatory kept
// falling back to stale snapshot data. This function makes the same
// query from Vercel's network, tries the mirrors in order until one
// answers, and lets the CDN cache each answer for a few seconds so
// every visitor shares one fresh copy instead of hammering the
// mirrors once per browser. Writes are never proxied: wallet actions
// keep going through the wallet, reads only pass through here, and
// only calls to The Commons' own contracts are allowed.
const UPSTREAMS = [
  ["https://rpc.testnet.fastnear.com", 3500],
  ["https://rpc.testnet.near.org", 5000],
  ["https://archival-rpc.testnet.near.org", 5000],
];
const CONTRACTS = new Set([
  "commons.bindingvow1106.testnet",
  "commonsv2.bindingvow1106.testnet",
  "commonsv3.bindingvow1106.testnet",
  "commonsv4.bindingvow1106.testnet",
  "commonsv5.bindingvow1106.testnet",
  "commonsv6.bindingvow1106.testnet",
  "commonsv7.bindingvow1106.testnet",
]);

export default async function handler(req, res) {
  if (req.method !== "GET") {
    res.status(405).json({ error: "GET only" });
    return;
  }
  const account = String(req.query.account || "");
  const method = String(req.query.method || "");
  const argsParam = String(req.query.args || "");
  if (
    !CONTRACTS.has(account) ||
    !/^[a-z][a-z0-9_]*$/.test(method) ||
    !argsParam ||
    argsParam.length > 8192
  ) {
    res.status(400).json({ error: "bad request" });
    return;
  }
  let argsJson;
  try {
    argsJson = Buffer.from(
      argsParam.replace(/-/g, "+").replace(/_/g, "/"),
      "base64"
    ).toString("utf8");
    JSON.parse(argsJson);
  } catch (e) {
    res.status(400).json({ error: "bad args" });
    return;
  }
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: "1",
    method: "query",
    params: {
      request_type: "call_function",
      finality: "final",
      account_id: account,
      method_name: method,
      args_base64: Buffer.from(argsJson, "utf8").toString("base64"),
    },
  });
  let lastErr = null;
  for (const [url, timeoutMs] of UPSTREAMS) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        signal: ctl.signal,
      });
      const d = JSON.parse(await r.text());
      if (d && (d.result || d.error)) {
        res.setHeader(
          "Cache-Control",
          "public, s-maxage=6, stale-while-revalidate=30"
        );
        res.status(200).json(d);
        return;
      }
      lastErr = new Error("empty upstream response");
    } catch (e) {
      lastErr = e;
    } finally {
      clearTimeout(timer);
    }
  }
  res.setHeader("Cache-Control", "no-store");
  res.status(502).json({
    error: "all upstreams failed",
    detail: String((lastErr && lastErr.message) || lastErr),
  });
}
