# Immich period importer

This is a small, dry-run-first bridge for reviewing photos from Immich before
creating an AdventureLog trip. It reads photo metadata for an inclusive date
period and writes a portable JSON manifest. It never modifies Immich, and only
calls AdventureLog when `--apply --confirm CREATE` is explicitly supplied.

## Quick start

```sh
export IMMICH_BASE_URL=http://immich.example
export IMMICH_API_KEY='your-key'
python3 immich_period_import.py \
  --start 2026-08-01 --end 2026-08-07 \
  --output august-trip.json
```

The script uses Immich's metadata search endpoint (`POST /api/search/metadata`)
and paginates results. If a proxy or server version exposes another path, use
`--search-path`. API keys are only sent in the request header and are never
written to the manifest.

For local use, copy `.env.example` to `.env` and fill in the values. The script
loads only that `.env` beside the script, with existing shell environment
variables taking precedence; it never prints or writes secret values.

For offline testing, pass an Immich response or an array of asset objects:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --output manifest.json
```

The AdventureLog writer is available only when explicitly confirmed. It creates
one AdventureLog location, one visit, and then attaches each manifest asset:

```sh
export ADVENTURELOG_BASE_URL=http://adventurelog.example
export ADVENTURELOG_TOKEN='al_your-adventurelog-api-key'
python3 immich_period_import.py \
  --start 2026-08-01 --end 2026-08-07 \
  --output august-trip.json \
  --trip-name 'August holiday' --apply --confirm CREATE
```

The location and visit are created through `/api/locations` and `/api/visits`
with the configured IANA timezone (default `Europe/Berlin`). The existing AdventureLog Immich integration must be configured for the same
user, since the image attachment request references Immich asset IDs. Always
run a dry-run and review the manifest first. If attachment fails after the
location is created, the script reports the created location and number of
attachments; review/remove that draft in AdventureLog and retry attachment
carefully. Older deployments can adapt endpoint paths with `--location-path`,
`--image-path`, and `--content-type`. The defaults match the currently
deployed instance (`/api/locations` and `/api/images` without trailing
slashes); other AdventureLog releases or reverse proxies may require overrides.
AdventureLog authentication uses the `X-API-Key` header; provide the key
(typically starting with `al_`) through `ADVENTURELOG_TOKEN`.

If an earlier run created the location and stopped while attaching images, use
resume mode. It fetches the existing location, skips already attached Immich
IDs, and never creates another location or visit:

```sh
python3 immich_period_import.py \
  --start 2026-08-01 --end 2026-08-07 \
  --input-json august-trip.json --output august-trip.json \
  --location-id 20b54f0f-4109-4ed4-b5b2-474b99eafb00 \
  --apply --confirm CREATE
```

`--location-id` can also be set with `ADVENTURELOG_LOCATION_ID`. This is the
safe recovery path for partial image attachment failures. Use a version of
this script containing the manifest-ID deduplication fix before resuming; it
will not POST the same Immich ID twice during one run. This tool does not
delete existing duplicate association records.

## Collection rebuild plan

To organize a period into city-based locations, pass `--collection-name`.
Without `--apply`, this writes a dry-run plan grouped by normalized country and
city. Groups meeting the threshold become city locations. Smaller GPS groups
fold into the nearest retained city in the same country; countries without a
retained city, and assets without GPS, become one `Travel / Other` group. Change
the threshold with `--min-location-assets`:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --collection-name 'Italy holiday' \
  --output italy-rebuild-plan.json
```

After reviewing the plan, the guarded apply creates one collection, locations
attached to that collection, visits, and only unique assets in each group. It
does not delete or alter legacy flat locations:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --collection-name 'Italy holiday' \
  --output italy-rebuild-plan.json --apply --confirm CREATE
```

The collection endpoint defaults to `/api/collections`; override it with
`--collection-path` or `ADVENTURELOG_COLLECTION_PATH` for another release.

Optionally enrich the dry-run plan with nearby named OpenStreetMap POI
suggestions. This is read-only, never renames a location, and is intended for
manual review before any apply:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --collection-name 'Italy holiday' \
  --suggest-pois --poi-radius 100 --output italy-rebuild-plan.json
```

The lookup is a nearby named-POI query against OpenStreetMap's Overpass API;
each candidate includes both its OSM object link and an OpenStreetMap map link.
Results (including provider failures) are cached in `poi-cache.json`, and a
named User-Agent is sent. The default public-service guard permits at most four
uncached requests per run, with a two-second gap and a 20-second timeout. Tune
those limits only for an approved/private endpoint with `--overpass-url`,
`--poi-max-requests`, `--poi-min-interval`, and `--poi-timeout`; use
`--poi-max-requests 0` for cache-only operation or `--no-poi` for generic
labels with no public lookup. A timeout, HTTP error, or request-cap result is
recorded per group in `poi_errors`; the complete rebuild plan is still written.

For long imports, use checkpointed batches. The default state file is
`rebuild-state.json` beside the output; `--state-file` overrides it. The
collection, locations, and visits are created once, then image associations are
checkpointed after every successful POST. Re-running with the same state file
verifies existing locations and resumes without recreating them:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --collection-name 'Italy holiday' \
  --output italy-rebuild-plan.json --state-file italy-rebuild-state.json \
  --batch-size 50 --apply --confirm CREATE
```

`--batch-size 0` (the default) attaches all remaining images in one run.
Use `--parallelism 4` to run up to four attachment requests concurrently;
the default is `1`. State writes remain serialized and checkpointed after each
successful attachment. Run only one importer process per state file at a time;
multiple concurrent processes can still race before either one checkpoints.

If a previous overlap created duplicate AdventureLog image associations, use
the state-based dedupe report first. It only reads AdventureLog associations
and never contacts Immich:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --state-file italy-rebuild-state.json --dedupe \
  --dedupe-report italy-dedupe.json
```

Review the report. Only if the listed association IDs are correct, run the
separate destructive confirmation command:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --state-file italy-rebuild-state.json --dedupe-apply \
  --dedupe-confirm DELETE_DUPLICATES --dedupe-report italy-dedupe.json
```

This deletes only duplicate `/api/images/{association-id}` records, retains
one association per `immich_id` (preferring primary), then re-fetches each
location and verifies uniqueness. It never deletes Immich assets or originals.
For cautious cleanup, add `--dedupe-batch-size 50`; each apply recomputes fresh
duplicates, deletes at most that many rows, and reports what remains. Re-run
the same command to finish later batches; reports are never used as deletion
authority.

## Itinerary days from photo dates

The deployed v0.13 API uses `/api/itinerary-days` for day metadata and
`/api/itineraries` for dated location items. Preview generic days first:

```sh
python3 immich_period_import.py --start 2026-08-01 --end 2026-08-07 \
  --input-json response.json --collection-name 'Italy holiday' \
  --state-file italy-rebuild-state.json --create-itinerary-days \
  --output italy-itinerary-plan.json
```

After reviewing the plan, add `--apply --confirm CREATE`. This requires the
existing clean rebuild state, creates missing `Day N` records, and adds each
matching grouped Location once per relevant date. Re-running is idempotent;
existing day/item records are detected first. Override the endpoints with
`--itinerary-day-path` and `--itinerary-path` if needed.

Group visits preserve the earliest/latest `taken_at` timestamps as
`start_at`/`end_at` in the plan and visit payload. Existing checkpointed
locations can be updated explicitly with `--update-visits`; this GETs each
stored location, finds its visit, and PATCHes only its dates/timezone. The
default `--group-timezone-policy country` uses `Europe/Rome` for Italy groups
and `Europe/Berlin` for `Travel / Other`; use `--group-timezone-policy fixed`
to use `--timezone` for every group. No update occurs without the explicit
`--update-visits` flag and an existing state file.
Use `--visits-only` together with `--update-visits` when only visit timestamps
should change; it validates checkpoint groups, performs no image POSTs, and
does not modify attachment state.

## Safe Sample City day-stop pilot

`--day-stop-pilot` is an intentionally narrow replacement for the shared-city
gallery model. It is locked to the existing **Sample City trip** collection and
**2026-09-03** (`Day 1`), and selects at most 30 assets. It groups the selected
photos sequentially by capture time and GPS proximity, creates one new
AdventureLog Location per stop, and attaches only that stop's selected asset
IDs. Each Visit uses the actual earliest/latest capture timestamp in
`Europe/Rome`.

The source criterion is capture date only: all selected assets must have a
Rome-local capture date of `2026-09-03`. `Sample City trip` is resolved separately
and exactly as the existing **AdventureLog** target collection only during the
guarded apply; it is not assumed to be an Immich album.

Run this first; it writes a reviewable plan and never calls AdventureLog. The
plan includes all source/selected IDs, proposed labels with POI confidence and
source, local timestamp ranges, GPS centres, and the exact location/visit/day/
itinerary payloads that an apply would use. Nearby named POIs are looked up
through OpenStreetMap/Overpass and cached beside the output. The plan includes
an OSM object link and a map link for every candidate. Public-service defaults
are deliberately conservative: at most four uncached requests per run, at
least two seconds apart, with a 20-second timeout. Unavailable, rate-limited,
or weak POI results use a distinct generic city/time-of-day label (for example,
`Naples — Morning stop 1`, `Naples — Morning stop 2`).

```sh
python3 immich_period_import.py --day-stop-pilot \
  --start 2026-09-03 --end 2026-09-03 \
  --poi-max-requests 4 --poi-min-interval 2 --poi-timeout 20 \
  --output sample-pilot-plan.json
```

Only after reviewing that plan, use the guarded apply. The checkpoint makes it
safe to resume after an interruption without creating duplicate locations,
visits, itinerary items, or image associations:

```sh
python3 immich_period_import.py --day-stop-pilot \
  --start 2026-09-03 --end 2026-09-03 \
  --input-json sample-trip.json --output sample-pilot-plan.json \
  --state-file sample-pilot-state.json --apply --confirm CREATE
```

### Background-safe isolated pilot pipeline

For the already-created isolated pilot collection, retain its checkpoint and
use its own state/output/cache files. First run the dry-run and inspect the
plan (especially `groups[].label`, `poi_candidates`, and the map links). It
reads credentials only from the local `.env` and makes no AdventureLog writes:

```sh
cd /media/ppg91/Data/projects/immich-adventure-import
python3 immich_period_import.py --isolated-day-stop-pilot \
  --start 2026-09-03 --end 2026-09-03 --pilot-max-photos 30 \
  --output sample-isolated-pilot-plan.json \
  --state-file sample-isolated-pilot-state.json \
  --poi-cache sample-isolated-pilot-poi-cache.json \
  --poi-max-requests 4 --poi-min-interval 2 --poi-timeout 20
```

After review, the complete resumable apply can run in the background. The
checkpoint is written after each remote object/link, so do not start a second
process using the same state file. Capture the PID and follow the log:

```sh
nohup python3 immich_period_import.py --isolated-day-stop-pilot \
  --start 2026-09-03 --end 2026-09-03 --pilot-max-photos 30 \
  --output sample-isolated-pilot-plan.json \
  --state-file sample-isolated-pilot-state.json \
  --poi-cache sample-isolated-pilot-poi-cache.json \
  --poi-max-requests 4 --poi-min-interval 2 --poi-timeout 20 \
  --apply --confirm CREATE > sample-isolated-pilot.log 2>&1 &
echo $! > sample-isolated-pilot.pid
tail -f sample-isolated-pilot.log
```

If no new public lookup is wanted on a resume, add `--poi-max-requests 0`
(cache-only) or `--no-poi` (generic labels only). Keep the same plan inputs and
checkpoint: a different POI label can change a stop's generated Location name.

The apply first resolves exactly one existing collection named `Sample City trip`.
It creates `Day 1` only if that date has no day record and adds only the new
stop Locations to it. It never alters or deletes legacy locations, visits,
collections, associations, or itinerary items. Consequently it **refuses to
write** if `Day 1` already has a non-pilot Location item (such as the broad
`Sample City, Italy` gallery): leaving that item would still display its full shared
gallery. After the pilot is approved, remove that legacy itinerary association
separately and verify the new stop locations before expanding the importer.

## CLI design notes

The original command-line importer remains useful for batch exports and the
broader collection rebuild mode. The local UI above is the human-review path
for day-specific stop proposals and deliberately keeps its plan/state schema
separate from the older rebuild and Sample City pilot checkpoint files.

## Local review UI

`web_app.py` provides a dependency-free browser workflow for the day-specific
stop version of this importer, including inclusive multi-day ranges. It binds to `127.0.0.1` by default and stores the
review plan and remote-write checkpoint as JSON files. Credentials are read
from the local `.env`/environment and are never returned by the status API,
written to a plan, or included in logs.

Start it with an existing manifest (the browser can review any day represented
in the manifest):

```sh
cd /media/ppg91/Data/projects/immich-adventure-import
python3 web_app.py --manifest immich-manifest.json \
  --plan immich-review-plan.json --state immich-review-state.json
```

Open `http://127.0.0.1:8787`. Choose a start/end date, a total asset cap, and
an optional name for a new trip. The default target is a new isolated
collection named `Immich itinerary — YYYY-MM-DD`; the existing AdventureLog collections
are offered as an explicit alternative only when AdventureLog credentials are
configured. A selected date range and asset cap are used to make a read-only plan.
Each plan records stable stop keys, source/selected asset IDs, local
Europe/Rome timestamps, GPS centres, proxied Immich thumbnails, and POI
candidate slots. POI lookup is intentionally separate from the initial
preview: select one stop and click **Find nearby POIs**. That makes one
cache-backed, capped request for only that cluster and records any failure or
rate limit beside the stop. Results are cached and paced by two seconds.

Photo metadata can be loaded for the whole plan with **Load Immich metadata**,
or explicitly per stop with **Load tags, people, descriptions & camera data**.
The action reads full Immich asset details, shows an aggregate summary in the
editor, and includes a compact version in the proposed AdventureLog Visit
notes. Batches with more than ten photos use a background worker; partial
failures stay attached to the affected asset and can be retried.
The UI uses six concurrent Immich detail requests and four concurrent
AdventureLog image-link requests by default. Override these with
`--metadata-workers` and `--attachment-workers` if the services or proxy can
handle more concurrency. Already enriched photos are cached in the plan and
are not fetched again unless a refresh is requested.
The UI timeout defaults are 60 seconds per Immich request and 180 seconds per
AdventureLog request; tune them with `--immich-timeout` and
`--adventurelog-timeout` when using a slower reverse proxy.

The Immich API key must include the `asset.view` permission. Metadata search
can succeed with a key that lacks this permission, but thumbnail requests will
return HTTP 403 and the UI will show `Images missing asset.view permission` in
the header. Create/update the key in Immich with asset viewing enabled, update
`IMMICH_API_KEY` in the local `.env`, and restart the UI server; the key is
never sent to the browser.
For the metadata action, `asset.read` and `asset.view` are the important
permissions; `tag.read`/`tag.asset` are useful for future global tag browsing,
but per-photo details already include the tags and do not require a separate
tag-list request. Read-only `person.read` is likewise useful for future people
filters. No create, update, delete, download, upload, or admin permissions are
needed by this UI.

The browser flow is intentionally staged:

1. **Preview & edit** loads photos and lets you rename a stop, choose a nearby
   OSM candidate, exclude photos/stops, merge stops, split by an ISO timestamp,
   and reorder stops. Stop cards support drag-and-drop: drag to reorder, or
   drop one stop onto another to merge them. Every edit is saved atomically to
   the plan file. A large range is computed by a background worker and its
   progress is shown beside the Load button.
2. **Validate plan** performs no writes and displays the exact collection,
   Location, Visit, image-link, itinerary-day and itinerary-item diff. An
   existing day with legacy itinerary items is surfaced as a warning; the UI
   never removes or unlinks those records.
3. **Create reviewed trip** shows the read-only diff first. One explicit click
   starts a background job that creates or reuses the isolated day-specific
   collection, Locations, Visits, selected Location image links, itinerary
days, and itinerary items. The progress bar reports each object/link and
ends on the verification screen; no text confirmation is required. The
checkpoint is written after every remote object/link, and retries reuse
existing IDs and Immich IDs.

After the final itinerary step completes, the saved plan remains on disk for
audit and verification, but a browser reload starts with a clean review form.
Incomplete or failed creations remain resumable from the saved plan.

If the UI needs to stay running for a long background review, use `nohup`
with the same plan/state paths. Use one process per state file:

```sh
nohup python3 web_app.py --manifest immich-manifest.json \
  --plan immich-review-plan.json --state immich-review-state.json \
  > immich-review-ui.log 2>&1 &
echo $! > immich-review-ui.pid
```

The server has no delete endpoint and the browser never exposes API keys. Stop
it with `kill "$(cat immich-review-ui.pid)"` when finished. To expose it beyond
localhost, put it behind an authenticated reverse proxy and pass an explicit
`--host`; the application itself does not add user authentication.

For an OMV/systemd deployment, copy `deploy/immich-adventurelog.service` to
`/etc/systemd/system/`, place the private `.env` and writable review files under
the paths used by the unit, then run `systemctl daemon-reload` and
`systemctl enable --now immich-adventurelog.service`. The unit listens on
port `8787` and runs as the unprivileged `immich-adventurelog` user.

The implementation uses only Python's standard library; `requirements.txt` is
intentionally empty.
