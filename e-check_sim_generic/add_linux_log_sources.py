"""
Add Linux log sources (IP as log source identifier) from a CSV of IPs.

For each IP in the CSV:
  - resolve its hostname via reverse DNS
  - if the (short) hostname starts with s<digit>slp, create a new log
    source cloned from the template log source (TEMPLATE_LOG_SOURCE_ID),
    with:
      name       = "LinuxServer @ <ip> [<hostname>]"
      identifier = <ip>
    and every other field copied from the template as-is.

Runs on the RHEL host that has network/DNS access to the target IPs.
"""

import csv
import re
import socket

import requests

DEBUG = "on"  # "on" or "off" - when "on", pause for Enter after each step

TEMPLATE_LOG_SOURCE_ID = 977172
LOG_SOURCES_ENDPOINT = "/api/config/event_sources/log_source_management/log_sources"

HOSTNAME_PATTERN = re.compile(r"^s\dslp", re.IGNORECASE)
IP_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# Fields present on a GET'd log source that must not be sent back when
# creating a new one (server-generated / read-only). Adjust if your
# QRadar version's schema differs.
STRIP_FIELDS = (
    "id",
    "creation_date",
    "modification_date",
    "last_event_time",
    "status",
    "auto_discovered",
    "average_eps",
    "internal",
    "requires_deploy",
)


def debug_step(message):
    print(message)
    if DEBUG == "on":
        input("Press Enter to continue...")


def _headers(token, api_version):
    return {
        "SEC": token,
        "Version": api_version,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def read_ips_from_csv(csv_path):
    """Pull every cell that looks like an IPv4 address out of the CSV,
    de-duplicated, preserving first-seen order."""
    ips = []
    seen = set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            for cell in row:
                cell = cell.strip()
                if IP_PATTERN.match(cell) and cell not in seen:
                    seen.add(cell)
                    ips.append(cell)
    return ips


def resolve_hostname(ip):
    """Reverse-DNS lookup. Returns the short hostname (before the first
    dot), or None if it doesn't resolve."""
    try:
        fqdn = socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return None
    return fqdn.split(".")[0]


def get_template_log_source(console, token, api_version, verify_ssl):
    url = "{}{}/{}".format(console.rstrip("/"), LOG_SOURCES_ENDPOINT, TEMPLATE_LOG_SOURCE_ID)
    resp = requests.get(url, headers=_headers(token, api_version), verify=verify_ssl, timeout=60)
    if not resp.ok:
        raise RuntimeError(
            "{} {} fetching template log source {}: {}".format(
                resp.status_code, resp.reason, TEMPLATE_LOG_SOURCE_ID, resp.text
            )
        )
    return resp.json()


def build_new_log_source(template, ip, hostname):
    payload = dict(template)
    for field in STRIP_FIELDS:
        payload.pop(field, None)

    # Per-parameter ids are specific to the template's stored rows; a new
    # log source needs fresh ones assigned by QRadar.
    if isinstance(payload.get("protocol_parameters"), list):
        payload["protocol_parameters"] = [
            {k: v for k, v in param.items() if k != "id"}
            for param in payload["protocol_parameters"]
        ]

    payload["name"] = "LinuxServer @ {} [{}]".format(ip, hostname)
    payload["identifier"] = ip
    return payload


def create_log_source(console, token, api_version, verify_ssl, payload):
    url = "{}{}".format(console.rstrip("/"), LOG_SOURCES_ENDPOINT)
    resp = requests.post(
        url, headers=_headers(token, api_version), json=payload, verify=verify_ssl, timeout=60
    )
    if not resp.ok:
        raise RuntimeError(
            "{} {} creating log source {!r}: {}".format(
                resp.status_code, resp.reason, payload.get("name"), resp.text
            )
        )
    return resp.json()


def add_linux_log_sources_from_csv(console, token, api_version, verify_ssl, csv_path):
    ips = read_ips_from_csv(csv_path)
    print("Found {} IP(s) in {}".format(len(ips), csv_path))
    if not ips:
        return

    debug_step("Fetching template log source {}...".format(TEMPLATE_LOG_SOURCE_ID))
    try:
        template = get_template_log_source(console, token, api_version, verify_ssl)
    except (requests.RequestException, RuntimeError) as exc:
        print("ERROR: {}".format(exc))
        return

    created, skipped, failed = 0, 0, 0

    for ip in ips:
        debug_step("Resolving hostname for {}...".format(ip))
        hostname = resolve_hostname(ip)

        if not hostname:
            print("  {}: no reverse DNS entry, skipping.".format(ip))
            skipped += 1
            continue

        if not HOSTNAME_PATTERN.match(hostname):
            print("  {}: hostname {!r} does not match s<digit>slp, skipping.".format(ip, hostname))
            skipped += 1
            continue

        payload = build_new_log_source(template, ip, hostname)
        debug_step(
            "  {}: hostname {!r} matches. Will create log source "
            "name={!r} identifier={!r}".format(ip, hostname, payload["name"], payload["identifier"])
        )

        try:
            create_log_source(console, token, api_version, verify_ssl, payload)
            print("  {}: log source created.".format(ip))
            created += 1
        except (requests.RequestException, RuntimeError) as exc:
            print("  {}: ERROR creating log source: {}".format(ip, exc))
            failed += 1

    print(
        "\nDone. Created {}, skipped {}, failed {} (out of {} IP(s)).".format(
            created, skipped, failed, len(ips)
        )
    )
