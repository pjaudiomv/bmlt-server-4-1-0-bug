# CONTEXT — bmlt-server 4.1.0 orphan-format cleanup bug

> This document is written for a future LLM or engineer picking up context cold.
> It captures the root cause, the investigation, the methodology of the damage
> assessment, the rationale for decisions, and the shape of the fix and recovery
> so that anyone returning to this incident later can reconstruct the reasoning
> without re-deriving it.

## 1. What BMLT is (just enough to follow the bug)

BMLT (Basic Meeting List Toolbox) is a self-hosted PHP/Laravel + Svelte app that
a regional NA (Narcotics Anonymous) service body runs to publish their meeting
schedule. Each deployment is an independent "root server" owned by its service
body. The BMLT project (github.com/bmlt-enabled/bmlt-server) publishes releases;
admins upgrade on their own schedule.

Dijon (`https://dijon-api.bmlt.dev`) is a community tool that crawls opted-in
root servers daily and snapshots their meeting/format/service-body data. Because
snapshots are per-day and read-only, Dijon is effectively an external ground-truth
log of every participating server's state over time. That property is load-bearing
for this incident.

## 2. BMLT's format model (the background the bug is about)

Formats are meeting codes: `W` (Women), `O` (Open), `STEP` (Step study), `WC`
(Wheelchair accessible), `BT` (Basic Text), etc. They live in the
`comdef_formats` table.

Key schema detail: **each logical format has one row per language.** So a
single format like "Women" exists as multiple rows:

```
id  | shared_id_bigint | lang_enum | key_string
----+------------------+-----------+-----------
30  | 30               | en        | W
130 | 30               | de        | W
230 | 30               | es        | M          (translated key)
...
```

- `id` is the auto-increment primary key of an individual row.
- `shared_id_bigint` is the cross-language identifier of the logical format.
- Meetings reference formats by `shared_id_bigint`, **not `id`.**

The meeting table stores format assignments as a comma-separated string in
`comdef_meetings_main.formats`:

```
formats = "4,17,30"   -- this meeting is "Closed, Open, Women"
```

Those values are `shared_id_bigint`s. Confirmed by three places:

- `app/Models/Format.php:54-60` — the `meetings()` relation filters by `shared_id_bigint`.
- `app/Http/Controllers/Admin/MeetingController.php:179` — validator is `exists:comdef_formats,shared_id_bigint`.
- `app/Repositories/MeetingRepository.php:893-899` — aggregator import maps source IDs to `shared_id_bigint` when writing `formats`.

## 3. The bug

Migration `2025_12_31_233709_clean_orphaned_format_ids.php` (shipped in BMLT
**4.1.0**, released 2026-02-01) was supposed to strip format IDs from meetings'
`formats` column that no longer existed in `comdef_formats`. The logic was right.
The lookup column was wrong:

```php
// WRONG
$validFormatIds = DB::table('comdef_formats')
    ->pluck('id')
    ->map(fn($id) => (string)$id)
    ->flip();
```

It built the "valid" set from `id` (one value per row, across all languages),
then checked each meeting's comma-separated `formats` (which are
`shared_id_bigint` values) against it. Any `shared_id_bigint` not happening to
appear as an `id` anywhere was stripped.

### Why it didn't always trigger

On a fresh install, the seed migration inserts formats in a predictable order
across multiple languages. With ~57 unique `shared_id_bigint` values and ~498
rows across 9 languages, the `id` range 1..498 trivially contains all
`shared_id_bigint` values 1..57. **The two sets coincide. The bug is a silent
no-op.** That is why the author (and every test environment using
`RefreshDatabase`) never saw the bug.

### When it *does* trigger

On servers where the `id`↔`shared_id_bigint` alignment has drifted:

- Formats deleted and re-created over time (new row, much higher `id`).
- Entire language translations wiped (deletes a contiguous range of low `id`s,
  so the remaining `id`s start at a value > some `shared_id_bigint`s).
- Databases migrated in from pre-v4 BMLT where format insertion order was
  different.
- Admin-created custom formats with manually-assigned `shared_id_bigint`s.

On drifted databases, many `shared_id_bigint` values no longer have any matching
`id` row, so the migration strips them from every meeting's `formats` string —
effectively unassigning those format codes from every meeting at once.

### The reporter signature (#1490)

An admin on the "Autonomy Zone" server (Dijon id 18, metrorichna.org) opened
bmlt-server#1490 reporting that "meeting codes like `W`, `STEP`, and others all
got deleted on our server when updated. ALL meetings." Confirmed: that server
upgraded 2026-03-14 and dropped 1,970 format assignments across 1,333 meetings
in one day.

## 4. Why this wasn't caught in review

- PR #1433 (the buggy migration) shipped with **no tests.**
- `RefreshDatabase` seeds from a single-language-first sequence where `id` ==
  `shared_id_bigint` by construction. Any test that tried to verify the
  migration against a fresh DB would see a no-op and think the migration was
  correct.
- The regression test we ultimately wrote
  (`src/tests/Feature/CleanOrphanedFormatIdsMigrationTest.php`) avoids this
  by creating a format with `shared_id_bigint = 999999` explicitly, ensuring
  no auto-increment `id` in the test DB will coincide.

## 5. The fix

One-line change in the migration — replace the wrong column:

```php
// FIXED
$validFormatIds = DB::table('comdef_formats')
    ->pluck('shared_id_bigint')
    ->unique()
    ->map(fn($id) => (string)$id)
    ->flip();
```

`unique()` isn't strictly necessary (the subsequent `flip()` deduplicates by
using values as keys), but it makes intent explicit.

## 6. Damage assessment methodology (Dijon sweep)

We didn't trust the `comdef_changes` audit trail for recovery — it's incremental
and the `before_object` might already have been overwritten by a post-damage
edit. Dijon's daily full-state snapshots are strictly better.

### The sweep tool

`assess-format-damage.py` (copied into this repo).

Algorithm:

1. `GET /rootservers` — list every server Dijon tracks (38 enabled).
2. **Pre-filter by version.** For each server, fetch
   `<url>/client_interface/json/?switcher=GetServerInfo` and read `versionInt`.
   Skip any server on BMLT < 4.1.0 (4_001_000) — the buggy migration can't have
   run on them.
    - Initial attempt failed on 10 servers because `urllib.request` sent
      `User-Agent: Python-urllib/3.x`, which Cloudflare-fronted BMLT installs
      blocked. Fixed by sending a real browser User-Agent.
3. **Detect the upgrade day per server.** Walk every Dijon snapshot for that
   server since 2026-02-01 and find the single day with the largest drop in
   total format assignments (sum of `len(format_bmlt_ids)` across all meetings
   in the snapshot).
    - Skip pairs where the post-meeting count collapses (< 50% of pre). That
      signature is a Dijon fetch failure (empty snapshot), not a real event.
      Discovered when server 29 (NA Minnesota) had a 408 → 0 meeting-count
      transition that inflated the drop metric to 990 with no real damage.
4. **Diff pre vs post per meeting.** For the detected upgrade day, fetch the
   pre and post snapshots' meetings. For each meeting in pre, find the same
   `bmlt_id` in post and compute `pre.format_bmlt_ids - post.format_bmlt_ids`.
    - Ignore meetings deleted between snapshots (not our bug).
    - Aggregate: how many meetings lost at least one format, how many
      assignments lost total, which format `key_string`s were most commonly
      dropped.
5. **Distinguish real damage from noise.** The migration's fingerprint is
   hundreds or thousands of meetings losing a small set of format keys on a
   single day. A handful of meetings each losing one random format key across
   a multi-week window is admin churn, not the bug.

### Why not just use comdef_changes for detection

Would work for assessment on individual servers we have DB access to, but
requires direct DB queries. Dijon is a public, read-only API. Running an
assessment against 38 servers from a laptop without asking 38 admins for DB
access is dramatically simpler.

### Scale

38 enabled Dijon-tracked servers:

- 22 on BMLT < 4.1.0 (unaffected, pre-filtered out)
- 8 confirmed damaged (see README table)
- 6 on 4.1.0+ but no migration-damage signature
- 1 flagged by detection but the drop was deleted meetings, not format wipes
- 1 showed 1 meeting / 1 assignment — below the bug's noise floor

Servers not tracked by Dijon aren't in the count. Those admins either work
from their own DB backups or contact the project.

## 7. Recovery: the artisan command

File: `src/app/Console/Commands/RestoreFormatsFromDijon.php` (copied here).

Invocation:

```
php artisan bmlt:RestoreFormatsFromDijon --dijon-id=<N> [--date=YYYY-MM-DD] [--dry-run] [--force]
```

For the 8 known-affected servers the `--date` auto-fills from a hardcoded table
in the command (`AFFECTED_SERVERS` const). Running with no args prints that table.

### Algorithm

1. Fetch meetings from the specified Dijon snapshot.
2. Build a set of currently-valid `shared_id_bigint`s from the local
   `comdef_formats` table. (If a format was legitimately deleted since the
   snapshot, we don't want to resurrect it.)
3. For each Dijon meeting, look up the local meeting by `bmlt_id` →
   `id_bigint`.
    - Skip meetings that no longer exist locally (admin deleted them
      post-damage — nothing to restore).
4. Compute `missing = (dijon_format_ids ∩ valid_shared_ids) - current_format_ids`.
5. If non-empty, set `new formats = current ∪ missing`, numerically sorted.
6. Show a preview table; confirm; write in a single DB transaction via raw
   `DB::table('comdef_meetings_main')->where(...)->update(['formats' => ...])`.

### Safety properties

- **Only adds, never removes.** Admins who manually re-added format codes post-
  damage keep their manual edits.
- **Idempotent.** Running the command a second time finds nothing to do because
  the first run closed every delta.
- **`--dry-run`** prints the full preview table without writing.
- **Validates against current formats** — can't resurrect genuinely deleted ones.
- **Writes via raw DB::table update**, bypassing Eloquent events. This
  deliberately skips audit-trail creation because we don't want the restoration
  to look like a user edit. The fact that it was done is captured in release
  notes and in the admin's terminal transcript.

### What it can't do

- Meetings that were deleted since the damage: unrecoverable; skipped.
- Formats deleted since the damage: not resurrected.
- Servers not in Dijon: no snapshot source, not supported by this command.
- Servers whose admin uploaded new meetings with format references after the
  damage: those meetings aren't in the pre-damage snapshot so they're
  left alone.

### Why not a recovery migration (we considered and rejected)

A migration that auto-ran recovery on upgrade would repeat the exact failure
mode that caused #1490: mass unsupervised mutation, no dry-run, no opt-in, one
bug in the logic damages every server again. We went with an opt-in artisan
command with a preview mode deliberately.

## 8. Decision log

- **Read-only sweep first, recovery tool second.** Don't touch production state
  until we know the blast radius.
- **Dijon, not audit trail.** Ground-truth full snapshots > incremental
  audit records.
- **Python for the sweep, PHP for the recovery command.** The sweep talks to an
  external API from a laptop; it's not part of the product. The recovery
  command is run on admins' servers; it belongs in the Laravel codebase.
- **Browser User-Agent for the version pre-filter.** Default `urllib` UA gets
  blocked by Cloudflare-fronted BMLT servers. Confirmed by 10 false-unreachable
  servers becoming reachable when UA changed.
- **Narrow, targeted re-sweeps over a blanket re-sweep.** When we discovered
  the UA problem after a full sweep, we ran a cheap `--version-only` pass on
  all 38 servers to produce per-server classification, then timeline-walked
  only the 4 new 4.1.0+ servers that hadn't been walked before. Saved ~70% of
  the HTTP work.
- **Hardcoded table in the command, not auto-detect from local URL.** The
  BMLT server determines its own URL from request context, which doesn't exist
  in an artisan CLI context. Asking the admin for `--dijon-id` (which they can
  look up from a published table) is more reliable than URL heuristics.
- **Not including borderline "1 meeting / 1 assignment" servers in the
  hardcoded table.** Those look like admin churn, not bug damage. Admins
  can still invoke the command manually if they disagree.

## 9. Artifacts shipped in 4.2.1

On the `main` branch of `bmlt-enabled/bmlt-server`:

- `src/database/migrations/2025_12_31_233709_clean_orphaned_format_ids.php` —
  the one-line fix
- `src/app/Console/Commands/RestoreFormatsFromDijon.php` — the recovery command
- `src/tests/Feature/CleanOrphanedFormatIdsMigrationTest.php` — regression test
  (3 cases; the key one fails on the old code, passes on the fix)
- `src/tests/Feature/RestoreFormatsFromDijonTest.php` — recovery command tests
  (5 cases, Http::fake)
- `CHANGELOG.md` — 4.2.1 entry

In this out-of-tree archive:

- `README.md` — short, human-readable
- `CONTEXT.md` — this file
- `RestoreFormatsFromDijon.php` — copy of the command
- `assess-format-damage.py` — the Dijon sweep tool
- `CleanOrphanedFormatIdsMigrationTest.php` — copy of the regression test

## 10. Data: server-by-server results

```
Dijon ID  Upgrade date   Meetings affected  Assignments lost  Server
--------  ------------   -----------------  ----------------  ------
18        2026-03-14            1,333             1,970       Autonomy Zone (metrorichna.org) — #1490 reporter
21        2026-04-15              375               622       NA Colorado
3         2026-02-02              498               528       Southeastern Zonal Forum
33        2026-02-02              326               487       German-Speaking Region (narcotics-anonymous.de)
5         2026-02-02              440               457       Western States Zonal Forum
45        2026-02-02              213               214       Chicagoland Region
9         2026-02-02              111               111       Texas, Louisiana, Mississippi, Arkansas
16        2026-04-03               95               100       Canadian Assembly
--------  ------------   -----------------  ----------------
TOTAL                          3,391             4,489
```

Most admins upgraded on Feb 2 — the day after release. The pre-damage
`--date` to pass to the recovery command is always the day *before* the upgrade
day.

## 11. Test the fix yourself

```bash
cd src
vendor/bin/phpunit tests/Feature/CleanOrphanedFormatIdsMigrationTest.php
# 3 tests, 4 assertions, OK

# Verify the test actually catches the bug by reverting line 24 of
# 2025_12_31_233709_clean_orphaned_format_ids.php to ->pluck('id'):
# 2 of 3 tests fail with:
#   Expected: '999999'
#   Actual:   ''
```

## 12. Pointers

- Upstream issue: https://github.com/bmlt-enabled/bmlt-server/issues/1490
- Offending PR: https://github.com/bmlt-enabled/bmlt-server/pull/1433
- Dijon API docs: https://dijon-api.bmlt.dev/docs
- Dijon OpenAPI: https://dijon-api.bmlt.dev/openapi.json
