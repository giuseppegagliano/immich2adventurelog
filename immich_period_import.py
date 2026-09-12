#!/usr/bin/env python3
"""Read Immich photo metadata for a date period and write a portable manifest.

This tool deliberately does not write to AdventureLog.  The manifest is an
interchange format for a later, separately verified importer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from urllib.parse import quote
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_SEARCH_PATH = "/api/search/metadata"
DEFAULT_LOCATION_PATH = "/api/locations"
DEFAULT_VISIT_PATH = "/api/visits"
DEFAULT_IMAGE_PATH = "/api/images"
DEFAULT_COLLECTION_PATH = "/api/collections"
DEFAULT_ITINERARY_DAY_PATH = "/api/itinerary-days"
DEFAULT_ITINERARY_PATH = "/api/itineraries"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PILOT_COLLECTION_NAME = "Sample City trip"
ISOLATED_PILOT_COLLECTION_NAME = "Sample City trip — 2026-09-03 pilot"
PILOT_DAY = "2026-09-03"
PILOT_TIMEZONE = "Europe/Rome"
PILOT_STATE_SCHEMA = "immich-adventure-sample-pilot-state/v1"
ISOLATED_PILOT_STATE_SCHEMA = "immich-adventure-sample-isolated-pilot-state/v1"
POI_USER_AGENT = "immich-adventure-import/0.1 (read-only POI suggestions)"
DEDUPE_CONFIRM_TEXT = "DELETE_DUPLICATES"
CONFIRM_TEXT = "CREATE"


def load_local_dotenv() -> None:
    """Load simple KEY=value entries from this script's .env, without logging values."""
    dotenv_path = Path(__file__).resolve().parent / ".env"
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; use YYYY-MM-DD") from exc


def iso_day(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else None


def asset_day(asset: dict[str, Any]) -> str | None:
    exif = asset.get("exifInfo") or {}
    for key in ("dateTimeOriginal", "fileCreatedAt", "fileModifiedAt", "createdAt"):
        value = exif.get(key) if key in exif else asset.get(key)
        result = iso_day(value)
        if result:
            return result
    return None


def location(asset: dict[str, Any]) -> dict[str, Any] | None:
    exif = asset.get("exifInfo") or {}
    lat = exif.get("latitude", asset.get("latitude"))
    lon = exif.get("longitude", asset.get("longitude"))
    names = {k: exif.get(k) for k in ("city", "state", "country") if exif.get(k)}
    if lat is None and lon is None and not names:
        return None
    return {"latitude": lat, "longitude": lon, **names}


def normalize_asset(asset: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Keep stable, useful fields and avoid copying potentially huge API data."""
    exif = asset.get("exifInfo") or {}
    result: dict[str, Any] = {
        "id": asset.get("id"),
        "type": asset.get("type"),
        "filename": asset.get("originalFileName") or asset.get("filename"),
        "original_path": asset.get("originalPath"),
        "day": asset_day(asset),
        "taken_at": exif.get("dateTimeOriginal") or asset.get("fileCreatedAt") or asset.get("createdAt"),
        "location": location(asset),
        "description": asset.get("description") or exif.get("description"),
        "is_favorite": asset.get("isFavorite"),
        "tags": sorted({str(tag.get("name") or tag.get("value")) for tag in (asset.get("tags") or []) if isinstance(tag, dict) and (tag.get("name") or tag.get("value"))} | {str(tag) for tag in (asset.get("tags") or []) if isinstance(tag, str)}),
        "people": sorted({str(person.get("name")) for person in (asset.get("people") or []) if isinstance(person, dict) and person.get("name")} | {str(person) for person in (asset.get("people") or []) if isinstance(person, str)}),
        "thumbhash": asset.get("thumbhash"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "mime_type": asset.get("originalMimeType"),
        "camera": {key: exif.get(key) for key in ("make", "model", "lensModel", "fNumber", "focalLength", "iso") if exif.get(key)},
    }
    asset_id = result["id"]
    if asset_id:
        root = base_url.rstrip("/")
        result["thumbnail_url"] = f"{root}/api/assets/{asset_id}/thumbnail"
        result["original_url"] = f"{root}/api/assets/{asset_id}/original"
    return result


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, search_path: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.search_path = "/" + search_path.lstrip("/")
        self.timeout = timeout

    def search(self, start: date, end: date, page: int, size: int) -> dict[str, Any]:
        # These are the documented Immich metadata-search names.  --search-path
        # permits adapting to a server version/proxy without changing the script.
        payload = {
            "takenAfter": f"{start.isoformat()}T00:00:00.000Z",
            "takenBefore": f"{end.isoformat()}T23:59:59.999Z",
            "type": "IMAGE",
            "withExif": True,
            "withStack": False,
            "order": "asc",
            "page": page,
            "size": size,
        }
        request = urllib.request.Request(
            self.base_url + self.search_path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"Immich returned HTTP {exc.code} from {self.search_path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to Immich: {exc.reason}") from exc

class AdventureLogClient:
    """Minimal writer kept separate so the read-only export remains safe."""

    def __init__(self, base_url: str, token: str, location_path: str, visit_path: str, image_path: str, collection_path: str = DEFAULT_COLLECTION_PATH, itinerary_day_path: str = DEFAULT_ITINERARY_DAY_PATH, itinerary_path: str = DEFAULT_ITINERARY_PATH, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.location_path = "/" + location_path.lstrip("/")
        self.visit_path = "/" + visit_path.lstrip("/")
        self.image_path = "/" + image_path.lstrip("/")
        self.collection_path = "/" + collection_path.lstrip("/")
        self.itinerary_day_path = "/" + itinerary_day_path.lstrip("/")
        self.itinerary_path = "/" + itinerary_path.lstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-API-Key": self.token, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
            return data if isinstance(data, dict) else {"response": data}
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"AdventureLog returned HTTP {exc.code} from {path}: {detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"AdventureLog request timed out after {self.timeout}s at {path}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to AdventureLog: {exc.reason}") from exc

    def get_location(self, location_id: str) -> dict[str, Any]:
        path = f"{self.location_path.rstrip('/')}/{quote(str(location_id), safe='')}"
        request = urllib.request.Request(
            self.base_url + path,
            headers={"X-API-Key": self.token, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise RuntimeError("AdventureLog location response was not an object")
            return data
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"AdventureLog returned HTTP {exc.code} from {path}: {detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"AdventureLog request timed out after {self.timeout}s at {path}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to AdventureLog: {exc.reason}") from exc


    def create_location(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.location_path, payload)

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.collection_path, payload)

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(self.base_url + path, headers={"X-API-Key": self.token, "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"AdventureLog returned HTTP {exc.code} from {path}: {detail}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"AdventureLog request timed out after {self.timeout}s at {path}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to AdventureLog: {exc.reason}") from exc

    def list_itinerary_days(self) -> list[dict[str, Any]]:
        data = self._get(self.itinerary_day_path)
        values = data.get("results", []) if isinstance(data, dict) else data
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_collections(self) -> list[dict[str, Any]]:
        data = self._get(self.collection_path)
        values = data.get("results", []) if isinstance(data, dict) else data
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_itinerary_items(self) -> list[dict[str, Any]]:
        data = self._get(self.itinerary_path)
        values = data.get("results", []) if isinstance(data, dict) else data
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def create_itinerary_day(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.itinerary_day_path, payload)

    def create_itinerary_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.itinerary_path, payload)

    def create_visit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.visit_path, payload)

    def update_visit(self, visit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"{self.visit_path.rstrip('/')}/{quote(str(visit_id), safe='')}"
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"X-API-Key": self.token, "Content-Type": "application/json", "Accept": "application/json"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
            return data if isinstance(data, dict) else {"response": data}
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"AdventureLog returned HTTP {exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to AdventureLog: {exc.reason}") from exc

    def delete_image(self, image_id: str) -> None:
        path = f"{self.image_path.rstrip('/')}/{quote(str(image_id), safe='')}"
        request = urllib.request.Request(self.base_url + path, headers={"X-API-Key": self.token}, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"AdventureLog returned HTTP {exc.code} from {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not connect to AdventureLog: {exc.reason}") from exc

    def attach_image(self, immich_id: str, location_id: Any, content_type: str) -> dict[str, Any]:
        return self._post(self.image_path, {"immich_id": immich_id, "object_id": location_id, "content_type": content_type})


def _asset_detail(client: ImmichClient, asset_id: str) -> dict[str, Any]:
    path = f"/api/assets/{quote(str(asset_id), safe='')}"
    request = urllib.request.Request(
        client.base_url + path,
        headers={"x-api-key": client.api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=client.timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise RuntimeError("Immich asset response was not an object")
        return data
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RuntimeError(f"Immich returned HTTP {exc.code} from {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not connect to Immich: {exc.reason}") from exc


def response_assets(response: dict[str, Any]) -> list[dict[str, Any]]:
    nested = response.get("assets")
    if isinstance(nested, dict) and isinstance(nested.get("items"), list):
        return [item for item in nested["items"] if isinstance(item, dict)]
    for key in ("assets", "items", "results"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def fetch_assets(client: ImmichClient, start: date, end: date, page_size: int) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    page = 1
    while True:
        response = client.search(start, end, page, page_size)
        batch = response_assets(response)
        assets.extend(batch)
        next_page = response.get("nextPage")
        if len(batch) < page_size or next_page in (None, False, ""):
            break
        page = int(next_page) if isinstance(next_page, int) else page + 1
    return assets


def make_summary(assets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    assets = list(assets)
    days = Counter(a.get("day") for a in assets if a.get("day"))
    countries = Counter((a.get("location") or {}).get("country") for a in assets if (a.get("location") or {}).get("country"))
    return {
        "asset_count": len(assets),
        "days_with_photos": sorted(days),
        "photos_by_day": dict(sorted(days.items())),
        "countries": dict(sorted(countries.items())),
        "has_gps": sum(1 for a in assets if a.get("location") and ((a["location"].get("latitude") is not None) or (a["location"].get("longitude") is not None))),
    }


def infer_trip_name(manifest: dict[str, Any]) -> str:
    period = manifest["period"]
    countries = list((manifest.get("summary") or {}).get("countries") or {})
    suffix = ", ".join(countries[:2])
    return f"Trip {period['start']} to {period['end']}" + (f" ({suffix})" if suffix else "")


def representative_coordinates(assets: Iterable[dict[str, Any]], preferred_country: str | None = None) -> tuple[Any, Any] | None:
    assets = list(assets)
    if preferred_country:
        for asset in assets:
            point = asset.get("location") or {}
            if point.get("country") == preferred_country and point.get("latitude") is not None and point.get("longitude") is not None:
                return point["latitude"], point["longitude"]
    for asset in assets:
        point = asset.get("location") or {}
        if point.get("latitude") is not None and point.get("longitude") is not None:
            return point["latitude"], point["longitude"]
    return None


def attach_missing(manifest: dict[str, Any], client: AdventureLogClient, location_id: str, content_type: str, existing_ids: set[str]) -> tuple[int, int]:
    skipped = 0
    attached = 0
    try:
        for asset in manifest.get("assets", []):
            asset_id = str(asset["id"]) if asset.get("id") else None
            if not asset_id or asset_id in existing_ids:
                if asset_id and asset_id in existing_ids:
                    skipped += 1
                continue
            client.attach_image(asset_id, location_id, content_type)
            existing_ids.add(asset_id)
            attached += 1
    except RuntimeError as exc:
        raise RuntimeError(f"location {location_id} and its visit were retained; image attachment stopped after {attached} new item(s), skipping {skipped}: {exc}. Retry with --location-id {location_id}.") from exc
    return skipped, attached


def existing_immich_ids(location: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    images = location.get("images") or []
    if isinstance(images, dict):
        images = images.get("results") or images.get("items") or []
    if isinstance(images, list):
        for image in images:
            if isinstance(image, dict) and image.get("immich_id"):
                values.add(str(image["immich_id"]))
    return values


def dedupe_location_images(location: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    images = location.get("images") or []
    if isinstance(images, dict):
        images = images.get("results") or images.get("items") or []
    groups: dict[str, list[dict[str, Any]]] = {}
    for image in images if isinstance(images, list) else []:
        if isinstance(image, dict) and image.get("immich_id"):
            groups.setdefault(str(image["immich_id"]), []).append(image)
    deletions: list[dict[str, Any]] = []
    for immich_id, records in groups.items():
        ordered = sorted(records, key=lambda item: not bool(item.get("is_primary")))
        for duplicate in ordered[1:]:
            if duplicate.get("id"):
                deletions.append({"id": str(duplicate["id"]), "immich_id": immich_id})
    report = {"total_associations": sum(len(records) for records in groups.values()), "unique_immich_ids": len(groups), "duplicate_count": len(deletions), "delete": deletions}
    return deletions, report


def dedupe_state_locations(state: dict[str, Any], client: AdventureLogClient, report_path: Path, apply: bool, batch_size: int = 0) -> None:
    if batch_size < 0:
        raise ValueError("--dedupe-batch-size must be zero or positive")
    report: dict[str, Any] = {"schema": "immich-adventure-dedupe-report/v1", "collection_id": state.get("collection_id"), "groups": [], "deleted_association_ids": []}
    deleted_count = 0
    for group_name, group_state in state.get("groups", {}).items():
        location_id = group_state.get("location_id")
        if not location_id:
            raise RuntimeError(f"state group {group_name} has no location_id")
        location = client.get_location(str(location_id))
        deletions, details = dedupe_location_images(location)
        report["groups"].append({"name": group_name, "location_id": str(location_id), **details})
        if apply:
            for deletion in deletions:
                if batch_size and deleted_count >= batch_size:
                    break
                client.delete_image(deletion["id"])
                report["deleted_association_ids"].append(deletion["id"])
                deleted_count += 1
            verified = client.get_location(str(location_id))
            _, verification = dedupe_location_images(verified)
            report["groups"][-1]["remaining_duplicate_count"] = verification["duplicate_count"]
            report["groups"][-1]["verification"] = verification
    report["apply"] = apply
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote dedupe report to {report_path}; {sum(group['duplicate_count'] for group in report['groups'])} duplicate association(s) found")


def _clean_label(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _asset_coordinates(asset: dict[str, Any]) -> tuple[float, float] | None:
    point = asset.get("location") or {}
    try:
        if point.get("latitude") is None or point.get("longitude") is None:
            return None
        return float(point["latitude"]), float(point["longitude"])
    except (TypeError, ValueError):
        return None


def _centroid(assets: Iterable[dict[str, Any]]) -> tuple[float, float] | None:
    points = [point for asset in assets if (point := _asset_coordinates(asset))]
    if not points:
        return None
    return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points)


def _distance_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(value))


def _poi_point(element: dict[str, Any]) -> tuple[float, float] | None:
    if element.get("lat") is not None and element.get("lon") is not None:
        return _asset_coordinates({"location": {"latitude": element["lat"], "longitude": element["lon"]}})
    center = element.get("center") or {}
    if center.get("lat") is not None and center.get("lon") is not None:
        return _asset_coordinates({"location": {"latitude": center["lat"], "longitude": center["lon"]}})
    return None


def _poi_category(tags: dict[str, Any]) -> str:
    for key in ("tourism", "historic", "amenity", "leisure", "shop", "railway"):
        if tags.get(key):
            return f"{key}:{tags[key]}"
    return "place"


def _poi_cache_value(value: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Read both the current cache format and the previous list-only format."""
    if isinstance(value, list):
        return value, None
    if isinstance(value, dict):
        elements = value.get("elements")
        error = value.get("error")
        return elements if isinstance(elements, list) else None, str(error) if error else None
    return None, None


def _osm_map_url(lat: float, lon: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}"


def suggest_pois(
    plan: dict[str, Any], endpoint: str, radius_m: int, cache_path: Path,
    max_requests: int = 4, min_interval_seconds: float = 2.0, request_timeout: int = 20,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Read nearby named OSM POIs with a cache, strict request budget and pacing.

    Cached results (including provider failures) make no public request.  A
    fresh request is limited to ``max_requests`` per run and spaced by at
    least ``min_interval_seconds`` so this remains suitable for a background
    import against a shared Overpass endpoint.
    """
    if radius_m < 1:
        raise ValueError("--poi-radius must be positive")
    if max_requests < 0:
        raise ValueError("--poi-max-requests must be zero or positive")
    if min_interval_seconds < 0:
        raise ValueError("--poi-min-interval must be zero or positive")
    if request_timeout < 1:
        raise ValueError("--poi-timeout must be positive")
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    suggestions: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    requested = 0
    last_request_at: float | None = None
    for group in plan.get("groups", []):
        coordinates = group.get("coordinates")
        if not coordinates:
            continue
        lat, lon = coordinates
        cache_key = hashlib.sha256(f"{endpoint}|{lat:.6f}|{lon:.6f}|{radius_m}".encode()).hexdigest()
        if cache_key in cache:
            elements, cached_error = _poi_cache_value(cache[cache_key])
            if cached_error:
                errors[group["name"]] = f"cached provider failure: {cached_error}"
                continue
            if elements is None:
                # Treat malformed old cache entries as a miss rather than
                # trusting them to suppress a useful lookup indefinitely.
                cache.pop(cache_key, None)
        else:
            elements = None
        if elements is None:
            if requested >= max_requests:
                errors[group["name"]] = f"POI request cap reached ({max_requests} fresh request(s) per run)"
                continue
            if last_request_at is not None:
                delay = min_interval_seconds - (time.monotonic() - last_request_at)
                if delay > 0:
                    time.sleep(delay)
            query = (
                f"[out:json][timeout:{min(15, request_timeout)}];"
                f"nwr(around:{radius_m},{lat},{lon})[name]"
                '[~"^(tourism|historic|amenity|leisure|shop|railway)$"~"."];out center tags;'
            )
            request = urllib.request.Request(endpoint, data=query.encode("utf-8"), headers={"User-Agent": POI_USER_AGENT, "Content-Type": "text/plain", "Accept": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:
                    decoded = json.load(response)
                elements = decoded.get("elements", []) if isinstance(decoded, dict) else []
            except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
                errors[group["name"]] = str(exc)
                # A rate-limited/unavailable shared service should not be hit
                # again merely because this resumable importer is restarted.
                cache[cache_key] = {"error": str(exc), "cached_at": datetime.now(timezone.utc).isoformat()}
                requested += 1
                last_request_at = time.monotonic()
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                continue
            requested += 1
            last_request_at = time.monotonic()
            cache[cache_key] = {"elements": elements, "cached_at": datetime.now(timezone.utc).isoformat()}
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        candidates = []
        origin = (float(lat), float(lon))
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags") or {}
            name = tags.get("name")
            point = _poi_point(element)
            if not name or not point:
                continue
            element_type, element_id = element.get("type"), element.get("id")
            candidates.append({
                "name": name,
                "category": _poi_category(tags),
                "distance_m": round(_distance_km(origin, point) * 1000, 1),
                "osm_url": f"https://www.openstreetmap.org/{element_type}/{element_id}",
                "openstreetmap_map_url": _osm_map_url(point[0], point[1]),
            })
        suggestions[group["name"]] = sorted(candidates, key=lambda item: item["distance_m"])[:10]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return suggestions, errors


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _rome_timestamp(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.astimezone(ZoneInfo(PILOT_TIMEZONE)).isoformat() if parsed else None


def _pilot_asset_day(asset: dict[str, Any]) -> str | None:
    """Use the local capture date, rather than the UTC calendar date."""
    parsed = _timestamp(asset.get("taken_at"))
    return parsed.astimezone(ZoneInfo(PILOT_TIMEZONE)).date().isoformat() if parsed else asset.get("day")


def _stop_period_label(value: Any) -> str:
    parsed = _timestamp(value)
    hour = parsed.astimezone(ZoneInfo(PILOT_TIMEZONE)).hour if parsed else 12
    if hour < 12:
        return "Morning stop"
    if hour < 18:
        return "Afternoon stop"
    return "Evening stop"


def _pilot_stop_key(asset_ids: Iterable[str]) -> str:
    joined = "|".join(sorted(str(value) for value in asset_ids))
    return "sample-2026-09-03-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _cluster_pilot_assets(assets: list[dict[str, Any]], distance_km: float = 0.35, gap_minutes: int = 120) -> list[list[dict[str, Any]]]:
    """Deterministic sequential GPS/time stops; missing GPS is time-only."""
    if distance_km <= 0 or gap_minutes < 1:
        raise ValueError("pilot stop distance and time gap must be positive")
    ordered = sorted(assets, key=lambda asset: (_timestamp(asset.get("taken_at")) or datetime.min.replace(tzinfo=timezone.utc), str(asset.get("id"))))
    clusters: list[list[dict[str, Any]]] = []
    for asset in ordered:
        if not clusters:
            clusters.append([asset])
            continue
        current = clusters[-1]
        previous_time = _timestamp(current[-1].get("taken_at"))
        current_time = _timestamp(asset.get("taken_at"))
        time_close = bool(previous_time and current_time and (current_time - previous_time).total_seconds() <= gap_minutes * 60)
        first_point, point = _centroid(current), _asset_coordinates(asset)
        place_close = not first_point or not point or _distance_km(first_point, point) <= distance_km
        if time_close and place_close:
            current.append(asset)
        else:
            clusters.append([asset])
    return clusters


def _stop_label(group: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str, str, str]:
    """Only use a nearby, named POI when distance makes that attribution useful."""
    for candidate in candidates:
        distance = candidate.get("distance_m")
        if not isinstance(distance, (int, float)) or distance > 75:
            continue
        confidence = "high" if distance <= 25 else "medium"
        return _clean_label(candidate["name"]), confidence, "OpenStreetMap/Overpass"
    city = _clean_label(group.get("city")) or "Sample City"
    return f"{city} — {_stop_period_label(group.get('start_at'))}", "low", "generic GPS/time fallback"


def build_day_stop_pilot_plan(manifest: dict[str, Any], max_photos: int = 30, poi_suggestions: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Build the deliberately narrow 3 September Sample City stop-location pilot."""
    if not 1 <= max_photos <= 30:
        raise ValueError("--pilot-max-photos must be between 1 and 30")
    seen: set[str] = set()
    day_assets = []
    for asset in manifest.get("assets", []):
        asset_id = str(asset.get("id") or "")
        if asset_id and asset_id not in seen and _pilot_asset_day(asset) == PILOT_DAY:
            seen.add(asset_id)
            day_assets.append(asset)
    day_assets.sort(key=lambda asset: (_timestamp(asset.get("taken_at")) or datetime.max.replace(tzinfo=timezone.utc), str(asset.get("id"))))
    selected = [asset for asset in day_assets if _timestamp(asset.get("taken_at"))][:max_photos]
    excluded_without_timestamp = [str(asset["id"]) for asset in day_assets if not _timestamp(asset.get("taken_at"))]
    groups: list[dict[str, Any]] = []
    for number, cluster in enumerate(_cluster_pilot_assets(selected), 1):
        timestamps = sorted(str(asset["taken_at"]) for asset in cluster if _timestamp(asset.get("taken_at")))
        coordinates = _centroid(cluster)
        city = _clean_label((cluster[0].get("location") or {}).get("city")) or "Sample City"
        stop_key = _pilot_stop_key(str(asset["id"]) for asset in cluster)
        group = {
            "key": stop_key,
            "proposal_key": f"stop-{number:02d}",
            "name": f"{city} — stop {number}",
            "city": city,
            "country": _clean_label((cluster[0].get("location") or {}).get("country")) or "Italy",
            "asset_ids": [str(asset["id"]) for asset in cluster],
            "asset_count": len(cluster),
            "start_at": _rome_timestamp(timestamps[0]),
            "end_at": _rome_timestamp(timestamps[-1]),
            "coordinates": list(coordinates) if coordinates else None,
        }
        if coordinates:
            group["openstreetmap_map_url"] = _osm_map_url(*coordinates)
        groups.append(group)
    suggestions = poi_suggestions or {}
    provisional_labels: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    for group in groups:
        candidates = suggestions.get(group["name"], [])
        label, confidence, source = _stop_label(group, candidates)
        provisional_labels.append((label, confidence, source, candidates))
    repeated_generic = Counter(label for label, confidence, _, _ in provisional_labels if confidence == "low")
    generic_index: Counter[str] = Counter()
    for group, (label, confidence, source, candidates) in zip(groups, provisional_labels):
        if confidence == "low" and repeated_generic[label] > 1:
            generic_index[label] += 1
            label = f"{label} {generic_index[label]}"
        group["name"] = label
        group["label"] = {"value": label, "confidence": confidence, "source": source}
        group["poi_candidates"] = candidates
        group["visit"] = {
            "location": f"$new_location:{group['key']}", "start_date": group["start_at"],
            "end_date": group["end_at"], "timezone": PILOT_TIMEZONE,
            "notes": f"Sample City day-stop pilot from {group['asset_count']} Immich photo(s).",
        }
        location_payload: dict[str, Any] = {"name": label, "collections": ["$existing_collection:Sample City trip"]}
        if group["coordinates"]:
            location_payload["latitude"], location_payload["longitude"] = group["coordinates"]
        group["location"] = location_payload
        group["itinerary_item"] = {"collection": "$existing_collection:Sample City trip", "content_type": "location", "object_id": f"$new_location:{group['key']}", "date": PILOT_DAY, "is_global": False, "order": int(group["proposal_key"].split("-")[1]) - 1}
    return {
        "schema": "immich-adventure-sample-day-stop-pilot/v1",
        "pilot": {"collection_name": PILOT_COLLECTION_NAME, "date": PILOT_DAY, "timezone": PILOT_TIMEZONE, "max_photos": max_photos, "legacy_location_policy": "No legacy location, visit, collection, image association, or itinerary item is changed. Apply refuses if a non-pilot location is already on Day 1, because leaving it there would retain its shared gallery."},
        "source_asset_count": len(day_assets), "source_asset_ids": [str(asset["id"]) for asset in day_assets],
        "selected_asset_count": len(selected), "selected_asset_ids": [str(asset["id"]) for asset in selected],
        "excluded_missing_timestamp_ids": excluded_without_timestamp,
        "summary": make_summary(selected), "assets": selected, "groups": groups,
        "itinerary_day": {"collection": "$existing_collection:Sample City trip", "date": PILOT_DAY, "name": "Day 1", "description": "Sample City day-stop photo pilot."},
        "next_step": "Dry run only. Review every source asset and proposed stop. Remote writes require --day-stop-pilot --apply --confirm CREATE.",
    }


def build_isolated_day_stop_pilot_plan(manifest: dict[str, Any], max_photos: int = 30, poi_suggestions: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    plan = build_day_stop_pilot_plan(manifest, max_photos, poi_suggestions)
    plan["schema"] = "immich-adventure-sample-isolated-pilot/v1"
    plan["pilot"]["collection_name"] = ISOLATED_PILOT_COLLECTION_NAME
    plan["pilot"]["legacy_location_policy"] = "Creates and uses only the isolated pilot collection; existing Sample City trip records are never read for mutation or changed."
    for group in plan["groups"]:
        group["location"]["collections"] = [f"$new_collection:{ISOLATED_PILOT_COLLECTION_NAME}"]
        group["itinerary_item"]["collection"] = f"$new_collection:{ISOLATED_PILOT_COLLECTION_NAME}"
    plan["itinerary_day"]["collection"] = f"$new_collection:{ISOLATED_PILOT_COLLECTION_NAME}"
    return plan


def build_rebuild_plan(manifest: dict[str, Any], collection_name: str, min_location_assets: int) -> dict[str, Any]:
    """Group assets by retained city anchors without contacting AdventureLog."""
    if min_location_assets < 1:
        raise ValueError("--min-location-assets must be positive")
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for asset in manifest.get("assets", []):
        point = asset.get("location") or {}
        country = _clean_label(point.get("country")) or "Unknown"
        city = _clean_label(point.get("city")) or "Nearby/Other"
        buckets.setdefault((country.casefold(), city.casefold()), []).append(asset)
    unique_buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    for key, assets in buckets.items():
        country = _clean_label((assets[0].get("location") or {}).get("country")) or "Unknown"
        city = _clean_label((assets[0].get("location") or {}).get("city")) or "Nearby/Other"
        seen = set()
        unique_buckets[key] = [asset for asset in assets if asset.get("id") and not (asset["id"] in seen or seen.add(asset["id"]))]
        labels[key] = (country, city)
    anchors = {
        key: assets for key, assets in unique_buckets.items()
        if len(assets) >= min_location_assets
        and labels[key][0] != "Unknown"
        and labels[key][1].casefold() != "nearby/other"
        and _centroid(assets) is not None
    }
    folded: dict[tuple[str, str], list[dict[str, Any]]] = {key: list(assets) for key, assets in anchors.items()}
    other: list[dict[str, Any]] = []
    for key, assets in unique_buckets.items():
        if key in anchors:
            continue
        country, _ = labels[key]
        source_centroid = _centroid(assets)
        candidates = [(anchor_key, _distance_km(source_centroid, _centroid(anchor_assets)))
                      for anchor_key, anchor_assets in anchors.items()
                      if labels[anchor_key][0].casefold() == country.casefold() and source_centroid and _centroid(anchor_assets)]
        if candidates:
            nearest = min(candidates, key=lambda item: item[1])[0]
            folded[nearest].extend(assets)
        else:
            other.extend(assets)
    if other:
        folded[("travel", "other")] = other
        labels[("travel", "other")] = ("Travel", "Other")
    groups = []
    for key in sorted(folded):
        assets = folded[key]
        country, city = labels[key]
        unique = []
        seen = set()
        for asset in assets:
            if asset.get("id") and asset["id"] not in seen:
                seen.add(asset["id"])
                unique.append(asset)
        days = sorted({asset.get("day") for asset in unique if asset.get("day")})
        timestamps = sorted({asset.get("taken_at") for asset in unique if asset.get("taken_at")})
        groups.append({
            "name": "Travel / Other" if key == ("travel", "other") else f"{city}, {country}",
            "country": country,
            "city": city,
            "asset_count": len(unique),
            "asset_ids": [asset["id"] for asset in unique],
            "start": days[0] if days else manifest["period"]["start"],
            "end": days[-1] if days else manifest["period"]["end"],
            "start_at": timestamps[0] if timestamps else f"{days[0] if days else manifest['period']['start']}T00:00:00Z",
            "end_at": timestamps[-1] if timestamps else f"{days[-1] if days else manifest['period']['end']}T23:59:59Z",
            "coordinates": representative_coordinates(unique, country),
        })
    return {
        "schema": "immich-adventure-rebuild/v1",
        "generated_at": manifest.get("generated_at"),
        "source": manifest.get("source"),
        "period": manifest["period"],
        "collection": {"name": collection_name},
        "min_location_assets": min_location_assets,
        "summary": manifest.get("summary", {}),
        "groups": groups,
        "assets": manifest.get("assets", []),
        "next_step": "Review groups before using --apply; this mode creates new collection locations and does not delete legacy locations.",
    }


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_itinerary_plan(plan: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build dated location membership from photo days without contacting AdventureLog."""
    assets = {str(asset.get("id")): asset for asset in plan.get("assets", []) if asset.get("id")}
    groups = {group["name"]: group for group in plan.get("groups", [])}
    state_groups = (state or {}).get("groups", {})
    by_day: dict[str, list[dict[str, Any]]] = {}
    for group_name, group in groups.items():
        location_id = (state_groups.get(group_name) or {}).get("location_id")
        days: dict[str, int] = {}
        for asset_id in group.get("asset_ids", []):
            day = assets.get(str(asset_id), {}).get("day")
            if day:
                days[day] = days.get(day, 0) + 1
        for day, count in days.items():
            by_day.setdefault(day, []).append({"group": group_name, "location_id": location_id, "asset_count": count})
    result = []
    for index, (day, locations) in enumerate(sorted(by_day.items()), 1):
        result.append({"date": day, "name": f"Day {index}", "locations": locations})
    return {"days": result}


def apply_itinerary_plan(plan: dict[str, Any], client: AdventureLogClient, state: dict[str, Any]) -> None:
    collection_id = state.get("collection_id")
    if not collection_id:
        raise ValueError("itinerary creation requires collection_id in state")
    itinerary_plan = build_itinerary_plan(plan, state)
    existing_days = {(str(day.get("collection")), day.get("date")) for day in client.list_itinerary_days()}
    existing_items = {(str(item.get("collection")), str(item.get("object_id")), item.get("date")) for item in client.list_itinerary_items()}
    created_days = 0
    created_items = 0
    for day in itinerary_plan["days"]:
        day_key = (str(collection_id), day["date"])
        if day_key not in existing_days:
            client.create_itinerary_day({"collection": collection_id, "date": day["date"], "name": day["name"], "description": "Generated from Immich photo dates."})
            existing_days.add(day_key)
            created_days += 1
        for order, location in enumerate(day["locations"]):
            location_id = location.get("location_id")
            if not location_id:
                raise RuntimeError(f"itinerary group {location['group']} has no checkpointed location_id")
            item_key = (str(collection_id), str(location_id), day["date"])
            if item_key in existing_items:
                continue
            client.create_itinerary_item({"collection": collection_id, "content_type": "location", "object_id": location_id, "date": day["date"], "is_global": False, "order": order})
            existing_items.add(item_key)
            created_items += 1
    print(f"Applied itinerary plan; created {created_days} day(s) and {created_items} location item(s)")


def read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("schema") != "immich-adventure-rebuild-state/v1":
        raise ValueError(f"unsupported rebuild state file: {path}")
    return state


def read_pilot_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("schema") not in (PILOT_STATE_SCHEMA, ISOLATED_PILOT_STATE_SCHEMA):
        raise ValueError(f"unsupported Sample City pilot state file: {path}")
    return state


def _object_id(value: dict[str, Any], description: str) -> str:
    object_id = value.get("id") or value.get("pk")
    if not object_id:
        raise RuntimeError(f"AdventureLog {description} response contained no id or pk")
    return str(object_id)


def _pilot_collection_id(client: AdventureLogClient, state: dict[str, Any] | None) -> str:
    matches = [item for item in client.list_collections() if _clean_label(item.get("name")) == PILOT_COLLECTION_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one existing collection named {PILOT_COLLECTION_NAME!r}; found {len(matches)}. No pilot objects were created.")
    collection_id = _object_id(matches[0], "collection")
    if state and str(state.get("collection_id")) != collection_id:
        raise RuntimeError("pilot state belongs to a different Sample City trip collection; no objects were created")
    return collection_id


def apply_day_stop_pilot_plan(plan: dict[str, Any], client: AdventureLogClient, content_type: str, state_path: Path) -> dict[str, Any]:
    """Create only stop-specific pilot records; never detach or mutate legacy records."""
    state = read_pilot_state(state_path)
    collection_id = _pilot_collection_id(client, state)
    if state is None:
        state = {"schema": PILOT_STATE_SCHEMA, "collection_id": collection_id, "stops": {}, "itinerary_day_id": None}
    existing_days = {(str(day.get("collection")), day.get("date")): day for day in client.list_itinerary_days()}
    existing_items = client.list_itinerary_items()
    known_locations = {str(stop.get("location_id")) for stop in state["stops"].values() if stop.get("location_id")}
    legacy_items = [item for item in existing_items if str(item.get("collection")) == collection_id and item.get("date") == PILOT_DAY and str(item.get("object_id")) not in known_locations]
    if legacy_items:
        legacy_ids = ", ".join(sorted({str(item.get("object_id")) for item in legacy_items if item.get("object_id")})) or "unknown"
        raise RuntimeError(f"Day 1 already contains non-pilot location item(s): {legacy_ids}. This pilot will not detach or alter them, because their shared galleries would remain visible. Verify/remove those legacy items separately after the pilot is approved, then retry. No pilot objects were created.")
    created_locations = 0
    attached = 0
    try:
        for group in plan["groups"]:
            stop_state = state["stops"].get(group["key"])
            if stop_state:
                location_id = str(stop_state.get("location_id") or "")
                if not location_id:
                    raise RuntimeError(f"pilot state stop {group['key']} has no location_id")
                live = client.get_location(location_id)
                attached_ids = set(str(value) for value in stop_state.get("attached_asset_ids", [])) | existing_immich_ids(live)
                stop_state["attached_asset_ids"] = sorted(attached_ids)
            else:
                payload = dict(group["location"])
                payload["collections"] = [collection_id]
                location_id = _object_id(client.create_location(payload), "pilot location")
                visit = dict(group["visit"])
                visit["location"] = location_id
                client.create_visit(visit)
                stop_state = {"location_id": location_id, "attached_asset_ids": [], "label": group["name"]}
                state["stops"][group["key"]] = stop_state
                created_locations += 1
                write_state(state_path, state)
            present = set(str(value) for value in stop_state.get("attached_asset_ids", []))
            for asset_id in group["asset_ids"]:
                if asset_id in present:
                    continue
                client.attach_image(asset_id, location_id, content_type)
                present.add(asset_id)
                stop_state["attached_asset_ids"] = sorted(present)
                attached += 1
                write_state(state_path, state)
        day_key = (collection_id, PILOT_DAY)
        if day_key not in existing_days:
            day_payload = dict(plan["itinerary_day"])
            day_payload["collection"] = collection_id
            state["itinerary_day_id"] = _object_id(client.create_itinerary_day(day_payload), "itinerary day")
            write_state(state_path, state)
        else:
            state["itinerary_day_id"] = str(existing_days[day_key].get("id") or existing_days[day_key].get("pk") or "") or None
            write_state(state_path, state)
        item_keys = {(str(item.get("collection")), str(item.get("object_id")), item.get("date")) for item in existing_items}
        for order, group in enumerate(plan["groups"]):
            location_id = str(state["stops"][group["key"]]["location_id"])
            item_key = (collection_id, location_id, PILOT_DAY)
            if item_key in item_keys:
                continue
            client.create_itinerary_item({"collection": collection_id, "content_type": "location", "object_id": location_id, "date": PILOT_DAY, "is_global": False, "order": order})
            item_keys.add(item_key)
    except RuntimeError as exc:
        raise RuntimeError(f"Sample City pilot stopped after {created_locations} location(s) and {attached} image(s). Its checkpoint is {state_path}; legacy records were not changed. {exc}") from exc
    report = {"collection_id": collection_id, "locations": [{"id": str(state["stops"][group["key"]]["location_id"]), "label": group["name"], "photo_count": len(group["asset_ids"])} for group in plan["groups"]]}
    print("Applied Sample City day-stop pilot: " + json.dumps(report, ensure_ascii=False))
    return report


def apply_isolated_day_stop_pilot_plan(plan: dict[str, Any], client: AdventureLogClient, content_type: str, state_path: Path, include_itinerary: bool = True) -> dict[str, Any]:
    """Checkpointed isolated collection apply; does not inspect or alter legacy trip records."""
    state = read_pilot_state(state_path) if state_path.exists() else None
    if state and state.get("schema") != ISOLATED_PILOT_STATE_SCHEMA:
        raise ValueError(f"unsupported isolated pilot state file: {state_path}")
    if state is None:
        collection_id = _object_id(client.create_collection({"name": ISOLATED_PILOT_COLLECTION_NAME, "start_date": PILOT_DAY, "end_date": PILOT_DAY, "description": "Isolated Sample City day-stop pilot."}), "isolated pilot collection")
        state = {"schema": ISOLATED_PILOT_STATE_SCHEMA, "collection_id": collection_id, "stops": {}, "itinerary_day_id": None, "itinerary_items": {}}
        write_state(state_path, state)
    collection_id = str(state["collection_id"])
    for group in plan["groups"]:
        stop = state["stops"].get(group["key"])
        if stop:
            location_id = str(stop["location_id"])
            attached_ids = set(stop.get("attached_asset_ids", [])) | existing_immich_ids(client.get_location(location_id))
        else:
            payload = dict(group["location"]); payload["collections"] = [collection_id]
            location_id = _object_id(client.create_location(payload), "isolated pilot location")
            visit = dict(group["visit"]); visit["location"] = location_id
            visit_id = _object_id(client.create_visit(visit), "isolated pilot visit")
            stop = {"location_id": location_id, "visit_id": visit_id, "attached_asset_ids": [], "label": group["name"]}
            state["stops"][group["key"]] = stop; attached_ids = set(); write_state(state_path, state)
        for asset_id in group["asset_ids"]:
            if asset_id in attached_ids:
                continue
            client.attach_image(asset_id, location_id, content_type)
            attached_ids.add(asset_id); stop["attached_asset_ids"] = sorted(attached_ids); write_state(state_path, state)
    if include_itinerary:
        apply_isolated_day_stop_pilot_itinerary(plan, client, state_path, state=state)
    report = {"collection_id": collection_id, "locations": [{"id": state["stops"][g["key"]]["location_id"], "visit_id": state["stops"][g["key"]].get("visit_id"), "item_id": state["itinerary_items"].get(g["key"]), "label": g["name"], "photo_count": len(g["asset_ids"])} for g in plan["groups"]], "itinerary_day_id": state.get("itinerary_day_id")}
    print("Applied isolated Sample City pilot: " + json.dumps(report, ensure_ascii=False))
    return report


def apply_isolated_day_stop_pilot_itinerary(plan: dict[str, Any], client: AdventureLogClient, state_path: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create missing isolated-pilot day/items as a separately confirmed step."""
    state = state or read_pilot_state(state_path)
    if not state or state.get("schema") != ISOLATED_PILOT_STATE_SCHEMA:
        raise ValueError(f"isolated pilot location state is required: {state_path}")
    state.setdefault("itinerary_items", {})
    collection_id = str(state.get("collection_id") or "")
    if not collection_id:
        raise ValueError("isolated pilot state has no collection_id")
    days = {(str(day.get("collection")), day.get("date")): day for day in client.list_itinerary_days()}
    day = days.get((collection_id, PILOT_DAY))
    if day:
        state["itinerary_day_id"] = str(day.get("id") or day.get("pk") or "") or None
    elif not state.get("itinerary_day_id"):
        payload = dict(plan["itinerary_day"]); payload["collection"] = collection_id
        state["itinerary_day_id"] = _object_id(client.create_itinerary_day(payload), "isolated pilot itinerary day")
    items = {(str(item.get("collection")), str(item.get("object_id")), item.get("date")): item for item in client.list_itinerary_items()}
    created = 0
    for order, group in enumerate(plan["groups"]):
        stop = state.get("stops", {}).get(group["key"])
        if not stop or not stop.get("location_id"):
            raise RuntimeError(f"pilot state has no location for stop {group['key']}")
        location_id = str(stop["location_id"]); key = (collection_id, location_id, PILOT_DAY)
        item = items.get(key)
        if item:
            state["itinerary_items"][group["key"]] = str(item.get("id") or item.get("pk") or "") or None
        elif group["key"] not in state["itinerary_items"]:
            state["itinerary_items"][group["key"]] = _object_id(client.create_itinerary_item({"collection": collection_id, "content_type": "location", "object_id": location_id, "date": PILOT_DAY, "is_global": False, "order": order}), "isolated pilot itinerary item")
            created += 1
        write_state(state_path, state)
    return {"collection_id": collection_id, "itinerary_day_id": state.get("itinerary_day_id"), "created_items": created, "item_ids": dict(state["itinerary_items"])}


def group_timezone(group: dict[str, Any], default_timezone: str, policy: str) -> str:
    if policy == "country" and group.get("country", "").casefold() == "italy":
        return "Europe/Rome"
    return default_timezone


def visit_payload(group: dict[str, Any], location_id: str, timezone_name: str) -> dict[str, Any]:
    return {
        "location": location_id,
        "start_date": group.get("start_at") or f"{group['start']}T00:00:00Z",
        "end_date": group.get("end_at") or f"{group['end']}T23:59:59Z",
        "timezone": timezone_name,
        "notes": f"Grouped from Immich: {group['asset_count']} photo(s).",
    }


def apply_rebuild_plan(plan: dict[str, Any], client: AdventureLogClient, timezone_name: str, content_type: str, state_path: Path | None = None, batch_size: int = 0, update_visits: bool = False, timezone_policy: str = "country", visits_only: bool = False, parallelism: int = 1) -> None:
    if batch_size < 0:
        raise ValueError("--batch-size must be zero or positive")
    if parallelism < 1:
        raise ValueError("--parallelism must be positive")
    period = plan["period"]
    state = read_state(state_path) if state_path else None
    if (update_visits or visits_only) and not state:
        raise ValueError("--update-visits requires an existing --state-file")
    if visits_only and not update_visits:
        raise ValueError("--visits-only requires --update-visits")
    if visits_only:
        missing = [group["name"] for group in plan.get("groups", []) if group["name"] not in state.get("groups", {})]
        if missing:
            raise ValueError(f"--visits-only state is missing groups: {', '.join(missing)}")
    if state:
        collection_id = state.get("collection_id")
        if not collection_id:
            raise RuntimeError("rebuild state has no collection_id")
    else:
        collection = client.create_collection({
            "name": plan["collection"]["name"],
            "start_date": period["start"],
            "end_date": period["end"],
            "description": "Grouped from Immich photo metadata.",
        })
        collection_id = collection.get("id") or collection.get("pk")
        if not collection_id:
            raise RuntimeError("AdventureLog collection response contained no id or pk; no locations were created")
        state = {"schema": "immich-adventure-rebuild-state/v1", "collection_id": collection_id, "groups": {}}
        if state_path:
            write_state(state_path, state)
    by_id = {str(asset.get("id")): asset for asset in plan.get("assets", []) if asset.get("id")}
    created_locations = 0
    attached = 0
    try:
        # Complete the collection structure before consuming the attachment batch.
        for group in plan.get("groups", []):
            group_state = state["groups"].get(group["name"])
            if group_state:
                location_id = group_state.get("location_id")
                if not location_id:
                    raise RuntimeError(f"state group {group['name']} has no location_id")
                existing_location = client.get_location(str(location_id))
                externally_attached = existing_immich_ids(existing_location)
                state_attached = set(str(asset_id) for asset_id in group_state.get("attached_asset_ids", []))
                if not visits_only and externally_attached - state_attached:
                    group_state["attached_asset_ids"] = sorted(state_attached | externally_attached)
                    if state_path:
                        write_state(state_path, state)
                if update_visits:
                    visits = existing_location.get("visits") or []
                    visit_id = visits[0].get("id") if isinstance(visits, list) and visits and isinstance(visits[0], dict) else None
                    if not visit_id:
                        raise RuntimeError(f"location {location_id} has no visit to update")
                    client.update_visit(str(visit_id), visit_payload(group, str(location_id), group_timezone(group, timezone_name, timezone_policy)))
            else:
                payload: dict[str, Any] = {"name": group["name"], "collections": [collection_id]}
                if group.get("coordinates"):
                    payload["latitude"], payload["longitude"] = group["coordinates"]
                location = client.create_location(payload)
                location_id = location.get("id") or location.get("pk")
                if not location_id:
                    raise RuntimeError(f"location response for {group['name']} contained no id or pk")
                client.create_visit(visit_payload(group, str(location_id), group_timezone(group, timezone_name, timezone_policy)))
                group_state = {"location_id": location_id, "attached_asset_ids": []}
                state["groups"][group["name"]] = group_state
                created_locations += 1
                if state_path:
                    write_state(state_path, state)
        if visits_only:
            print(f"Updated visits for {len(plan.get('groups', []))} group(s); no image attachments changed")
            return
        # Merge live associations for every group before scheduling attachments.
        pending: list[tuple[str, str, dict[str, Any]]] = []
        for group in plan.get("groups", []):
            group_state = state["groups"][group["name"]]
            location_id = group_state["location_id"]
            group_manifest = {"assets": [by_id[asset_id] for asset_id in group["asset_ids"] if asset_id in by_id]}
            seen = set(str(asset_id) for asset_id in group_state.get("attached_asset_ids", []))
            for asset in group_manifest["assets"]:
                asset_id = str(asset["id"])
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                pending.append((asset_id, str(location_id), group_state))
        if batch_size:
            pending = pending[:batch_size]
        checkpoint_lock = threading.Lock()

        def attach_one(item: tuple[str, str, dict[str, Any]]) -> None:
            nonlocal attached
            asset_id, location_id, group_state = item
            client.attach_image(asset_id, location_id, content_type)
            with checkpoint_lock:
                attached += 1
                current = set(str(value) for value in group_state.get("attached_asset_ids", []))
                current.add(asset_id)
                group_state["attached_asset_ids"] = sorted(current)
                if state_path:
                    write_state(state_path, state)

        # The client is stateless per request; only checkpoint mutation is locked.
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            list(executor.map(attach_one, pending))
    except RuntimeError as exc:
        raise RuntimeError(f"collection {collection_id} was created with {created_locations} location(s) and {attached} image(s); rebuild stopped: {exc}. Review the collection before retrying; legacy locations were not deleted.") from exc
    remaining = sum(max(0, len(group["asset_ids"]) - len(state["groups"].get(group["name"], {}).get("attached_asset_ids", []))) for group in plan.get("groups", []))
    print(f"Applied collection {collection_id}; created {created_locations} location(s), attached {attached} image(s), remaining {remaining}")


def resume_manifest(manifest: dict[str, Any], client: AdventureLogClient, location_id: str, content_type: str) -> None:
    location = client.get_location(location_id)
    existing = existing_immich_ids(location)
    skipped, attached = attach_missing(manifest, client, location_id, content_type, existing)
    print(f"Resumed AdventureLog location {location_id}; skipped {skipped} existing image(s), attached {attached} new image(s)")


def apply_manifest(manifest: dict[str, Any], client: AdventureLogClient, trip_name: str, timezone_name: str, content_type: str) -> None:
    period = manifest["period"]
    summary = manifest.get("summary") or {}
    location_payload: dict[str, Any] = {
        "name": trip_name,
    }
    countries = (summary.get("countries") or {})
    preferred_country = max(countries, key=countries.get) if countries else None
    coordinates = representative_coordinates(manifest.get("assets", []), preferred_country)
    if coordinates:
        location_payload["latitude"], location_payload["longitude"] = coordinates
    created = client.create_location(location_payload)
    location_id = created.get("id") or created.get("pk")
    if not location_id:
        raise RuntimeError("AdventureLog location response contained no id or pk; refusing to attach images")
    visit_payload = {
        "location": location_id,
        "start_date": f"{period['start']}T00:00:00Z",
        "end_date": f"{period['end']}T23:59:59Z",
        "timezone": timezone_name,
        "notes": f"Imported from Immich: {summary.get('asset_count', 0)} photo(s). Review this draft in AdventureLog.",
    }
    try:
        client.create_visit(visit_payload)
    except RuntimeError as exc:
        raise RuntimeError(f"location {location_id} was created but visit creation failed: {exc}. Recover by reviewing/removing that draft in AdventureLog before retrying.") from exc
    _, attached = attach_missing(manifest, client, str(location_id), content_type, set())
    print(f"Applied AdventureLog draft {created.get('id', created.get('pk', '(response had no id)'))}; attached {attached} image(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Immich metadata by period; dry-run by default, guarded AdventureLog write with --apply --confirm CREATE.")
    parser.add_argument("--start", required=True, type=parse_day, help="first day, YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=parse_day, help="last day, YYYY-MM-DD (inclusive)")
    parser.add_argument("--output", "-o", type=Path, default=Path("immich-manifest.json"))
    parser.add_argument("--base-url", default=os.environ.get("IMMICH_BASE_URL"), help="Immich URL (or IMMICH_BASE_URL)")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (or IMMICH_API_KEY)")
    parser.add_argument("--search-path", default=os.environ.get("IMMICH_SEARCH_PATH", DEFAULT_SEARCH_PATH), help=f"metadata endpoint (default: {DEFAULT_SEARCH_PATH})")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--input-json", type=Path, help="offline fixture: read an Immich response/list instead of making a network request")
    parser.add_argument("--adventurelog-base-url", default=os.environ.get("ADVENTURELOG_BASE_URL"), help="AdventureLog URL (or ADVENTURELOG_BASE_URL; only needed with --apply)")
    parser.add_argument("--adventurelog-token", default=os.environ.get("ADVENTURELOG_TOKEN"), help="Bearer token (or ADVENTURELOG_TOKEN; only needed with --apply)")
    parser.add_argument("--location-id", default=os.environ.get("ADVENTURELOG_LOCATION_ID"), help="resume an existing AdventureLog location; skips creation and attaches only missing images")
    parser.add_argument("--location-path", default=os.environ.get("ADVENTURELOG_LOCATION_PATH", DEFAULT_LOCATION_PATH))
    parser.add_argument("--visit-path", default=os.environ.get("ADVENTURELOG_VISIT_PATH", DEFAULT_VISIT_PATH))
    parser.add_argument("--image-path", default=os.environ.get("ADVENTURELOG_IMAGE_PATH", DEFAULT_IMAGE_PATH))
    parser.add_argument("--collection-path", default=os.environ.get("ADVENTURELOG_COLLECTION_PATH", DEFAULT_COLLECTION_PATH))
    parser.add_argument("--itinerary-day-path", default=os.environ.get("ADVENTURELOG_ITINERARY_DAY_PATH", DEFAULT_ITINERARY_DAY_PATH))
    parser.add_argument("--itinerary-path", default=os.environ.get("ADVENTURELOG_ITINERARY_PATH", DEFAULT_ITINERARY_PATH))
    parser.add_argument("--content-type", default=os.environ.get("ADVENTURELOG_CONTENT_TYPE", "location"))
    parser.add_argument("--timezone", default=os.environ.get("ADVENTURELOG_TIMEZONE", "Europe/Berlin"), help="visit IANA timezone (default: Europe/Berlin)")
    parser.add_argument("--trip-name", help="AdventureLog location name; otherwise inferred from period/countries")
    parser.add_argument("--collection-name", help="build a dry-run grouping plan by normalized country/city")
    parser.add_argument("--create-itinerary-days", action="store_true", help="plan/create generic itinerary days and location items from photo dates")
    parser.add_argument("--min-location-assets", type=int, default=5, help="minimum assets per city anchor; smaller groups fold to nearest anchor or Travel / Other (default: 5)")
    parser.add_argument("--state-file", type=Path, help="checkpoint file for collection apply/resume (default: rebuild-state.json beside --output)")
    parser.add_argument("--batch-size", type=int, default=0, help="maximum new image attachments per collection run; 0 means unlimited")
    parser.add_argument("--parallelism", type=int, default=1, help="concurrent image attachments for collection apply (default: 1)")
    parser.add_argument("--update-visits", action="store_true", help="with an existing collection state, PATCH stored visits to exact group timestamps")
    parser.add_argument("--visits-only", action="store_true", help="with --update-visits, PATCH visits and skip all image attachment work")
    parser.add_argument("--suggest-pois", action="store_true", help="read nearby named OSM POIs into the rebuild plan for review")
    parser.add_argument("--day-stop-pilot", action="store_true", help="safe, day-specific Sample City trip pilot: 2026-09-03 only; creates separate stop locations only after --apply --confirm CREATE")
    parser.add_argument("--isolated-day-stop-pilot", action="store_true", help="create only the isolated `Sample City trip — 2026-09-03 pilot` collection after --apply --confirm CREATE")
    parser.add_argument("--pilot-max-photos", type=int, default=30, help="Sample City pilot maximum selected photos (1-30; default: 30)")
    parser.add_argument("--overpass-url", default=os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS_URL), help="Overpass interpreter endpoint")
    parser.add_argument("--poi-radius", type=int, default=100, help="POI search radius in metres (default: 100)")
    parser.add_argument("--poi-cache", type=Path, help="local POI response cache (default: poi-cache.json beside output)")
    parser.add_argument("--poi-max-requests", type=int, default=4, help="maximum uncached public POI requests per run; 0 is cache-only (default: 4)")
    parser.add_argument("--poi-min-interval", type=float, default=2.0, help="minimum seconds between uncached public POI requests (default: 2.0)")
    parser.add_argument("--poi-timeout", type=int, default=20, help="per-request public POI timeout in seconds (default: 20)")
    parser.add_argument("--no-poi", action="store_true", help="skip all public POI lookups; use cached-free generic stop labels")
    parser.add_argument("--dedupe", action="store_true", help="report duplicate AdventureLog image associations from rebuild state")
    parser.add_argument("--dedupe-apply", action="store_true", help="delete only reported duplicate AdventureLog associations")
    parser.add_argument("--dedupe-confirm", help=f"required with --dedupe-apply; pass exactly {DEDUPE_CONFIRM_TEXT!r}")
    parser.add_argument("--dedupe-report", type=Path, help="dedupe report path (default: dedupe-report.json beside output)")
    parser.add_argument("--dedupe-batch-size", type=int, default=0, help="maximum duplicate associations to delete per dedupe apply; 0 means unlimited")
    parser.add_argument("--group-timezone-policy", choices=("country", "fixed"), default=os.environ.get("ADVENTURELOG_GROUP_TIMEZONE_POLICY", "country"), help="collection visit timezone policy: country maps Italy to Europe/Rome, fixed uses --timezone")
    parser.add_argument("--confirm", help=f"required with --apply; pass exactly {CONFIRM_TEXT!r}")
    parser.add_argument("--apply", action="store_true", help="create an AdventureLog draft and attach images (requires --confirm CREATE)")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_local_dotenv()
    args = build_parser().parse_args(argv)
    if args.end < args.start:
        print("error: --end must be on or after --start", file=sys.stderr)
        return 2
    if args.apply and args.confirm != CONFIRM_TEXT:
        print(f"error: --apply requires --confirm {CONFIRM_TEXT}", file=sys.stderr)
        return 2
    if args.page_size < 1:
        print("error: --page-size must be positive", file=sys.stderr)
        return 2
    if args.min_location_assets < 1:
        print("error: --min-location-assets must be positive", file=sys.stderr)
        return 2
    if args.batch_size < 0:
        print("error: --batch-size must be zero or positive", file=sys.stderr)
        return 2
    if args.parallelism < 1:
        print("error: --parallelism must be positive", file=sys.stderr)
        return 2
    if args.collection_name and args.location_id:
        print("error: --collection-name cannot be combined with --location-id", file=sys.stderr)
        return 2
    if args.visits_only and not args.update_visits:
        print("error: --visits-only requires --update-visits", file=sys.stderr)
        return 2
    if args.visits_only and not args.collection_name:
        print("error: --visits-only requires --collection-name", file=sys.stderr)
        return 2
    if args.dedupe_apply and args.dedupe_confirm != DEDUPE_CONFIRM_TEXT:
        print(f"error: --dedupe-apply requires --dedupe-confirm {DEDUPE_CONFIRM_TEXT}", file=sys.stderr)
        return 2
    if args.dedupe_confirm and not args.dedupe_apply:
        print("error: --dedupe-confirm is only valid with --dedupe-apply", file=sys.stderr)
        return 2
    if args.create_itinerary_days and not args.collection_name:
        print("error: --create-itinerary-days requires --collection-name", file=sys.stderr)
        return 2
    if args.dedupe_batch_size < 0:
        print("error: --dedupe-batch-size must be zero or positive", file=sys.stderr)
        return 2
    if (args.dedupe or args.dedupe_apply) and args.apply:
        print("error: dedupe mode cannot be combined with --apply", file=sys.stderr)
        return 2
    if args.suggest_pois and not args.collection_name:
        print("error: --suggest-pois requires --collection-name", file=sys.stderr)
        return 2
    if args.suggest_pois and args.no_poi:
        print("error: --suggest-pois cannot be combined with --no-poi", file=sys.stderr)
        return 2
    if args.poi_radius < 1:
        print("error: --poi-radius must be positive", file=sys.stderr)
        return 2
    if args.poi_max_requests < 0:
        print("error: --poi-max-requests must be zero or positive", file=sys.stderr)
        return 2
    if args.poi_min_interval < 0:
        print("error: --poi-min-interval must be zero or positive", file=sys.stderr)
        return 2
    if args.poi_timeout < 1:
        print("error: --poi-timeout must be positive", file=sys.stderr)
        return 2
    if args.day_stop_pilot or args.isolated_day_stop_pilot:
        if args.day_stop_pilot and args.isolated_day_stop_pilot:
            print("error: choose only one Sample City pilot mode", file=sys.stderr)
            return 2
        if args.start.isoformat() != PILOT_DAY or args.end.isoformat() != PILOT_DAY:
            print(f"error: --day-stop-pilot is restricted to {PILOT_DAY} only", file=sys.stderr)
            return 2
        if not 1 <= args.pilot_max_photos <= 30:
            print("error: --pilot-max-photos must be between 1 and 30", file=sys.stderr)
            return 2
        if args.collection_name or args.location_id or args.create_itinerary_days or args.update_visits or args.visits_only or args.suggest_pois:
            print("error: Sample City pilot modes cannot be combined with legacy collection/location/itinerary/visit/POI modes", file=sys.stderr)
            return 2

    try:
        if args.dedupe or args.dedupe_apply:
            state_path = args.state_file or args.output.with_name("rebuild-state.json")
            if not state_path.exists():
                raise ValueError(f"dedupe mode requires an existing state file: {state_path}")
            if not args.adventurelog_base_url or not args.adventurelog_token:
                raise ValueError("dedupe mode requires ADVENTURELOG_BASE_URL and ADVENTURELOG_TOKEN")
            state = read_state(state_path)
            if not state:
                raise ValueError(f"dedupe mode requires an existing state file: {state_path}")
            client = AdventureLogClient(args.adventurelog_base_url, args.adventurelog_token, args.location_path, args.visit_path, args.image_path, args.collection_path)
            dedupe_state_locations(state, client, args.dedupe_report or args.output.with_name("dedupe-report.json"), args.dedupe_apply, args.dedupe_batch_size)
            return 0
        if args.input_json:
            with args.input_json.open(encoding="utf-8") as stream:
                loaded = json.load(stream)
            if (
                isinstance(loaded, dict)
                and loaded.get("schema") == "immich-adventure-manifest/v1"
                and isinstance(loaded.get("assets"), list)
                and isinstance(loaded.get("period"), dict)
            ):
                manifest = loaded
                if manifest["period"].get("start") != args.start.isoformat() or manifest["period"].get("end") != args.end.isoformat():
                    raise ValueError("--start/--end must match the supplied manifest period")
            else:
                raw_assets = loaded if isinstance(loaded, list) else response_assets(loaded)
                assets = [normalize_asset(item, args.base_url or "") for item in raw_assets if isinstance(item, dict)]
                manifest = {
                    "schema": "immich-adventure-manifest/v1",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "input-json",
                    "period": {"start": args.start.isoformat(), "end": args.end.isoformat()},
                    "summary": make_summary(assets),
                    "assets": assets,
                    "next_step": "Review this manifest before implementing any AdventureLog write integration.",
                }
        else:
            if not args.base_url or not args.api_key:
                raise ValueError("set IMMICH_BASE_URL and IMMICH_API_KEY (or pass --base-url and --api-key); use --input-json for offline testing")
            raw_assets = fetch_assets(ImmichClient(args.base_url, args.api_key, args.search_path), args.start, args.end, args.page_size)
            assets = [normalize_asset(item, args.base_url or "") for item in raw_assets if isinstance(item, dict)]
            manifest = {
                "schema": "immich-adventure-manifest/v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": args.base_url.rstrip("/") + args.search_path,
                "period": {"start": args.start.isoformat(), "end": args.end.isoformat()},
                "summary": make_summary(assets),
                "assets": assets,
                "next_step": "Review this manifest before implementing any AdventureLog write integration.",
            }
        if args.day_stop_pilot or args.isolated_day_stop_pilot:
            # POI calls are read-only, cache-backed, capped and paced.
            pilot_base = build_day_stop_pilot_plan(manifest, args.pilot_max_photos)
            cache_path = args.poi_cache or args.output.with_name("sample-pilot-poi-cache.json")
            if args.no_poi:
                suggestions, errors = {}, {}
            else:
                suggestions, errors = suggest_pois(
                    pilot_base, args.overpass_url, args.poi_radius, cache_path,
                    args.poi_max_requests, args.poi_min_interval, args.poi_timeout,
                )
            manifest = (build_isolated_day_stop_pilot_plan if args.isolated_day_stop_pilot else build_day_stop_pilot_plan)(manifest, args.pilot_max_photos, suggestions)
            manifest["poi_errors"] = errors
            manifest["poi_attribution"] = "© OpenStreetMap contributors"
            manifest["poi_lookup"] = {
                "enabled": not args.no_poi, "provider": "OpenStreetMap Overpass",
                "radius_m": args.poi_radius, "max_fresh_requests": args.poi_max_requests,
                "min_interval_seconds": args.poi_min_interval, "timeout_seconds": args.poi_timeout,
                "cache": str(cache_path),
            }
        elif args.collection_name:
            manifest = build_rebuild_plan(manifest, args.collection_name, args.min_location_assets)
            if args.suggest_pois:
                cache_path = args.poi_cache or args.output.with_name("poi-cache.json")
                manifest["poi_suggestions"], manifest["poi_errors"] = suggest_pois(
                    manifest, args.overpass_url, args.poi_radius, cache_path,
                    args.poi_max_requests, args.poi_min_interval, args.poi_timeout,
                )
                manifest["poi_attribution"] = "© OpenStreetMap contributors"
            if args.create_itinerary_days:
                state_path = args.state_file or args.output.with_name("rebuild-state.json")
                state_for_plan = read_state(state_path) if state_path.exists() else None
                manifest["itinerary_plan"] = build_itinerary_plan(manifest, state_for_plan)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {len(manifest['assets'])} asset(s) to {args.output}")
        print(json.dumps(manifest["summary"], indent=2))
        if args.apply:
            if not args.adventurelog_base_url or not args.adventurelog_token:
                raise ValueError("--apply requires ADVENTURELOG_BASE_URL and ADVENTURELOG_TOKEN (or corresponding arguments)")
            client = AdventureLogClient(args.adventurelog_base_url, args.adventurelog_token, args.location_path, args.visit_path, args.image_path, args.collection_path, args.itinerary_day_path, args.itinerary_path)
            if args.isolated_day_stop_pilot:
                if not manifest["groups"]:
                    raise ValueError("Sample City isolated pilot selected no timestamped photos; no remote objects will be created")
                state_path = args.state_file or args.output.with_name("sample-isolated-pilot-state.json")
                apply_isolated_day_stop_pilot_plan(manifest, client, args.content_type, state_path)
            elif args.day_stop_pilot:
                if not manifest["groups"]:
                    raise ValueError("Sample City pilot selected no timestamped photos; no remote objects will be created")
                state_path = args.state_file or args.output.with_name("sample-pilot-state.json")
                apply_day_stop_pilot_plan(manifest, client, args.content_type, state_path)
            elif args.create_itinerary_days:
                state_path = args.state_file or args.output.with_name("rebuild-state.json")
                state = read_state(state_path) if state_path.exists() else None
                if not state:
                    raise ValueError("--create-itinerary-days --apply requires an existing rebuild state file")
                apply_itinerary_plan(manifest, client, state)
            elif args.collection_name:
                state_path = args.state_file or args.output.with_name("rebuild-state.json")
                apply_rebuild_plan(manifest, client, args.timezone, args.content_type, state_path, args.batch_size, args.update_visits, args.group_timezone_policy, args.visits_only, args.parallelism)
            elif args.location_id:
                resume_manifest(manifest, client, args.location_id, args.content_type)
            else:
                apply_manifest(manifest, client, args.trip_name or infer_trip_name(manifest), args.timezone, args.content_type)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
