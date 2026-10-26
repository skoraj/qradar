#!/usr/bin/env python3
"""
For every ls_registry.json entry with "update": "update", set the identifier of
the log source in LS_to_be_updated to the entry's key (the new identifier),
then mark the entry "update": "done".

The log source is addressed by its ID from update_targets; before changing it
the script checks that its current name in QRadar equals LS_to_be_updated.

Python 3.6 compatible, standard library only.

Usage:
    python3 update_log_sources.py --dry-run   # show what would change, change nothing
    python3 update_log_sources.py --test      # change ONLY the first log source
    python3 update_log_sources.py             # change all marked log sources
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
               params={"fields": "id,name,type_id,protocol_type_id,protocol_parameters"})


def current_identifier(ls):
    for p in ls.get("protocol_parameters") or []:
        if p.get("name") == "identifier":
            return p.get("value")
    return None


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Update log source identifiers from ls_registry.json")
    ap.add_argument("--registry", default="ls_registry.json")
    ap.add_argument("--host", default=QRADAR_HOST)
    ap.add_argument("--ca-file", help="CA bundle (default: certificate verification disabled)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="update only the first log source")
    mode.add_argument("--dry-run", action="store_true", help="show changes, update nothing")
    args = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
    if not token:
        sys.exit("Set QRADAR_TOKEN at the top of the script (or as an environment variable).")
    ctx = build_context(args.ca_file)

    with open(args.registry, encoding="utf-8") as f:
        registry = json.load(f)

    todo = [(k, v) for k, v in registry.items() if v.get("update") == "update"]
    print("{} entries marked update".format(len(todo)))
    if args.test:
        print("TEST MODE: only the first eligible log source will be changed")
    if args.dry_run:
        print("DRY RUN: nothing will be changed")

    changed = skipped = failed = 0
    for new_identifier, entry in todo:
        targets = entry.get("update_targets") or []
        wanted_name = entry.get("LS_to_be_updated")
        tag = "[{}] {}".format(entry.get("id"), new_identifier)

        if len(targets) != 1:
            print("{}: SKIP, {} matching log sources (needs exactly 1)".format(tag, len(targets)))
            skipped += 1
            continue
        ls_id = targets[0]["id"]

        try:
            ls = get_log_source(args.host, token, ctx, ls_id)
        except ApiError as e:
            print("{}: FAIL reading log source {}: {}".format(tag, ls_id, e))
            failed += 1
            continue

        old_identifier = current_identifier(ls)
        print("\n{}".format(tag))
        print("    log source id : {}".format(ls_id))
        print("    name          : {}".format(ls.get("name")))
        print("    identifier    : {}  ->  {}".format(old_identifier, new_identifier))

        if ls.get("name") != wanted_name:
            print("    SKIP: name in QRadar differs from LS_to_be_updated ('{}')".format(wanted_name))
            skipped += 1
            continue
        if old_identifier is None:
            print("    SKIP: log source has no 'identifier' protocol parameter")
            skipped += 1
            continue
        if old_identifier == new_identifier:
            print("    already set, marking done")
        elif args.dry_run:
            print("    (dry run, not changed)")
            continue
        else:
            # Send the full parameter list with only the identifier replaced
            params = [dict(p, value=new_identifier) if p.get("name") == "identifier" else p
                      for p in ls["protocol_parameters"]]
            try:
                api(args.host, token, ctx, "POST", "{}/{}".format(LS_PATH, ls_id),
                    body={"protocol_parameters": params})
                check = current_identifier(get_log_source(args.host, token, ctx, ls_id))
            except ApiError as e:
                print("    FAIL: {}".format(e))
                failed += 1
                continue
            if check != new_identifier:
                print("    FAIL: QRadar still reports identifier '{}'".format(check))
                failed += 1
                continue
            print("    OK, updated")

        entry["update"] = "done"
        entry["previous_identifier"] = old_identifier
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(args.registry, registry)   # save after each change
        changed += 1

        if args.test:
            break

    print("\nchanged: {}  skipped: {}  failed: {}".format(changed, skipped, failed))


if __name__ == "__main__":
    main()
