<?php

namespace Tests\Feature;

use App\Models\Format;
use App\Models\Meeting;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

class CleanOrphanedFormatIdsMigrationTest extends TestCase
{
    use RefreshDatabase;

    private function runMigration(): void
    {
        $path = database_path('migrations/2025_12_31_233709_clean_orphaned_format_ids.php');
        $migration = require $path;
        $migration->up();
    }

    public function testMigrationPreservesSharedIdsWhenIdDiffersFromSharedId()
    {
        // The `formats` column in comdef_meetings_main stores shared_id_bigint
        // values, not id values. Each format has one row per language, so on
        // multi-language installs the auto-increment `id` diverges from
        // `shared_id_bigint`. Regression for issue #1490, where an earlier
        // version of this migration plucked `id` and wiped legitimate codes.
        Meeting::query()->delete();
        Format::query()->delete();

        Format::create([
            'shared_id_bigint' => 999999,
            'key_string' => 'W',
            'name_string' => 'Women',
            'lang_enum' => 'en',
            'format_type_enum' => 'FC3',
        ]);
        Format::create([
            'shared_id_bigint' => 999999,
            'key_string' => 'W',
            'name_string' => 'Mujeres',
            'lang_enum' => 'es',
            'format_type_enum' => 'FC3',
        ]);

        $meeting = Meeting::create([
            'service_body_bigint' => 1,
            'formats' => '999999',
        ]);

        $this->runMigration();

        $meeting->refresh();
        $this->assertEquals('999999', $meeting->formats);
    }

    public function testMigrationStripsSharedIdsThatNoLongerExist()
    {
        Meeting::query()->delete();
        Format::query()->delete();

        Format::create([
            'shared_id_bigint' => 999999,
            'key_string' => 'W',
            'name_string' => 'Women',
            'lang_enum' => 'en',
            'format_type_enum' => 'FC3',
        ]);

        $meeting = Meeting::create([
            'service_body_bigint' => 1,
            'formats' => '999999,888888',
        ]);

        $this->runMigration();

        $meeting->refresh();
        $this->assertEquals('999999', $meeting->formats);
    }

    public function testMigrationLeavesMeetingsWithEmptyFormatsAlone()
    {
        Meeting::query()->delete();
        Format::query()->delete();

        Format::create([
            'shared_id_bigint' => 999999,
            'key_string' => 'W',
            'name_string' => 'Women',
            'lang_enum' => 'en',
            'format_type_enum' => 'FC3',
        ]);

        $meetingEmpty = Meeting::create([
            'service_body_bigint' => 1,
            'formats' => '',
        ]);
        $meetingNull = Meeting::create([
            'service_body_bigint' => 1,
            'formats' => null,
        ]);

        $this->runMigration();

        $meetingEmpty->refresh();
        $meetingNull->refresh();
        $this->assertEquals('', $meetingEmpty->formats);
        $this->assertNull($meetingNull->formats);
    }
}
