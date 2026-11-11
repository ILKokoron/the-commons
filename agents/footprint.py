#!/usr/bin/env python3
"""Footprint capture for The Commons demo runners.

The decision layer (reasoning record, commit, reveal, outcome) is
what the dataset product ships. The footprint layer is everything
around it: every game-action attempt the runner makes, including
the failed ones, the retries, and the keeper settle races, each as
one structured JSON record appended to
agents/logs/footprint/<account>.jsonl with fields ts, agent,
room_id, epoch, action, outcome, tx_id, error.

Two halves:

1. record(): append one record per attempt. Pure side effect: every
   exception is swallowed, so a footprint problem can never break
   or block the game loop.

2. maybe_bundle(epoch): when the runner observes a newer epoch
   than the last bundled one, each finished epoch's records for
   this account are bundled and POSTed to the platform record
   store exactly like the MCP server publishes reasoning records
   (POST {store}/api/records, bearer write token, returned id must
   equal the sha256 of the posted bytes). The store accepts one
   JSON object per POST (the rule reasoning records follow), so a
   bundle is one envelope object {"kind": "footprint-bundle",
   room_id, epoch, agent, records: [...]} and the sha256 of its
   canonical bytes is the content id. Successful uploads are
   indexed in agents/logs/footprint/index.json under
   "<room_id>/<epoch>/<account>". A failed upload is retried once
   per newly observed epoch. Store credentials come from the
   runner env (COMMONS_STORE_URL, COMMONS_STORE_TOKEN) when set,
   else from store-credentials/credentials.env. With no
   credentials the bundle stays local only and one plain line
   goes to the runner log; play is never blocked.
"""

import hashlib
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
FP_DIR = os.path.join(HERE, "logs", "footprint")
INDEX_PATH = os.path.join(FP_DIR, "index.json")
DEFAULT_CREDS = os.path.join(BASE, "store-credentials",
                             "credentials.env")
UPLOAD_TIMEOUT = 10  # seconds; one upload per epoch, off the play path


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bundle_bytes(room, epoch, account, records):
    """Canonical bundle bytes: one JSON envelope object holding the
    epoch's records in timestamp order. The store takes one JSON
    object per POST, and the sha256 of these bytes is the bundle's
    content id, so the bundle verifies exactly like a reasoning
    record does."""
    envelope = {
        "kind": "footprint-bundle",
        "room_id": room,
        "epoch": epoch,
        "agent": account,
        "records": records,
    }
    return json.dumps(envelope, separators=(",", ":"),
                      ensure_ascii=False).encode()


class Footprint:
    def __init__(self, account, room, on_log=None):
        self.account = account
        self.room = int(room)
        self._log = on_log or (lambda msg: None)
        self._store = None       # None = unresolved, False = none
        self._next_bundle = None
        self._pending = []       # epochs whose upload failed, retried
        self._last_seen = None

    # ---------------------------------------------------------- store

    def _store_config(self):
        if self._store is not None:
            return self._store or None
        url = os.environ.get("COMMONS_STORE_URL", "")
        token = os.environ.get("COMMONS_STORE_TOKEN", "")
        if (not url or not token) and os.path.exists(DEFAULT_CREDS):
            try:
                cfg = {}
                with open(DEFAULT_CREDS) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") \
                                and "=" in line:
                            k, v = line.split("=", 1)
                            cfg[k.strip()] = v.strip()
                url = url or cfg.get("COMMONS_STORE_URL", "")
                token = token or cfg.get("COMMONS_STORE_TOKEN", "")
            except Exception:
                pass
        self._store = (url.rstrip("/"), token) if url and token \
            else False
        return self._store or None

    # -------------------------------------------------------- records

    def path(self):
        return os.path.join(FP_DIR, f"{self.account}.jsonl")

    def record(self, epoch, action, ok, tx_id=None, error=None):
        """Append one footprint record. Never raises."""
        try:
            rec = {
                "ts": _now_iso(),
                "agent": self.account,
                "room_id": self.room,
                "epoch": epoch,
                "action": action,
                "outcome": "success" if ok else "failed",
                "tx_id": tx_id if ok else None,
                "error": None if ok else str(error or "")[:500],
            }
            if ok:
                rec["error"] = None
            os.makedirs(FP_DIR, exist_ok=True)
            with open(self.path(), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -------------------------------------------------------- bundles

    def _read_records(self, epoch):
        out = []
        try:
            with open(self.path()) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("room_id") == self.room \
                            and rec.get("epoch") == epoch:
                        out.append(rec)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return out

    def _index_get(self, key):
        try:
            with open(INDEX_PATH) as f:
                return json.load(f).get(key)
        except Exception:
            return None

    def _save_index(self, key, cid):
        idx = {}
        try:
            with open(INDEX_PATH) as f:
                idx = json.load(f)
        except Exception:
            idx = {}
        idx[key] = cid
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f, indent=1, sort_keys=True)
        os.replace(tmp, INDEX_PATH)

    def _bundle_epoch(self, epoch):
        """Upload one epoch bundle. Returns False only when an
        upload was attempted and failed (caller queues a retry);
        True for uploaded, empty, and local-only outcomes."""
        records = self._read_records(epoch)
        if not records:
            return True
        records.sort(key=lambda r: (r.get("ts", ""), r.get("action", "")))
        canonical = _bundle_bytes(self.room, epoch, self.account,
                                  records)
        cid = hashlib.sha256(canonical).hexdigest()
        cfg = self._store_config()
        if not cfg:
            self._log(f"footprint: epoch {epoch} bundle kept local "
                      f"only (no store credentials)")
            return True
        url, token = cfg
        if self._index_get(
                f"{self.room}/{epoch}/{self.account}") == cid:
            return True  # this exact bundle is already in the store
        try:
            req = urllib.request.Request(
                url + "/api/records",
                data=canonical,
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
            # Never follow a redirect on this POST: a redirect
            # would re-send the bearer token to wherever it
            # points (same rule as the MCP store upload).
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg,
                                     headers, newurl):
                    return None
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=UPLOAD_TIMEOUT) as resp:
                out = json.loads(resp.read())
            got = out.get("id") or out.get("log_ref")
            if not out.get("ok") or got != cid:
                self._log(f"footprint: epoch {epoch} bundle upload "
                          f"returned unexpected id; kept local only")
                return False
        except Exception as e:
            self._log(f"footprint: epoch {epoch} bundle upload "
                      f"failed ({str(e)[:120]}); kept local only")
            return False
        try:
            self._save_index(f"{self.room}/{epoch}/{self.account}",
                             cid)
        except Exception:
            pass
        return True

    def _start_epoch(self, current):
        """First epoch to bundle from: the oldest epoch present in
        the local JSONL (so a restart re-bundles anything that
        never made it to the store), else the current epoch."""
        oldest = None
        try:
            with open(self.path()) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("room_id") == self.room \
                            and isinstance(rec.get("epoch"), int):
                        ep = rec["epoch"]
                        oldest = ep if oldest is None \
                            else min(oldest, ep)
        except Exception:
            pass
        return current if oldest is None else min(oldest, current)

    def maybe_bundle(self, current_epoch):
        """Bundle every finished epoch once a newer one is observed,
        retrying failed uploads once per newly observed epoch.
        Never raises; at most one small upload per epoch per pass."""
        try:
            current = int(current_epoch)
            if self._next_bundle is None:
                self._next_bundle = self._start_epoch(current)
            new_epoch_seen = current != self._last_seen
            self._last_seen = current
            if new_epoch_seen and self._pending:
                still = []
                queue = list(self._pending)
                while queue:
                    ep = queue.pop(0)
                    if self._bundle_epoch(ep):
                        continue
                    # Fail fast: a dead store must not multiply
                    # into one timeout per queued epoch per pass.
                    still.append(ep)
                    still.extend(queue)
                    break
                self._pending = still
            attempts = 0
            while self._next_bundle < current and attempts < 8:
                attempts += 1
                ep = self._next_bundle
                # Defer the freshest finished epoch until this
                # account's settle attempt for it is on record (the
                # runner attempts it in this same pass, after the
                # bundle step used to run). Without a settle record
                # and less than 2 epochs of distance, wait for a
                # later pass rather than shipping a bundle that
                # misses the settle it will later claim.
                if current - ep < 2 and not any(
                        r.get("action") == "settle"
                        for r in self._read_records(ep)):
                    break
                self._next_bundle += 1
                if not self._bundle_epoch(ep):
                    self._pending.append(ep)
        except Exception:
            pass
