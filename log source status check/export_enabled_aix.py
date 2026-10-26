#!/usr/bin/env python3
"""
Export ENABLED QRadar log sources of the given types (default 286 and 85)
to a JSON file for further processing.

Python 3.6 compatible, standard library only.

Usage:
    Set QRADAR_HOST and QRADAR_TOKEN below, then:
    python3 export_enabled_log_sources.py
    python3 export_enabled_log_sources.py --type-ids 286 85 --out ls.json

TLS certificate verification is DISABLED by default.
Pass --ca-file rootca.crt to verify the Console certificate instead.

Output (list of objects):
    [{"id": 123, "name": "LinuxServer @ 10.1.2.3", "identifier": "10.1.2.3",
      "type_id": 286}, ...]
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
QRADAR_HOST = "192.168.6.22"     # Console IP or FQDN
QRADAR_TOKEN = ""                # authorized service token (SEC)
# ------------------------------------------------------------------------------

TYPE_IDS = [286, 85]

API_PATH = "/api/config/event_sources/log_source_management/log_sources"
API_VERSION = "19.0"
PAGE_SIZE = 500
FIELDS = "id,name,type_id,enabled,protocol_parameters(name,value)"


def build_context(ca_file):
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_page(host, token, api_filter, start, end, ctx, retries=3):
    query = urllib.parse.urlencode({"filter": api_filter, "fields": FIELDS, "sort": "+id"})
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
                return json.loads(resp.read().decode("utf-8")), content_range
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


def fetch_type(host, token, ctx, type_id):
    api_filter = "type_id={} and enabled=true".format(type_id)
    records = []
    start = 0
    total = None
    while True:
        page, content_range = fetch_page(host, token, api_filter, start,
                                         start + PAGE_SIZE - 1, ctx)
        if total is None:
            total = total_from_content_range(content_range)
        records.extend(page)
        print("  type {}: fetched {} of {}".format(
            type_id, len(records), total if total is not None else "?"))
        if len(page) < PAGE_SIZE or (total is not None and len(records) >= total):
            break
        start += PAGE_SIZE
    if total is not None and len(records) != total:
        print("WARNING: type {}: expected {}, got {}".format(type_id, total, len(records)),
              file=sys.stderr)
    return records


def get_identifier(ls):
    for p in ls.get("protocol_parameters") or []:
        if p.get("name") == "identifier":
            return p.get("value", "")
    return ""


def main():
    ap = argparse.ArgumentParser(description="Export enabled QRadar log sources by type_id to JSON")
    ap.add_argument("--host", default=QRADAR_HOST, help="QRadar Console FQDN or IP")
    ap.add_argument("--type-ids", type=int, nargs="+", default=TYPE_IDS,
                    help="log source type_ids (default: 286 85)")
    ap.add_argument("--out", default="log_sources_enabled.json", help="JSON output file")
    ap.add_argument("--ca-file", help="CA bundle to verify the Console certificate "
                                      "(default: verification disabled)")
    args = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    ctx = build_context(args.ca_file)

    output = []
    for type_id in args.type_ids:
        for ls in fetch_type(args.host, token, ctx, type_id):
            if ls.get("enabled") is False:   # extra guard on top of the API filter
                continue
            output.append({
                "id": ls.get("id"),
                "name": ls.get("name"),
                "identifier": get_identifier(ls),
                "type_id": ls.get("type_id"),
            })

    missing = sum(1 for x in output if not x["identifier"])
    if missing:
        print("WARNING: {} log sources have no identifier parameter".format(missing),
              file=sys.stderr)

    # Write to a temp file first so a failed run never leaves a half-written file
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=1, ensure_ascii=False)
    os.replace(tmp, args.out)

    per_type = ", ".join("{}: {}".format(t, sum(1 for x in output if x["type_id"] == t))
                         for t in args.type_ids)
    print("Wrote {} enabled log sources ({}) to {}".format(len(output), per_type, args.out))


if __name__ == "__main__":
    main()
