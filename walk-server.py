#!/usr/bin/env python3
"""Walk every snapshot for a server and show total format assignments per day.
A migration-damage event will show up as a sudden drop between consecutive snapshots."""

import argparse
import json
import sys
import urllib.request

DEFAULT_DIJON = "https://dijon-api.bmlt.dev"


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dijon", default=DEFAULT_DIJON)
    ap.add_argument("--root-server", type=int, required=True)
    ap.add_argument("--since", default="2026-01-01",
                    help="only walk snapshots on or after this date")
    args = ap.parse_args()

    sid = args.root_server
    snapshots = sorted(
        [s for s in get(f"{args.dijon}/rootservers/{sid}/snapshots")
         if s["date"] >= args.since],
        key=lambda s: s["date"],
    )
    print(f"Server {sid}: {len(snapshots)} snapshots since {args.since}")
    print()
    print(f"{'date':<12}  {'meetings':>8}  {'total_fmts':>10}  {'mtgs_w_fmt':>10}  {'Δtotal':>8}  {'Δmtgs_w_fmt':>12}")
    print("-" * 80)

    prev_total = None
    prev_mtgs_with_fmts = None
    for s in snapshots:
        date = s["date"]
        try:
            meetings = get(f"{args.dijon}/rootservers/{sid}/snapshots/{date}/meetings")
        except Exception as e:
            print(f"{date:<12}  ERROR: {e}", file=sys.stderr)
            continue
        total = sum(len(m.get("format_bmlt_ids") or []) for m in meetings)
        with_fmts = sum(1 for m in meetings if m.get("format_bmlt_ids"))
        d_total = "" if prev_total is None else f"{total - prev_total:+d}"
        d_mtgs = "" if prev_mtgs_with_fmts is None else f"{with_fmts - prev_mtgs_with_fmts:+d}"
        print(f"{date:<12}  {len(meetings):>8}  {total:>10}  {with_fmts:>10}  {d_total:>8}  {d_mtgs:>12}")
        prev_total = total
        prev_mtgs_with_fmts = with_fmts


if __name__ == "__main__":
    main()
