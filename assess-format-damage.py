#!/usr/bin/env python3
"""
Assess the damage caused by the BMLT 4.1.0 orphan-format cleanup migration
(bmlt-server issue #1490) using Dijon snapshot history.

For each root server Dijon tracks, picks one snapshot on or before --before
(pre-damage) and one on or after --after (post-damage), then diffs the
`format_bmlt_ids` per meeting. Reports: meetings affected, total format
assignments lost, and which format keys were most commonly dropped.

Read-only against the Dijon API. No production servers are touched.

Usage:
    python3 assess-format-damage.py                      # all enabled servers
    python3 assess-format-damage.py --root-server 42     # one server
    python3 assess-format-damage.py --json > out.json    # machine-readable
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

DEFAULT_DIJON = "https://dijon-api.bmlt.dev"
# 4.1.0 was released 2026-02-01. Pick a pre snapshot from before, post from after.
DEFAULT_BEFORE = "2026-01-31"
DEFAULT_AFTER = "2026-02-01"
# versionInt threshold: 4.1.0 == 4_001_000. Only servers at or above this could
# have run the buggy orphan-cleanup migration.
MIN_AFFECTED_VERSION_INT = 4_001_000


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def http_get_json(url, timeout=120):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_server_version_int(server_url, timeout=30):
    """Return the BMLT server's versionInt, or None if unreachable/malformed."""
    url = server_url.rstrip("/") + "/client_interface/json/?switcher=GetServerInfo"
    try:
        data = http_get_json(url, timeout=timeout)
    except Exception:
        return None
    # versionInt comes back as a string like "4001000"; some older servers
    # may omit it. Fall back to parsing `version`.
    v = data.get("versionInt") if isinstance(data, dict) else None
    if isinstance(data, list) and data:
        v = data[0].get("versionInt") or (isinstance(data[0], dict) and data[0].get("versionInt"))
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def pick_pre(snapshots, before):
    candidates = [s for s in snapshots if s["date"] <= before]
    return max(candidates, key=lambda s: s["date"]) if candidates else None


def pick_post(snapshots, after, mode="earliest"):
    candidates = [s for s in snapshots if s["date"] >= after]
    if not candidates:
        return None
    return (max if mode == "latest" else min)(candidates, key=lambda s: s["date"])


def detect_upgrade_day(dijon, sid, since=None):
    """Walk snapshots and find the single day with the largest drop in
    total format assignments. Returns (pre_snapshot, post_snapshot, drop)
    or None if nothing looks like an upgrade event.

    Skips snapshot pairs where the post-meeting count collapses relative
    to pre (< 50% retained) — that signature is a Dijon fetch failure,
    not a real mass event, and would produce a misleading large drop."""
    snapshots = sorted(
        http_get_json(f"{dijon}/rootservers/{sid}/snapshots"),
        key=lambda s: s["date"],
    )
    if since:
        snapshots = [s for s in snapshots if s["date"] >= since]
    worst = None  # (drop, pre, post)
    prev = None
    prev_total = None
    prev_mcount = None
    for s in snapshots:
        meetings = http_get_json(
            f"{dijon}/rootservers/{sid}/snapshots/{s['date']}/meetings"
        )
        total = sum(len(m.get("format_bmlt_ids") or []) for m in meetings)
        mcount = len(meetings)
        if prev is not None:
            drop = prev_total - total
            broken_snapshot = (
                prev_mcount >= 10 and mcount < max(10, prev_mcount * 0.5)
            )
            if not broken_snapshot and (worst is None or drop > worst[0]):
                worst = (drop, prev, s)
        prev = s
        prev_total = total
        prev_mcount = mcount
    return worst


def assess_server(dijon, server, before, after, post_mode, detect, min_drop, since, skip_version_check, verbose):
    sid = server["id"]
    name = server.get("name") or ""

    if not skip_version_check:
        url = server.get("url") or ""
        version_int = fetch_server_version_int(url) if url else None
        if version_int is None:
            return {
                "server": server,
                "skipped": "version unreachable",
            }
        if version_int < MIN_AFFECTED_VERSION_INT:
            return {
                "server": server,
                "version_int": version_int,
                "skipped": f"pre-4.1.0 (versionInt={version_int})",
            }
        if verbose:
            print(f"[{sid}] {name}: versionInt={version_int}", file=sys.stderr)

    if detect:
        try:
            found = detect_upgrade_day(dijon, sid, since=since)
        except urllib.error.HTTPError as e:
            return {"server": server, "error": f"detect: {e.code}"}
        if not found or found[0] < min_drop:
            return {
                "server": server,
                "skipped": "no single-day drop exceeding threshold",
                "biggest_drop": found[0] if found else None,
            }
        drop, pre, post = found
        if verbose:
            print(f"[{sid}] {name}: detected upgrade pre={pre['date']} post={post['date']} drop={drop}", file=sys.stderr)
    else:
        try:
            snapshots = http_get_json(f"{dijon}/rootservers/{sid}/snapshots")
        except urllib.error.HTTPError as e:
            return {"server": server, "error": f"snapshots: {e.code}"}
        if not snapshots:
            return {"server": server, "skipped": "no snapshots"}
        pre = pick_pre(snapshots, before)
        post = pick_post(snapshots, after, post_mode)
        if not pre or not post:
            return {
                "server": server,
                "pre_date": pre["date"] if pre else None,
                "post_date": post["date"] if post else None,
                "skipped": "missing pre or post snapshot in window",
            }

    if verbose:
        print(
            f"[{sid}] {name}: pre={pre['date']} post={post['date']}",
            file=sys.stderr,
        )

    try:
        pre_meetings = http_get_json(
            f"{dijon}/rootservers/{sid}/snapshots/{pre['date']}/meetings"
        )
        post_meetings = http_get_json(
            f"{dijon}/rootservers/{sid}/snapshots/{post['date']}/meetings"
        )
    except urllib.error.HTTPError as e:
        return {"server": server, "error": f"meetings: {e.code}"}

    post_by_id = {m["bmlt_id"]: m for m in post_meetings}

    meetings_affected = 0
    total_lost = 0
    lost_by_format = defaultdict(int)
    sample_affected = []  # keep a few example meeting ids for spot-checking

    for pre_m in pre_meetings:
        mid = pre_m["bmlt_id"]
        post_m = post_by_id.get(mid)
        if not post_m:
            # Meeting was deleted between snapshots; not relevant to this bug.
            continue
        pre_ids = set(pre_m.get("format_bmlt_ids") or [])
        post_ids = set(post_m.get("format_bmlt_ids") or [])
        lost = pre_ids - post_ids
        if not lost:
            continue
        meetings_affected += 1
        total_lost += len(lost)
        key_by_id = {
            f["bmlt_id"]: f.get("key_string") or f"#{f['bmlt_id']}"
            for f in (pre_m.get("formats") or [])
        }
        for fid in lost:
            lost_by_format[key_by_id.get(fid, f"#{fid}")] += 1
        if len(sample_affected) < 5:
            sample_affected.append(
                {
                    "meeting_id": mid,
                    "name": pre_m.get("name"),
                    "lost_ids": sorted(lost),
                    "lost_keys": sorted(
                        {key_by_id.get(fid, f"#{fid}") for fid in lost}
                    ),
                }
            )

    return {
        "server": server,
        "pre_date": pre["date"],
        "post_date": post["date"],
        "pre_meetings": len(pre_meetings),
        "post_meetings": len(post_meetings),
        "meetings_affected": meetings_affected,
        "total_lost": total_lost,
        "lost_by_format": dict(lost_by_format),
        "sample_affected": sample_affected,
    }


def print_table(results):
    damaged = [
        r
        for r in results
        if not r.get("skipped")
        and not r.get("error")
        and r.get("meetings_affected", 0) > 0
    ]
    clean = [
        r
        for r in results
        if not r.get("skipped")
        and not r.get("error")
        and r.get("meetings_affected", 0) == 0
    ]
    skipped = [r for r in results if r.get("skipped")]
    errored = [r for r in results if r.get("error")]

    damaged.sort(key=lambda r: r["total_lost"], reverse=True)

    header = f"{'id':>5}  {'pre':>10}  {'post':>10}  {'mtgs':>6}  {'affected':>8}  {'lost':>6}  name / url"
    print(header)
    print("-" * len(header))
    for r in damaged:
        s = r["server"]
        label = f"{s.get('name') or ''} — {s.get('url') or ''}"
        print(
            f"{s['id']:>5}  {r['pre_date']:>10}  {r['post_date']:>10}  "
            f"{r['pre_meetings']:>6}  {r['meetings_affected']:>8}  "
            f"{r['total_lost']:>6}  {label}"
        )

    print()
    print(f"Damaged servers:   {len(damaged)}")
    print(
        f"  meetings affected: {sum(r['meetings_affected'] for r in damaged)}"
    )
    print(
        f"  assignments lost:  {sum(r['total_lost'] for r in damaged)}"
    )
    print(f"Clean servers:     {len(clean)}")
    skip_reasons = defaultdict(int)
    for r in skipped:
        skip_reasons[r["skipped"]] += 1
    print(f"Skipped:           {len(skipped)}")
    for reason, count in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason}: {count}")
    print(f"Errored:           {len(errored)}")

    if damaged:
        all_lost = defaultdict(int)
        for r in damaged:
            for k, v in r["lost_by_format"].items():
                all_lost[k] += v
        print()
        print("Top lost format keys (across all damaged servers):")
        for k, v in sorted(all_lost.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {k:<10}  {v}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dijon", default=DEFAULT_DIJON)
    ap.add_argument(
        "--before",
        default=DEFAULT_BEFORE,
        help=f"pre-damage snapshot date must be <= this (default {DEFAULT_BEFORE})",
    )
    ap.add_argument(
        "--after",
        default=DEFAULT_AFTER,
        help=f"post-damage snapshot date must be >= this (default {DEFAULT_AFTER})",
    )
    ap.add_argument(
        "--root-server",
        type=int,
        help="restrict to a single root server id",
    )
    ap.add_argument(
        "--include-disabled",
        action="store_true",
        help="also include servers Dijon has marked is_enabled=false",
    )
    ap.add_argument(
        "--post-mode",
        choices=["earliest", "latest"],
        default="earliest",
        help="when picking the post-snapshot: 'earliest' ≥ --after (tight, good when you know the upgrade date), or 'latest' (current state)",
    )
    ap.add_argument(
        "--detect-upgrade",
        action="store_true",
        help="auto-detect each server's upgrade day by finding the single largest day-over-day drop in total format assignments, and use that day's pre/post as the comparison",
    )
    ap.add_argument(
        "--min-drop",
        type=int,
        default=50,
        help="with --detect-upgrade, minimum single-day drop to flag as an upgrade event (default 50)",
    )
    ap.add_argument(
        "--since",
        default="2026-02-01",
        help="with --detect-upgrade, only walk snapshots on or after this date (default 2026-02-01, the 4.1.0 release date)",
    )
    ap.add_argument(
        "--skip-version-check",
        action="store_true",
        help="skip the pre-flight GetServerInfo call; by default we filter to servers on BMLT 4.1.0+",
    )
    ap.add_argument(
        "--version-only",
        action="store_true",
        help="just run the version check for each server and report; do not walk snapshots or run detection",
    )
    ap.add_argument(
        "--only-ids",
        help="comma-separated list of root server ids to process (skip all others)",
    )
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the tabular report",
    )
    args = ap.parse_args()

    servers = http_get_json(f"{args.dijon}/rootservers")
    if args.root_server:
        servers = [s for s in servers if s["id"] == args.root_server]
    elif args.only_ids:
        wanted = {int(x) for x in args.only_ids.split(",") if x.strip()}
        servers = [s for s in servers if s["id"] in wanted]
    elif not args.include_disabled:
        servers = [s for s in servers if s.get("is_enabled")]

    if args.version_only:
        out = []
        for s in servers:
            url = s.get("url") or ""
            v = fetch_server_version_int(url) if url else None
            entry = {"id": s["id"], "name": s.get("name"), "url": url, "versionInt": v}
            if args.verbose:
                print(f"[{s['id']}] {s.get('name')}: versionInt={v}", file=sys.stderr)
            out.append(entry)
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    results = []
    for s in servers:
        try:
            r = assess_server(
                args.dijon, s, args.before, args.after,
                args.post_mode, args.detect_upgrade, args.min_drop,
                args.since, args.skip_version_check, args.verbose,
            )
            if r:
                results.append(r)
        except Exception as e:
            print(f"[{s['id']}] unexpected error: {e}", file=sys.stderr)
            results.append({"server": s, "error": str(e)})

    if args.json:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_table(results)


if __name__ == "__main__":
    main()
