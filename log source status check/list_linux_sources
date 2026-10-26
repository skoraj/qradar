#!/usr/bin/env python3
"""
Export QRadar log sources of a given type to a JSON file for later lookups
(by name regex or by log source identifier).

Python 3.6 compatible, standard library only.

Usage:
    Set QRADAR_HOST and QRADAR_TOKEN below, then:
    python3 export_log_sources.py
    python3 export_log_sources.py --type-id 11 --out ls.json

TLS certificate verification is DISABLED by default.
Pass --ca-file rootca.crt to verify the Console certificate instead.

Output (list of objects):
    [{"id": 123, "identifier": "10.1.2.3", "name": "WinSrv @ 10.1.2.3",
      "type_id": 11, "status": "SUCCESS"}, ...]
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- Connection settings: fill these in -------------------------------------
QRADAR_HOST = ""     # Console IP or FQDN
QRADAR_TOKEN = ""                # authorized service token (SEC)
# ------------------------------------------------------------------------------

API_PATH = "/api/config/event_sources/log_source_management/log_sources"
API_VERSION = "19.0"
PAGE_SIZE = 500
FIELDS = "id,name,type_id,status(status),protocol_parameters(name,value)"


def build_context(ca_file):
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    # Default: no certificate or hostname verification
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_page(host, token, type_id, start, end, ctx, retries=3):
    query = urllib.parse.urlencode({
        "filter": "type_id={}".format(type_id),
        "fields": FIELDS,
        "sort": "+id",
    })
    url = "https://{}{}?{}".format(host, API_PATH, query)
    req = urllib.request.Request(url, headers={
        "SEC": token,
        "Version": API_VERSION,
        "Accept": "application/json",
        "Range": "items={}-{}".format(start, end),
    })

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                content_range = resp.headers.get("Content-Range", "")
                data = json.loads(resp.read().decode("utf-8"))
                return data, content_range
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if 400 <= e.code < 500:  # retrying will not help
                sys.exit("HTTP {} from QRadar: {}".format(e.code, body))
            err = "HTTP {}: {}".format(e.code, body)
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
        print("  attempt {}/{} failed: {}".format(attempt, retries, err), file=sys.stderr)
        time.sleep(5 * attempt)

    sys.exit("Giving up on items {}-{}".format(start, end))


def total_from_content_range(content_range):
    # Format: "items 0-499/6123"
    try:
        return int(content_range.rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return None


def get_identifier(ls):
    for p in ls.get("protocol_parameters") or []:
        if p.get("name") == "identifier":
            return p.get("value", "")
    return ""


def slim(ls):
    return {
        "id": ls.get("id"),
        "identifier": get_identifier(ls),
        "name": ls.get("name"),
        "type_id": ls.get("type_id"),
        "status": (ls.get("status") or {}).get("status", ""),
    }


def main():
    ap = argparse.ArgumentParser(description="Export QRadar log sources by type_id to JSON")
    ap.add_argument("--host", default=QRADAR_HOST,
                    help="QRadar Console FQDN or IP (default: QRADAR_HOST in the script)")
    ap.add_argument("--type-id", type=int, default=11, help="log source type_id (default 11)")
    ap.add_argument("--out", default="log_sources_type{type_id}.json", help="JSON output file")
    ap.add_argument("--ca-file", help="CA bundle to verify the Console certificate "
                                      "(default: verification disabled)")
    ap.add_argument("--raw", action="store_true",
                    help="save full API records instead of the slim format")
    args = ap.parse_args()

    # Token from the script; the QRADAR_TOKEN environment variable overrides it if set
    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    if not args.host:
        sys.exit("Set QRADAR_HOST at the top of the script or pass --host.")

    ctx = build_context(args.ca_file)
    out_file = args.out.format(type_id=args.type_id)

    records = []
    start = 0
    total = None
    while True:
        end = start + PAGE_SIZE - 1
        page, content_range = fetch_page(args.host, token, args.type_id, start, end, ctx)
        if total is None:
            total = total_from_content_range(content_range)
        records.extend(page)
        print("Fetched {} of {}".format(len(records), total if total is not None else "?"))
        if len(page) < PAGE_SIZE or (total is not None and len(records) >= total):
            break
        start += PAGE_SIZE

    output = records if args.raw else [slim(ls) for ls in records]

    # Write to a temp file first so a failed run never leaves a half-written file
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=1, ensure_ascii=False)
    os.replace(tmp, out_file)
    print("Wrote {} log sources to {}".format(len(output), out_file))

    if total is not None and len(records) != total:
        print("WARNING: expected {} records, got {}".format(total, len(records)), file=sys.stderr)


if __name__ == "__main__":
    main()
