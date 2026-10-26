#!/usr/bin/env python3
"""
For every ls_registry.json entry (key = new log source identifier) that has a
"sendingip" and is not yet "done":
  1. find the log source in log_sources_type11.json whose identifier == sendingip
  2. change that log source's identifier in QRadar to the entry key
  3. mark the entry "update": "done"

Safety checks (entry is skipped, nothing changed):
  - sendingip holds more than one IP
  - zero or more than one log source has sendingip as identifier
  - another log source already has the new identifier
  - the identifier in QRadar is no longer sendingip (export is out of date)

Python 3.6 compatible, standard library only.

Usage:
    python3 update_by_sendingip.py --dry-run   # show what would change, change nothing
    python3 update_by_sendingip.py --test      # change ONLY the first log source
    python3 update_by_sendingip.py             # change all
    python3 update_by_sendingip.py --status unknown   # only entries with update=unknown
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
QRADAR_TOKEN = ""                # authorized service token (SEC), needs write access
# ------------------------------------------------------------------------------

API_VERSION = "19.0"
LS_PATH = "/api/config/event_sources/log_source_management/log_sources"


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


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Set log source identifiers using sendingip")
    ap.add_argument("--registry", default="ls_registry.json")
    ap.add_argument("--log-sources", default="log_sources_type11.json")
    ap.add_argument("--host", default=QRADAR_HOST)
    ap.add_argument("--ca-file", help="CA bundle (default: certificate verification disabled)")
    ap.add_argument("--status", help="only entries with this update value (e.g. unknown)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="update only the first log source")
    mode.add_argument("--dry-run", action="store_true", help="show changes, update nothing")
    args = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token and not args.dry_run:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    ctx = build_context(args.ca_file)

    registry = load_json(args.registry)
    log_sources = load_json(args.log_sources)

    by_identifier = {}
    for ls in log_sources:
        by_identifier.setdefault((ls.get("identifier") or "").strip(), []).append(ls)

    todo = [(k, v) for k, v in registry.items()
            if v.get("sendingip") and v.get("update") != "done"
            and (args.status is None or v.get("update") == args.status)]
    print("{} entries with sendingip to process".format(len(todo)))
    if args.test:
        print("TEST MODE: only the first eligible log source will be changed")
    if args.dry_run:
        print("DRY RUN: nothing will be changed")

    counts = {"changed": 0, "would change": 0, "skipped": 0, "not found": 0, "failed": 0}
    for new_identifier, entry in todo:
        ip = entry["sendingip"].strip()
        tag = "[{}] {} (update={}, sendingip={})".format(
            entry.get("id"), new_identifier, entry.get("update"), ip)

        if "," in ip:
            print("{}: SKIP, several sending IPs".format(tag))
            counts["skipped"] += 1
            continue

        matches = by_identifier.get(ip, [])
        if not matches:
            print("{}: no log source with identifier {}".format(tag, ip))
            counts["not found"] += 1
            continue
        if len(matches) > 1:
            print("{}: SKIP, {} log sources have identifier {}: {}".format(
                tag, len(matches), ip, ", ".join(str(m.get("id")) for m in matches)))
            counts["skipped"] += 1
            continue

        existing = by_identifier.get(new_identifier, [])
        if existing:
            print("{}: SKIP, identifier already used by log source {} '{}'".format(
                tag, existing[0].get("id"), existing[0].get("name")))
            counts["skipped"] += 1
            continue

        target = matches[0]
        ls_id = target.get("id")
        print("\n{}".format(tag))
        print("    log source id : {}".format(ls_id))
        print("    name          : {}".format(target.get("name")))
        print("    identifier    : {}  ->  {}".format(ip, new_identifier))

        if args.dry_run:
            print("    (dry run, not changed)")
            counts["would change"] += 1
            continue

        try:
            ls = get_log_source(args.host, token, ctx, ls_id)
            old_identifier = current_identifier(ls)
            if old_identifier != ip:
                print("    SKIP: identifier in QRadar is now '{}', not {} "
                      "(re-run export_log_sources.py)".format(old_identifier, ip))
                counts["skipped"] += 1
                continue
            params = [dict(p, value=new_identifier) if p.get("name") == "identifier" else p
                      for p in ls["protocol_parameters"]]
            api(args.host, token, ctx, "POST", "{}/{}".format(LS_PATH, ls_id),
                body={"protocol_parameters": params})
            check = current_identifier(get_log_source(args.host, token, ctx, ls_id))
        except ApiError as e:
            print("    FAIL: {}".format(e))
            counts["failed"] += 1
            continue
        if check != new_identifier:
            print("    FAIL: QRadar still reports identifier '{}'".format(check))
            counts["failed"] += 1
            continue

        print("    OK, updated")
        entry["update"] = "done"
        entry["previous_identifier"] = ip
        entry["LS_updated"] = ls.get("name")
        entry["LS_updated_id"] = ls_id
        entry["updated_by"] = "sendingip"
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(args.registry, registry)   # save after each change
        counts["changed"] += 1

        if args.test:
            break

    print("\n" + "  ".join("{}: {}".format(k, v) for k, v in counts.items()))


if __name__ == "__main__":
    main()
