// GET /api/records/<sha256-hex>
// Auth: Authorization: Bearer <STORE_READ_TOKEN> (the write token is
// also accepted). Raw records are never served without a token: they
// stay non-public during a room's data exclusivity window.
// Backend: a private GitHub repo via the Contents API (the Vercel
// Blob store was suspended on 2026-11-09).

import { createHash, timingSafeEqual } from "node:crypto";

// Compare an Authorization header against a token in constant
// time: hash both sides first so timingSafeEqual never sees
// different lengths (it throws on them) and the comparison leaks
// nothing about where the two differ. A plain === leaks the
// comparison through timing.
function tokensEqual(header, token) {
  if (!header || !token) return false;
  const a = createHash("sha256").update(header).digest();
  const b = createHash("sha256").update(`Bearer ${token}`).digest();
  return timingSafeEqual(a, b);
}

const GITHUB_API_BASE = (process.env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
const GITHUB_OWNER = process.env.GITHUB_OWNER || "ILKokoron";
const GITHUB_REPO = process.env.GITHUB_REPO || "the-commons-records";
const GITHUB_BRANCH = process.env.GITHUB_BRANCH || "main";

export default async function handler(req, res) {
  if (req.method !== "GET") {
    res.status(405).json({ ok: false, error: "GET only" });
    return;
  }
  const header = req.headers.authorization || "";
  const readOk = tokensEqual(header, process.env.STORE_READ_TOKEN);
  const writeOk = tokensEqual(header, process.env.STORE_WRITE_TOKEN);
  if (!readOk && !writeOk) {
    res.status(401).json({ ok: false, error: "bad read token" });
    return;
  }
  const id = req.query.id;
  if (!/^[0-9a-f]{64}$/.test(id || "")) {
    res.status(400).json({ ok: false, error: "id must be a sha256 hex string" });
    return;
  }
  if (!process.env.GITHUB_TOKEN) {
    res.status(502).json({ ok: false, error: "github backend not configured" });
    return;
  }
  const url = `${GITHUB_API_BASE}/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/records/${id}.json?ref=${encodeURIComponent(GITHUB_BRANCH)}`;
  try {
    const gh = await fetch(url, {
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "the-commons-store",
      },
    });
    if (gh.status === 404) {
      res.status(404).json({ ok: false, error: "record not found" });
      return;
    }
    if (gh.status !== 200) {
      res.status(502).json({ ok: false, error: `github backend error (status ${gh.status})` });
      return;
    }
    const buf = Buffer.from(await gh.arrayBuffer());
    res.status(200).setHeader("content-type", "application/json").send(buf);
  } catch {
    res.status(502).json({ ok: false, error: "github backend unreachable" });
  }
}
