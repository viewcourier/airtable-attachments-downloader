# airtable-attachments-downloader

A zero-dependency Python command line tool that downloads every attachment of an Airtable base, table or view into local folders and writes a `manifest.csv` of what it saved. One file, `airtable_attachments.py`, Python 3.9 or newer, standard library only, nothing to install.

```
export AIRTABLE_TOKEN=your_personal_access_token
python3 airtable_attachments.py appXXXXXXXXXXXXXX
```

- Finds the attachment fields (type `multipleAttachments`) of every table through Airtable's metadata API, so you do not list them by hand.
- Downloads into `<out>/<table>/<record primary field or record ID>/<filename>`.
- Sanitizes names for Windows, macOS and Linux and never overwrites: clashing file names become `name (2).ext`, clashing record folders get the record ID appended.
- Limit it to one table, one view, or specific attachment fields.
- `--skip-existing` makes re-runs cheap; `--dry-run` lists what would be downloaded.
- Stays under Airtable's 5 requests per second limit and retries HTTP 429 and 5xx responses.

## Install

```
git clone https://github.com/viewcourier/airtable-attachments-downloader.git
cd airtable-attachments-downloader
python3 airtable_attachments.py --help
```

Or just download `airtable_attachments.py`; it has no dependencies.

## Usage

```
python3 airtable_attachments.py BASE_ID [--table NAME_OR_ID] [--view NAME_OR_ID] [--field NAME_OR_ID]
                                [--out DIR] [--skip-existing] [--dry-run]
```

| Option | Meaning |
| --- | --- |
| `BASE_ID` | Base ID, starts with `app`. Required. |
| `--table <name or id>` | Only this table. Repeat for several. Default: all tables with attachment fields. |
| `--view <name or id>` | Only the records in this view, in the view's order. Needs exactly one `--table`. |
| `--field <name or id>` | Only this attachment field. Repeat for several. Default: all attachment fields. |
| `--out <dir>` | Output directory. Default `./attachments`. Created if missing. |
| `--skip-existing` | Do not download a file that already exists at its target path with the same size as Airtable reports. |
| `--dry-run` | Print every file that would be downloaded and the total size. Writes nothing. |

The token is read from the `AIRTABLE_TOKEN` environment variable only. It is never written to disk or printed, and it is sent to `api.airtable.com` only, not to the attachment download host.

Exit codes: `0` success, `1` error (bad token, no access, unknown table or field, network), `2` bad arguments, `3` finished but some downloads failed (marked `failed` in `manifest.csv`).

### Examples

```
# every attachment in every table
python3 airtable_attachments.py appXXXXXXXXXXXXXX

# one table, only its "Invoice PDF" field, into ./invoices
python3 airtable_attachments.py appXXXXXXXXXXXXXX --table Invoices --field "Invoice PDF" --out invoices

# only the records of a view
python3 airtable_attachments.py appXXXXXXXXXXXXXX --table Invoices --view "Unpaid"

# see what would happen first
python3 airtable_attachments.py appXXXXXXXXXXXXXX --dry-run

# run again later, fetch only what is new
python3 airtable_attachments.py appXXXXXXXXXXXXXX --skip-existing
```

## Token scopes

Create a personal access token at [airtable.com/create/tokens](https://airtable.com/create/tokens) with these scopes and access to the base (or to all bases in the workspace):

- `data.records:read`: read the records and their attachment URLs.
- `schema.bases:read`: find which fields are attachment fields.

## Example output

```
attachments/
  manifest.csv
  Invoices/
    Acme Corp/
      invoice-2026-10.pdf
      receipt.png
      receipt (2).png          second file with the same name in the same record
    Globex/
      contract.pdf
    recAbC123dEf456/           record without a primary field value: folder is the record ID
      scan.jpg
```

`manifest.csv` has one row per attachment:

```
table,record_id,field,filename,size,status,local_path
Invoices,recAAAAAAAAAAAAAA,Invoice PDF,invoice-2026-10.pdf,48213,downloaded,Invoices/Acme Corp/invoice-2026-10.pdf
```

`status` is `downloaded`, `skipped` (with `--skip-existing`) or `failed`. `local_path` is relative to `--out`, with forward slashes. Console output is one line per table and a summary:

```
Invoices: Invoice PDF, Images
Notes: no attachment fields, skipped
Done: 52 file(s): 52 downloaded, 0 skipped, 0 failed (31.4 MB) -> /path/to/attachments
```

## Attachment URLs expire

Airtable's API returns attachment URLs that stop working after about two hours. That is why a CSV export that contains attachment links gives you dead links the next day. This tool avoids the problem by downloading every file **immediately after fetching the page of records that contains its URL** (100 records per page), never from a list collected earlier. A download that still fails with HTTP 403 or 410 is reported as a likely expired URL; run the tool again to get fresh ones, with `--skip-existing` to keep what you already have.

## Limits

- **Rate limit.** Airtable allows 5 requests per second per base. API requests are spaced 220 ms apart. On HTTP 429 the tool waits 30 seconds (or the `Retry-After` value) and tries again, up to 6 times; 5xx responses are retried with a growing wait.
- **File names.** Characters that are not allowed on common file systems (`/ \ : * ? " < > |` and control characters) become `_`, leading dots are replaced, Windows reserved names such as `CON` are prefixed, and names are cut to 150 bytes keeping the extension. Name clashes are compared case-insensitively.
- **Skip check.** `--skip-existing` compares file size only. It does not detect a replaced file of identical size.
- **Downloads are sequential.** Simple and predictable; very large bases take a while.
- **Not included:** other field types, record data, comments. Use a CSV export for the data itself.

## Tests

```
python3 -m unittest        # or: python3 -m pytest -q
```

The tests mock all HTTP (no network, no token needed) and cover pagination, 429 retry, request spacing, file name sanitizing and de-duplication, `--skip-existing`, field filtering and exit codes.

## Other ways to get your Airtable attachments

- Browser tool, no install: [download Airtable attachments](https://viewcourier.com/tools/airtable-download-attachments?utm_source=github&utm_medium=readme&utm_campaign=attachments_repo) from a table in your browser.
- Guide: [why Airtable CSV attachment links expire](https://viewcourier.com/guides/airtable-csv-attachment-links-expired?utm_source=github&utm_medium=readme&utm_campaign=attachments_repo) and what to do about it.
- Want your data as a file on a schedule instead? [ViewCourier](https://viewcourier.com/?utm_source=github&utm_medium=readme&utm_campaign=attachments_repo) emails Airtable views as CSV or Excel on a schedule, with no script to run or host.

## License

MIT. Copyright (c) 2026 ViewCourier.
