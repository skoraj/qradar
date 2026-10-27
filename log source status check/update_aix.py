#!/usr/bin/env python3
"""
For log sources in log_sources_enabled.json named
    "AIXServer @ <ip> [<hostname>]"  or  "AIXAudit @ <ip> [<hostname>]"
if <hostname> is an entry (key) in ls_registry.json and the log source's
identifier is an IPv4 address, change the identifier in QRadar to <hostname>.

Safety checks (log source is skipped, nothing changed):
  - another log source of the same type already has <hostname> as identifier
    (or another one in this run would get it)
  - the identifier in QRadar is no longer the one in the export

Every decision is written to a CSV report (also in --dry-run).

Python 3.6 compatible, standard library only.

Usage:
    python3 update_aix.py --dry-run   # show what would change, change nothing
    python3 update_aix.py --test      # change ONLY the first log source
    python3 update_aix.py             # change all
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---- Connection settings: fill these in -------------------------------------
QRADAR_HOST = "192.168.6.22"     # Console IP or FQDN
QRADAR_TOKEN = ""                # authorized service token (SEC), needs write access
# ------------------------------------------------------------------------------

API_VERSION = "19.0"
LS_PATH = "/api/config/event_sources/log_source_management/log_sources"

# Literal [ ] around the hostname; extra spaces are tolerated, e.g. "[ host ]"
NAME_RE = re.compile(r"^\s*(AIXServer|AIXAudit)\s*@\s*\S+\s*\[\s*(?P<host>[^\]\s]+)\s*\]\s*$")
IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


class ApiError(Exception):
    pass


def build_context(ca_file):
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def api(host, token, ctx, method, path, params=None, body=None):
    url = "https://{}{}".format(host, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"SEC": token, "Version": API_VERSION, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError("HTTP {} on {} {}: {}".format(
            e.code, method, path, e.read().decode("utf-8", "replace")))
    except (urllib.error.URLError, OSError) as e:
        raise ApiError("{} {} failed: {}".format(method, path, e))


def get_log_source(host, token, ctx, ls_id):
    return api(host, token, ctx, "GET", "{}/{}".format(LS_PATH, ls_id),
               params={"fields": "id,name,type_id,protocol_parameters"})


def current_identifier(ls):
    for p in ls.get("protocol_parameters") or []:
        if p.get("name") == "identifier":
            return p.get("value")
    return None


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError) as e:
        sys.exit("Cannot read {}: {}".format(path, e))


def main():
    ap = argparse.ArgumentParser(description="Set AIX log source identifiers to hostname")
    ap.add_argument("--log-sources", default="log_sources_enabled.json")
    ap.add_argument("--registry", default="ls_registry.json")
    ap.add_argument("--report", default="aix_update_report.csv")
    ap.add_argument("--host", default=QRADAR_HOST)
    ap.add_argument("--ca-file", help="CA bundle (default: certificate verification disabled)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="update only the first log source")
    mode.add_argument("--dry-run", action="store_true", help="show changes, update nothing")
    args = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token and not args.dry_run:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    ctx = build_context(args.ca_file)

    log_sources = load_json(args.log_sources)
    registry = load_json(args.registry)

    # identifiers already in use, per log source type
    used = {}
    for ls in log_sources:
        used.setdefault(ls.get("type_id"), set()).add((ls.get("identifier") or "").strip())

    if args.test:
        print("TEST MODE: only the first eligible log source will be changed")
    if args.dry_run:
        print("DRY RUN: nothing will be changed")

    report = []
    counts = {}

    def record(ls, hostname, result, note=""):
        counts[result] = counts.get(result, 0) + 1
        report.append([ls.get("id"), ls.get("type_id"), ls.get("name"),
                       ls.get("identifier"), hostname, result, note])

    stop = False
    for ls in log_sources:
        name = ls.get("name") or ""
        m = NAME_RE.match(name)
        if not m:
            record(ls, "", "name format not matched")
            continue
        hostname = m.group("host").strip()
        identifier = (ls.get("identifier") or "").strip()

        if stop:
            record(ls, hostname, "not processed (test mode)")
            continue
        if hostname not in registry:
            record(ls, hostname, "hostname not in registry")
            continue
        if not IP_RE.match(identifier):
            record(ls, hostname, "identifier not an IP",
                   "already hostname" if identifier == hostname else "")
            continue
        type_used = used.setdefault(ls.get("type_id"), set())
        if hostname in type_used:
            print("[{}] {}: SKIP, identifier {} already used by another log source of type {}"
                  .format(ls.get("id"), name, hostname, ls.get("type_id")))
            record(ls, hostname, "skipped", "identifier already in use")
            continue

        print("\nlog source id : {}".format(ls.get("id")))
        print("    name       : {}".format(name))
        print("    identifier : {}  ->  {}".format(identifier, hostname))

        if args.dry_run:
            print("    (dry run, not changed)")
            type_used.add(hostname)          # so a second one in this run is flagged
            record(ls, hostname, "would change")
            continue

        try:
            live = get_log_source(args.host, token, ctx, ls["id"])
            if current_identifier(live) == hostname:   # e.g. changed by an earlier --test run
                print("    already set in QRadar, nothing to do")
                record(ls, hostname, "already changed")
                continue
            if current_identifier(live) != identifier:
                print("    SKIP: identifier in QRadar is now '{}' (re-run export_enabled_aix.py)"
                      .format(current_identifier(live)))
                record(ls, hostname, "skipped", "export out of date")
                continue
            params = [dict(p, value=hostname) if p.get("name") == "identifier" else p
                      for p in live["protocol_parameters"]]
            api(args.host, token, ctx, "POST", "{}/{}".format(LS_PATH, ls["id"]),
                body={"protocol_parameters": params})
            check = current_identifier(get_log_source(args.host, token, ctx, ls["id"]))
        except ApiError as e:
            print("    FAIL: {}".format(e))
            record(ls, hostname, "failed", str(e)[:300])
            continue
        if check != hostname:
            print("    FAIL: QRadar still reports identifier '{}'".format(check))
            record(ls, hostname, "failed", "identifier not changed")
            continue

        print("    OK, updated")
        type_used.discard(identifier)
        type_used.add(hostname)
        record(ls, hostname, "changed", time.strftime("%Y-%m-%d %H:%M:%S"))
        if args.test:
            stop = True

    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "type_id", "name", "old_identifier", "hostname", "result", "note"])
        w.writerows(report)

    print("\n" + "  ".join("{}: {}".format(k, v) for k, v in sorted(counts.items())))
    print("Report saved to {}".format(args.report))


if __name__ == "__main__":
    main()
