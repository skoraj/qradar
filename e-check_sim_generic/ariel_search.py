"""
Ariel search: log_source_type id=67 (Universal DSM / generic), grouped by
log source identifier, over the last 24 hours. Saves the distinct
identifiers to generic-MM_HH-ddmmyy.csv.
"""

import csv
import time
from datetime import datetime
from urllib.parse import quote

import requests

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


def _headers(token, api_version):
    return {
        "SEC": token,
        "Version": api_version,
        "Accept": "application/json",
    }


def create_search(console, token, api_version, verify_ssl, aql):
    # QRadar's Ariel API does not reliably URL-decode query_expression when
    # passed as a URL query string (neither "+" nor "%20" get decoded).
    # Send it as an application/x-www-form-urlencoded POST body instead,
    # where decoding is unambiguous.
    url = "{}/api/ariel/searches".format(console.rstrip("/"))
    headers = _headers(token, api_version)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = "query_expression=" + quote(aql, safe="")
    resp = requests.post(url, headers=headers, data=body, verify=verify_ssl, timeout=60)
    if not resp.ok:
        raise RuntimeError(
            "{} {} creating search: {}".format(resp.status_code, resp.reason, resp.text)
        )
    data = resp.json()
    search_id = data.get("search_id") or data.get("cursor_id")
    if not search_id:
        raise RuntimeError("Could not obtain search_id from response: {}".format(data))
    return search_id


def get_search_status(console, token, api_version, verify_ssl, search_id):
    url = "{}/api/ariel/searches/{}".format(console.rstrip("/"), search_id)
    resp = requests.get(url, headers=_headers(token, api_version), verify=verify_ssl, timeout=60)
    resp.raise_for_status()
    return resp.json()


def wait_for_search(console, token, api_version, verify_ssl, search_id):
    print("Waiting {} second(s) for the search to run...".format(SEARCH_WAIT_SECONDS))
    time.sleep(SEARCH_WAIT_SECONDS)

    deadline = time.monotonic() + SEARCH_POLL_TIMEOUT_SECONDS
    while True:
        data = get_search_status(console, token, api_version, verify_ssl, search_id)
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


def fetch_search_results(console, token, api_version, verify_ssl, search_id):
    url = "{}/api/ariel/searches/{}/results".format(console.rstrip("/"), search_id)
    resp = requests.get(url, headers=_headers(token, api_version), verify=verify_ssl, timeout=120)
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
    return filename


def run_search_and_save(console, token, api_version, verify_ssl):
    """Run the search and save results to CSV.

    Returns the CSV filename, or None if nothing was found or the request
    failed (the error is printed either way).
    """
    print(
        "Starting search: log_source_type id={}, last 24 hours, "
        "grouped by Log Source Identifier".format(LOG_SOURCE_TYPE_ID)
    )
    print("AQL: {}".format(AQL_QUERY))

    try:
        search_id = create_search(console, token, api_version, verify_ssl, AQL_QUERY)
        print("Search created: {}".format(search_id))
        wait_for_search(console, token, api_version, verify_ssl, search_id)
        events = fetch_search_results(console, token, api_version, verify_ssl, search_id)
    except (requests.RequestException, RuntimeError) as exc:
        print("ERROR: {}".format(exc))
        return None

    identifiers = sorted({e.get("ls_identifier") for e in events if e.get("ls_identifier")})

    if not identifiers:
        print(
            "No log source identifiers found for log_source_type id={} "
            "in the last 24 hours.".format(LOG_SOURCE_TYPE_ID)
        )
        return None

    filename = save_identifiers_csv(identifiers)
    print("Saved {} log source identifier(s) to {}".format(len(identifiers), filename))
    return filename
