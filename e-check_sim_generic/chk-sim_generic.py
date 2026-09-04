#!/usr/bin/env python3
"""
QRadar Generic (Universal DSM) Log Source Identifier Check
============================================================

REST API v19 tool with a simple interactive menu:

  1 - Run an AQL search for log_source_type id=67 (Universal DSM / generic),
      grouped by "Log Source Identifier", over the last 24 hours. Waits
      1 minute for the search to run, checks the results, then saves each
      distinct log source identifier to generic-MM_HH-ddmmyy.csv.
      (see ariel_search.py)
  0 - Delete the data file(s) created during this run of the script.

CONFIG
------
Connection settings are read from chk-sim_generic.cfg, next to this
script (see chk-sim_generic.cfg.example for the format).
"""

import configparser
import os
import sys
from pathlib import Path

import urllib3

import ariel_search

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

VERIFY_SSL = False  # SSL certificate checking is disabled
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Files created by this script during the current run (for menu option 0).
created_files = []


def run_search_and_save():
    filename = ariel_search.run_search_and_save(QRADAR_CONSOLE, QRADAR_TOKEN, API_VERSION, VERIFY_SSL)
    if filename:
        created_files.append(filename)


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
        "identifiers to CSV".format(ariel_search.LOG_SOURCE_TYPE_ID)
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
