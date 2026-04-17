#!/usr/bin/env python3
"""
Generate SQL UPDATE statements to restore meeting format assignments
wiped by the bmlt-server 4.1.0 orphan-format cleanup bug (#1490), for
admins who have phpMyAdmin / SQL console access but not shell or artisan.

This tool never touches your server. It reads:
  1. The pre-damage state from a Dijon snapshot (read-only public API).
  2. Your server's current published meetings + valid format list from
     your own public BMLT endpoints.

Then it diffs the two per-meeting and writes an .sql file of UPDATE
statements you can paste into phpMyAdmin (or any MySQL client).

Safety:
  - Only ADDS missing format codes; never removes any. If you manually
    re-added codes after the damage, your manual edits are preserved.
  - Excludes format codes that no longer exist in your current
    comdef_formats table.
  - Review the generated .sql before running it. Each UPDATE has a
    comment showing the format keys being added.

Limitations:
  - Published meetings only. The public BMLT API filters to
    published=1 and cannot return unpublished meetings. If some of
    your affected meetings are unpublished, this tool can't see them;
    you would need shell / artisan access, or temporarily publish
    them before generating SQL.
  - Assumes table prefix 'comdef_'. If your DB has a non-default
    prefix, sed s/comdef_/your_prefix_/g the generated file.

Usage:
  python3 generate-recovery-sql.py \\
      --dijon-id 18 \\
      --bmlt-url https://metrorichna.org/BMLT/main_server/ \\
      --out restore.sql

  # --date is auto-filled for known-affected servers; pass --date
  # YYYY-MM-DD to override or for servers not in the hardcoded list.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_DIJON = "https://dijon-api.bmlt.dev"

# Known servers affected by the 4.1.0 bug, with their pre-damage snapshot
# date (the last snapshot Dijon has before the upgrade-day format wipe).
# See CONTEXT.md in this repo for how these were identified.
AFFECTED_SERVERS = {
    3:  ("2026-02-01", "Southeastern Zonal Forum"),
    5:  ("2026-02-01", "Western States Zonal Forum"),
    9:  ("2026-02-01", "Texas, Louisiana, Mississippi, Arkansas"),
    16: ("2026-04-02", "Canadian Assembly"),
    18: ("2026-03-13", "Autonomy Zone"),
    21: ("2026-04-14", "NA Colorado"),
    33: ("2026-02-01", "German-Speaking Region"),
    45: ("2026-02-01", "Chicagoland Region"),
}


def http_get_json(url, timeout=120):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": BROWSER_UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dijon-id", type=int,
                    help="Dijon root_server_id for your server")
    ap.add_argument("--date", help="Pre-damage snapshot date (YYYY-MM-DD); "
                    "auto-filled for servers in the known-affected list")
    ap.add_argument("--bmlt-url", help="Your BMLT server URL, e.g. "
                    "https://example.org/main_server/")
    ap.add_argument("--dijon", default=DEFAULT_DIJON,
                    help=f"Dijon API base URL (default {DEFAULT_DIJON})")
    ap.add_argument("--table-prefix", default="comdef_",
                    help="DB table prefix (default comdef_)")
    ap.add_argument("--out", default="-",
                    help="Output file, or - for stdout (default -)")
    ap.add_argument("--list", action="store_true",
                    help="List known-affected servers and exit")
    args = ap.parse_args()

    if args.list:
        print("Dijon ID  Pre-damage date  Server")
        print("--------  ---------------  ------")
        for sid, (date, name) in sorted(AFFECTED_SERVERS.items()):
            print(f"{sid:>8}  {date:<15}  {name}")
        return

    if not args.dijon_id or not args.bmlt_url:
        ap.error("--dijon-id and --bmlt-url are required (or use --list)")

    if args.date is None:
        if args.dijon_id not in AFFECTED_SERVERS:
            ap.error(f"No auto-fill date for dijon-id={args.dijon_id}. "
                     "Pass --date YYYY-MM-DD explicitly.")
        args.date = AFFECTED_SERVERS[args.dijon_id][0]
        print(f"Using known pre-damage date for "
              f"{AFFECTED_SERVERS[args.dijon_id][1]}: {args.date}",
              file=sys.stderr)

    dijon_url = (
        f"{args.dijon}/rootservers/{args.dijon_id}"
        f"/snapshots/{args.date}/meetings"
    )
    print(f"Fetching Dijon pre-damage snapshot: {dijon_url}", file=sys.stderr)
    try:
        dijon_meetings = http_get_json(dijon_url)
    except urllib.error.HTTPError as e:
        sys.exit(f"Dijon fetch failed: HTTP {e.code}")

    dijon_by_id = {}
    for m in dijon_meetings:
        mid = parse_int(m.get("bmlt_id"))
        if mid is None:
            continue
        fmt_ids = set()
        for f in (m.get("format_bmlt_ids") or []):
            fi = parse_int(f)
            if fi is not None:
                fmt_ids.add(fi)
        dijon_by_id[mid] = fmt_ids

    base = args.bmlt_url.rstrip("/")
    meetings_url = (
        f"{base}/client_interface/json/?switcher=GetSearchResults"
        f"&data_field_key=id_bigint,format_shared_id_list"
    )
    formats_url = f"{base}/client_interface/json/?switcher=GetFormats&show_all=1"

    print(f"Fetching current published meetings: {meetings_url}", file=sys.stderr)
    current_meetings = http_get_json(meetings_url)
    current_by_id = {}
    for m in current_meetings:
        mid = parse_int(m.get("id_bigint"))
        if mid is None:
            continue
        fmt_str = m.get("format_shared_id_list") or ""
        fmt_ids = set()
        for tok in fmt_str.split(","):
            fi = parse_int(tok.strip())
            if fi is not None:
                fmt_ids.add(fi)
        current_by_id[mid] = fmt_ids

    print(f"Fetching current valid formats: {formats_url}", file=sys.stderr)
    current_formats = http_get_json(formats_url)
    valid_shared_ids = set()
    key_by_id = {}
    for f in current_formats:
        fid = parse_int(f.get("id"))
        if fid is not None:
            valid_shared_ids.add(fid)
            key_by_id[fid] = f.get("key_string") or str(fid)

    print(f"Dijon snapshot:   {len(dijon_by_id)} meetings", file=sys.stderr)
    print(f"Currently local:  {len(current_by_id)} published meetings, "
          f"{len(valid_shared_ids)} valid format shared_ids", file=sys.stderr)

    out_stream = sys.stdout if args.out == "-" else open(args.out, "w")

    def write(line=""):
        out_stream.write(line + "\n")

    header_lines = [
        "-- SQL recovery for bmlt-server 4.1.0 orphan-format cleanup bug (#1490)",
        f"-- Generated by generate-recovery-sql.py",
        f"-- Dijon id:              {args.dijon_id}",
        f"-- Pre-damage snapshot:   {args.date}",
        f"-- BMLT server:           {base}",
        f"-- Table prefix:          {args.table_prefix}",
        "--",
        "-- Review before running. Each UPDATE adds missing format codes only;",
        "-- no formats are ever removed. Safe to paste into phpMyAdmin.",
        "--",
    ]
    for line in header_lines:
        write(line)
    write()

    n_updates = 0
    n_added_total = 0
    n_missing_locally = 0
    for mid in sorted(dijon_by_id):
        dijon_ids = dijon_by_id[mid]
        current_ids = current_by_id.get(mid)
        if current_ids is None:
            n_missing_locally += 1
            continue
        missing = (dijon_ids - current_ids) & valid_shared_ids
        if not missing:
            continue
        new_ids = sorted(current_ids | missing)
        new_str = ",".join(str(i) for i in new_ids)
        added_keys = ", ".join(key_by_id.get(i, f"#{i}") for i in sorted(missing))
        table = f"{args.table_prefix}meetings_main"
        write(f"-- meeting {mid}: adding {added_keys}")
        write(f"UPDATE {table} SET formats = '{new_str}' "
              f"WHERE id_bigint = {mid};")
        write()
        n_updates += 1
        n_added_total += len(missing)

    write(f"-- {n_updates} UPDATE statements, "
          f"{n_added_total} format assignments to restore.")
    if n_missing_locally:
        write(f"-- {n_missing_locally} Dijon meetings have no published "
              "match locally (deleted or unpublished since the snapshot).")

    if args.out != "-":
        out_stream.close()

    print(f"\nWrote {n_updates} UPDATE statement(s) adding "
          f"{n_added_total} format assignments.", file=sys.stderr)
    if n_missing_locally:
        print(f"Skipped {n_missing_locally} Dijon meetings not currently "
              "visible via your public API (deleted or unpublished).",
              file=sys.stderr)
    if args.out != "-":
        print(f"SQL written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
