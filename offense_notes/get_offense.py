#!/usr/bin/env python3
"""
Eksport offense z QRadar do CSV:
OffenseID;RuleID;Rule_name;closing_reason_id;closing_reason_name;start_time

Jeden wiersz = jedna regula na offense
(offense z kilkoma regulami wystepuje w kilku wierszach).
Filtr: start_time w podanym roku (domyslnie 2025), czas lokalny maszyny, na ktorej dziala skrypt.

Uzycie:
  1) uzupelnij CONSOLE i TOKEN na poczatku pliku, potem: python3 export_offenses.py
  2) opcjonalnie nadpisz z linii komend: --host --year --api-version --cafile --insecure -o
Token mozna tez podac w zmiennej QRADAR_TOKEN (ma pierwszenstwo przed TOKEN z pliku).
Wymaga: python3 + requests. Token potrzebuje roli z dostepem do Offenses i Analytics (Rules).
"""
import argparse
import csv
import datetime as dt
import os
import sys
import time

import requests

# ================= KONFIGURACJA (uzupelnij) =================
CONSOLE = "https://<IP_KONSOLI>"   # np. "https://10.170.19.2"
TOKEN = ""                          # authorized service token (SEC); wpisany tu lub w QRADAR_TOKEN
YEAR = 2025                         # rok start_time offense
API_VERSION = "19.0"
VERIFY_TLS = True                   # True / False / "/sciezka/do/ca.pem"
OUTPUT = "offenses.csv"
# ============================================================

PAGE = 500


def api_get(sess, base, path, params=None, paginate=False):
    """GET z opcjonalna paginacja naglowkiem Range."""
    url = base + path
    if not paginate:
        r = sess.get(url, params=params, timeout=120)
        r.raise_for_status()
        return r.json()
    out, start = [], 0
    while True:
        h = {"Range": "items=%d-%d" % (start, start + PAGE - 1)}
        r = sess.get(url, params=params, headers=h, timeout=120)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < PAGE:
            return out
        start += PAGE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CONSOLE, help="adres konsoli (domyslnie CONSOLE z pliku)")
    ap.add_argument("--year", type=int, default=YEAR, help="rok start_time offense")
    ap.add_argument("--api-version", default=API_VERSION)
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("-o", "--out", default=OUTPUT)
    a = ap.parse_args()

    token = os.environ.get("QRADAR_TOKEN") or TOKEN
    if not token:
        sys.exit("Uzupelnij TOKEN na poczatku pliku lub ustaw zmienna QRADAR_TOKEN.")
    if "<" in a.host:
        sys.exit("Uzupelnij CONSOLE na poczatku pliku (lub podaj --host).")

    s = requests.Session()
    s.headers.update({"SEC": token, "Accept": "application/json", "Version": a.api_version})
    s.verify = False if a.insecure else (a.cafile or VERIFY_TLS)
    if s.verify is False:
        try:
            requests.packages.urllib3.disable_warnings()
        except Exception:
            pass
    base = a.host.rstrip("/") + "/api"

    # slowniki: powody zamkniecia i reguly
    reasons = {x["id"]: x["text"] for x in api_get(
        s, base, "/siem/offense_closing_reasons", {"fields": "id,text"}, paginate=True)}
    rules = {x["id"]: x["name"] for x in api_get(
        s, base, "/analytics/rules", {"fields": "id,name"}, paginate=True)}

    since_ms = int(time.mktime(dt.datetime(a.year, 1, 1).timetuple()) * 1000)
    until_ms = int(time.mktime(dt.datetime(a.year + 1, 1, 1).timetuple()) * 1000)
    offenses = api_get(
        s, base, "/siem/offenses",
        {"fields": "id,start_time,status,closing_reason_id,rules",
         "filter": "start_time>=%d and start_time<%d" % (since_ms, until_ms), "sort": "+id"},
        paginate=True)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["OffenseID", "RuleID", "Rule_name", "closing_reason_id",
                    "closing_reason_name", "start_time"])
        for o in offenses:
            st = dt.datetime.fromtimestamp(o["start_time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            crid = o.get("closing_reason_id")
            crid_s = "" if crid is None else crid
            crn = reasons.get(crid, "") if crid is not None else ""
            rl = o.get("rules") or [{"id": "", "type": ""}]
            for r in rl:
                w.writerow([o["id"], r["id"], rules.get(r["id"], ""), crid_s, crn, st])
    print("Offenses (%d): %d, zapisano do %s" % (a.year, len(offenses), a.out))


if __name__ == "__main__":
    main()
