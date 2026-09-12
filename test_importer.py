import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import json
import urllib.error

from immich_period_import import AdventureLogClient, apply_manifest
from web_app import build_review_plan, edit_plan, validate_plan


class FakeClient:
    def __init__(self):
        self.collection_payload = None
        self.location_payload = None
        self.visit_payload = None
        self.updated_visit_payload = None
        self.itinerary_days = []
        self.itinerary_items = []
        self.collections = [{"id": "sample-collection-1", "name": "Sample City trip"}]
        self.attachments = []
        self.calls = []

    def create_location(self, payload):
        self.calls.append("location")
        self.location_payload = payload
        return {"id": "created-location-42"}

    def create_collection(self, payload):
        self.calls.append("collection")
        self.collection_payload = payload
        return {"id": "created-collection-42"}

    def create_visit(self, payload):
        self.calls.append("visit")
        self.visit_payload = payload
        return {"id": "created-visit-42"}

    def get_location(self, location_id):
        self.calls.append("get")
        return {"id": location_id, "images": [{"immich_id": "immich-a"}], "visits": [{"id": "visit-42"}]}

    def update_visit(self, visit_id, payload):
        self.calls.append("update-visit")
        self.updated_visit_payload = (visit_id, payload)
        return {}

    def list_itinerary_days(self):
        return list(self.itinerary_days)

    def list_collections(self):
        return list(self.collections)

    def list_itinerary_items(self):
        return list(self.itinerary_items)

    def create_itinerary_day(self, payload):
        self.itinerary_days.append({"collection": payload["collection"], "date": payload["date"], "id": "day-1"})
        self.location_payload = payload
        return {"id": "day-1"}

    def create_itinerary_item(self, payload):
        self.itinerary_items.append(payload)
        return {"id": "item-1"}

    def attach_image(self, immich_id, location_id, content_type):
        self.calls.append("image")
        self.attachments.append((immich_id, location_id, content_type))
        return {}


class WriterTest(unittest.TestCase):
    def test_adventurelog_uses_x_api_key_header(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{}'

        captured = {}

        def fake_urlopen(request, timeout):
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        with patch("immich_period_import.urllib.request.urlopen", fake_urlopen):
            AdventureLogClient("http://adventurelog", "al_test-key", "/api/locations", "/api/visits", "/api/images").create_location({"name": "Test"})
        headers = {key.lower(): value for key, value in captured["headers"].items()}
        self.assertEqual(headers["x-api-key"], "al_test-key")
        self.assertNotIn("authorization", headers)

    def test_every_attachment_uses_created_location_id(self):
        client = FakeClient()
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "summary": {"asset_count": 2},
            "assets": [{"id": "immich-a"}, {"id": "immich-b"}],
        }
        apply_manifest(manifest, client, "Test trip", "Europe/Berlin", "location")
        self.assertEqual(client.location_payload, {
            "name": "Test trip",
        })
        self.assertNotIn("period", client.location_payload)
        self.assertNotIn("visit", client.location_payload)
        self.assertEqual(client.visit_payload, {
            "location": "created-location-42",
            "start_date": "2026-08-01T00:00:00Z",
            "end_date": "2026-08-02T23:59:59Z",
            "timezone": "Europe/Berlin",
            "notes": "Imported from Immich: 2 photo(s). Review this draft in AdventureLog.",
        })
        self.assertEqual(client.calls, ["location", "visit", "image", "image"])
        self.assertEqual(client.attachments, [
            ("immich-a", "created-location-42", "location"),
            ("immich-b", "created-location-42", "location"),
        ])

    def test_resume_gets_location_and_skips_existing_without_create(self):
        client = FakeClient()
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "summary": {"asset_count": 2},
            "assets": [{"id": "immich-a"}, {"id": "immich-b"}],
        }
        from immich_period_import import resume_manifest
        resume_manifest(manifest, client, "existing-location", "location")
        self.assertEqual(client.calls, ["get", "image"])
        self.assertEqual(client.attachments, [("immich-b", "existing-location", "location")])

    def test_repeated_manifest_id_is_attached_only_once(self):
        client = FakeClient()
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "summary": {"asset_count": 2},
            "assets": [{"id": "repeated"}, {"id": "repeated"}],
        }
        from immich_period_import import resume_manifest
        resume_manifest(manifest, client, "existing-location", "location")
        self.assertEqual(client.attachments, [("repeated", "existing-location", "location")])

    def test_representative_coordinates_prefer_dominant_country(self):
        from immich_period_import import representative_coordinates
        assets = [
            {"location": {"country": "Germany", "latitude": 51.0, "longitude": 10.0}},
            {"location": {"country": "Italy", "latitude": 41.9, "longitude": 12.5}},
        ]
        self.assertEqual(representative_coordinates(assets, "Italy"), (41.9, 12.5))

    def test_collection_plan_and_apply_group_assets_without_nested_visits(self):
        from immich_period_import import apply_rebuild_plan, build_rebuild_plan
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-03"},
            "summary": {"asset_count": 2, "countries": {"Italy": 2}},
            "assets": [
                {"id": "rome-a", "day": "2026-08-01", "location": {"country": "Italy", "city": "Rome", "latitude": 41.9, "longitude": 12.5}},
                {"id": "rome-b", "day": "2026-08-03", "location": {"country": "Italy", "city": "Rome", "latitude": 41.91, "longitude": 12.51}},
            ],
        }
        plan = build_rebuild_plan(manifest, "Italy trip", 1)
        self.assertEqual(plan["groups"][0]["name"], "Rome, Italy")
        client = FakeClient()
        apply_rebuild_plan(plan, client, "Europe/Berlin", "location")
        self.assertEqual(client.calls, ["collection", "location", "visit", "image", "image"])
        self.assertEqual(client.collection_payload, {"name": "Italy trip", "start_date": "2026-08-01", "end_date": "2026-08-03", "description": "Grouped from Immich photo metadata."})
        self.assertEqual(client.location_payload["collections"], ["created-collection-42"])
        self.assertNotIn("visits", client.location_payload)

    def test_small_city_folds_to_nearest_same_country_anchor(self):
        from immich_period_import import build_rebuild_plan
        assets = []
        for prefix, city, lat, lon in (("rome", "Rome", 41.90, 12.50), ("naples", "Naples", 40.85, 14.27)):
            for suffix in ("a", "b"):
                assets.append({"id": f"{prefix}-{suffix}", "day": "2026-08-01", "location": {"country": "Italy", "city": city, "latitude": lat, "longitude": lon}})
        assets.append({"id": "sample-a", "day": "2026-08-02", "location": {"country": "Italy", "city": "Sample City", "latitude": 41.07, "longitude": 14.33}})
        plan = build_rebuild_plan({"period": {"start": "2026-08-01", "end": "2026-08-02"}, "summary": {}, "assets": assets}, "Italy", 2)
        groups = {group["name"]: set(group["asset_ids"]) for group in plan["groups"]}
        self.assertEqual(set(groups), {"Rome, Italy", "Naples, Italy"})
        self.assertIn("sample-a", groups["Naples, Italy"])

    def test_collection_apply_checkpoint_batch_then_resume_without_recreate(self):
        from immich_period_import import apply_rebuild_plan, build_rebuild_plan
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "summary": {"asset_count": 2, "countries": {"Italy": 2}},
            "assets": [
                {"id": "rome-a", "day": "2026-08-01", "location": {"country": "Italy", "city": "Rome", "latitude": 41.9, "longitude": 12.5}},
                {"id": "rome-b", "day": "2026-08-02", "location": {"country": "Italy", "city": "Rome", "latitude": 41.9, "longitude": 12.5}},
            ],
        }
        plan = build_rebuild_plan(manifest, "Italy", 1)
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            apply_rebuild_plan(plan, client, "Europe/Berlin", "location", state_path, 1)
            self.assertEqual(client.calls, ["collection", "location", "visit", "image"])
            apply_rebuild_plan(plan, client, "Europe/Berlin", "location", state_path, 0)
        self.assertEqual(client.calls, ["collection", "location", "visit", "image", "get", "image"])
        self.assertEqual(client.attachments, [("rome-a", "created-location-42", "location"), ("rome-b", "created-location-42", "location")])

    def test_update_visits_uses_exact_times_and_country_timezone(self):
        from immich_period_import import apply_rebuild_plan, build_rebuild_plan
        manifest = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "summary": {"asset_count": 1, "countries": {"Italy": 1}},
            "assets": [{"id": "rome-a", "day": "2026-08-01", "taken_at": "2026-08-01T14:23:00Z", "location": {"country": "Italy", "city": "Rome", "latitude": 41.9, "longitude": 12.5}}],
        }
        plan = build_rebuild_plan(manifest, "Italy", 1)
        self.assertEqual(plan["groups"][0]["start_at"], "2026-08-01T14:23:00Z")
        self.assertEqual(plan["groups"][0]["end_at"], "2026-08-01T14:23:00Z")
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            apply_rebuild_plan(plan, client, "Europe/Berlin", "location", state_path)
            client.calls.clear()
            apply_rebuild_plan(plan, client, "Europe/Berlin", "location", state_path, update_visits=True, visits_only=True)
        self.assertEqual(client.calls, ["get", "update-visit"])
        self.assertEqual(client.updated_visit_payload, ("visit-42", {
            "location": "created-location-42",
            "start_date": "2026-08-01T14:23:00Z",
            "end_date": "2026-08-01T14:23:00Z",
            "timezone": "Europe/Rome",
            "notes": "Grouped from Immich: 1 photo(s).",
        }))
        self.assertEqual(client.attachments, [("rome-a", "created-location-42", "location")])

    def test_poi_suggestions_are_cached_and_normalized_without_network(self):
        from immich_period_import import suggest_pois
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"elements":[{"type":"node","id":7,"lat":41.9005,"lon":12.5005,"tags":{"name":"Test Museum","tourism":"museum"}}]}'

        plan = {"groups": [{"name": "Rome, Italy", "coordinates": [41.9, 12.5]}]}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "poi-cache.json"
            with patch("immich_period_import.urllib.request.urlopen", return_value=Response()) as mocked:
                result, errors = suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache)
                self.assertEqual(result["Rome, Italy"][0]["name"], "Test Museum")
                self.assertEqual(result["Rome, Italy"][0]["category"], "tourism:museum")
                self.assertEqual(result["Rome, Italy"][0]["openstreetmap_map_url"], "https://www.openstreetmap.org/?mlat=41.900500&mlon=12.500500#map=18/41.900500/12.500500")
                self.assertEqual(errors, {})
                headers = {key.lower(): value for key, value in mocked.call_args.args[0].header_items()}
                self.assertIn("immich-adventure-import", headers["user-agent"])
                suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache)
                self.assertEqual(mocked.call_count, 1)

    def test_poi_error_is_recorded_and_does_not_abort_other_groups(self):
        from immich_period_import import suggest_pois
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"elements":[]}'

        plan = {"groups": [
            {"name": "Rome, Italy", "coordinates": [41.9, 12.5]},
            {"name": "Pompei, Italy", "coordinates": [40.75, 14.5]},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "poi-cache.json"
            failure = urllib.error.HTTPError("http://overpass.test", 504, "Gateway Timeout", {}, None)
            with patch("immich_period_import.urllib.request.urlopen", side_effect=[Response(), failure]):
                suggestions, errors = suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache)
            self.assertIn("Rome, Italy", suggestions)
            self.assertEqual(suggestions["Rome, Italy"], [])
            self.assertIn("Pompei, Italy", errors)
            self.assertTrue(cache.exists())
            self.assertEqual(len(json.loads(cache.read_text(encoding="utf-8"))), 2)

    def test_poi_requests_are_paced_and_capped(self):
        from immich_period_import import suggest_pois
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"elements":[]}'

        plan = {"groups": [
            {"name": "Rome, Italy", "coordinates": [41.9, 12.5]},
            {"name": "Naples, Italy", "coordinates": [40.85, 14.25]},
            {"name": "Sample City, Italy", "coordinates": [41.07, 14.33]},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "poi-cache.json"
            with patch("immich_period_import.urllib.request.urlopen", return_value=Response()) as request, \
                 patch("immich_period_import.time.monotonic", side_effect=[0.0, 0.0, 1.5]), \
                 patch("immich_period_import.time.sleep") as sleep:
                _, errors = suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache, max_requests=2, min_interval_seconds=1.5)
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(1.5)
            self.assertIn("Sample City, Italy", errors)
            self.assertIn("request cap reached", errors["Sample City, Italy"])

    def test_poi_provider_failure_is_cached_to_avoid_repeat_requests(self):
        from immich_period_import import suggest_pois
        plan = {"groups": [{"name": "Rome, Italy", "coordinates": [41.9, 12.5]}]}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "poi-cache.json"
            failure = urllib.error.HTTPError("http://overpass.test", 429, "Too Many Requests", {}, None)
            with patch("immich_period_import.urllib.request.urlopen", side_effect=failure) as request:
                _, first_errors = suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache)
            with patch("immich_period_import.urllib.request.urlopen") as request_again:
                _, second_errors = suggest_pois(plan, "http://overpass.test/api/interpreter", 100, cache)
            self.assertEqual(request.call_count, 1)
            request_again.assert_not_called()
            self.assertIn("Rome, Italy", first_errors)
            self.assertIn("cached provider failure", second_errors["Rome, Italy"])

    def test_state_dedupe_retains_primary_and_verifies_after_delete(self):
        from immich_period_import import dedupe_state_locations
        class DedupeClient:
            def __init__(self):
                self.images = [
                    {"id": "old-association", "immich_id": "asset-1", "is_primary": False},
                    {"id": "primary-association", "immich_id": "asset-1", "is_primary": True},
                    {"id": "unique-association", "immich_id": "asset-2", "is_primary": False},
                ]
                self.deleted = []

            def get_location(self, location_id):
                return {"id": location_id, "images": list(self.images)}

            def delete_image(self, image_id):
                self.deleted.append(image_id)
                self.images = [image for image in self.images if image["id"] != image_id]

        state = {"schema": "immich-adventure-rebuild-state/v1", "collection_id": "collection-1", "groups": {"Rome, Italy": {"location_id": "location-1", "attached_asset_ids": []}}}
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "dedupe.json"
            client = DedupeClient()
            dedupe_state_locations(state, client, report_path, False)
            self.assertEqual(client.deleted, [])
            dedupe_state_locations(state, client, report_path, True)
            self.assertEqual(client.deleted, ["old-association"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["groups"][0]["verification"]["duplicate_count"], 0)

    def test_state_dedupe_batch_recomputes_and_finishes_next_run(self):
        from immich_period_import import dedupe_state_locations
        class BatchClient:
            def __init__(self):
                self.images = [
                    {"id": "keep", "immich_id": "asset-1", "is_primary": True},
                    {"id": "drop-1", "immich_id": "asset-1", "is_primary": False},
                    {"id": "drop-2", "immich_id": "asset-1", "is_primary": False},
                    {"id": "unique", "immich_id": "asset-2", "is_primary": False},
                ]
                self.deleted = []

            def get_location(self, location_id):
                return {"images": list(self.images)}

            def delete_image(self, image_id):
                self.deleted.append(image_id)
                self.images = [image for image in self.images if image["id"] != image_id]

        state = {"schema": "immich-adventure-rebuild-state/v1", "collection_id": "c", "groups": {"Rome, Italy": {"location_id": "l", "attached_asset_ids": []}}}
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "dedupe.json"
            client = BatchClient()
            dedupe_state_locations(state, client, report_path, True, 1)
            first = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(len(client.deleted), 1)
            self.assertEqual(first["groups"][0]["remaining_duplicate_count"], 1)
            dedupe_state_locations(state, client, report_path, True, 1)
            second = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(client.deleted, ["drop-1", "drop-2"])
            self.assertEqual(second["groups"][0]["remaining_duplicate_count"], 0)
            self.assertNotIn("keep", client.deleted)
            self.assertNotIn("unique", client.deleted)

    def test_itinerary_plan_creates_days_and_location_items_idempotently(self):
        from immich_period_import import apply_itinerary_plan, build_itinerary_plan
        plan = {
            "period": {"start": "2026-08-01", "end": "2026-08-02"},
            "groups": [{"name": "Rome, Italy", "asset_ids": ["a", "b"]}],
            "assets": [{"id": "a", "day": "2026-08-01"}, {"id": "b", "day": "2026-08-02"}],
        }
        state = {"collection_id": "collection-1", "groups": {"Rome, Italy": {"location_id": "location-1"}}}
        itinerary = build_itinerary_plan(plan, state)
        self.assertEqual([day["date"] for day in itinerary["days"]], ["2026-08-01", "2026-08-02"])
        client = FakeClient()
        apply_itinerary_plan(plan, client, state)
        self.assertEqual(len(client.itinerary_days), 2)
        self.assertEqual(client.itinerary_items[0], {"collection": "collection-1", "content_type": "location", "object_id": "location-1", "date": "2026-08-01", "is_global": False, "order": 0})
        apply_itinerary_plan(plan, client, state)
        self.assertEqual(len(client.itinerary_days), 2)
        self.assertEqual(len(client.itinerary_items), 2)

    def test_day_stop_pilot_clusters_day_specific_stops_and_uses_rome_times(self):
        from immich_period_import import build_day_stop_pilot_plan
        manifest = {"assets": [
            {"id": "before", "taken_at": "2026-09-02T21:30:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "b", "taken_at": "2026-09-03T07:15:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.0701, "longitude": 14.3301}},
            {"id": "c", "taken_at": "2026-09-03T13:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.10, "longitude": 14.37}},
        ]}
        plan = build_day_stop_pilot_plan(manifest, 30, {"Sample City — stop 1": [{"name": "Pilot Museum", "distance_m": 20, "category": "tourism:museum"}]})
        self.assertEqual(plan["source_asset_ids"], ["a", "b", "c"])
        self.assertEqual([group["asset_ids"] for group in plan["groups"]], [["a", "b"], ["c"]])
        self.assertEqual(plan["groups"][0]["name"], "Pilot Museum")
        self.assertEqual(plan["groups"][0]["label"]["confidence"], "high")
        self.assertEqual(plan["groups"][0]["visit"]["start_date"], "2026-09-03T09:00:00+02:00")
        self.assertEqual(plan["groups"][1]["name"], "Sample City — Afternoon stop")

    def test_day_stop_pilot_apply_is_checkpointed_and_does_not_duplicate(self):
        from immich_period_import import apply_day_stop_pilot_plan, build_day_stop_pilot_plan
        plan = build_day_stop_pilot_plan({"assets": [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
        ]})
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "pilot-state.json"
            report = apply_day_stop_pilot_plan(plan, client, "location", state)
            self.assertEqual(report["locations"][0]["photo_count"], 1)
            self.assertEqual(client.attachments, [("a", "created-location-42", "location")])
            first_calls = list(client.calls)
            apply_day_stop_pilot_plan(plan, client, "location", state)
        self.assertEqual(client.attachments, [("a", "created-location-42", "location")])
        self.assertEqual(client.calls, first_calls + ["get"])
        self.assertEqual(len(client.itinerary_items), 1)

    def test_day_stop_pilot_numbers_repeated_generic_stop_labels(self):
        from immich_period_import import build_day_stop_pilot_plan
        manifest = {"assets": [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Naples", "country": "Italy", "latitude": 40.85, "longitude": 14.25}},
            {"id": "b", "taken_at": "2026-09-03T08:00:00Z", "location": {"city": "Naples", "country": "Italy", "latitude": 40.86, "longitude": 14.26}},
        ]}
        plan = build_day_stop_pilot_plan(manifest)
        self.assertEqual([group["name"] for group in plan["groups"]], ["Naples — Morning stop 1", "Naples — Morning stop 2"])

    def test_day_stop_pilot_refuses_to_leave_legacy_gallery_on_day_one(self):
        from immich_period_import import apply_day_stop_pilot_plan, build_day_stop_pilot_plan
        plan = build_day_stop_pilot_plan({"assets": [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
        ]})
        client = FakeClient()
        client.itinerary_items = [{"collection": "sample-collection-1", "object_id": "legacy-sample", "date": "2026-09-03"}]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "non-pilot location"):
                apply_day_stop_pilot_plan(plan, client, "location", Path(directory) / "pilot-state.json")
        self.assertEqual(client.calls, [])

    def test_review_plan_is_day_specific_and_editable(self):
        manifest = {"source": "fixture", "assets": [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "b", "taken_at": "2026-09-03T07:15:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.0701, "longitude": 14.3301}},
            {"id": "other", "taken_at": "2026-09-04T07:00:00Z", "location": {"city": "Naples", "country": "Italy", "latitude": 40.85, "longitude": 14.25}},
        ]}
        plan = build_review_plan(manifest, "2026-09-03", 30)
        self.assertEqual(plan["selected_asset_ids"], ["a", "b"])
        self.assertEqual(plan["collection"]["mode"], "isolated")
        key = plan["groups"][0]["key"]
        edit_plan(plan, "rename", {"key": key, "name": "Royal Palace"})
        self.assertEqual(plan["groups"][0]["label"]["confidence"], "manual")
        self.assertEqual(plan["groups"][0]["location"]["name"], "Royal Palace")
        edit_plan(plan, "exclude_asset", {"key": key, "asset_id": "b"})
        self.assertEqual(plan["selected_asset_ids"], ["a"])

    def test_review_plan_applies_poi_suggestions_to_period_label(self):
        manifest = {"assets": [{"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}}]}
        plan = build_review_plan(manifest, "2026-09-03", 10, poi_suggestions={"Sample City — Morning stop": [{"name": "Royal Palace", "distance_m": 20}]})
        self.assertEqual(plan["groups"][0]["name"], "Royal Palace")
        self.assertEqual(plan["groups"][0]["label"]["source"], "OpenStreetMap/Overpass")

    def test_review_plan_supports_inclusive_ranges_and_separate_trip_name(self):
        assets = [
            {"id": "day-one", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "day-two", "taken_at": "2026-09-04T07:00:00Z", "location": {"city": "Naples", "country": "Italy", "latitude": 40.85, "longitude": 14.25}},
            {"id": "outside", "taken_at": "2026-09-05T07:00:00Z", "location": {"city": "Rome", "country": "Italy", "latitude": 41.90, "longitude": 12.50}},
        ]
        plan = build_review_plan({"assets": assets}, "2026-09-03", 10, collection_name="Reviewed Italy", end_day="2026-09-04")
        self.assertEqual(plan["period"], {"start": "2026-09-03", "end": "2026-09-04"})
        self.assertEqual(plan["days"], ["2026-09-03", "2026-09-04"])
        self.assertEqual(plan["selected_asset_ids"], ["day-one", "day-two"])
        self.assertEqual(plan["collection"]["name"], "Reviewed Italy")

    def test_ui_preview_defers_poi_network_work_until_stop_action(self):
        from web_app import App
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"assets": [{"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}}]}))
            args = SimpleNamespace(manifest=manifest_path, plan=root / "plan.json", state=root / "state.json", poi_cache=None, overpass_url="http://overpass.test", poi_radius=100, poi_max_requests=4, poi_min_interval=2.0, poi_timeout=20)
            app = App(args)
            with patch("web_app.importer.suggest_pois") as lookup:
                plan = app.preview({"start_day": "2026-09-03", "end_day": "2026-09-03", "asset_cap": 10, "collection": "isolated"})
            lookup.assert_not_called()
            self.assertFalse(plan["poi_lookup"]["enabled"])

    def test_review_plan_merge_split_and_validation_detect_overlap(self):
        assets = [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "b", "taken_at": "2026-09-03T13:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.20, "longitude": 14.50}},
        ]
        plan = build_review_plan({"assets": assets}, "2026-09-03", 30)
        self.assertEqual(len(plan["groups"]), 2)
        keys = [g["key"] for g in plan["groups"]]
        edit_plan(plan, "merge", {"keys": keys})
        self.assertEqual(len(plan["groups"]), 1)
        merged = plan["groups"][0]["key"]
        edit_plan(plan, "split", {"key": merged, "boundary": "2026-09-03T10:00:00Z"})
        self.assertEqual([g["asset_count"] for g in plan["groups"]], [1, 1])
        plan["groups"][1]["asset_ids"].append("a")
        self.assertFalse(validate_plan(plan)["ok"])

    def test_ui_writes_are_staged_and_idempotent(self):
        from web_app import apply_ui_itinerary, apply_ui_locations
        manifest = {"assets": [{"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}}]}
        plan = build_review_plan(manifest, "2026-09-03", 10)
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "ui-state.json"
            events = []
            first = apply_ui_locations(plan, client, state_path, progress=lambda *event: events.append(event))
            self.assertEqual([x[0] for x in client.attachments], ["a"])
            self.assertFalse(client.itinerary_days)
            apply_ui_locations(plan, client, state_path)
            self.assertEqual([x[0] for x in client.attachments], ["a"])
            report = apply_ui_itinerary(plan, client, state_path, progress=lambda *event: events.append(event))
            self.assertEqual(report["itinerary_day_id"], "day-1")
            apply_ui_itinerary(plan, client, state_path)
            self.assertEqual(len(client.itinerary_days), 1)
            self.assertEqual(len(client.itinerary_items), 1)
            self.assertEqual(events[-1][0], "itinerary")
            self.assertEqual(events[-1][1:3], (2, 2))

    def test_ui_range_creates_one_itinerary_day_per_photo_day(self):
        from web_app import apply_ui_itinerary, apply_ui_locations
        manifest = {"assets": [
            {"id": "a", "taken_at": "2026-09-03T07:00:00Z", "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33}},
            {"id": "b", "taken_at": "2026-09-04T07:00:00Z", "location": {"city": "Naples", "country": "Italy", "latitude": 40.85, "longitude": 14.25}},
        ]}
        plan = build_review_plan(manifest, "2026-09-03", 10, end_day="2026-09-04")
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "ui-range-state.json"
            apply_ui_locations(plan, client, state_path)
            apply_ui_itinerary(plan, client, state_path)
        self.assertEqual([x["date"] for x in client.itinerary_days], ["2026-09-03", "2026-09-04"])
        self.assertEqual([x["date"] for x in client.itinerary_items], ["2026-09-03", "2026-09-04"])

    def test_normalize_asset_keeps_review_metadata(self):
        from immich_period_import import normalize_asset
        asset = normalize_asset({
            "id": "photo-1", "originalFileName": "castle.jpg", "description": "Evening light",
            "isFavorite": True, "tags": [{"name": "holiday"}], "people": [{"name": "Alex"}],
            "width": 1200, "height": 800, "originalMimeType": "image/jpeg", "thumbhash": "abc",
            "exifInfo": {"dateTimeOriginal": "2026-09-03T07:00:00Z", "make": "Test", "model": "Camera"},
        }, "http://immich.test")
        self.assertEqual(asset["tags"], ["holiday"])
        self.assertEqual(asset["people"], ["Alex"])
        self.assertEqual(asset["description"], "Evening light")
        self.assertEqual(asset["camera"], {"make": "Test", "model": "Camera"})

    def test_ui_can_enrich_one_stop_and_rebuild_visit_notes(self):
        from web_app import App
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"assets": [{
                "id": "a", "taken_at": "2026-09-03T07:00:00Z",
                "location": {"city": "Sample City", "country": "Italy", "latitude": 41.07, "longitude": 14.33},
            }]}))
            args = SimpleNamespace(manifest=manifest_path, plan=root / "plan.json", state=root / "state.json", poi_cache=None, overpass_url="http://overpass.test", poi_radius=100, poi_max_requests=4, poi_min_interval=2.0, poi_timeout=20)
            app = App(args)
            app.preview({"start_day": "2026-09-03", "end_day": "2026-09-03", "asset_cap": 10, "collection": "isolated"})
            detail = {"id": "a", "description": "Castle at dusk", "isFavorite": True, "tags": [{"name": "holiday"}], "people": [{"name": "Alex"}], "exifInfo": {"dateTimeOriginal": "2026-09-03T07:00:00Z", "make": "Test", "model": "Camera"}}
            with patch.dict("os.environ", {"IMMICH_BASE_URL": "http://immich.test", "IMMICH_API_KEY": "secret"}), patch("web_app.importer._asset_detail", return_value=detail):
                plan = app.enrich({"key": app.read_plan()["groups"][0]["key"]})
            group = plan["groups"][0]
            self.assertEqual(group["metadata_summary"]["tags"], ["holiday"])
            self.assertEqual(group["metadata_summary"]["people"], ["Alex"])
            self.assertIn("Castle at dusk", group["visit"]["notes"])
            self.assertIn("Tags: holiday", group["visit"]["notes"])

    def test_completed_review_plan_is_not_reloaded_as_active_ui_plan(self):
        from web_app import App, UI_SCHEMA
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"assets": [], "summary": {"days_with_photos": ["2026-09-05"]}}))
            (root / "plan.json").write_text(json.dumps({
                "schema": UI_SCHEMA,
                "day": "2026-09-03",
                "remote": {"itinerary_complete": True},
            }))
            args = SimpleNamespace(
                manifest=manifest_path, plan=root / "plan.json", state=root / "state.json",
                poi_cache=None, overpass_url="http://overpass.test", poi_radius=100,
                poi_max_requests=4, poi_min_interval=2.0, poi_timeout=20,
            )
            app = App(args)
            with patch.object(app, "collections", return_value=([{"id": "isolated"}], None)), patch.object(app, "thumbnail_status", return_value="no asset to check"):
                status = app.status()
            self.assertFalse(status["plan"])
            self.assertTrue(status["completed_plan"])
            self.assertEqual(status["default_day"], "2026-09-05")


if __name__ == "__main__":
    unittest.main()
