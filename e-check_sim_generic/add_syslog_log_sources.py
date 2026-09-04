"""
Add syslog-based Linux log sources (IP as log source identifier) from a
CSV of IPs, for hosts NOT covered by add_linux_log_sources.py.

For each IP in the CSV:
  - resolve its hostname via reverse DNS
  - skip it if the hostname is unknown, or if it matches s<digit>slp
    (that case is handled by add_linux_log_sources.py instead)
  - otherwise, run an Ariel search over the last SEARCH_MINUTES minutes:
      logsourceidentifier = <ip> AND payload matches PAYLOAD_REGEX
  - if the event count is greater than 0, create a new log source from
    the same template used by add_linux_log_sources.py (linux-ls-template.json)

Runs on the RHEL host that has network/DNS access to the target IPs.
"""

import requests

import ariel_search
from add_linux_log_sources import (
    HOSTNAME_PATTERN,
    LINUX_LS_IP_IDENTIFIER_TEMPLATE,
    build_new_log_source,
    create_log_source,
    load_template,
    read_ips_from_csv,
    remove_ip_from_csv,
    resolve_hostname,
)

DEBUG = "on"  # "on" or "off" - when "on", pause for Enter after each step

SEARCH_MINUTES = 15
PAYLOAD_REGEX = r"^\<\d{1,2}\>\w+\s+\d+\s+\S+\s+\S+\s+\w+\[\d+\]"


def debug_step(message):
    print(message)
    if DEBUG == "on":
        input("Press Enter to continue...")


def build_payload_check_aql(ip):
    return (
        "SELECT COUNT(*) AS event_count "
        "FROM events "
        "WHERE \"logsourceidentifier\" = '{ip}' "
        "AND UTF8(payload) IMATCHES '{regex}' "
        "LAST {minutes} MINUTES"
    ).format(ip=ip, regex=PAYLOAD_REGEX, minutes=SEARCH_MINUTES)


def check_syslog_events(console, token, api_version, verify_ssl, ip):
    """Run the payload-match search for one IP. Returns the event count."""
    aql = build_payload_check_aql(ip)
    debug_step("Running search for {}:\n  {}".format(ip, aql))

    search_id = ariel_search.create_search(console, token, api_version, verify_ssl, aql)
    debug_step("  Search created: {}".format(search_id))
    ariel_search.wait_for_search(console, token, api_version, verify_ssl, search_id)
    events = ariel_search.fetch_search_results(console, token, api_version, verify_ssl, search_id)

    if not events:
        return 0
    return events[0].get("event_count") or 0


def add_syslog_log_sources_from_csv(console, token, api_version, verify_ssl, csv_path):
    ips = read_ips_from_csv(csv_path)
    print("Found {} IP(s) in {}".format(len(ips), csv_path))
    if not ips:
        return

    debug_step("Loading template from {}...".format(LINUX_LS_IP_IDENTIFIER_TEMPLATE))
    try:
        template = load_template()
    except (OSError, ValueError) as exc:
        print("ERROR: could not load template {}: {}".format(LINUX_LS_IP_IDENTIFIER_TEMPLATE, exc))
        return

    created, skipped, failed = 0, 0, 0

    for ip in ips:
        debug_step("Resolving hostname for {}...".format(ip))
        hostname = resolve_hostname(ip)

        if not hostname:
            print("  {}: no reverse DNS entry, skipping.".format(ip))
            skipped += 1
            continue

        if HOSTNAME_PATTERN.match(hostname):
            print(
                "  {}: hostname {!r} matches s<digit>slp, "
                "handled by add_linux_log_sources.py instead, skipping.".format(ip, hostname)
            )
            skipped += 1
            continue

        try:
            event_count = check_syslog_events(console, token, api_version, verify_ssl, ip)
        except (requests.RequestException, RuntimeError) as exc:
            print("  {}: ERROR running search: {}".format(ip, exc))
            failed += 1
            continue

        print(
            "  {}: {} matching event(s) in the last {} minute(s).".format(
                ip, event_count, SEARCH_MINUTES
            )
        )

        if event_count <= 0:
            skipped += 1
            continue

        payload = build_new_log_source(template, ip, hostname)
        debug_step(
            "  {}: hostname {!r}, {} matching event(s). Will create log source "
            "name={!r}".format(ip, hostname, event_count, payload["name"])
        )

        try:
            create_log_source(console, token, api_version, verify_ssl, payload)
            print("  {}: log source created.".format(ip))
            created += 1
            remove_ip_from_csv(csv_path, ip)
        except (requests.RequestException, RuntimeError) as exc:
            print("  {}: ERROR creating log source: {}".format(ip, exc))
            failed += 1

    print(
        "\nDone. Created {}, skipped {}, failed {} (out of {} IP(s)).".format(
            created, skipped, failed, len(ips)
        )
    )
