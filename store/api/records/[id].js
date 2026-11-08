import { get } from "@vercel/blob";

// GET /api/records/<sha256-hex>
// Auth: Authorization: Bearer <STORE_READ_TOKEN> (the write token is
// also accepted). Raw records are never served without a token: they
// stay non-public during a room's data exclusivity window.
export default async function handler(req, res) {
  if (req.method !== "GET") {
    res.status(405).json({ ok: false, error: "GET only" });
    return;
  }
  const header = req.headers.authorization || "";
  const readOk = process.env.STORE_READ_TOKEN && header === `Bearer ${process.env.STORE_READ_TOKEN}`;
  const writeOk = process.env.STORE_WRITE_TOKEN && header === `Bearer ${process.env.STORE_WRITE_TOKEN}`;
  if (!readOk && !writeOk) {
    res.status(401).json({ ok: false, error: "bad read token" });
    return;
  }
  const id = req.query.id;
  if (!/^[0-9a-f]{64}$/.test(id || "")) {
    res.status(400).json({ ok: false, error: "id must be a sha256 hex string" });
    return;
  }
  const result = await get(`records/${id}.json`, { access: "private" });
  if (!result || result.statusCode !== 200) {
    res.status(404).json({ ok: false, error: "record not found" });
    return;
  }
  const buf = Buffer.from(await new Response(result.stream).arrayBuffer());
  res.status(200).setHeader("content-type", "application/json").send(buf);
}
