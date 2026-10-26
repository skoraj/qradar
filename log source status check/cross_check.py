#!/usr/bin/env python3
"""
Cross-check search results against the exported log source list and mark
registry entries for update.

For every identifier in search_results.json, "update" is set to:
  - "n/a"      identifier does not match IDENTIFIER_REGEX
  - "update"   matches, and a log source in log_sources_type11.json has a name
               containing the identifier AND one of the identifier's source IPs;
               its name goes into "LS_to_be_updated"
  - "unknown"  matches, but no such log source exists
  - "done"     set by hand after the update; never overwritten by this script

Matched log sources are also stored in "update_targets" (id, name, ...)
for the next step (updating / disabling them).

"sendingip" is set for every entry (including "done") to the IP that appears
as both sourceip and destinationip in the same event; if no such event is in
the current results, the value from an earlier run is kept.

Python 3.6 compatible, standard library only.

Usage:
    python3 cross_check.py
    python3 cross_check.py --results search_results.json \
        --log-sources log_sources_type11.json --registry ls_registry.json
"""

import argparse
import json
import os
import re
import sys
import time

IDENTIFIER_REGEX = r"s\dsl*"   # applied with re.search, case-insensitive


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError) as e:
        sys.exit("Cannot read {}: {}".format(path, e))
    except ValueError as e:
        sys.exit("{} is not valid JSON: {}".format(path, e))


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def ip_in_name(ip, name):
    # Whole-IP match: 10.1.2.3 must not match inside 10.1.2.34 or 110.1.2.3
    return re.search(r"(?<![\d.]){}(?![\d])".format(re.escape(ip)), name) is not None


def find_targets(identifier, source_ips, log_sources):
    ident = identifier.lower()
    targets = []
    for ls in log_sources:
        name = ls.get("name") or ""
        if ident not in name.lower():
            continue
        hit = [ip for ip in source_ips if ip_in_name(ip, name)]
        if hit:
            targets.append({"id": ls.get("id"), "name": name,
                            "identifier": ls.get("identifier"),
                            "status": ls.get("status"),
                            "matched_ip": hit[0]})
    return targets


def main():
    ap = argparse.ArgumentParser(description="Mark registry entries for log source update")
    ap.add_argument("--results", default="search_results.json")
    ap.add_argument("--log-sources", default="log_sources_type11.json")
    ap.add_argument("--registry", default="ls_registry.json")
    ap.add_argument("--regex", default=IDENTIFIER_REGEX,
                    help="identifier regex (re.search, case-insensitive)")
    args = ap.parse_args()

    results = load_json(args.results)
    log_sources = load_json(args.log_sources)
    registry = load_json(args.registry) if os.path.exists(args.registry) else {}
    pattern = re.compile(args.regex, re.I)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    counts = {"update": 0, "unknown": 0, "n/a": 0, "done": 0}
    for entry in results:
        identifier = entry["identifier"]
        reg = registry.get(identifier)
        if reg is None:
            print("WARNING: {} not in registry, skipped (run search_events.py first)"
                  .format(identifier), file=sys.stderr)
            continue

        # sendingip: IPs seen as both sourceip and destinationip in one event.
        # Set for every entry (also done); kept from earlier runs if none found now.
        sending = sorted({e["sourceip"] for e in entry.get("events", [])
                          if e.get("sourceip") and e["sourceip"] != "0.0.0.0"
                          and e["sourceip"] == e.get("destinationip")})
        if sending:
            reg["sendingip"] = ", ".join(sending)
            if len(sending) > 1:
                print("WARNING: {} has several sending IPs: {}".format(
                    identifier, reg["sendingip"]), file=sys.stderr)

        # Entries already marked done by hand are left exactly as they are
        if reg.get("update") == "done":
            counts["done"] += 1
            print("[{}] {:<30} update=done (kept)".format(reg["id"], identifier))
            continue

        if not pattern.search(identifier):
            update, targets = "n/a", []
        else:
            source_ips = sorted({e["sourceip"] for e in entry.get("events", [])
                                 if e.get("sourceip") and e["sourceip"] != "0.0.0.0"})
            targets = find_targets(identifier, source_ips, log_sources)
            update = "update" if targets else "unknown"

        reg["update"] = update
        reg["update_targets"] = targets
        reg["checked"] = now
        if targets:
            reg["LS_to_be_updated"] = " | ".join(t["name"] for t in targets)
        else:
            reg.pop("LS_to_be_updated", None)
        counts[update] += 1

        print("[{}] {:<30} update={}".format(reg["id"], identifier, update))
        for t in targets:
            print("        -> log source {} '{}' (ip {}, status {})".format(
                t["id"], t["name"], t["matched_ip"], t["status"]))
        if len(targets) > 1:
            print("        WARNING: {} log sources matched, check before updating"
                  .format(len(targets)), file=sys.stderr)

    save_json(args.registry, registry)
    print("\nupdate: {}  unknown: {}  n/a: {}  done (kept): {}  -> saved to {}".format(
        counts["update"], counts["unknown"], counts["n/a"], counts["done"], args.registry))


if __name__ == "__main__":
    main()
