#!/usr/bin/env python3
"""airtable-attachments-downloader: download every attachment of an Airtable base,
table or view into local folders. Standard library only, Python 3.9+.

    AIRTABLE_TOKEN=... python3 airtable_attachments.py appXXXXXXXXXXXXXX [--table T] [--view V]

Layout:  <out>/<table>/<record primary field or id>/<filename>, plus <out>/manifest.csv

Copyright (c) 2026 ViewCourier - MIT License
"""

import argparse
import csv
import json
import os
import posixpath
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
MIN_INTERVAL = 0.22   # seconds between API requests: about 4.5 per second, under the 5/s limit
MAX_RETRIES = 6       # for HTTP 429 and 5xx
BACKOFF_BASE = 30.0   # Airtable asks clients to wait 30 s after a 429
PAGE_SIZE = 100
MANIFEST_COLUMNS = ["table", "record_id", "field", "filename", "size", "status", "local_path"]

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3

_RESERVED = {"con", "prn", "aux", "nul", *("com%d" % i for i in range(1, 10)), *("lpt%d" % i for i in range(1, 10))}


class AirtableError(Exception):
    pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def safe_name(name, fallback="unnamed", max_bytes=150):
    """Make a string safe as one file or directory name on Windows, macOS and Linux."""
    s = unicodedata.normalize("NFC", str(name if name is not None else ""))
    s = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", s)
    s = re.sub(r"^\.+", lambda m: "_" * len(m.group(0)), s)
    s = s.strip().rstrip(". ")
    if not s:
        return fallback
    stem, ext = os.path.splitext(s)
    if stem.lower() in _RESERVED:
        s = "_" + s
    if len(s.encode("utf-8")) > max_bytes:
        stem, ext = os.path.splitext(s)
        ext = ext if len(ext.encode("utf-8")) <= 20 else ""
        room = max_bytes - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(". ")
        s = (stem or fallback) + ext
    return s


def unique_name(name, used):
    """Return name, or 'stem (2).ext', 'stem (3).ext'... if it is already in `used` (case-insensitive).
    The chosen name is added to `used`."""
    candidate, n = name, 1
    stem, ext = os.path.splitext(name)
    while candidate.lower() in used:
        n += 1
        candidate = "%s (%d)%s" % (stem, n, ext)
    used.add(candidate.lower())
    return candidate


def primary_label(value):
    """Turn a primary field value (text, number, list, dict, ...) into a short string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(primary_label(v) for v in value if primary_label(v))
    if isinstance(value, dict):
        for key in ("name", "email", "text", "value"):
            if key in value:
                return primary_label(value[key])
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# HTTP client: rate limiting and retries
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, token, api_url="https://api.airtable.com", opener=None, sleep=time.sleep,
                 clock=time.monotonic, min_interval=MIN_INTERVAL, backoff=BACKOFF_BASE):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.opener = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
        self.sleep = sleep
        self.clock = clock
        self.min_interval = min_interval
        self.backoff = backoff
        self._last = None

    def _throttle(self):
        if self._last is not None:
            wait = self._last + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _open(self, url, headers, throttle):
        """Open url, retrying on 429 and 5xx. Returns the response object (caller closes)."""
        for attempt in range(MAX_RETRIES + 1):
            if throttle:
                self._throttle()
            try:
                return self.opener(urllib.request.Request(url, headers=headers))
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or 500 <= e.code < 600
                if not retryable or attempt == MAX_RETRIES:
                    raise
                wait = self.backoff if e.code == 429 else min(self.backoff * 2 ** attempt, 120.0)
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                log("  HTTP %d, retrying in %.0f s (%d/%d)" % (e.code, wait, attempt + 1, MAX_RETRIES))
                self.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == MAX_RETRIES:
                    raise
                self.sleep(min(self.backoff, 2.0 ** (attempt + 1)))

    def api_get(self, path, params=None):
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        try:
            resp = self._open(url, {"Authorization": "Bearer " + self.token, "Accept": "application/json"}, True)
        except urllib.error.HTTPError as e:
            raise AirtableError(describe_http_error(e))
        except (urllib.error.URLError, OSError) as e:
            raise AirtableError("network error: %s" % getattr(e, "reason", e))
        with resp:
            return json.loads(resp.read().decode("utf-8"))

    def download(self, url, dest):
        """Stream url into dest (via a .part file). The attachment CDN is a different host
        from the API, so the token is NOT sent."""
        resp = self._open(url, {"User-Agent": "airtable-attachments-downloader/" + VERSION}, False)
        part = dest + ".part"
        try:
            with resp, open(part, "wb") as f:
                shutil.copyfileobj(resp, f, 64 * 1024)
            os.replace(part, dest)
        finally:
            if os.path.exists(part):
                os.remove(part)
        return os.path.getsize(dest)


def describe_http_error(e):
    hint = {
        401: "token rejected (check AIRTABLE_TOKEN)",
        403: "token has no access to this base or is missing a scope "
             "(needs data.records:read and schema.bases:read)",
        404: "base or table not found (check the ID and the token's base access)",
        422: "Airtable rejected the request (check --view and field names)",
    }.get(e.code, "")
    detail = ""
    try:
        err = json.loads(e.read().decode("utf-8")).get("error")
        detail = err.get("message") if isinstance(err, dict) else str(err or "")
    except Exception:
        pass
    return "HTTP %d: %s" % (e.code, " - ".join(x for x in (hint, detail) if x) or e.reason)


def log(msg):
    print(msg, file=sys.stderr if msg.startswith("  HTTP") else sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def select_tables(schema_tables, wanted):
    """Filter tables by name or id. Unknown names raise AirtableError."""
    if not wanted:
        return list(schema_tables)
    chosen = []
    for w in wanted:
        match = next((t for t in schema_tables if t["id"] == w or t["name"] == w), None) or \
            next((t for t in schema_tables if t["name"].lower() == w.lower()), None)
        if not match:
            raise AirtableError("table not found: %r. Tables: %s" % (w, ", ".join(t["name"] for t in schema_tables)))
        if match not in chosen:
            chosen.append(match)
    return chosen


def attachment_fields(table, wanted_fields):
    """Attachment fields of a table, optionally limited to the names/ids in wanted_fields."""
    fields = [f for f in table.get("fields", []) if f.get("type") == "multipleAttachments"]
    if wanted_fields:
        wl = {w.lower() for w in wanted_fields}
        fields = [f for f in fields if f["id"] in wanted_fields or f["name"] in wanted_fields or f["name"].lower() in wl]
    return fields


def check_field_args(tables, wanted_fields):
    """Every --field must exist as an attachment field in at least one selected table."""
    for w in wanted_fields or []:
        if not any(attachment_fields(t, [w]) for t in tables):
            raise AirtableError("no attachment field named or with id %r in the selected tables" % w)


def process_table(client, base, table, fields, view, out_dir, skip_existing, dry_run, manifest, stats):
    tname = table["name"]
    primary_id = table.get("primaryFieldId")
    primary_name = next((f["name"] for f in table["fields"] if f["id"] == primary_id), table["fields"][0]["name"])
    table_dir = safe_name(tname, "table")
    folder_owner = {}   # lower-case folder name -> record id
    used_files = {}     # record id -> set of lower-case file names
    params = {"pageSize": PAGE_SIZE, "fields[]": [primary_name] + [f["name"] for f in fields if f["name"] != primary_name]}
    if view:
        params["view"] = view
    offset = None
    while True:
        page_params = dict(params)
        if offset:
            page_params["offset"] = offset
        data = client.api_get("/v0/%s/%s" % (base, table["id"]), page_params)
        # Attachment URLs expire after about two hours: download this page right now.
        for rec in data.get("records", []):
            atts = [(f["name"], a) for f in fields for a in (rec.get("fields", {}).get(f["name"]) or [])]
            if not atts:
                continue
            rid = rec["id"]
            folder = safe_name(primary_label(rec["fields"].get(primary_name)), rid)
            owner = folder_owner.setdefault(folder.lower(), rid)
            if owner != rid:
                folder = safe_name("%s_%s" % (folder, rid))
                folder_owner[folder.lower()] = rid
            used = used_files.setdefault(rid, set())
            for fname, att in atts:
                name = unique_name(safe_name(att.get("filename"), att.get("id", "file")), used)
                rel = posixpath.join(table_dir, folder, name)
                dest = os.path.join(out_dir, *rel.split("/"))
                size = att.get("size")
                row = {"table": tname, "record_id": rid, "field": fname, "filename": name,
                       "size": size if size is not None else "", "status": "", "local_path": rel}
                stats["files"] += 1
                if dry_run:
                    row["status"] = "dry-run"
                    stats["bytes"] += size or 0
                    log("  would download %s (%s)" % (rel, human_size(size)))
                elif skip_existing and os.path.isfile(dest) and (size is None or os.path.getsize(dest) == size):
                    row["status"] = "skipped"
                    stats["skipped"] += 1
                else:
                    try:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        row["size"] = client.download(att["url"], dest)
                        row["status"] = "downloaded"
                        stats["downloaded"] += 1
                        stats["bytes"] += row["size"]
                    except (urllib.error.URLError, OSError) as e:
                        row["status"] = "failed"
                        stats["failed"] += 1
                        log("  FAILED %s: %s%s" % (rel, getattr(e, "code", None) or getattr(e, "reason", e),
                                                   " (URL expired?)" if getattr(e, "code", None) in (403, 410) else ""))
                manifest.append(row)
        offset = data.get("offset")
        if not offset:
            break


def human_size(n):
    if n is None:
        return "? B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def write_manifest(out_dir, rows):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)


def build_parser():
    p = argparse.ArgumentParser(
        prog="airtable_attachments.py",
        description="Download all attachments of an Airtable base, table or view. "
                    "The token is read from the AIRTABLE_TOKEN environment variable "
                    "(scopes: data.records:read, schema.bases:read).")
    p.add_argument("base", help="base ID (starts with 'app')")
    p.add_argument("--table", action="append", metavar="NAME_OR_ID", help="only this table (repeatable). Default: all tables")
    p.add_argument("--view", metavar="NAME_OR_ID", help="only records in this view (needs exactly one --table)")
    p.add_argument("--field", action="append", metavar="NAME_OR_ID", help="only this attachment field (repeatable). Default: all attachment fields")
    p.add_argument("--out", default="attachments", metavar="DIR", help="output directory (default ./attachments)")
    p.add_argument("--skip-existing", action="store_true", help="do not download files that already exist with the same size")
    p.add_argument("--dry-run", action="store_true", help="list what would be downloaded; write nothing")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main(argv=None, env=None, opener=None, sleep=time.sleep, clock=time.monotonic):
    env = os.environ if env is None else env
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    token = env.get("AIRTABLE_TOKEN")
    if not token:
        print("error: set the AIRTABLE_TOKEN environment variable to a personal access token", file=sys.stderr)
        return EXIT_USAGE
    if not re.match(r"^app[A-Za-z0-9]{10,}$", args.base):
        print("error: base ID should look like appXXXXXXXXXXXXXX", file=sys.stderr)
        return EXIT_USAGE
    if args.view and not (args.table and len(args.table) == 1):
        print("error: --view needs exactly one --table", file=sys.stderr)
        return EXIT_USAGE

    client = Client(token, env.get("AIRTABLE_API_URL", "https://api.airtable.com"), opener=opener, sleep=sleep, clock=clock,
                    min_interval=float(env.get("AIRTABLE_MIN_INTERVAL", MIN_INTERVAL)),
                    backoff=float(env.get("AIRTABLE_RETRY_WAIT", BACKOFF_BASE)))
    stats = {"files": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    manifest = []
    try:
        schema = client.api_get("/v0/meta/bases/%s/tables" % args.base)
        tables = select_tables(schema.get("tables", []), args.table)
        check_field_args(tables, args.field)
        for table in tables:
            fields = attachment_fields(table, args.field)
            if not fields:
                log("%s: no attachment fields, skipped" % table["name"])
                continue
            log("%s: %s" % (table["name"], ", ".join(f["name"] for f in fields)))
            process_table(client, args.base, table, fields, args.view, args.out,
                          args.skip_existing, args.dry_run, manifest, stats)
    except AirtableError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    finally:
        if manifest and not args.dry_run:
            write_manifest(args.out, manifest)

    if args.dry_run:
        log("Dry run: %d file(s), %s, nothing written" % (stats["files"], human_size(stats["bytes"])))
    else:
        log("Done: %d file(s): %d downloaded, %d skipped, %d failed (%s) -> %s"
            % (stats["files"], stats["downloaded"], stats["skipped"], stats["failed"],
               human_size(stats["bytes"]), os.path.abspath(args.out)))
    return EXIT_PARTIAL if stats["failed"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
