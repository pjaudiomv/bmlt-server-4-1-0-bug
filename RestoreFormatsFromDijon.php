<?php

namespace App\Console\Commands;

use App\Models\Format;
use App\Models\Meeting;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;

class RestoreFormatsFromDijon extends Command
{
    protected $signature = 'bmlt:RestoreFormatsFromDijon
        {--dijon-id= : Dijon root_server_id for this server (see table below)}
        {--date= : Pre-damage snapshot date (YYYY-MM-DD); auto-filled for known affected servers}
        {--dijon-url=https://dijon-api.bmlt.dev : Dijon API base URL}
        {--dry-run : Show the plan without writing anything}
        {--force : Skip the confirmation prompt}';

    protected $description = 'Restore meeting format assignments wiped by the 4.1.0 orphan-cleanup migration by diffing against a Dijon snapshot. Only adds missing assignments; never removes.';

    /**
     * Known servers damaged by the 4.1.0 orphan-cleanup migration, identified via
     * a read-only sweep of Dijon snapshots. Each value is the pre-damage (last
     * known-good) snapshot date for that server — pass as --date when restoring.
     * See bmlt-enabled/bmlt-server#1490.
     *
     * If your server isn't in this list, either it wasn't affected, its BMLT
     * version was too old to run the buggy migration, or Dijon didn't have a
     * reachable GetServerInfo endpoint at sweep time. You can still run the
     * command by passing --dijon-id and --date explicitly.
     */
    private const AFFECTED_SERVERS = [
        3  => ['date' => '2026-02-01', 'name' => 'Southeastern Zonal Forum'],
        5  => ['date' => '2026-02-01', 'name' => 'Western States Zonal Forum'],
        9  => ['date' => '2026-02-01', 'name' => 'Texas, Louisiana, Mississippi, Arkansas'],
        16 => ['date' => '2026-04-02', 'name' => 'Canadian Assembly'],
        18 => ['date' => '2026-03-13', 'name' => 'Autonomy Zone'],
        21 => ['date' => '2026-04-14', 'name' => 'NA Colorado'],
        33 => ['date' => '2026-02-01', 'name' => 'German-Speaking Region'],
        45 => ['date' => '2026-02-01', 'name' => 'Chicagoland Region'],
    ];

    public function handle(): int
    {
        $dijonId = $this->option('dijon-id');
        $date = $this->option('date');
        $dijonUrl = rtrim($this->option('dijon-url'), '/');
        $dryRun = (bool)$this->option('dry-run');
        $force = (bool)$this->option('force');

        if ($dijonId && !$date) {
            $known = self::AFFECTED_SERVERS[(int)$dijonId] ?? null;
            if ($known) {
                $date = $known['date'];
                $this->info("Using known pre-damage snapshot date for {$known['name']} (Dijon id {$dijonId}): {$date}");
            }
        }

        if (!$dijonId || !$date) {
            $this->printUsage($dijonUrl);
            return 1;
        }

        $url = "{$dijonUrl}/rootservers/{$dijonId}/snapshots/{$date}/meetings";
        $this->info("Fetching {$url}");
        $response = Http::retry(3, 1000)->get($url);
        if (!$response->successful()) {
            $this->error("Dijon request failed: HTTP {$response->status()}");
            $this->line($response->body());
            return 1;
        }
        $dijonMeetings = collect($response->json());
        $this->info("Dijon snapshot: {$dijonMeetings->count()} meetings.");

        $validSharedIds = Format::query()->pluck('shared_id_bigint')->unique()->flip();
        $localMeetings = Meeting::query()->get()->keyBy('id_bigint');
        $this->info("Local server: {$localMeetings->count()} meetings, {$validSharedIds->count()} format shared_ids.");

        $plan = [];
        $skippedNotLocal = 0;
        foreach ($dijonMeetings as $dm) {
            $mid = (int)($dm['bmlt_id'] ?? 0);
            if (!$mid) {
                continue;
            }
            $local = $localMeetings->get($mid);
            if (!$local) {
                $skippedNotLocal++;
                continue;
            }

            $dijonIds = collect($dm['format_bmlt_ids'] ?? [])->map(fn ($id) => (int)$id);
            $currentIds = $local->formats
                ? collect(explode(',', $local->formats))->map(fn ($id) => (int)trim($id))->filter()
                : collect();

            $missing = $dijonIds
                ->diff($currentIds)
                ->filter(fn ($id) => $validSharedIds->has($id))
                ->values();

            if ($missing->isEmpty()) {
                continue;
            }

            $newFormats = $currentIds->merge($missing)->unique()->sort()->values()->join(',');
            $keyByFormatId = collect($dm['formats'] ?? [])->keyBy('bmlt_id');
            $addedKeys = $missing
                ->map(fn ($id) => $keyByFormatId->get($id)['key_string'] ?? "#{$id}")
                ->join(', ');

            $plan[] = [
                'meeting_id' => $mid,
                'name' => mb_strimwidth((string)($dm['name'] ?? ''), 0, 40, '…'),
                'current' => (string)($local->formats ?? ''),
                'restored' => $newFormats,
                'added' => $addedKeys,
                'added_count' => $missing->count(),
            ];
        }

        if ($skippedNotLocal) {
            $this->line("Skipped {$skippedNotLocal} Dijon meetings that no longer exist locally.");
        }

        if (empty($plan)) {
            $this->info('No meetings need restoration.');
            return 0;
        }

        $displayRows = array_map(
            fn ($p) => [$p['meeting_id'], $p['name'], $p['current'], $p['restored'], $p['added']],
            $plan
        );
        $this->table(['Meeting', 'Name', 'Current', 'Restored', 'Added'], $displayRows);
        $totalAdded = array_sum(array_column($plan, 'added_count'));
        $this->info(sprintf('%d meetings would gain %d format assignments.', count($plan), $totalAdded));

        if ($dryRun) {
            $this->info('(--dry-run; no changes written)');
            return 0;
        }

        if (!$force && !$this->confirm('Apply these changes?', false)) {
            $this->info('Aborted.');
            return 0;
        }

        DB::transaction(function () use ($plan) {
            foreach ($plan as $p) {
                DB::table('comdef_meetings_main')
                    ->where('id_bigint', $p['meeting_id'])
                    ->update(['formats' => $p['restored']]);
            }
        });
        $this->info(sprintf('Updated %d meetings, restored %d format assignments.', count($plan), $totalAdded));
        return 0;
    }

    private function printUsage(string $dijonUrl): void
    {
        $this->error('Missing --dijon-id. For known affected servers the pre-damage date is auto-filled.');
        $this->line('');
        $this->line('Servers known to be affected by the 4.1.0 orphan-cleanup bug (#1490):');
        $rows = [];
        foreach (self::AFFECTED_SERVERS as $id => $info) {
            $rows[] = [$id, $info['name'], $info['date']];
        }
        $this->table(['Dijon id', 'Server', 'Pre-damage date'], $rows);
        $this->line('Example:');
        $this->line('  php artisan bmlt:RestoreFormatsFromDijon --dijon-id=18 --dry-run');
        $this->line('');
        $this->line("If your server is not in this table but you know it was affected,");
        $this->line("find your Dijon id at  {$dijonUrl}/rootservers");
        $this->line("and pick a pre-damage snapshot date (usually the day before you upgraded to 4.1.0)");
        $this->line("from {$dijonUrl}/rootservers/<id>/snapshots, then pass both --dijon-id and --date.");
    }
}
