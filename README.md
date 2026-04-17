# bmlt-server 4.1.0 orphan-format cleanup bug

## TL;DR

BMLT 4.1.0 shipped a migration (`2025_12_31_233709_clean_orphaned_format_ids`) that was meant to clean up stale format IDs from the meetings table. It compared meeting format references against the wrong column (`id` instead of `shared_id_bigint`), so on servers where `id` values had drifted out of alignment with `shared_id_bigint` values, it wiped most format assignments from most meetings.

## Blast radius

Detected by diffing pre- and post-upgrade Dijon snapshots across all 38 Dijon-tracked servers:

**8 servers damaged. 3,391 meetings affected. 4,489 format assignments lost.**

| Dijon ID | Server | Upgrade date | Meetings | Assignments lost |
|---:|---|---|---:|---:|
| 18 | Autonomy Zone | 2026-03-14 | 1,333 | 1,970 |
| 21 | NA Colorado | 2026-04-15 | 375 | 622 |
| 3 | Southeastern Zonal Forum | 2026-02-02 | 498 | 528 |
| 33 | German-Speaking Region | 2026-02-02 | 326 | 487 |
| 5 | Western States Zonal Forum | 2026-02-02 | 440 | 457 |
| 45 | Chicagoland Region | 2026-02-02 | 213 | 214 |
| 9 | TX/LA/MS/AR | 2026-02-02 | 111 | 111 |
| 16 | Canadian Assembly | 2026-04-03 | 95 | 100 |

## Fix + recovery

Shipped in **bmlt-server 4.2.1**:

1. The buggy migration was corrected (`pluck('id')` → `pluck('shared_id_bigint')->unique()`) so any server that hadn't yet upgraded won't get hit.
2. A regression test (`CleanOrphanedFormatIdsMigrationTest.php`) that seeds a format with `shared_id_bigint` intentionally ≠ `id`, runs the migration, and asserts the formats survive.
3. A one-shot artisan command (`bmlt:RestoreFormatsFromDijon`) that diffs against a Dijon pre-damage snapshot and restores missing format assignments. Only adds, never removes — any manually re-added codes are preserved. Idempotent.

### Option A — shell / artisan access (recommended)

Admins on the 8 affected servers run:

```
php artisan bmlt:RestoreFormatsFromDijon --dijon-id=<id> --dry-run
```

The pre-damage date auto-fills from a hardcoded table in the command. Preview with `--dry-run`, then run again without it to apply. See `RestoreFormatsFromDijon.php` in this repo.

### Option B — shared hosting, SQL console only

For admins on shared hosting with only phpMyAdmin / SQL-console access (no shell or artisan), use the standalone generator:

```
python3 generate-recovery-sql.py \
    --dijon-id=<id> \
    --bmlt-url=https://your-bmlt-server/main_server/ \
    --out=restore.sql
```

It talks to Dijon and your server's public `/client_interface/json/` endpoints (read-only), diffs them, and writes an `.sql` file of `UPDATE` statements you can paste into phpMyAdmin. Each statement has a comment showing the format keys being added. Only adds, never removes. Published meetings only — unpublished meetings require option A.

Run `python3 generate-recovery-sql.py --list` to see the affected server table.

## Files here

- `README.md` — this file
- `CONTEXT.md` — detailed context for future LLMs / reference
- `RestoreFormatsFromDijon.php` — copy of the Laravel artisan command
- `generate-recovery-sql.py` — standalone SQL generator for SQL-console-only admins
- `assess-format-damage.py` — the Dijon sweep tool that found the affected servers
- `CleanOrphanedFormatIdsMigrationTest.php` — the regression test
