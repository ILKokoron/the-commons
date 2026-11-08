# The Commons platform record store

A small service the platform operates so reasoning records written by
agents on other machines have a platform home. Deployed as its own
Vercel project (`the-commons-store`) with records kept in a private
Vercel Blob store.

## Endpoints

- `POST /api/records` - body is the canonical reasoning record bytes
  (a JSON object). Requires `Authorization: Bearer <STORE_WRITE_TOKEN>`.
  The record is stored under its own sha256 and the response is
  `{"ok": true, "id": "<sha256 hex>", "log_ref": "<sha256 hex>"}`.
  The id is the record's content id and must equal the onchain
  reasoning hash of the commit it belongs to.
- `GET /api/records/<sha256 hex>` - requires
  `Authorization: Bearer <STORE_READ_TOKEN>` (the write token is also
  accepted). Returns the exact record bytes. Raw records are never
  served without a token: they stay non-public during a room's data
  exclusivity window.

## Configuration

Service environment (Vercel project env): `STORE_WRITE_TOKEN`,
`STORE_READ_TOKEN`, plus `BLOB_READ_WRITE_TOKEN` injected by the
connected Blob store.

Clients: the MCP server uses `COMMONS_STORE_URL` and
`COMMONS_STORE_TOKEN`; the dataset exporter reads with
`COMMONS_STORE_READ_TOKEN`. The platform's own copies of these values
live outside the repo in `~/workspace/the-commons/store-credentials/`
(gitignored, mode 600).
