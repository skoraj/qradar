#!/usr/bin/env python3
"""
QRadar Generic (Universal DSM) Log Source Identifier Check
============================================================

REST API v19 tool with a simple interactive menu:

  1 - Run an AQL search for log_source_type id=67 (Universal DSM / generic),
      grouped by "Log Source Identifier", over the last 24 hours. Waits
      1 minute for the search to run, checks the results, then saves each
      distinct log source identifier to generic-MM_HH-ddmmyy.csv.
  0 - Delete the data file(s) created during this run of the script.

CONFIG
------
Fill in QRADAR_CONSOLE and QRADAR_TOKEN below before running.
"""

import configparser
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

# =========================================================================
# CONFIG - connection settings are read from chk-sim_generic.cfg, next to
# this script (see chk-sim_generic.cfg.example for the format).
# =========================================================================
CONFIG_FILE = Path(__file__).resolve().parent / "chk-sim_generic.cfg"


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(
            "ERROR: config file not found: {}\n"
            "Copy chk-sim_generic.cfg.example to chk-sim_generic.cfg and fill in "
            "your console address and API token.".format(CONFIG_FILE)
        )

    parser = configparser.ConfigParser()
    parser.read(CONFIG_FILE, encoding="utf-8")

    try:
        section = parser["qradar"]
        console = section["console"]
        token = section["token"]
    except KeyError as exc:
        sys.exit("ERROR: {} is missing required key {} in [qradar] section.".format(CONFIG_FILE, exc))
    api_version = section.get("api_version", "19.0")

    if "<your-console" in console or "<your-api-token" in token:
        sys.exit("ERROR: edit {} and set console/token before running.".format(CONFIG_FILE))

    return console, token, api_version


QRADAR_CONSOLE, QRADAR_TOKEN, API_VERSION = load_config()
# =========================================================================

VERIFY_SSL = False  # SSL certificate checking is disabled
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG_SOURCE_TYPE_ID = 67

SEARCH_WAIT_SECONDS = 60          # initial wait before checking results
SEARCH_POLL_INTERVAL_SECONDS = 5  # extra poll interval if not done after the initial wait
SEARCH_POLL_TIMEOUT_SECONDS = 300 # additional time to allow beyond the initial wait

AQL_QUERY = (
    'SELECT "logsourceidentifier" AS ls_identifier, COUNT(*) AS event_count '
    "FROM events "
    "WHERE devicetype = {type_id} "
    'GROUP BY "ls_identifier" '
    "LAST 24 HOURS"
).format(type_id=LOG_SOURCE_TYPE_ID)

# Files created by this script during the current run (for menu option 0).
created_files = []


def _headers():
    return {
        "SEC": QRADAR_TOKEN,
        "Version": API_VERSION,
        "Accept": "application/json",
    }


def create_search():
    # QRadar's Ariel API does not reliably URL-decode query_expression when
    # passed as a URL query string (neither "+" nor "%20" get decoded).
    # Send it as an application/x-www-form-urlencoded POST body instead,
    # where decoding is unambiguous.
    url = "{}/api/ariel/searches".format(QRADAR_CONSOLE.rstrip("/"))
    headers = _headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = "query_expression=" + quote(AQL_QUERY, safe="")
    resp = requests.post(url, headers=headers, data=body, verify=VERIFY_SSL, timeout=60)
    if not resp.ok:
        raise RuntimeError(
            "{} {} creating search: {}".format(resp.status_code, resp.reason, resp.text)
        )
    data = resp.json()
    search_id = data.get("search_id") or data.get("cursor_id")
    if not search_id:
        raise RuntimeError("Could not obtain search_id from response: {}".format(data))
    return search_id


def get_search_status(search_id):
    url = "{}/api/ariel/searches/{}".format(QRADAR_CONSOLE.rstrip("/"), search_id)
    resp = requests.get(url, headers=_headers(), verify=VERIFY_SSL, timeout=60)
    resp.raise_for_status()
    return resp.json()


def wait_for_search(search_id):
    print("Waiting {} second(s) for the search to run...".format(SEARCH_WAIT_SECONDS))
    time.sleep(SEARCH_WAIT_SECONDS)

    deadline = time.monotonic() + SEARCH_POLL_TIMEOUT_SECONDS
    while True:
        data = get_search_status(search_id)
        status = data.get("status")
        print("  Search {} status: {}".format(search_id, status))

        if status == "COMPLETED":
            return data
        if status in ("CANCELED", "ERROR"):
            raise RuntimeError(
                "Search {} ended with status {}: {}".format(search_id, status, data)
            )
        if time.monotonic() > deadline:
            raise RuntimeError(
                "Search {} did not complete within {}s".format(
                    search_id, SEARCH_WAIT_SECONDS + SEARCH_POLL_TIMEOUT_SECONDS
                )
            )

        time.sleep(SEARCH_POLL_INTERVAL_SECONDS)


def fetch_search_results(search_id):
    url = "{}/api/ariel/searches/{}/results".format(QRADAR_CONSOLE.rstrip("/"), search_id)
    resp = requests.get(url, headers=_headers(), verify=VERIFY_SSL, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data.get("events", [])


def save_identifiers_csv(identifiers):
    filename = datetime.now().strftime("generic-%M_%H-%d%m%y.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["log_source_identifier"])
        for ident in identifiers:
            writer.writerow([ident])
    created_files.append(filename)
    return filename


def run_search_and_save():
    print(
        "Starting search: log_source_type id={}, last 24 hours, "
        "grouped by Log Source Identifier".format(LOG_SOURCE_TYPE_ID)
    )
    print("AQL: {}".format(AQL_QUERY))

    try:
        search_id = create_search()
        print("Search created: {}".format(search_id))
        wait_for_search(search_id)
        events = fetch_search_results(search_id)
    except (requests.RequestException, RuntimeError) as exc:
        print("ERROR: {}".format(exc))
        return

    identifiers = sorted({e.get("ls_identifier") for e in events if e.get("ls_identifier")})

    if not identifiers:
        print(
            "No log source identifiers found for log_source_type id={} "
            "in the last 24 hours.".format(LOG_SOURCE_TYPE_ID)
        )
        return

    filename = save_identifiers_csv(identifiers)
    print("Saved {} log source identifier(s) to {}".format(len(identifiers), filename))


def delete_session_files():
    if not created_files:
        print("No data files have been created in this session yet.")
        return

    for path in list(created_files):
        try:
            os.remove(path)
            print("Deleted {}".format(path))
            created_files.remove(path)
        except OSError as exc:
            print("ERROR: could not delete {}: {}".format(path, exc))


def print_menu():
    print("\nQRadar Generic Log Source Identifier Check")
    print(
        "  1 - Run search (log_source_type id={}, last 24h) and save "
        "identifiers to CSV".format(LOG_SOURCE_TYPE_ID)
    )
    print("  0 - Delete data file(s) created in this session")
    print("  q - Quit")


def main():
    while True:
        print_menu()
        choice = input("Select an option: ").strip().lower()

        if choice == "1":
            run_search_and_save()
        elif choice == "0":
            delete_session_files()
        elif choice in ("q", "quit", "exit"):
            print("Bye.")
            break
        else:
            print("Invalid option, try again.")


if __name__ == "__main__":
    main()
