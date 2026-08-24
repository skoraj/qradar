
Qradar qid offense notes · PY
#!/usr/bin/env python3
"""
Usage
------------------------------------------------------------------------
    python qradar_qid_offense_notes.py                # last 35 minutes
    python qradar_qid_offense_notes.py --days 3        # last 3 days
    python qradar_qid_offense_notes.py --qid 12345678  # override the QID
    python qradar_qid_offense_notes.py --dry-run        # don't write notes
"""
 
import argparse
import json
import os
import sys
import time
import re
from datetime import datetime, timedelta, timezone
 
import requests
import urllib3
 
API_VERSION = "28.0"
DEFAULT_QID = 28250180
DEFAULT_MINUTES_BACK = 35
 
# --------------------------------------------------------------------------
# QRadar connection settings
# --------------------------------------------------------------------------
QRADAR_HOST = ""          # e.g. "https://qradar.example.com"
QRADAR_TOKEN = ""         # your authorized-service SEC token
QRADAR_VERIFY_SSL = True  # set False for self-signed console certs
 
ACCOUNT_NAME_FIELD = "Account Name"
ASSIGNED_OFFENSES_FIELD = "Assigned Offenses"
USERNAME_FIELD = "Username"
 
NOTE_DEDUPE_INCLUDES_MAGNITUDE = False
 
SEARCH_POLL_INTERVAL_SECONDS = 3
SEARCH_POLL_TIMEOUT_SECONDS = 600
 
 
# --------------------------------------------------------------------------
# HTTP session helpers
# --------------------------------------------------------------------------
 
def build_session():
    host = os.environ.get("QRADAR_HOST") or QRADAR_HOST
    token = os.environ.get("QRADAR_TOKEN") or QRADAR_TOKEN
 
    if not host:
        sys.exit("ERROR: set QRADAR_HOST (env var or the constant at the top of this script).")
    if not token:
        sys.exit("ERROR: set QRADAR_TOKEN (env var or the constant at the top of this script).")
 
    env_verify_ssl = os.environ.get("QRADAR_VERIFY_SSL")
    if env_verify_ssl is not None:
        verify_ssl = env_verify_ssl.strip().lower() not in ("false", "0", "no")
    else:
        verify_ssl = QRADAR_VERIFY_SSL
 
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
 
    session = requests.Session()
    session.headers.update(
        {
            "SEC": token,
            "Version": API_VERSION,
            "Accept": "application/json",
        }
    )
    session.verify = verify_ssl
 
    return session, host.rstrip("/")
 
 
# --------------------------------------------------------------------------
# Ariel search (events)
# --------------------------------------------------------------------------
 
def build_aql(qid, start_ms, stop_ms):
    return (
        'SELECT "{account}" AS account_name, '
        '"{assigned}" AS assigned_offenses, '
        '"{username}" AS username '
        "FROM events "
        "WHERE QID = {qid} "
        "START {start_ms} STOP {stop_ms}"
    ).format(
        account=ACCOUNT_NAME_FIELD,
        assigned=ASSIGNED_OFFENSES_FIELD,
        username=USERNAME_FIELD,
        qid=qid,
        start_ms=start_ms,
        stop_ms=stop_ms,
    )
 
 
def create_search(session, host, aql):
    url = "{}/api/ariel/searches".format(host)
    resp = session.post(url, params={"query_expression": aql})
    resp.raise_for_status()
    data = resp.json()
    search_id = data.get("search_id") or data.get("cursor_id")
    if not search_id:
        sys.exit("ERROR: could not obtain search_id from response: {}".format(data))
    return search_id
 
 
def wait_for_search(session, host, search_id):
    url = "{}/api/ariel/searches/{}".format(host, search_id)
    deadline = time.monotonic() + SEARCH_POLL_TIMEOUT_SECONDS
 
    while True:
        resp = session.get(url)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
 
        if status == "COMPLETED":
            return data
        if status in ("CANCELED", "ERROR"):
            sys.exit("ERROR: Ariel search {} ended with status {}: {}".format(search_id, status, data))
        if time.monotonic() > deadline:
            sys.exit("ERROR: Ariel search {} did not complete within {}s".format(search_id, SEARCH_POLL_TIMEOUT_SECONDS))
 
        time.sleep(SEARCH_POLL_INTERVAL_SECONDS)
 
 
def fetch_search_results(session, host, search_id):
    url = "{}/api/ariel/searches/{}/results".format(host, search_id)
    resp = session.get(url)
    resp.raise_for_status()
    data = resp.json()
    return data.get("events", [])
 
 
def run_search(session, host, qid, start_ms, stop_ms):
    aql = build_aql(qid, start_ms, stop_ms)
    print("Running AQL search:\n  {}".format(aql))
    search_id = create_search(session, host, aql)
    wait_for_search(session, host, search_id)
    return fetch_search_results(session, host, search_id)
 
 
# --------------------------------------------------------------------------
# Offense helpers
# --------------------------------------------------------------------------
 
def split_offense_ids(raw_value):
    """'Assigned Offenses' may hold one id or a delimited list of ids."""
    if raw_value is None:
        return []
    if isinstance(raw_value, (list, tuple)):
        parts = raw_value
    else:
        parts = re.split(r"[,\s;]+", str(raw_value))
    ids = []
    for part in parts:
        part = str(part).strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            print("  WARNING: could not parse offense id from {!r}, skipping".format(part))
    return ids
 
 
def get_offense(session, host, offense_id):
    url = "{}/api/siem/offenses/{}".format(host, offense_id)
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json()
 
 
def get_offense_notes(session, host, offense_id):
    url = "{}/api/siem/offenses/{}/notes".format(host, offense_id)
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json()
 
 
def add_note_v2(session, host, offense_id, note_text):
    """Add a note to an offense (POST /api/siem/offenses/{id}/notes)."""
    url = "{}/api/siem/offenses/{}/notes".format(host, offense_id)
    resp = session.post(url, params={"note_text": note_text})
    resp.raise_for_status()
    return resp.json()
 
 
def note_already_exists(existing_notes, target_text):
    if NOTE_DEDUPE_INCLUDES_MAGNITUDE:
        for note in existing_notes:
            if note.get("note_text", "") == target_text:
                return True
        return False
 
    # Compare everything up to " with " so re-runs with a changed
    # magnitude don't produce duplicate notes for the same assignment.
    prefix = target_text.rsplit(" with ", 1)[0]
    for note in existing_notes:
        if note.get("note_text", "").startswith(prefix):
            return True
    return False
 
 
# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
 
def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--days",
        type=float,
        default=None,
        help="Search the last N days instead of the default {} minutes.".format(DEFAULT_MINUTES_BACK),
    )
    parser.add_argument(
        "--qid",
        type=int,
        default=DEFAULT_QID,
        help="QID to search for (default: {}).".format(DEFAULT_QID),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except actually POST new offense notes.",
    )
    return parser.parse_args()
 
 
def main():
    args = parse_args()
 
    now = datetime.now(timezone.utc)
    if args.days is not None:
        window = timedelta(days=args.days)
    else:
        window = timedelta(minutes=DEFAULT_MINUTES_BACK)
    start = now - window
 
    start_ms = int(start.timestamp() * 1000)
    stop_ms = int(now.timestamp() * 1000)
 
    session, host = build_session()
 
    events = run_search(session, host, args.qid, start_ms, stop_ms)
 
    if len(events) == 0:
        print("No events found for QID {} in the requested window. Nothing to do.".format(args.qid))
        return
 
    print("Found {} event(s) for QID {}.".format(len(events), args.qid))
 
    # Step: gather Account Name / Assigned Offenses / Username per event.
    gathered = []
    for event in events:
        account_name = event.get("account_name")
        username = event.get("username")
        offense_ids = split_offense_ids(event.get("assigned_offenses"))
 
        for offense_id in offense_ids:
            gathered.append(
                {
                    "offense_id": offense_id,
                    "account_name": account_name,
                    "username": username,
                }
            )
 
    print("Gathered {} offense assignment record(s):".format(len(gathered)))
    for row in gathered:
        print("  {}".format(json.dumps(row)))
 
    # De-duplicate identical (offense_id, account_name, username) triples
    # within this run before hitting the offense API repeatedly.
    seen = set()
    unique_assignments = []
    for row in gathered:
        key = (row["offense_id"], row["account_name"], row["username"])
        if key in seen:
            continue
        seen.add(key)
        unique_assignments.append(row)
 
    # Step: for each assignment, check magnitude + existing notes, add note if needed.
    for row in unique_assignments:
        offense_id = row["offense_id"]
        account_name = row["account_name"]
        username = row["username"]
 
        try:
            offense = get_offense(session, host, offense_id)
        except requests.HTTPError as exc:
            print("  ERROR: could not fetch offense {}: {}".format(offense_id, exc))
            continue
 
        magnitude = offense.get("magnitude")
 
        note_text = "Offense {} assigned to {} by {} with {}".format(
            offense_id, account_name, username, magnitude
        )
 
        try:
            existing_notes = get_offense_notes(session, host, offense_id)
        except requests.HTTPError as exc:
            print("  ERROR: could not fetch notes for offense {}: {}".format(offense_id, exc))
            continue
 
        if note_already_exists(existing_notes, note_text):
            print("  Offense {}: note already present, skipping.".format(offense_id))
            continue
 
        if args.dry_run:
            print("  Offense {}: [dry-run] would add note: {}".format(offense_id, note_text))
            continue
 
        try:
            add_note_v2(session, host, offense_id, note_text)
            print("  Offense {}: note added: {}".format(offense_id, note_text))
        except requests.HTTPError as exc:
            print("  ERROR: could not add note to offense {}: {}".format(offense_id, exc))
 
 
if __name__ == "__main__":
    main()
 


