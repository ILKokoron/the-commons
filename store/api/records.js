import { put } from "@vercel/blob";
import { createHash } from "node:crypto";

// POST /api/records
// Body: the canonical reasoning record bytes (a JSON object).
// Auth: Authorization: Bearer <STORE_WRITE_TOKEN>.
// The record is stored under its own sha256, so the returned content
// id is self-verifying: it must equal the onchain reasoning_hash.
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
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
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
  await put(`records/${id}.json`, body, {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "application/json",
  });
  res.status(200).json({ ok: true, id, log_ref: id });
}
