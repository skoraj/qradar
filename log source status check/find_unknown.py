#!/usr/bin/env python3
"""
For every ls_registry.json entry with "update": "unknown", list the log sources
in log_sources_type11.json whose name contains that log source identifier
(the entry's key), ignoring case. No IP check here.

Output CSV (one row per identifier, one column per matching log source):
    logsourceidentifier;logsource_1;logsource_2;...

Python 3.6 compatible, standard library only.

Usage:
    python3 find_unknown.py
    python3 find_unknown.py --out unknown.csv --delimiter ,
"""

import argparse
import csv
import json
import sys


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError) as e:
        sys.exit("Cannot read {}: {}".format(path, e))
    except ValueError as e:
        sys.exit("{} is not valid JSON: {}".format(path, e))


def main():
    ap = argparse.ArgumentParser(description="Find log sources for 'unknown' registry entries")
    ap.add_argument("--registry", default="ls_registry.json")
    ap.add_argument("--log-sources", default="log_sources_type11.json")
    ap.add_argument("--out", default="unknown_matches.csv")
    ap.add_argument("--delimiter", default=";", help="CSV delimiter (default ;)")
    args = ap.parse_args()

    registry = load_json(args.registry)
    log_sources = load_json(args.log_sources)

    rows = []
    for identifier in sorted(k for k, v in registry.items() if v.get("update") == "unknown"):
        ident = identifier.lower()
        names = sorted(ls.get("name") or "" for ls in log_sources
                       if ident in (ls.get("name") or "").lower())
        rows.append([identifier] + names)

    width = max([len(r) for r in rows] + [2])
    header = ["logsourceidentifier"] + ["logsource_{}".format(i) for i in range(1, width)]

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=args.delimiter)
        w.writerow(header)
        for r in rows:
            w.writerow(r + [""] * (width - len(r)))

    found = sum(1 for r in rows if len(r) > 1)
    multi = sum(1 for r in rows if len(r) > 2)
    print("unknown entries: {}  with a match: {}  with more than one: {}  none: {}".format(
        len(rows), found, multi, len(rows) - found))
    print("Saved to {}".format(args.out))


if __name__ == "__main__":
    main()
