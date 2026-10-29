"""Tests with a mocked HTTP layer: no network. Run: python3 -m unittest  (or python3 -m pytest -q)"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import airtable_attachments as aa  # noqa: E402

BASE = "appTESTTESTTEST1"
CDN = "https://dl.airtable.test/files/"


class Resp(io.BytesIO):
    pass


def http_error(code, headers=None, body=b"{}"):
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, io.BytesIO(body))


def att(name, content=b"data", aid=None):
    return {"id": aid or "att" + name, "filename": name, "size": len(content), "url": CDN + urllib.parse.quote(name) + "?c=" + content.decode()}


def schema():
    return {"tables": [
        {"id": "tblA", "name": "Invoices", "primaryFieldId": "fldN", "fields": [
            {"id": "fldN", "name": "Name", "type": "singleLineText"},
            {"id": "fldP", "name": "PDF", "type": "multipleAttachments"},
            {"id": "fldI", "name": "Images", "type": "multipleAttachments"}]},
        {"id": "tblB", "name": "Notes", "primaryFieldId": "fldT", "fields": [
            {"id": "fldT", "name": "Title", "type": "singleLineText"}]},
    ]}


class FakeAirtable:
    """Serves the schema, paginated records and attachment downloads. Records the requests."""

    def __init__(self, pages, errors=None):
        self.pages = pages          # list of record lists for table tblA
        self.errors = errors or {}  # url-substring -> list of exceptions to raise first
        self.requests = []

    def __call__(self, req):
        url = req.full_url
        self.requests.append((url, dict(req.header_items())))
        for key, errs in self.errors.items():
            if key in url and errs:
                raise errs.pop(0)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/v0/meta/bases/%s/tables" % BASE:
            return Resp(json.dumps(schema()).encode())
        if parsed.path == "/v0/%s/tblA" % BASE:
            q = urllib.parse.parse_qs(parsed.query)
            idx = int(q.get("offset", ["0"])[0])
            body = {"records": self.pages[idx]}
            if idx + 1 < len(self.pages):
                body["offset"] = str(idx + 1)
            return Resp(json.dumps(body).encode())
        if url.startswith(CDN):
            return Resp(urllib.parse.parse_qs(parsed.query)["c"][0].encode())
        raise http_error(404)

    def api_queries(self):
        return [urllib.parse.parse_qs(urllib.parse.urlparse(u).query) for u, _ in self.requests
                if urllib.parse.urlparse(u).path == "/v0/%s/tblA" % BASE]

    def downloads(self):
        return [u for u, _ in self.requests if u.startswith(CDN)]


def rec(rid, name, **fields):
    f = {"Name": name}
    f.update(fields)
    return {"id": rid, "fields": f}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        self.sleeps = []
        self.now = [0.0]

    def tearDown(self):
        self.tmp.cleanup()

    def sleep(self, s):
        self.sleeps.append(s)
        self.now[0] += s

    def run_cli(self, fake, *args, token="patSECRET"):
        env = {"AIRTABLE_TOKEN": token} if token else {}
        argv = [BASE, "--out", self.out] + list(args)
        return aa.main(argv, env=env, opener=fake, sleep=self.sleep, clock=lambda: self.now[0])

    def manifest(self):
        with open(os.path.join(self.out, "manifest.csv"), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def read(self, *parts):
        with open(os.path.join(self.out, *parts), "rb") as f:
            return f.read()


class HelperTests(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(aa.safe_name('a/b\\c:d*e?"f<g>h|i.pdf'), "a_b_c_d_e__f_g_h_i.pdf")
        self.assertNotIn("/", aa.safe_name("../../etc/passwd"))
        self.assertFalse(aa.safe_name("../x").startswith("."))
        self.assertEqual(aa.safe_name("  report.  "), "report")
        self.assertEqual(aa.safe_name(""), "unnamed")
        self.assertEqual(aa.safe_name(None, "rec1"), "rec1")
        self.assertEqual(aa.safe_name("CON.txt"), "_CON.txt")
        self.assertEqual(aa.safe_name("tab\tname\n.txt"), "tab_name_.txt")

    def test_safe_name_truncates_keeping_extension(self):
        n = aa.safe_name("é" * 300 + ".pdf")
        self.assertLessEqual(len(n.encode("utf-8")), 150)
        self.assertTrue(n.endswith(".pdf"))

    def test_unique_name(self):
        used = set()
        self.assertEqual(aa.unique_name("a.png", used), "a.png")
        self.assertEqual(aa.unique_name("a.png", used), "a (2).png")
        self.assertEqual(aa.unique_name("A.PNG", used), "A (3).PNG")  # case-insensitive file systems
        self.assertEqual(aa.unique_name("noext", used), "noext")
        self.assertEqual(aa.unique_name("noext", used), "noext (2)")

    def test_primary_label(self):
        self.assertEqual(aa.primary_label(" Acme "), "Acme")
        self.assertEqual(aa.primary_label(12), "12")
        self.assertEqual(aa.primary_label(["a", "b"]), "a, b")
        self.assertEqual(aa.primary_label(None), "")


class DownloadTests(Base):
    def test_downloads_layout_and_manifest(self):
        fake = FakeAirtable([[rec("rec1", "Acme", PDF=[att("inv.pdf", b"PDFDATA")], Images=[att("a.png", b"PNG")])]])
        self.assertEqual(self.run_cli(fake), 0)
        self.assertEqual(self.read("Invoices", "Acme", "inv.pdf"), b"PDFDATA")
        self.assertEqual(self.read("Invoices", "Acme", "a.png"), b"PNG")
        rows = self.manifest()
        self.assertEqual([(r["table"], r["record_id"], r["field"], r["filename"], r["size"], r["local_path"]) for r in rows], [
            ("Invoices", "rec1", "PDF", "inv.pdf", "7", "Invoices/Acme/inv.pdf"),
            ("Invoices", "rec1", "Images", "a.png", "3", "Invoices/Acme/a.png")])
        self.assertEqual(list(rows[0].keys()), aa.MANIFEST_COLUMNS)
        self.assertEqual(os.listdir(os.path.join(self.out, "Invoices", "Acme")).count("inv.pdf"), 1)

    def test_table_without_attachment_fields_is_skipped(self):
        fake = FakeAirtable([[]])
        self.assertEqual(self.run_cli(fake), 0)
        self.assertFalse([u for u, _ in fake.requests if "tblB" in u])

    def test_pagination_downloads_each_page_before_fetching_next(self):
        fake = FakeAirtable([
            [rec("rec1", "One", PDF=[att("1.pdf", b"1")])],
            [rec("rec2", "Two", PDF=[att("2.pdf", b"22")])],
            [rec("rec3", "Three", PDF=[att("3.pdf", b"333")])]])
        self.assertEqual(self.run_cli(fake), 0)
        self.assertEqual(len(self.manifest()), 3)
        urls = [u for u, _ in fake.requests]
        pages = [i for i, u in enumerate(urls) if "/v0/%s/tblA" % BASE in u]
        dls = [i for i, u in enumerate(urls) if u.startswith(CDN)]
        self.assertEqual(len(pages), 3)
        # attachments of page N are downloaded before page N+1 is requested (URLs expire)
        self.assertLess(dls[0], pages[1])
        self.assertLess(dls[1], pages[2])
        self.assertIn("offset=2", urls[pages[2]])

    def test_token_only_sent_to_api_not_to_download_host(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf")])]])
        self.run_cli(fake)
        for url, headers in fake.requests:
            auth = {k.lower(): v for k, v in headers.items()}.get("authorization")
            if url.startswith(CDN):
                self.assertIsNone(auth)
            else:
                self.assertEqual(auth, "Bearer patSECRET")

    def test_429_retry_on_api_and_download(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf", b"ok")])]], errors={
            "/v0/%s/tblA" % BASE: [http_error(429)],
            "/files/": [http_error(429, {"Retry-After": "7"}), http_error(503)]})
        self.assertEqual(self.run_cli(fake), 0)
        self.assertEqual(self.read("Invoices", "A", "x.pdf"), b"ok")
        self.assertIn(30.0, self.sleeps)  # default Airtable 429 wait
        self.assertIn(7.0, self.sleeps)   # Retry-After honoured
        self.assertEqual(len(fake.downloads()), 3)

    def test_429_gives_up_after_max_retries(self):
        fake = FakeAirtable([[]], errors={"/v0/%s/tblA" % BASE: [http_error(429) for _ in range(20)]})
        self.assertEqual(self.run_cli(fake), 1)
        self.assertEqual(sum(1 for u, _ in fake.requests if "tblA" in u), aa.MAX_RETRIES + 1)

    def test_api_requests_are_spaced(self):
        fake = FakeAirtable([[rec("r%d" % i, "N%d" % i)] for i in range(4)])
        self.run_cli(fake)
        self.assertGreaterEqual(len([s for s in self.sleeps if abs(s - aa.MIN_INTERVAL) < 1e-9]), 4)

    def test_filename_sanitizing_and_dedup(self):
        fake = FakeAirtable([[rec("rec1", "Q3/2026: Acme?", PDF=[
            att("a/b.pdf", b"1", "att1"), att("a_b.pdf", b"2", "att2"), att("a_b.pdf", b"3", "att3"), att("..", b"4", "att4")],
            Images=[att("a_b.pdf", b"5", "att5")])]])
        self.assertEqual(self.run_cli(fake), 0)
        d = os.path.join(self.out, "Invoices", "Q3_2026_ Acme_")
        self.assertEqual(sorted(os.listdir(d)), ["__", "a_b (2).pdf", "a_b (3).pdf", "a_b (4).pdf", "a_b.pdf"])
        self.assertEqual([self.read("Invoices", "Q3_2026_ Acme_", n) for n in ("a_b.pdf", "a_b (2).pdf", "a_b (3).pdf", "a_b (4).pdf")],
                         [b"1", b"2", b"3", b"5"])
        paths = [r["local_path"] for r in self.manifest()]
        self.assertEqual(len(paths), len(set(paths)))

    def test_record_folder_clash_uses_record_id_and_empty_primary_uses_id(self):
        fake = FakeAirtable([[
            rec("recAAA", "Same", PDF=[att("x.pdf", b"1")]),
            rec("recBBB", "same", PDF=[att("x.pdf", b"2")]),
            {"id": "recCCC", "fields": {"PDF": [att("x.pdf", b"3")]}}]])
        self.assertEqual(self.run_cli(fake), 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "Invoices"))), ["Same", "recCCC", "same_recBBB"])

    def test_skip_existing(self):
        pages = [[rec("rec1", "A", PDF=[att("x.pdf", b"123"), att("y.pdf", b"45")])]]
        self.assertEqual(self.run_cli(FakeAirtable(pages)), 0)
        # y.pdf corrupted (wrong size) -> re-downloaded; x.pdf intact -> skipped
        with open(os.path.join(self.out, "Invoices", "A", "y.pdf"), "wb") as f:
            f.write(b"bad-size")
        fake = FakeAirtable(pages)
        self.assertEqual(self.run_cli(fake, "--skip-existing"), 0)
        self.assertEqual(len(fake.downloads()), 1)
        self.assertIn("y.pdf", fake.downloads()[0])
        self.assertEqual(self.read("Invoices", "A", "y.pdf"), b"45")
        self.assertEqual({r["filename"]: r["status"] for r in self.manifest()}, {"x.pdf": "skipped", "y.pdf": "downloaded"})
        # without the flag everything is downloaded again
        fake = FakeAirtable(pages)
        self.run_cli(fake)
        self.assertEqual(len(fake.downloads()), 2)

    def test_field_filtering(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf")], Images=[att("y.png")])]])
        self.assertEqual(self.run_cli(fake, "--field", "Images"), 0)
        self.assertEqual(os.listdir(os.path.join(self.out, "Invoices", "A")), ["y.png"])
        q = fake.api_queries()[0]
        self.assertEqual(q["fields[]"], ["Name", "Images"])
        self.assertEqual([r["field"] for r in self.manifest()], ["Images"])

    def test_field_by_id_and_unknown_field(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf")], Images=[att("y.png")])]])
        self.assertEqual(self.run_cli(fake, "--field", "fldP"), 0)
        self.assertEqual(os.listdir(os.path.join(self.out, "Invoices", "A")), ["x.pdf"])
        self.assertEqual(self.run_cli(FakeAirtable([[]]), "--field", "Name"), 1)  # not an attachment field
        self.assertEqual(self.run_cli(FakeAirtable([[]]), "--field", "Nope"), 1)

    def test_table_and_view_params(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf")])]])
        self.assertEqual(self.run_cli(fake, "--table", "invoices", "--view", "Paid"), 0)
        self.assertEqual(fake.api_queries()[0]["view"], ["Paid"])
        self.assertEqual(self.run_cli(FakeAirtable([[]]), "--table", "Missing"), 1)
        self.assertEqual(self.run_cli(FakeAirtable([[]]), "--view", "Paid"), 2)  # view needs one table

    def test_dry_run_writes_nothing(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf", b"123")])]])
        self.assertEqual(self.run_cli(fake, "--dry-run"), 0)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(fake.downloads(), [])

    def test_failed_download_exit_code_and_no_partial_file(self):
        fake = FakeAirtable([[rec("rec1", "A", PDF=[att("x.pdf"), att("y.pdf", b"yy")])]],
                            errors={"x.pdf": [http_error(403)]})
        self.assertEqual(self.run_cli(fake), 3)
        self.assertEqual(os.listdir(os.path.join(self.out, "Invoices", "A")), ["y.pdf"])
        self.assertEqual({r["filename"]: r["status"] for r in self.manifest()}, {"x.pdf": "failed", "y.pdf": "downloaded"})


class CliTests(Base):
    def test_missing_token(self):
        self.assertEqual(self.run_cli(FakeAirtable([[]]), token=None), 2)

    def test_bad_base_id(self):
        self.assertEqual(aa.main(["nope"], env={"AIRTABLE_TOKEN": "x"}, opener=FakeAirtable([[]])), 2)

    def test_auth_error_is_reported_without_token(self):
        def deny(req):
            raise http_error(401, body=b'{"error":{"message":"Invalid token"}}')
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            code = self.run_cli(deny, token="patSUPERSECRET")
        finally:
            sys.stderr = old
        self.assertEqual(code, 1)
        self.assertIn("401", err.getvalue())
        self.assertNotIn("patSUPERSECRET", err.getvalue())

    def test_help_exits_zero(self):
        old, sys.stdout = sys.stdout, io.StringIO()
        try:
            self.assertEqual(aa.main(["--help"], env={}), 0)
        finally:
            sys.stdout = old


if __name__ == "__main__":
    unittest.main()
