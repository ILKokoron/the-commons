import { createHash } from "node:crypto";

// POST /api/records
// Body: the canonical reasoning record bytes (a JSON object).
// Auth: Authorization: Bearer <STORE_WRITE_TOKEN>.
// The record is stored under its own sha256, so the returned content
// id is self-verifying: it must equal the onchain reasoning_hash.
// Backend: a private GitHub repo via the Contents API (the Vercel
// Blob store was suspended on 2026-11-09). Records are immutable and
// content-addressed, so an existing id is a success, never an
// overwrite.

const GITHUB_API_BASE = (process.env.GITHUB_API_BASE || "https://api.github.com").replace(/\/+$/, "");
const GITHUB_OWNER = process.env.GITHUB_OWNER || "ILKokoron";
const GITHUB_REPO = process.env.GITHUB_REPO || "the-commons-records";
const GITHUB_BRANCH = process.env.GITHUB_BRANCH || "main";

function contentsPath(id) {
  return `${GITHUB_API_BASE}/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/records/${id}.json`;
}

function githubHeaders(extra = {}) {
  return {
    Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "the-commons-store",
    ...extra,
  };
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ ok: false, error: "POST only" });
    return;
  }
  const token = process.env.STORE_WRITE_TOKEN;
  if (!token || req.headers.authorization !== `Bearer ${token}`) {
    res.status(401).json({ ok: false, error: "bad write token" });
    return;
  }
  const MAX_BODY = 1024 * 1024;
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) {
      res.status(413).json({ ok: false, error: "record too large (1 MB limit)" });
      return;
    }
    chunks.push(chunk);
  }
  const body = Buffer.concat(chunks);
  if (!body.length) {
    res.status(400).json({ ok: false, error: "empty body" });
    return;
  }
  try {
    const parsed = JSON.parse(body.toString("utf8"));
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("not an object");
    }
  } catch {
    res.status(400).json({ ok: false, error: "body must be a JSON object (the canonical record bytes)" });
    return;
  }
  const id = createHash("sha256").update(body).digest("hex");
  if (!process.env.GITHUB_TOKEN) {
    res.status(502).json({ ok: false, error: "github backend not configured" });
    return;
  }
  try {
    // PUT-first: the common new-record path is exactly one GitHub
    // call. A create with no sha fails 422 when the path already
    // exists, which for an immutable content-addressed record is the
    // idempotent case; only then pay for the exists-check GET.
    const putRes = await fetch(contentsPath(id), {
      method: "PUT",
      headers: githubHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        message: `record ${id}`,
        content: body.toString("base64"),
        branch: GITHUB_BRANCH,
      }),
    });
    // 201 created, or 200 in the unlikely case GitHub treats it as
    // an update.
    if (putRes.status === 201 || putRes.status === 200) {
      res.status(200).json({ ok: true, id, log_ref: id });
      return;
    }
    if (putRes.status === 422) {
      const existing = await fetch(`${contentsPath(id)}?ref=${encodeURIComponent(GITHUB_BRANCH)}`, {
        headers: githubHeaders(),
      });
      if (existing.status === 200) {
        res.status(200).json({ ok: true, id, log_ref: id });
        return;
      }
      res.status(502).json({ ok: false, error: `github backend error (status ${existing.status})` });
      return;
    }
    res.status(502).json({ ok: false, error: `github backend error (status ${putRes.status})` });
  } catch {
    res.status(502).json({ ok: false, error: "github backend unreachable" });
  }
}
