#!/usr/bin/env python3
"""
Search QRadar for events from log source type 67 with selected QIDs, keep only
log sources whose identifier is NOT an IPv4 address, and group by identifier.

Python 3.6 compatible, standard library only.

Usage:
    Set QRADAR_HOST and QRADAR_TOKEN below, then:
    python3 search_events.py
    python3 search_events.py --minutes 60 --out results.json
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

# ---- Search criteria ---------------------------------------------------------
LOG_SOURCE_TYPE_ID = 67
QIDS = [4750017, 80250006, 18750001]
EXCLUDE_IDENTIFIER_REGEX = r"^\d+\.\d+\.\d+\.\d+$"   # skip plain IPv4 identifiers
# ------------------------------------------------------------------------------

ID_PREFIX = "LS"   # IDs look like LS-0001

API_VERSION = "19.0"
PAGE_SIZE = 500
POLL_SECONDS = 3
SEARCH_TIMEOUT = 600


def build_context(ca_file):
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def api(host, token, ctx, method, path, params=None, headers=None, retries=3):
    url = "https://{}{}".format(host, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    hdrs = {"SEC": token, "Version": API_VERSION, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, method=method, headers=hdrs)

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if 400 <= e.code < 500:
                sys.exit("HTTP {} on {} {}: {}".format(e.code, method, path, body))
            err = "HTTP {}: {}".format(e.code, body)
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
        print("  attempt {}/{} failed: {}".format(attempt, retries, err), file=sys.stderr)
        time.sleep(5 * attempt)
    sys.exit("Giving up on {} {}".format(method, path))


def run_aql(host, token, ctx, aql):
    search = api(host, token, ctx, "POST", "/api/ariel/searches",
                 params={"query_expression": aql})
    search_id = search["search_id"]
    print("Search started: {}".format(search_id))

    deadline = time.time() + SEARCH_TIMEOUT
    while True:
        status = api(host, token, ctx, "GET", "/api/ariel/searches/{}".format(search_id))
        state = status.get("status")
        if state == "COMPLETED":
            break
        if state in ("ERROR", "CANCELED"):
            sys.exit("Search {}: {}".format(state, status.get("error_messages")))
        if time.time() > deadline:
            sys.exit("Search did not finish within {} s".format(SEARCH_TIMEOUT))
        time.sleep(POLL_SECONDS)

    rows = []
    start = 0
    while True:
        page = api(host, token, ctx, "GET",
                   "/api/ariel/searches/{}/results".format(search_id),
                   headers={"Range": "items={}-{}".format(start, start + PAGE_SIZE - 1)})
        batch = page.get("events", [])
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def load_registry(path):
    """Registry: {identifier: {"id": "LS-0001", "first_seen": ..., "last_seen": ...}}"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def assign_id(registry, identifier, now):
    entry = registry.get(identifier)
    if entry is None:
        used = [int(e["id"].split("-")[1]) for e in registry.values()]
        entry = {"id": "{}-{:04d}".format(ID_PREFIX, max(used, default=0) + 1),
                 "first_seen": now}
        registry[identifier] = entry
    entry["last_seen"] = now
    return entry["id"]


def main():
    ap = argparse.ArgumentParser(description="Search QRadar events grouped by log source identifier")
    ap.add_argument("--host", default=QRADAR_HOST, help="QRadar Console FQDN or IP")
    ap.add_argument("--minutes", type=int, default=15, help="time window (default 15)")
    ap.add_argument("--out", default="search_results.json", help="JSON output file")
    ap.add_argument("--registry", default="ls_registry.json",
                    help="file keeping the stable identifier -> ID mapping")
    ap.add_argument("--ca-file", help="CA bundle to verify the Console certificate "
                                      "(default: verification disabled)")
    args = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    ctx = build_context(args.ca_file)

    qid_filter = " OR ".join("qid={}".format(q) for q in QIDS)
    aql = ("SELECT logsourceidentifier, LOGSOURCENAME(logsourceid) AS lsname, "
           "qid, QIDNAME(qid) AS qidname, sourceip, destinationip, COUNT(*) AS cnt "
           "FROM events "
           "WHERE devicetype={type_id} AND ({qids}) "
           "AND NOT (logsourceidentifier MATCHES '{regex}') "
           "GROUP BY logsourceidentifier, qid, sourceip, destinationip "
           "LAST {minutes} MINUTES").format(type_id=LOG_SOURCE_TYPE_ID, qids=qid_filter,
                                            regex=EXCLUDE_IDENTIFIER_REGEX,
                                            minutes=args.minutes)
    print("AQL: {}".format(aql))

    rows = run_aql(args.host, token, ctx, aql)
    print("Search returned {} rows".format(len(rows)))

    registry = load_registry(args.registry)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    grouped = {}
    for r in rows:
        identifier = r.get("logsourceidentifier") or "<empty>"
        if identifier not in grouped:
            ls_id = assign_id(registry, identifier, now)
        g = grouped.setdefault(identifier, {"ls_id": ls_id,
                                            "identifier": identifier,
                                            "name": r.get("lsname"),
                                            "total": 0,
                                            "events": []})
        cnt = int(r.get("cnt") or 0)
        g["total"] += cnt
        g["events"].append({"qid": r.get("qid"), "qidname": r.get("qidname"),
                            "sourceip": r.get("sourceip"),
                            "destinationip": r.get("destinationip"),
                            "count": cnt})

    result = sorted(grouped.values(), key=lambda g: g["total"], reverse=True)

    for g in result:
        print("\n[{}] {}  ({}, {} events)".format(g["ls_id"], g["identifier"],
                                                  g["name"], g["total"]))
        for e in g["events"]:
            print("    qid={:<10} {:<16} -> {:<16} x{}".format(
                e["qid"], str(e["sourceip"]), str(e["destinationip"]), e["count"]))

    save_json(args.out, result)
    save_json(args.registry, registry)
    print("\n{} log source identifiers matched; saved to {}".format(len(result), args.out))


if __name__ == "__main__":
    main()
