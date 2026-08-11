#!/usr/bin/env python3
"""
QRadar Log Source Verification Tool
=====================================

Pulls all ENABLED log sources from a QRadar console via REST API v19,
stores id / name / identifier / last event time / status locally, and
lets you search that data later by name or identifier without hitting
the API again.

USAGE
-----
    # Fetch fresh data from QRadar and cache it locally
    python3 qradar_logsource_check.py fetch

    # Search cached data (case-insensitive substring match)
    python3 qradar_logsource_check.py search <term>

    # Fetch AND immediately search
    python3 qradar_logsource_check.py fetch --search <term>

    # List everything in the cache
    python3 qradar_logsource_check.py list

    # Check a CSV of hosts against the cached log source identifiers.
    # CSV format (no header needed): hostname,ip1,ip2,...,ip10 (up to 10 IP columns)
    python3 qradar_logsource_check.py check-csv hosts.csv

    # Same, but also save the results to a CSV report
    python3 qradar_logsource_check.py check-csv hosts.csv --output results.csv

CONFIG
------
Fill in QRADAR_CONSOLE and QRADAR_TOKEN below before running.
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

# =========================================================================
# CONFIG - update these two values
# =========================================================================
QRADAR_CONSOLE = "https://<your-console-hostname-or-ip>"   # e.g. https://qradar.example.com
QRADAR_TOKEN = "<your-api-token>"                            # SEC token from an authorized token
API_VERSION = "19.0"
VERIFY_SSL = False   # set True if the console has a valid/trusted cert
# =========================================================================

CACHE_FILE = Path(__file__).resolve().parent / "qradar_log_sources.json"

LOG_SOURCES_ENDPOINT = "/api/config/event_sources/log_source_management/log_sources"

FIELDS = "id,name,identifier,last_event_time,enabled,status"
PAGE_SIZE = 1000          # log sources per request; QRadar comfortably handles this size
MAX_RETRIES = 5           # retries per page on transient errors / rate limiting
RETRY_BACKOFF_SECONDS = 3 # base backoff, doubles each retry


def _headers():
    return {
        "SEC": QRADAR_TOKEN,
        "Version": API_VERSION,
        "Accept": "application/json",
    }


def _epoch_to_str(epoch_ms):
    """Convert QRadar epoch-milliseconds to a readable UTC timestamp."""
    if not epoch_ms:
        return "Never"
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (ValueError, OSError):
        return "Unknown"


def _extract_status(status_field):
    """
    QRadar returns status as a nested object, e.g.
        {"status": "SUCCESS", "last_updated": 172..., "messages": [...]}
    Normalize to a plain string.
    """
    if isinstance(status_field, dict):
        return status_field.get("status") or "Unknown"
    if isinstance(status_field, str):
        return status_field
    return "Unknown"


def _get_page(url, params, start, end):
    """
    Fetch a single page (Range: items=start-end) with retry/backoff on
    transient errors, timeouts, and rate limiting (429). Raises on
    unrecoverable errors or after exhausting retries.
    """
    headers = _headers()
    headers["Range"] = f"items={start}-{end}"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=headers, params=params, verify=VERIFY_SSL, timeout=120
            )
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            resp = None

        if resp is not None:
            if resp.status_code in (200, 206):
                return resp.json()

            if resp.status_code == 429 or resp.status_code >= 500:
                # rate limited or transient server error - retry
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            else:
                # unrecoverable (bad auth, bad filter, etc.)
                raise RuntimeError(
                    f"QRadar API error {resp.status_code}: {resp.text[:500]}"
                )

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(
                f"  Warning: page {start}-{end} failed ({last_error}). "
                f"Retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})...",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"Failed to fetch page {start}-{end} after {MAX_RETRIES} attempts: {last_error}"
    )


def fetch_enabled_log_sources():
    """
    Pull all enabled log sources from QRadar, paging through results via
    the Range header. Returns a list of dicts:
        {id, name, identifier, last_event_time, last_event_time_str, status}

    Handles large environments (thousands of log sources) by paging in
    PAGE_SIZE chunks, retrying transient failures/rate limits, and
    printing progress as it goes.
    """
    if VERIFY_SSL is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    url = f"{QRADAR_CONSOLE.rstrip('/')}{LOG_SOURCES_ENDPOINT}"
    params = {
        "filter": "enabled=true",
        "fields": FIELDS,
    }

    results = []
    start = 0

    while True:
        end = start + PAGE_SIZE - 1
        page = _get_page(url, params, start, end)

        if not page:
            break

        for ls in page:
            results.append(
                {
                    "id": ls.get("id"),
                    "name": ls.get("name"),
                    "identifier": ls.get("identifier"),
                    "last_event_time": ls.get("last_event_time"),
                    "last_event_time_str": _epoch_to_str(ls.get("last_event_time")),
                    "status": _extract_status(ls.get("status")),
                }
            )

        print(f"  Fetched {len(results)} log source(s) so far...")

        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    return results


def save_cache(rows):
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "count": len(rows),
        "log_sources": rows,
    }
    CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_cache():
    if not CACHE_FILE.exists():
        print(
            f"No cache found at {CACHE_FILE}. Run 'fetch' first.", file=sys.stderr
        )
        sys.exit(1)
    return json.loads(CACHE_FILE.read_text(encoding="utf-8"))


def search_log_sources(rows, term):
    term = term.lower()
    return [
        r
        for r in rows
        if term in (r.get("name") or "").lower()
        or term in (r.get("identifier") or "").lower()
    ]


def print_table(rows):
    if not rows:
        print("No matching log sources found.")
        return

    id_w = max(2, max(len(str(r["id"])) for r in rows))
    name_w = max(4, max(len(r["name"] or "") for r in rows))
    ident_w = max(10, max(len(r["identifier"] or "") for r in rows))
    time_w = max(10, max(len(r["last_event_time_str"] or "") for r in rows))
    status_w = max(6, max(len(r.get("status") or "") for r in rows))

    header = (
        f"{'ID':<{id_w}}  {'Name':<{name_w}}  "
        f"{'Identifier':<{ident_w}}  {'Last Event Time':<{time_w}}  {'Status':<{status_w}}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{str(r['id']):<{id_w}}  {r['name'] or '':<{name_w}}  "
            f"{r['identifier'] or '':<{ident_w}}  {r['last_event_time_str'] or '':<{time_w}}  "
            f"{r.get('status') or '':<{status_w}}"
        )
    print(f"\n{len(rows)} log source(s) shown.")


MAX_IPS_PER_ROW = 10


def load_targets_from_csv(csv_path):
    """
    Read a CSV of: hostname,ip1,ip2,...,ip10 (up to MAX_IPS_PER_ROW IP columns).
    No header row required. If the first cell of the first row is literally
    "hostname" (any case), that row is treated as a header and skipped.
    Blank IP cells are ignored. Returns a list of:
        {"hostname": str, "ips": [str, ...]}
    """
    targets = []
    path = Path(csv_path)
    if not path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            row = [c.strip() for c in row]
            if not row or not any(row):
                continue
            if i == 0 and row[0].lower() == "hostname":
                continue  # header row
            hostname = row[0]
            ips = [c for c in row[1 : 1 + MAX_IPS_PER_ROW] if c]
            if not hostname and not ips:
                continue
            targets.append({"hostname": hostname, "ips": ips})

    return targets


def _ip_boundary_pattern(ip):
    """Regex matching `ip` inside a larger string without matching a
    longer IP that merely contains it as a substring (e.g. searching for
    10.0.0.1 should not match 10.0.0.15)."""
    return re.compile(r"(?<!\d)" + re.escape(ip) + r"(?!\d)")


def find_matches_for_target(cache_rows, target):
    """
    Search cached log sources' `identifier` field for the target's
    hostname (case-insensitive substring) or any of its IPs (boundary-safe
    match). Returns a list of matched log source dicts, each annotated
    with "matched_terms" (which hostname/IP(s) triggered the match).
    """
    hostname = target.get("hostname") or ""
    hostname_lower = hostname.lower()
    ip_patterns = [(ip, _ip_boundary_pattern(ip)) for ip in target.get("ips", [])]

    matches = []
    for ls in cache_rows:
        identifier = ls.get("identifier") or ""
        identifier_lower = identifier.lower()
        matched_terms = []

        if hostname_lower and hostname_lower in identifier_lower:
            matched_terms.append(hostname)

        for ip, pattern in ip_patterns:
            if pattern.search(identifier):
                matched_terms.append(ip)

        if matched_terms:
            match = dict(ls)
            match["matched_terms"] = matched_terms
            matches.append(match)

    return matches


def run_csv_check(cache_rows, targets):
    """
    For each target, find matching log sources. Returns a flat list of
    report rows:
        {target_hostname, target_ips, matched_term, log_source_id,
         log_source_name, identifier, status, last_event_time}
    A target with no matches gets a single row with match fields empty
    and matched_term = "NOT FOUND".
    """
    report_rows = []
    for target in targets:
        matches = find_matches_for_target(cache_rows, target)
        ips_str = ", ".join(target.get("ips", []))

        if not matches:
            report_rows.append(
                {
                    "target_hostname": target.get("hostname") or "",
                    "target_ips": ips_str,
                    "matched_term": "NOT FOUND",
                    "log_source_id": "",
                    "log_source_name": "",
                    "identifier": "",
                    "status": "",
                    "last_event_time": "",
                }
            )
            continue

        for m in matches:
            report_rows.append(
                {
                    "target_hostname": target.get("hostname") or "",
                    "target_ips": ips_str,
                    "matched_term": ", ".join(m["matched_terms"]),
                    "log_source_id": m.get("id"),
                    "log_source_name": m.get("name"),
                    "identifier": m.get("identifier"),
                    "status": m.get("status"),
                    "last_event_time": m.get("last_event_time_str"),
                }
            )

    return report_rows


def print_report(report_rows):
    if not report_rows:
        print("No targets to report.")
        return

    cols = [
        ("target_hostname", "Target Host"),
        ("target_ips", "Target IPs"),
        ("matched_term", "Matched On"),
        ("log_source_id", "LS ID"),
        ("log_source_name", "Log Source Name"),
        ("status", "Status"),
        ("last_event_time", "Last Event Time"),
    ]

    widths = {
        key: max(len(label), max(len(str(r.get(key, "") or "")) for r in report_rows))
        for key, label in cols
    }

    header = "  ".join(f"{label:<{widths[key]}}" for key, label in cols)
    print(header)
    print("-" * len(header))
    for r in report_rows:
        print("  ".join(f"{str(r.get(key, '') or ''):<{widths[key]}}" for key, label in cols))

    found = sum(1 for r in report_rows if r["matched_term"] != "NOT FOUND")
    not_found = sum(1 for r in report_rows if r["matched_term"] == "NOT FOUND")
    print(f"\n{found} match row(s), {not_found} target(s) not found.")


def save_report_csv(report_rows, output_path):
    fieldnames = [
        "target_hostname",
        "target_ips",
        "matched_term",
        "log_source_id",
        "log_source_name",
        "identifier",
        "status",
        "last_event_time",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)


def main():
    parser = argparse.ArgumentParser(description="QRadar log source verification tool")
    sub = parser.add_subparsers(dest="command")
    sub.required = True  # set separately for compatibility with Python < 3.7

    fetch_p = sub.add_parser("fetch", help="Pull enabled log sources from QRadar and cache them")
    fetch_p.add_argument("--search", help="Search term to apply right after fetching")

    search_p = sub.add_parser("search", help="Search the local cache")
    search_p.add_argument("term", help="Substring to match against name or identifier")

    sub.add_parser("list", help="List all cached log sources")

    check_p = sub.add_parser(
        "check-csv",
        help="Check hosts/IPs from a CSV file against cached log source identifiers",
    )
    check_p.add_argument(
        "csv_file",
        help="CSV file: hostname,ip1,ip2,...,ip10 (up to 10 IP columns, no header required)",
    )
    check_p.add_argument("--output", help="Optional path to also save results as a CSV report")

    args = parser.parse_args()

    if args.command == "fetch":
        if "your-console" in QRADAR_CONSOLE or "your-api-token" in QRADAR_TOKEN:
            print(
                "ERROR: Update QRADAR_CONSOLE and QRADAR_TOKEN at the top of this "
                "script before running.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Fetching enabled log sources from {QRADAR_CONSOLE} ...")
        rows = fetch_enabled_log_sources()
        payload = save_cache(rows)
        print(f"Saved {payload['count']} enabled log source(s) to {CACHE_FILE}")

        if args.search:
            matches = search_log_sources(rows, args.search)
            print(f"\nSearch results for '{args.search}':")
            print_table(matches)
        else:
            print_table(rows)

    elif args.command == "search":
        cache = load_cache()
        matches = search_log_sources(cache["log_sources"], args.term)
        print(f"Search results for '{args.term}' (cache fetched {cache['fetched_at']}):")
        print_table(matches)

    elif args.command == "list":
        cache = load_cache()
        print(f"Cache fetched at {cache['fetched_at']} ({cache['count']} log sources)")
        print_table(cache["log_sources"])

    elif args.command == "check-csv":
        cache = load_cache()
        targets = load_targets_from_csv(args.csv_file)
        print(
            f"Checking {len(targets)} target(s) from {args.csv_file} against "
            f"{cache['count']} cached log source(s) (fetched {cache['fetched_at']})..."
        )
        report_rows = run_csv_check(cache["log_sources"], targets)
        print_report(report_rows)

        if args.output:
            save_report_csv(report_rows, args.output)
            print(f"\nSaved report to {args.output}")


if __name__ == "__main__":
    main()
