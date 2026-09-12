#!/usr/bin/env python3
"""Local, review-first UI for turning Immich photos into AdventureLog stops.

The server deliberately uses the importer module for Immich reads, clustering,
POI caching, and AdventureLog requests.  It has no third-party dependencies
and binds to loopback by default.  Plans are ordinary JSON files so a review
can be paused, inspected, copied, or resumed without another photo search.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

import immich_period_import as importer


UI_SCHEMA = "immich-adventure-review-plan/v1"
STATE_SCHEMA = "immich-adventure-ui-state/v1"
DEFAULT_ISOLATED_PREFIX = "Immich itinerary"
ProgressCallback = Callable[[str, int, int, str], None]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _asset_time(asset: dict[str, Any]) -> datetime | None:
    return importer._timestamp(asset.get("taken_at"))


def _local_day(asset: dict[str, Any], timezone_name: str) -> str | None:
    parsed = _asset_time(asset)
    if parsed:
        return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    return asset.get("day")


def _stop_key(day: str, asset_ids: list[str]) -> str:
    joined = "|".join(sorted(asset_ids))
    return f"review-{day}-" + hashlib.sha256(joined.encode()).hexdigest()[:12]


def _group_coordinates(assets: list[dict[str, Any]]) -> tuple[float, float] | None:
    return importer._centroid(assets)


def _metadata_summary(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a small, reviewable summary from the metadata we have loaded."""
    tags = sorted({tag for asset in assets for tag in (asset.get("tags") or []) if _clean(tag)})
    people = sorted({person for asset in assets for person in (asset.get("people") or []) if _clean(person)})
    descriptions = []
    for asset in assets:
        description = _clean(asset.get("description"))
        if description and description not in descriptions:
            descriptions.append(description)
    cameras = sorted({
        _clean(" ".join(str(value) for value in (asset.get("camera") or {}).values() if value))
        for asset in assets if _clean(" ".join(str(value) for value in (asset.get("camera") or {}).values() if value))
    })
    return {
        "loaded_count": sum(1 for asset in assets if asset.get("_metadata_loaded")),
        "tags": tags,
        "people": people,
        "descriptions": descriptions[:5],
        "cameras": cameras,
        "favorite_count": sum(1 for asset in assets if asset.get("is_favorite")),
    }


def _visit_notes(group: dict[str, Any]) -> str:
    summary = group.get("metadata_summary") or {}
    notes = [f"Day-specific stop from {group.get('asset_count', 0)} Immich photo(s)."]
    if summary.get("tags"):
        notes.append("Tags: " + ", ".join(summary["tags"][:20]) + ".")
    if summary.get("people"):
        notes.append("People: " + ", ".join(summary["people"][:20]) + ".")
    if summary.get("descriptions"):
        notes.append("Descriptions: " + " | ".join(summary["descriptions"][:3]) + ".")
    if summary.get("cameras"):
        notes.append("Camera: " + "; ".join(summary["cameras"][:3]) + ".")
    if summary.get("favorite_count"):
        notes.append(f"Favorites: {summary['favorite_count']}.")
    return _clean(" ".join(notes))[:2000]


def _make_group(day: str, number: int, assets: list[dict[str, Any]], collection_mode: str, collection_name: str, timezone_name: str, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    asset_ids = [str(a["id"]) for a in assets]
    coordinates = _group_coordinates(assets)
    timestamps = sorted(t for a in assets if (t := _asset_time(a)))
    point = assets[0].get("location") or {}
    city = _clean(point.get("city")) or "Nearby"
    key = _stop_key(day, asset_ids)
    group: dict[str, Any] = {
        "key": key,
        "proposal_key": f"stop-{number:02d}",
        "name": f"{city} — stop {number}",
        "city": city,
        "country": _clean(point.get("country")) or "Unknown",
        "asset_ids": asset_ids,
        "asset_count": len(asset_ids),
        "start_at": timestamps[0].astimezone(ZoneInfo(timezone_name)).isoformat() if timestamps else None,
        "end_at": timestamps[-1].astimezone(ZoneInfo(timezone_name)).isoformat() if timestamps else None,
        "coordinates": list(coordinates) if coordinates else None,
        "day": day,
        "poi_candidates": candidates or [],
        "metadata_summary": _metadata_summary(assets),
        "label": {"value": f"{city} — stop {number}", "confidence": "low", "source": "generic GPS/time fallback"},
    }
    if coordinates:
        group["openstreetmap_map_url"] = importer._osm_map_url(*coordinates)
    label, confidence, source = importer._stop_label(group, candidates or [])
    group["name"] = label
    group["label"] = {"value": label, "confidence": confidence, "source": source}
    if collection_mode == "isolated":
        collection_ref = f"$new_collection:{collection_name}"
    else:
        collection_ref = f"$existing_collection:{collection_name}"
    group["location"] = {"name": label, "collections": [collection_ref]}
    if coordinates:
        group["location"]["latitude"], group["location"]["longitude"] = coordinates
    group["visit"] = {
        "location": f"$new_location:{key}",
        "start_date": group["start_at"] or f"{day}T00:00:00+00:00",
        "end_date": group["end_at"] or f"{day}T23:59:59+00:00",
        "timezone": timezone_name,
        "notes": "",
    }
    group["visit"]["notes"] = _visit_notes(group)
    group["itinerary_item"] = {
        "collection": collection_ref, "content_type": "location",
        "object_id": f"$new_location:{key}", "date": day, "is_global": False,
        "order": number - 1,
    }
    return group


def _refresh_group(group: dict[str, Any], assets_by_id: dict[str, dict[str, Any]], timezone_name: str, collection_mode: str, collection_name: str) -> dict[str, Any]:
    assets = [assets_by_id[str(asset_id)] for asset_id in group.get("asset_ids", []) if str(asset_id) in assets_by_id]
    if not assets:
        raise ValueError("a stop must contain at least one selected photo")
    old_label = group.get("label") or {}
    key = group["key"]
    coordinates = _group_coordinates(assets)
    timestamps = sorted(t for a in assets if (t := _asset_time(a)))
    point = assets[0].get("location") or {}
    group["asset_ids"] = [str(a["id"]) for a in assets]
    group["asset_count"] = len(assets)
    group["metadata_summary"] = _metadata_summary(assets)
    group["city"] = _clean(point.get("city")) or "Nearby"
    group["country"] = _clean(point.get("country")) or "Unknown"
    group["coordinates"] = list(coordinates) if coordinates else None
    group["start_at"] = timestamps[0].astimezone(ZoneInfo(timezone_name)).isoformat() if timestamps else None
    group["end_at"] = timestamps[-1].astimezone(ZoneInfo(timezone_name)).isoformat() if timestamps else None
    group["label"] = old_label
    group["name"] = _clean(old_label.get("value")) or group.get("name") or f"{group['city']} — stop"
    collection_ref = f"$new_collection:{collection_name}" if collection_mode == "isolated" else f"$existing_collection:{collection_name}"
    group["location"] = {"name": group["name"], "collections": [collection_ref]}
    if coordinates:
        group["location"]["latitude"], group["location"]["longitude"] = coordinates
        group["openstreetmap_map_url"] = importer._osm_map_url(*coordinates)
    group["visit"] = {"location": f"$new_location:{key}", "start_date": group["start_at"] or f"{group.get('day', '')}T00:00:00+00:00", "end_date": group["end_at"] or f"{group.get('day', '')}T23:59:59+00:00", "timezone": timezone_name, "notes": _visit_notes(group)}
    group["itinerary_item"]["object_id"] = f"$new_location:{key}"
    return group


def build_review_plan(manifest: dict[str, Any], day: str, asset_cap: int = 30, timezone_name: str = "Europe/Rome", collection_mode: str = "isolated", collection_name: str | None = None, poi_suggestions: dict[str, list[dict[str, Any]]] | None = None, end_day: str | None = None) -> dict[str, Any]:
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("day must be YYYY-MM-DD") from exc
    end_day = end_day or day
    try:
        date.fromisoformat(end_day)
    except ValueError as exc:
        raise ValueError("end day must be YYYY-MM-DD") from exc
    if end_day < day:
        raise ValueError("end day must be on or after start day")
    if not 1 <= asset_cap <= 2000:
        raise ValueError("asset cap must be between 1 and 2000")
    if collection_mode not in ("isolated", "existing"):
        raise ValueError("collection mode must be isolated or existing")
    collection_name = collection_name or f"{DEFAULT_ISOLATED_PREFIX} — {day}"
    assets = manifest.get("assets", [])
    candidates = [a for a in assets if isinstance(a, dict) and a.get("id") and day <= (_local_day(a, timezone_name) or "") <= end_day]
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in candidates:
        asset_id = str(asset["id"])
        if asset_id not in seen:
            seen.add(asset_id); unique.append(asset)
    selected = [a for a in unique if _asset_time(a)][:asset_cap]
    missing_time = [str(a["id"]) for a in unique if not _asset_time(a)]
    groups: list[dict[str, Any]] = []
    suggestions = poi_suggestions or {}
    for current_day in sorted({_local_day(a, timezone_name) for a in selected if _local_day(a, timezone_name)}):
        day_assets = [a for a in selected if _local_day(a, timezone_name) == current_day]
        for index, cluster in enumerate(importer._cluster_pilot_assets(day_assets), 1):
            provisional = f"{_clean((cluster[0].get('location') or {}).get('city')) or 'Nearby'} — stop {index}"
            city = _clean((cluster[0].get("location") or {}).get("city")) or "Nearby"
            period_key = f"{city} — {importer._stop_period_label(cluster[0].get('taken_at'))}"
            groups.append(_make_group(current_day, index, cluster, collection_mode, collection_name, timezone_name, suggestions.get(provisional) or suggestions.get(period_key, [])))
    # Make generic labels unambiguous when POI service is unavailable.
    counts: dict[str, int] = {}
    for group in groups:
        if group["label"]["confidence"] == "low":
            counts[group["name"]] = counts.get(group["name"], 0) + 1
    indexes: dict[str, int] = {}
    for group in groups:
        if group["label"]["confidence"] == "low" and counts[group["name"]] > 1:
            indexes[group["name"]] = indexes.get(group["name"], 0) + 1
            group["name"] = f"{group['name']} {indexes[group['name']]}"
            group["label"]["value"] = group["name"]
            group["location"]["name"] = group["name"]
    plan = {
        "schema": UI_SCHEMA, "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": manifest.get("source", "local manifest"),
        "period": {"start": day, "end": end_day}, "day": day, "end_day": end_day, "days": sorted({_local_day(a, timezone_name) for a in selected if _local_day(a, timezone_name)}), "timezone": timezone_name,
        "collection": {"mode": collection_mode, "name": collection_name},
        "asset_cap": asset_cap, "source_asset_count": len(unique),
        "source_asset_ids": [str(a["id"]) for a in unique],
        "selected_asset_count": len(selected), "selected_asset_ids": [str(a["id"]) for a in selected],
        "excluded_missing_timestamp_ids": missing_time, "assets": selected, "groups": groups,
        "poi_errors": {}, "poi_lookup": {}, "remote": {"locations_complete": False, "itinerary_complete": False},
        "last_edit": None,
    }
    recompute_plan(plan)
    return plan


def recompute_plan(plan: dict[str, Any]) -> dict[str, Any]:
    assets_by_id = {str(a.get("id")): a for a in plan.get("assets", []) if a.get("id")}
    for index, group in enumerate(plan.get("groups", []), 1):
        group["proposal_key"] = f"stop-{index:02d}"
        _refresh_group(group, assets_by_id, plan.get("timezone", "Europe/Rome"), plan["collection"]["mode"], plan["collection"]["name"])
        group["itinerary_item"]["order"] = index - 1
    plan["selected_asset_ids"] = [str(asset_id) for group in plan.get("groups", []) for asset_id in group.get("asset_ids", [])]
    plan["selected_asset_count"] = len(plan["selected_asset_ids"])
    plan["last_edit"] = datetime.now(timezone.utc).isoformat()
    return plan


def edit_plan(plan: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if plan.get("remote", {}).get("locations_complete"):
        raise ValueError("editing is locked after pilot locations are created; start a new review")
    groups = plan.get("groups", [])
    by_key = {g["key"]: g for g in groups}
    if operation == "rename":
        group = by_key[payload["key"]]; name = _clean(payload.get("name"))
        if not name: raise ValueError("stop name cannot be empty")
        group["name"] = name; group["label"] = {"value": name, "confidence": "manual", "source": "user edit"}
    elif operation == "choose_poi":
        group = by_key[payload["key"]]; index = int(payload["index"]); candidate = group.get("poi_candidates", [])[index]
        group["name"] = _clean(candidate.get("name")) or group["name"]
        group["label"] = {"value": group["name"], "confidence": "high" if float(candidate.get("distance_m", 999)) <= 25 else "medium", "source": "OpenStreetMap/Overpass"}
    elif operation == "exclude_asset":
        group = by_key[payload["key"]]; asset_id = str(payload["asset_id"])
        group["asset_ids"] = [a for a in group.get("asset_ids", []) if str(a) != asset_id]
        if not group["asset_ids"]: groups.remove(group)
    elif operation == "exclude_stop":
        plan["groups"] = [g for g in groups if g["key"] != payload["key"]]
    elif operation == "reorder":
        order = [str(k) for k in payload.get("keys", [])]
        ordered = [by_key[k] for k in order if k in by_key]
        ordered += [g for g in groups if g["key"] not in order]
        plan["groups"] = ordered
    elif operation == "merge":
        keys = [str(k) for k in payload.get("keys", [])]
        selected = [by_key[k] for k in keys if k in by_key]
        if len(selected) < 2: raise ValueError("choose at least two stops to merge")
        assets_by_id = {str(a["id"]): a for a in plan["assets"]}
        ids: list[str] = []
        for group in selected:
            ids.extend(str(a) for a in group["asset_ids"] if str(a) not in ids)
        merged = dict(selected[0]); merged["key"] = _stop_key(str(selected[0].get("day") or plan["day"]), ids); merged["asset_ids"] = ids
        merged["label"] = {"value": f"{merged.get('city') or 'Nearby'} — merged stop", "confidence": "manual", "source": "user edit"}
        merged["name"] = merged["label"]["value"]
        plan["groups"] = [g for g in groups if g not in selected]
        insert_at = min(groups.index(g) for g in selected)
        plan["groups"].insert(min(insert_at, len(plan["groups"])), merged)
    elif operation == "split":
        group = by_key[payload["key"]]
        boundary = importer._timestamp(str(payload.get("boundary")))
        if not boundary: raise ValueError("split boundary must be an ISO timestamp")
        assets_by_id = {str(a["id"]): a for a in plan["assets"]}
        left, right = [], []
        for asset_id in group["asset_ids"]:
            asset = assets_by_id.get(str(asset_id)); taken = _asset_time(asset) if asset else None
            (left if taken and taken <= boundary else right).append(str(asset_id))
        if not left or not right: raise ValueError("split boundary must leave photos on both sides")
        index = groups.index(group); group_day = str(group.get("day") or plan["day"]); groups[index:index + 1] = [dict(group, key=_stop_key(group_day, left), asset_ids=left, label={"value": f"{group['city']} — split A", "confidence": "manual", "source": "user edit"}, name=f"{group['city']} — split A"), dict(group, key=_stop_key(group_day, right), asset_ids=right, label={"value": f"{group['city']} — split B", "confidence": "manual", "source": "user edit"}, name=f"{group['city']} — split B")]
    else:
        raise ValueError(f"unknown edit operation: {operation}")
    return recompute_plan(plan)


def _state_read(path: Path) -> dict[str, Any] | None:
    if not path.exists(): return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA: raise ValueError(f"unsupported UI state file: {path}")
    return value


def _state_write(path: Path, state: dict[str, Any]) -> None:
    importer.write_state(path, state)


def validate_plan(plan: dict[str, Any], client: importer.AdventureLogClient | None = None, state_path: Path | None = None) -> dict[str, Any]:
    assets = {str(a.get("id")): a for a in plan.get("assets", []) if a.get("id")}
    assigned: list[str] = [str(asset_id) for g in plan.get("groups", []) for asset_id in g.get("asset_ids", [])]
    duplicate_ids = sorted({x for x in assigned if assigned.count(x) > 1})
    missing_ids = sorted(set(assigned) - set(assets))
    empty = [g.get("key") for g in plan.get("groups", []) if not g.get("asset_ids")]
    errors = []
    if duplicate_ids: errors.append(f"photo assigned to multiple stops: {', '.join(duplicate_ids[:5])}")
    if missing_ids: errors.append(f"plan references missing photo IDs: {', '.join(missing_ids[:5])}")
    if empty: errors.append("a stop has no selected photos")
    state = _state_read(state_path) if state_path else None
    remote: dict[str, Any] = {"collections": [], "legacy_items": [], "state": state or {}}
    collection_found: bool | None = None
    if client:
        try:
            remote["collections"] = client.list_collections()
            collection_id = (state or {}).get("collection_id")
            if plan["collection"]["mode"] == "isolated" and collection_id:
                known_collection_ids = {str(item.get("id") or item.get("pk")) for item in remote["collections"]}
                collection_found = str(collection_id) in known_collection_ids
                if not collection_found:
                    errors.append(f"saved checkpoint collection {collection_id} was not found on AdventureLog; start a fresh checkpoint")
            if plan["collection"]["mode"] == "existing":
                remote["legacy_items"] = [item for item in client.list_itinerary_items() if item.get("date") in plan.get("days", [plan["day"]]) and str(item.get("collection")) == str(plan["collection"].get("id"))]
        except RuntimeError as exc:
            remote["error"] = str(exc)
    reusable_state = state if collection_found is not False else None
    collection_id = (reusable_state or {}).get("collection_id")
    locations = []
    visits = []
    images = []
    items = []
    for group in plan.get("groups", []):
        stop = (reusable_state or {}).get("stops", {}).get(group["key"], {})
        locations.append({"key": group["key"], "name": group["name"], "status": "reused" if stop.get("location_id") else "create", "id": stop.get("location_id")})
        visits.append({"location_key": group["key"], "status": "create" if not stop.get("visit_id") else "reused", "id": stop.get("visit_id")})
        already = set(str(x) for x in stop.get("attached_asset_ids", []))
        images.append({"location_key": group["key"], "count": len([x for x in group["asset_ids"] if str(x) not in already]), "status": "create"})
        items.append({"location_key": group["key"], "status": "create" if not stop.get("itinerary_item_id") else "reused", "id": stop.get("itinerary_item_id")})
    itinerary_days = [{"date": current_day, "status": "create" if current_day not in (reusable_state or {}).get("itinerary_days", {}) else "reused", "id": (reusable_state or {}).get("itinerary_days", {}).get(current_day)} for current_day in plan.get("days", [plan["day"]])]
    return {"ok": not errors, "errors": errors, "warnings": remote.get("legacy_items", []), "plan": {"collection": {"name": plan["collection"]["name"], "mode": plan["collection"]["mode"], "status": "reuse" if collection_id else "create", "id": collection_id}, "locations": locations, "visits": visits, "image_links": images, "itinerary_days": itinerary_days, "itinerary_items": items}, "remote": remote}


def _client(config: dict[str, Any], timeout: int = 180) -> importer.AdventureLogClient:
    if not config.get("adventurelog_base_url") or not config.get("adventurelog_token"):
        raise ValueError("AdventureLog is not configured; add ADVENTURELOG_BASE_URL and ADVENTURELOG_TOKEN to the local .env")
    return importer.AdventureLogClient(config["adventurelog_base_url"], config["adventurelog_token"], importer.DEFAULT_LOCATION_PATH, importer.DEFAULT_VISIT_PATH, importer.DEFAULT_IMAGE_PATH, importer.DEFAULT_COLLECTION_PATH, importer.DEFAULT_ITINERARY_DAY_PATH, importer.DEFAULT_ITINERARY_PATH, timeout=timeout)


def _emit_progress(progress: ProgressCallback | None, phase: str, current: int, total: int, message: str) -> None:
    if progress:
        progress(phase, current, max(total, 1), message)


def apply_ui_locations(plan: dict[str, Any], client: importer.AdventureLogClient, state_path: Path, progress: ProgressCallback | None = None, attachment_workers: int = 1) -> dict[str, Any]:
    if attachment_workers < 1:
        raise ValueError("attachment_workers must be positive")
    state = _state_read(state_path)
    mode = plan["collection"]["mode"]
    total = 1 + sum(2 + len(group.get("asset_ids", [])) for group in plan.get("groups", []))
    current = 0
    _emit_progress(progress, "locations", current, total, "Preparing the collection and day-specific objects")
    if state and str(state.get("plan_fingerprint")) != plan_fingerprint(plan):
        raise ValueError("the saved plan changed after remote writes; use the existing plan or start a new review")
    if state is None:
        if mode == "isolated":
            matches = [x for x in client.list_collections() if _clean(x.get("name")) == plan["collection"]["name"]]
            if len(matches) > 1:
                raise ValueError("more than one isolated target collection has this name; refusing to guess")
            if matches:
                collection_id = importer._object_id(matches[0], "UI collection")
            else:
                created = client.create_collection({"name": plan["collection"]["name"], "start_date": plan["period"]["start"], "end_date": plan["period"]["end"], "description": "Isolated collection created from the Immich review UI."})
                collection_id = importer._object_id(created, "UI collection")
        else:
            collection_id = str(plan["collection"].get("id") or "")
            if not collection_id: raise ValueError("existing target collection has no ID")
        state = {"schema": STATE_SCHEMA, "collection_id": collection_id, "collection_name": plan["collection"]["name"], "day": plan["day"], "days": plan.get("days", [plan["day"]]), "stops": {}, "itinerary_days": {}, "itinerary_day_id": None, "plan_fingerprint": plan_fingerprint(plan)}
        _state_write(state_path, state)
    current += 1
    _emit_progress(progress, "locations", current, total, f"Collection ready: {plan['collection']['name']}")
    if mode == "existing":
        known = {str(s.get("location_id")) for s in state.get("stops", {}).values() if s.get("location_id")}
        legacy = [x for x in client.list_itinerary_items() if str(x.get("collection")) == str(state["collection_id"]) and x.get("date") in plan.get("days", [plan["day"]]) and str(x.get("object_id")) not in known]
        if legacy:
            ids = ", ".join(sorted({str(x.get("object_id")) for x in legacy}))
            raise ValueError(f"target day already has non-review itinerary item(s): {ids}; no existing gallery was changed")
    state.setdefault("stops", {})
    by_id = {str(a["id"]): a for a in plan.get("assets", [])}
    pending: list[tuple[str, str, dict[str, Any], str]] = []
    for group in plan.get("groups", []):
        stop = state["stops"].get(group["key"])
        if stop:
            location_id = str(stop["location_id"])
            live = client.get_location(location_id)
            attached = importer.existing_immich_ids(live) | set(str(x) for x in stop.get("attached_asset_ids", []))
            location_message = f"Reusing Location for {group['name']}"
        else:
            payload = dict(group["location"]); payload["collections"] = [state["collection_id"]]
            location_id = importer._object_id(client.create_location(payload), "UI location")
            visit = dict(group["visit"]); visit["location"] = location_id
            visit_id = importer._object_id(client.create_visit(visit), "UI visit")
            stop = {"location_id": location_id, "visit_id": visit_id, "attached_asset_ids": [], "label": group["name"]}
            state["stops"][group["key"]] = stop; attached = set(); _state_write(state_path, state)
            location_message = f"Created Location and Visit for {group['name']}"
        current += 1
        _emit_progress(progress, "locations", current, total, location_message)
        if stop.get("visit_id"):
            current += 1
            _emit_progress(progress, "locations", current, total, f"Visit ready for {group['name']}")
        for asset_id in group["asset_ids"]:
            asset_id = str(asset_id)
            if asset_id not in by_id:
                current += 1
                _emit_progress(progress, "locations", current, total, f"Skipped missing photo {asset_id}")
                continue
            if asset_id in attached:
                current += 1
                _emit_progress(progress, "locations", current, total, f"Photo already linked: {asset_id}")
                continue
            pending.append((asset_id, location_id, stop, group["name"]))
    checkpoint_lock = threading.Lock()

    def attach_one(item: tuple[str, str, dict[str, Any], str]) -> tuple[str, str]:
        asset_id, location_id, stop, group_name = item
        client.attach_image(asset_id, location_id, plan.get("content_type", "location"))
        with checkpoint_lock:
            attached = set(str(value) for value in stop.get("attached_asset_ids", []))
            attached.add(asset_id)
            stop["attached_asset_ids"] = sorted(attached)
            _state_write(state_path, state)
        return asset_id, group_name

    attachment_errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=attachment_workers) as executor:
        futures = [executor.submit(attach_one, item) for item in pending]
        for future in as_completed(futures):
            try:
                asset_id, group_name = future.result()
            except Exception as exc:
                attachment_errors.append(exc)
            else:
                current += 1
                _emit_progress(progress, "locations", current, total, f"Linked photo {asset_id} to {group_name}")
    if attachment_errors:
        raise attachment_errors[0]
    state["locations_complete"] = True; _state_write(state_path, state)
    _emit_progress(progress, "locations", total, total, "Locations and photo links complete")
    return verification_report(plan, state)


def apply_ui_itinerary(plan: dict[str, Any], client: importer.AdventureLogClient, state_path: Path, progress: ProgressCallback | None = None) -> dict[str, Any]:
    state = _state_read(state_path)
    if not state or not state.get("locations_complete"): raise ValueError("create pilot locations first")
    state.setdefault("itinerary_items", {})
    state.setdefault("itinerary_days", {})
    collection_id = str(state["collection_id"])
    days = {(str(x.get("collection")), x.get("date")): x for x in client.list_itinerary_days()}
    total = len(plan.get("days", [plan["day"]])) + len(plan.get("groups", []))
    current = 0
    _emit_progress(progress, "itinerary", current, total, "Preparing the final itinerary")
    for day_number, current_day in enumerate(plan.get("days", [plan["day"]]), 1):
        day = days.get((collection_id, current_day))
        if day:
            state["itinerary_days"][current_day] = str(day.get("id") or day.get("pk") or "") or None
        elif current_day not in state["itinerary_days"]:
            payload = {"collection": collection_id, "date": current_day, "name": f"Day {day_number} — {current_day}", "description": "Created from the reviewed Immich day-stop plan."}
            state["itinerary_days"][current_day] = importer._object_id(client.create_itinerary_day(payload), "UI itinerary day")
        state["itinerary_day_id"] = state["itinerary_days"].get(plan["day"])
        _state_write(state_path, state)
        current += 1
        _emit_progress(progress, "itinerary", current, total, f"Itinerary day ready: {current_day}")
    existing = {(str(x.get("collection")), str(x.get("object_id")), x.get("date")): x for x in client.list_itinerary_items()}
    for order, group in enumerate(plan.get("groups", [])):
        stop = state["stops"].get(group["key"]); location_id = str(stop["location_id"])
        found = existing.get((collection_id, location_id, group.get("day", plan["day"])))
        if found:
            state["itinerary_items"][group["key"]] = str(found.get("id") or found.get("pk") or "") or None
        elif group["key"] not in state["itinerary_items"]:
            day_groups = [g for g in plan.get("groups", []) if g.get("day", plan["day"]) == group.get("day", plan["day"])]
            item = client.create_itinerary_item({"collection": collection_id, "content_type": "location", "object_id": location_id, "date": group.get("day", plan["day"]), "is_global": False, "order": day_groups.index(group)})
            state["itinerary_items"][group["key"]] = importer._object_id(item, "UI itinerary item")
        _state_write(state_path, state)
        current += 1
        _emit_progress(progress, "itinerary", current, total, f"Itinerary item ready: {group['name']}")
    state["itinerary_complete"] = True; _state_write(state_path, state)
    _emit_progress(progress, "itinerary", total, total, "Final itinerary complete")
    return verification_report(plan, state)


def plan_fingerprint(plan: dict[str, Any]) -> str:
    value = {"day": plan.get("day"), "collection": plan.get("collection"), "groups": [{"key": g.get("key"), "name": g.get("name"), "asset_ids": g.get("asset_ids")} for g in plan.get("groups", [])]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def verification_report(plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {"collection": {"id": state.get("collection_id"), "name": state.get("collection_name")}, "day": plan.get("period", {}).get("start"), "days": state.get("itinerary_days", {}), "itinerary_day_id": state.get("itinerary_day_id"), "locations": [{"id": s.get("location_id"), "visit_id": s.get("visit_id"), "label": s.get("label"), "day": g.get("day"), "photo_count": len(s.get("attached_asset_ids", [])), "itinerary_item_id": state.get("itinerary_items", {}).get(g["key"])} for g in plan.get("groups", []) if (s := state.get("stops", {}).get(g["key"]))], "checklist": ["Collection exists", "Each stop Location exists", "Each stop has a Visit", "Only selected Immich IDs are linked", "Final itinerary day/items are created or reused"]}


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Immich → AdventureLog review</title>
<style>
:root{--ink:#243044;--muted:#6d7789;--paper:#f7f8fb;--card:#fff;--line:#e5e8ef;--blue:#3569e8;--teal:#16877b;--warn:#b76a13;--danger:#c84d4d;--shadow:0 12px 35px #1c29400d}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}button,input,select{font:inherit}button{border:0;border-radius:8px;padding:9px 13px;background:#eef2fa;color:var(--ink);cursor:pointer}button.primary{background:var(--blue);color:#fff}button.confirm{background:var(--teal);color:#fff}button.danger{color:#9e3333;background:#fff0f0}.shell{max-width:1440px;margin:auto;padding:22px}.top{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:18px}.eyebrow{text-transform:uppercase;letter-spacing:.12em;color:var(--blue);font-size:11px;font-weight:700}.top h1{margin:3px 0 0;font-size:26px}.top p{margin:4px 0;color:var(--muted)}.service{font-size:12px;color:var(--muted);text-align:right}.service span{display:inline-block;margin-left:8px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:#c5cad4;margin-right:4px}.dot.ok{background:#35a17a}.dot.bad{background:#d66b62}.setup,.card{background:var(--card);border:1px solid var(--line);border-radius:13px;box-shadow:var(--shadow)}.setup{padding:14px;display:flex;flex-wrap:wrap;gap:12px;align-items:end}.field{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}.field input,.field select{min-width:150px;border:1px solid var(--line);border-radius:7px;padding:9px;background:#fff;color:var(--ink)}.field.wide select{min-width:290px}.steps{display:flex;gap:4px;margin:18px 0 12px}.step{padding:8px 12px;color:var(--muted);border-bottom:2px solid transparent}.step.active{color:var(--blue);border-color:var(--blue);font-weight:650}.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(350px,.8fr);gap:14px}.card{padding:16px}.card h2,.card h3{margin:0 0 8px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.toolbar .actions{display:flex;gap:7px;flex-wrap:wrap}.stats{display:flex;gap:18px;color:var(--muted);font-size:12px}.stats b{display:block;color:var(--ink);font-size:20px}.map{height:200px;border-radius:10px;background:linear-gradient(145deg,#edf4f4,#e9eef8);position:relative;overflow:hidden;margin-bottom:14px}.map:before,.map:after{content:"";position:absolute;background:#fff8;border:1px solid #d4deea;transform:rotate(-17deg);width:130%;height:32px;left:-10%;top:42%;}.map:after{transform:rotate(28deg);top:65%;height:20px}.pin{position:absolute;width:16px;height:16px;border-radius:50% 50% 50% 0;background:var(--blue);transform:rotate(-45deg);box-shadow:0 1px 5px #182d5577;z-index:1}.pin em{font-style:normal;display:block;transform:rotate(45deg);color:white;font-size:9px;text-align:center;padding-top:1px}.timeline{display:flex;flex-direction:column;gap:10px}.stop{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fff;cursor:pointer}.stop.selected{border:2px solid #8ca7ed;padding:11px}.stophead{display:flex;gap:10px;align-items:start}.stophead input{margin-top:4px}.stophead h3{font-size:16px;margin:0}.stopmeta{color:var(--muted);font-size:12px;margin:3px 0 8px}.badge{border-radius:20px;padding:3px 7px;font-size:11px;background:#fff0cc;color:#8d620b;white-space:nowrap}.badge.high{background:#dff5e9;color:#23724e}.badge.manual{background:#e9e4fb;color:#654ea1}.photos{display:flex;gap:5px;overflow:auto;padding-bottom:3px}.thumb{width:53px;height:53px;object-fit:cover;border-radius:6px;background:#e9edf4;flex:none}.photo{position:relative}.photo button{position:absolute;right:1px;top:1px;padding:0;width:17px;height:17px;border-radius:50%;background:#fff;color:#c54b4b;font-size:12px;line-height:17px}.inspect label{display:block;color:var(--muted);font-size:12px;margin:13px 0 4px}.inspect input,.inspect textarea{width:100%;padding:9px;border:1px solid var(--line);border-radius:7px}.inspect textarea{min-height:70px}.candidate{display:flex;justify-content:space-between;gap:8px;width:100%;margin:5px 0;text-align:left}.candidate small{color:var(--muted)}.notice{padding:10px;border-radius:8px;background:#fff7e2;color:#80550c;margin:10px 0;font-size:12px}.notice.error{background:#fff0f0;color:#9e3333}.diff{display:grid;grid-template-columns:1fr 1fr;gap:8px}.diff div{background:#f6f8fc;border:1px solid var(--line);padding:10px;border-radius:8px}.diff b{display:block;font-size:22px}.hidden{display:none!important}.modal{position:fixed;inset:0;background:#17213780;display:grid;place-items:center;padding:20px;z-index:4}.modalbox{background:#fff;border-radius:14px;padding:22px;max-width:430px;width:100%;box-shadow:0 20px 70px #07122d55}.modalbox h2{margin-top:0}.modalbox input{width:100%;padding:11px;border:1px solid var(--line);border-radius:7px}.modalactions{display:flex;justify-content:end;gap:8px;margin-top:15px}.verifyrow{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:7px;padding:9px 0;border-bottom:1px solid var(--line);font-size:12px}.muted{color:var(--muted)}@media(max-width:850px){.layout{grid-template-columns:1fr}.service{text-align:left}.top{display:block}.verifyrow{grid-template-columns:1fr 1fr}}
 </style><style>.metadata{margin-top:8px;padding:9px;border:1px solid var(--line);border-radius:8px;background:#f6f8fc;font-size:12px}.metadata div+div{margin-top:4px}.creation-progress{margin:12px 0;padding:12px;border:1px solid var(--line);border-radius:9px;background:#f6f8fc}.progress-track{height:10px;border-radius:10px;background:#e2e7f0;overflow:hidden}.progress-bar{height:100%;width:0;background:var(--blue);transition:width .25s ease}.progress-label{display:flex;justify-content:space-between;gap:10px;margin-top:7px;font-size:12px}</style></head><body><main class="shell"><header class="top"><div><div class="eyebrow">Local review workspace</div><h1>Immich <span class="muted">→</span> AdventureLog</h1><p>Shape day-specific photo stops before anything is written.</p></div><div class="service" id="service"></div></header>
<section class="setup"><label class="field"><span>Start date</span><input id="startDay" type="date"></label><label class="field"><span>End date</span><input id="endDay" type="date"></label><label class="field"><span>Total asset cap</span><input id="cap" type="number" min="1" max="2000" value="30"></label><label class="field"><span>New trip name</span><input id="tripName" placeholder="Immich itinerary — dates"></label><label class="field wide"><span>AdventureLog target</span><select id="collection"></select></label><button class="primary" id="load">Load photos</button><span id="progress" class="muted"></span></section>
<nav class="steps"><span class="step active" data-step="preview">1 Preview & edit</span><span class="step" data-step="validate">2 Review & create</span><span class="step" data-step="create">3 Create trip</span><span class="step" data-step="verify">4 Finished</span></nav>
<section id="preview" class="layout"><div class="card"><div class="toolbar"><div><h2>Proposed stops</h2><div class="stats" id="stats"></div></div><div class="actions"><button id="metadata">Load Immich metadata</button><button id="merge">Merge checked</button><button id="validate" class="primary">Validate plan</button></div></div><div class="map" id="map"></div><div id="timeline" class="timeline"><div class="muted">Choose a day and load photos to begin.</div></div></div><aside class="card inspect"><h2>Stop editor</h2><div id="inspector" class="muted">Select a stop to inspect and edit it.</div></aside></section>
<section id="validatePanel" class="card hidden"><div class="toolbar"><div><h2>Review & create</h2><p class="muted">Read-only preview of the objects that will be created or reused.</p></div><div class="actions"><button id="backEdit">Back to edit</button><button id="create" class="confirm">Create reviewed trip</button></div></div><div id="creationProgress" class="creation-progress hidden"><div class="progress-track"><div id="progressBar" class="progress-bar"></div></div><div class="progress-label"><span id="creationMessage">Preparing…</span><b id="progressPercent">0%</b></div></div><button id="resetCheckpoint" class="danger hidden" style="margin:0 0 12px">Start fresh checkpoint</button><div id="validation"></div></section>
<section id="verifyPanel" class="card hidden"><div class="toolbar"><div><h2>Finished</h2><p class="muted">AdventureLog objects and the final itinerary are complete.</p></div></div><div id="verification"></div></section>
</main><script>
let plan=null, selectedKey=null, checked=new Set(), validation=null;
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options={}){let r=await fetch(path,{headers:{'Content-Type':'application/json'},...options}), d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
function setStep(s){document.querySelectorAll('.step').forEach(x=>x.classList.toggle('active',x.dataset.step===s));$('preview').classList.toggle('hidden',s!=='preview');$('validatePanel').classList.toggle('hidden',s!=='validate');$('verifyPanel').classList.toggle('hidden',s!=='verify')}
function status(d){let thumbOk=d.thumbnails==='ready',planText=d.plan?'available':d.completed_plan?'last import complete; ready for a new review':'none';$('service').innerHTML=`<span><i class="dot ${d.immich?'ok':'bad'}"></i>Immich ${d.immich?'ready':'not configured'}</span><span><i class="dot ${thumbOk?'ok':'bad'}"></i>Images ${esc(d.thumbnails||'unknown')}</span><span><i class="dot ${d.adventurelog?'ok':'bad'}"></i>AdventureLog ${d.adventurelog?'ready':'not configured'}</span><span><i class="dot ${d.plan?'ok':'bad'}"></i>Saved plan ${esc(planText)}</span>`}
function render(){if(!plan)return;let total=plan.selected_asset_count||0,errors=plan.poi_errors||{},lookup=plan.poi_lookup||{};$('stats').innerHTML=`<span><b>${total}</b>selected photos</span><span><b>${plan.groups.length}</b>stops</span><span><b>${Object.keys(errors).length}</b>POI issues</span><span><b>${esc(plan.period.start)} → ${esc(plan.period.end)}</b>review range</span>`;let poiNotice=Object.keys(errors).length?`POI lookup has ${Object.keys(errors).length} issue(s): ${Object.entries(errors).map(([k,v])=>`${esc(k)} — ${esc(v)}`).join(' · ')}`:`POIs are loaded on demand per stop; results are cached at ${esc(lookup.cache||'the configured cache')}.`;$('timeline').innerHTML=`<div class="notice">${poiNotice}</div>`+(plan.groups.length?plan.groups.map((g,i)=>`<article class="stop ${selectedKey===g.key?'selected':''}" draggable="true" data-key="${esc(g.key)}"><div class="stophead"><input type="checkbox" class="check" data-key="${esc(g.key)}" ${checked.has(g.key)?'checked':''}><div style="flex:1"><h3>${esc(g.name)}</h3><div class="stopmeta">${esc(g.day)} · ${g.asset_count} photos · ${esc(fmt(g.start_at))} → ${esc(fmt(g.end_at))} · ${g.coordinates?g.coordinates.map(x=>Number(x).toFixed(5)).join(', '):'no GPS'}</div></div><span class="badge ${g.label.confidence}">${esc(g.label.confidence)}</span></div><div class="photos">${g.asset_ids.map(id=>photo(id,g)).join('')}</div><div class="muted" style="font-size:11px;margin-top:6px">${esc(g.label.source)} · drag to reorder, drop onto another stop to merge ${g.openstreetmap_map_url?`· <a href="${esc(g.openstreetmap_map_url)}" target="_blank" rel="noreferrer">map</a>`:''}</div></article>`).join(''):'<div class="muted">No stops remain. Load another date range or adjust the review.</div>');
document.querySelectorAll('.stop').forEach(x=>{x.onclick=e=>{if(e.target.closest('button,input,a'))return;selectedKey=x.dataset.key;render();renderInspector()};x.ondragstart=e=>{draggedKey=x.dataset.key;e.dataTransfer.effectAllowed='move'};x.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='move'};x.ondrop=e=>{e.preventDefault();let target=x.dataset.key;if(!draggedKey||draggedKey===target)return;let from=plan.groups.findIndex(g=>g.key===draggedKey),to=plan.groups.findIndex(g=>g.key===target);if(e.shiftKey||window.confirm(`Merge ${draggedKey} into ${target}?`)){saveEdit('merge',{keys:[draggedKey,target]})}else{let keys=plan.groups.map(g=>g.key);keys.splice(to,0,keys.splice(from,1)[0]);saveEdit('reorder',{keys})}draggedKey=null}});document.querySelectorAll('.check').forEach(x=>x.onchange=()=>{x.checked?checked.add(x.dataset.key):checked.delete(x.dataset.key)});renderMap()}
function photo(id,g){let a=plan.assets.find(x=>String(x.id)===String(id));return `<span class="photo"><img class="thumb" src="/media/thumbnail/${encodeURIComponent(id)}" loading="lazy" title="${esc(a?.filename||id)}" onerror="imageFailed(this)"><button title="Exclude photo" onclick="excludePhoto('${esc(g.key)}','${esc(id)}')">×</button></span>`}
function imageFailed(img){let n=document.createElement('span');n.style.cssText='display:inline-flex;width:53px;height:53px;border-radius:6px;background:#fff0f0;color:#a54343;align-items:center;justify-content:center;text-align:center;font-size:9px;line-height:1.1;padding:4px;flex:none';n.title='Immich thumbnail unavailable; the API key needs asset.view permission';n.textContent='thumbnail unavailable';img.replaceWith(n)}
function fmt(x){return x?new Date(x).toLocaleString([], {dateStyle:'short',timeStyle:'short'}):'unknown time'}
function renderMap(){let points=plan.groups.map(g=>g.coordinates).filter(Boolean);let minLat=Math.min(...points.map(x=>x[0])),maxLat=Math.max(...points.map(x=>x[0])),minLon=Math.min(...points.map(x=>x[1])),maxLon=Math.max(...points.map(x=>x[1]));let spanLat=maxLat-minLat||.01,spanLon=maxLon-minLon||.01;$('map').innerHTML=points.length?plan.groups.map((g,i)=>{if(!g.coordinates)return '';let left=8+(g.coordinates[1]-minLon)/spanLon*84,top=82-(g.coordinates[0]-minLat)/spanLat*72;return `<a class="pin" style="left:${left}%;top:${top}%" title="${esc(g.name)}" href="${esc(g.openstreetmap_map_url||'#')}" target="_blank"><em>${i+1}</em></a>`}).join(''):'<div style="padding:75px;text-align:center;color:#6d7789">No GPS coordinates in this selection</div>'}
function renderInspector(){let g=plan?.groups.find(x=>x.key===selectedKey);if(!g){$('inspector').innerHTML='<span class="muted">Select a stop to inspect and edit it.</span>';return}let c=(g.poi_candidates||[]).map((x,i)=>`<button class="candidate" onclick="choosePoi(${i})"><span>${esc(x.name)}<br><small>${esc(x.category||'POI')} · ${x.distance_m} m</small></span><span>Use</span></button>`).join('');let poiError=(plan.poi_errors||{})[g.key],meta=g.metadata_summary||{},metaError=(g.asset_ids||[]).map(id=>(plan.metadata_errors||{})[id]).filter(Boolean)[0];let metaRows=[meta.tags?.length?`<div><b>Tags:</b> ${esc(meta.tags.join(', '))}</div>`:'',meta.people?.length?`<div><b>People:</b> ${esc(meta.people.join(', '))}</div>`:'',meta.descriptions?.length?`<div><b>Descriptions:</b> ${esc(meta.descriptions.join(' | '))}</div>`:'',meta.cameras?.length?`<div><b>Camera:</b> ${esc(meta.cameras.join('; '))}</div>`:'',meta.favorite_count?`<div><b>Favorites:</b> ${meta.favorite_count}</div>`:''].filter(Boolean).join('');$('inspector').innerHTML=`<label>Stop name</label><input id="name" value="${esc(g.name)}"><button class="primary" style="margin-top:7px" onclick="renameStop()">Save name</button><label>Photo metadata</label><button class="primary" onclick="enrichMetadata()">${meta.loaded_count===g.asset_count?'Refresh':'Load'} tags, people, descriptions &amp; camera data (${g.asset_count} photos)</button>${metaRows?`<div class="metadata">${metaRows}</div>`:'<div class="muted" style="margin-top:7px">Metadata has not been loaded for this stop.</div>'}${metaError?`<div class="notice error">${esc(metaError)}</div>`:''}<label>Nearby alternatives</label><button class="primary" onclick="findPoi()">${g.poi_candidates?.length?'Refresh':'Find'} nearby POIs for this stop</button>${poiError?`<div class="notice error">${esc(poiError)}</div>`:''}${c||'<div class="muted" style="margin-top:7px">No POI search has been run for this stop.</div>'}<label>Split by time boundary</label><input id="boundary" type="datetime-local"><button style="margin-top:7px" onclick="splitStop()">Split stop</button><label>Stop order</label><div><button onclick="moveStop(-1)">Move earlier</button> <button onclick="moveStop(1)">Move later</button> <button class="danger" onclick="excludeStop()">Exclude whole stop</button></div><div class="notice">Edits are saved locally after each action. Remote writes stay locked until you click Create reviewed trip.</div>`}
async function saveEdit(operation,payload){try{let d=await api('/api/edit',{method:'POST',body:JSON.stringify({operation,payload})});plan=d.plan;render();renderInspector()}catch(e){alert(e.message)}}
function renameStop(){saveEdit('rename',{key:selectedKey,name:$('name').value})}function choosePoi(i){saveEdit('choose_poi',{key:selectedKey,index:i})}function excludePhoto(k,id){event.stopPropagation();saveEdit('exclude_asset',{key:k,asset_id:id})}function excludeStop(){saveEdit('exclude_stop',{key:selectedKey});selectedKey=null}function splitStop(){let v=$('boundary').value;if(v)saveEdit('split',{key:selectedKey,boundary:new Date(v).toISOString()})}function moveStop(dir){let i=plan.groups.findIndex(g=>g.key===selectedKey), keys=plan.groups.map(g=>g.key),j=i+dir;if(j>=0&&j<keys.length){[keys[i],keys[j]]=[keys[j],keys[i]];saveEdit('reorder',{keys})}}
function merge(){if(checked.size<2)return alert('Check at least two stops first.');saveEdit('merge',{keys:[...checked]});checked.clear()}
let draggedKey=null;
async function load(){try{$('progress').textContent='Preparing preview…';let d=await api('/api/preview',{method:'POST',body:JSON.stringify({start_day:$('startDay').value,end_day:$('endDay').value,asset_cap:Number($('cap').value),trip_name:$('tripName').value,collection:$('collection').value})});if(d.job_id){let id=d.job_id;while(true){await new Promise(r=>setTimeout(r,500));let job=await api('/api/jobs/'+id);$('progress').textContent=job.message||job.status;if(job.status==='complete'){d={plan:job.plan};break}if(job.status==='error')throw Error(job.message)}}plan=d.plan;$('progress').textContent='';selectedKey=plan.groups[0]?.key||null;render();renderInspector();setStep('preview')}catch(e){$('progress').textContent='';alert(e.message)}}
async function findPoi(){try{$('progress').textContent='Looking up POIs for this stop…';let d=await api('/api/poi',{method:'POST',body:JSON.stringify({key:selectedKey})});if(d.job_id){let id=d.job_id;while(true){await new Promise(r=>setTimeout(r,500));let job=await api('/api/jobs/'+id);$('progress').textContent=job.message||job.status;if(job.status==='complete'){d={plan:job.plan};break}if(job.status==='error')throw Error(job.message)}}plan=d.plan;$('progress').textContent='';render();renderInspector()}catch(e){$('progress').textContent='';alert(e.message)}}
async function enrichMetadata(){try{$('progress').textContent='Loading photo metadata…';let d=await api('/api/enrich',{method:'POST',body:JSON.stringify({key:selectedKey})});if(d.job_id){let id=d.job_id;while(true){await new Promise(r=>setTimeout(r,500));let job=await api('/api/jobs/'+id);$('progress').textContent=job.message||job.status;if(job.status==='complete'){d={plan:job.plan};break}if(job.status==='error')throw Error(job.message)}}plan=d.plan;$('progress').textContent='';render();renderInspector()}catch(e){$('progress').textContent='';alert(e.message)}}
async function loadAllMetadata(){try{$('progress').textContent='Loading Immich metadata for selected photos…';$('metadata').disabled=true;let d=await api('/api/enrich-all',{method:'POST',body:JSON.stringify({})});if(d.job_id){let id=d.job_id;while(true){await new Promise(r=>setTimeout(r,500));let job=await api('/api/jobs/'+id);$('progress').textContent=job.message||job.status;if(job.status==='complete'){d={plan:job.plan};break}if(job.status==='error')throw Error(job.message)}}plan=d.plan;$('progress').textContent='';render();renderInspector()}catch(e){$('progress').textContent='';alert(e.message)}finally{$('metadata').disabled=false}}
async function validate(){try{validation=await api('/api/validate',{method:'POST'});$('validation').innerHTML=`${validation.errors.length?`<div class="notice error">${validation.errors.map(esc).join('<br>')}</div>`:''}${validation.warnings.length?'<div class="notice">Existing itinerary items are shown as a warning; the UI will not remove or unlink them.</div>':''}<div class="diff">${diff('Collection',validation.plan.collection)}${diff('Locations',validation.plan.locations)}${diff('Visits',validation.plan.visits)}${diff('Image links',validation.plan.image_links)}${diff('Itinerary days',validation.plan.itinerary_days)}${diff('Itinerary items',validation.plan.itinerary_items)}</div><pre style="white-space:pre-wrap;font-size:11px;background:#f6f8fc;padding:10px;border-radius:8px;margin-top:12px">${esc(JSON.stringify(validation.plan,null,2))}</pre>`;let stale=(validation.errors||[]).some(e=>String(e).includes('checkpoint collection'));$('resetCheckpoint').classList.toggle('hidden',!stale);setStep('validate')}catch(e){alert(e.message)}}
function diff(title,v){let n=Array.isArray(v)?v.filter(x=>x.status==='create').length:(v.status==='create'?1:0);return `<div><span>${esc(title)}</span><b>${n}</b><small class="muted">new object(s); reuse is shown in details</small></div>`}
function updateCreationProgress(job){let percent=Math.max(0,Math.min(100,Number(job.progress||0)));$('progressBar').style.width=percent+'%';$('progressPercent').textContent=percent+'%';$('creationMessage').textContent=job.message||job.status}
async function startCreation(){if(!validation?.ok){let recover=(validation?.errors||[]).some(e=>String(e).includes('checkpoint collection'));if(recover){$('creationProgress').classList.remove('hidden');$('resetCheckpoint').classList.remove('hidden');$('creationMessage').style.color='var(--danger)';$('creationMessage').textContent='The saved checkpoint is unavailable. You can preserve it and start a fresh local checkpoint.'}return alert('Resolve validation errors first.')}$('create').disabled=true;$('backEdit').disabled=true;$('creationProgress').classList.remove('hidden');$('resetCheckpoint').classList.add('hidden');$('creationMessage').style.color='';updateCreationProgress({progress:0,message:'Starting reviewed trip creation'});try{let d=await api('/api/create-trip',{method:'POST',body:JSON.stringify({})});let id=d.job_id;while(true){await new Promise(r=>setTimeout(r,450));let job=await api('/api/jobs/'+id);updateCreationProgress(job);if(job.status==='complete'){d={report:job.report};break}if(job.status==='error')throw Error(job.message)}$('verification').innerHTML=verification(d.report);setStep('verify')}catch(e){$('create').disabled=false;$('backEdit').disabled=false;$('creationMessage').style.color='var(--danger)';$('creationMessage').textContent='Creation stopped: '+e.message;if(String(e.message).includes('checkpoint')||String(e.message).includes('collection'))$('resetCheckpoint').classList.remove('hidden');alert(e.message)}}
async function resetCheckpoint(){try{await api('/api/reset-checkpoint',{method:'POST',body:JSON.stringify({})});$('resetCheckpoint').classList.add('hidden');$('creationMessage').style.color='';$('creationMessage').textContent='Old checkpoint backed up. Rechecking the current plan…';await validate()}catch(e){alert(e.message)}}
function verification(r){let days=Object.entries(r.days||{}).map(([day,id])=>`${esc(day)} (${esc(id||'reused')})`).join(' · ');return `<div class="notice">Remote writes completed idempotently. Save this page’s IDs with the plan.</div><p><b>Collection:</b> ${esc(r.collection.name)} · <b>ID:</b> ${esc(r.collection.id)}<br><b>Itinerary days:</b> ${days||'not created yet'}</p>${r.locations.map(x=>`<div class="verifyrow"><span>${esc(x.day)} · ${esc(x.label)}</span><span>Location ${esc(x.id)}</span><span>Visit ${esc(x.visit_id)}</span><span>${x.photo_count} photos · item ${esc(x.itinerary_item_id||'pending')}</span></div>`).join('')}<h3>Checklist</h3><ul>${r.checklist.map(x=>`<li>✓ ${esc(x)}</li>`).join('')}</ul>${r.adventurelog_url?`<p><a href="${esc(r.adventurelog_url)}" target="_blank" rel="noreferrer">Open AdventureLog</a></p>`:''}`}
async function init(){try{let d=await api('/api/status');status(d);$('startDay').value=d.default_day;$('endDay').value=d.default_day;for(let c of d.collections){let o=document.createElement('option');o.value=c.mode==='isolated'?'isolated':c.id;o.textContent=c.name+(c.mode==='isolated'?' (new, recommended)':'');o.dataset.name=c.name;o.dataset.mode=c.mode;$('collection').appendChild(o)}if(d.plan){let saved=await api('/api/plan');if(saved.plan){plan=saved.plan;let period=plan.period||{};$('startDay').value=period.start||d.default_day;$('endDay').value=period.end||period.start||d.default_day;$('tripName').value=plan.collection?.mode==='isolated'?plan.collection.name:'';render();renderInspector()}}else{$('tripName').value='';$('cap').value='30';setStep('preview')}}catch(e){$('service').textContent=e.message}}
$('load').onclick=load;$('metadata').onclick=loadAllMetadata;$('merge').onclick=merge;$('validate').onclick=validate;$('backEdit').onclick=()=>setStep('preview');$('create').onclick=startCreation;$('resetCheckpoint').onclick=resetCheckpoint;document.querySelectorAll('.step').forEach(x=>x.onclick=()=>x.dataset.step==='preview'&&setStep('preview'));init();
</script></body></html>'''


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        importer.load_local_dotenv()
        self.manifest_path = args.manifest
        self.plan_path = args.plan
        self.state_path = args.state
        if getattr(args, "metadata_workers", 6) < 1 or getattr(args, "attachment_workers", 4) < 1:
            raise ValueError("metadata-workers and attachment-workers must be positive")
        if getattr(args, "immich_timeout", 60) < 1 or getattr(args, "adventurelog_timeout", 180) < 1:
            raise ValueError("immich-timeout and adventurelog-timeout must be positive")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="immich-review")
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists(): return {"assets": [], "source": "Immich API"}
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("assets"), list): raise ValueError(f"manifest must contain an assets list: {self.manifest_path}")
        return value

    def config(self) -> dict[str, Any]:
        return {"immich_base_url": os.environ.get("IMMICH_BASE_URL"), "immich_api_key": os.environ.get("IMMICH_API_KEY"), "adventurelog_base_url": os.environ.get("ADVENTURELOG_BASE_URL"), "adventurelog_token": os.environ.get("ADVENTURELOG_TOKEN")}

    def read_plan(self) -> dict[str, Any] | None:
        if not self.plan_path.exists(): return None
        value = json.loads(self.plan_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("schema") == UI_SCHEMA else None

    def save_plan(self, plan: dict[str, Any]) -> None:
        importer.write_state(self.plan_path, plan)

    def reset_checkpoint(self) -> str | None:
        """Move the incompatible local checkpoint aside for a fresh review."""
        if not self.state_path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.state_path.with_name(f"{self.state_path.name}.stale-{stamp}-{uuid.uuid4().hex[:6]}")
        self.state_path.replace(backup)
        return str(backup)

    def collections(self) -> tuple[list[dict[str, Any]], str | None]:
        values = [{"id": "isolated", "name": f"{DEFAULT_ISOLATED_PREFIX} — choose a day", "mode": "isolated"}]
        config = self.config()
        if not config["adventurelog_base_url"] or not config["adventurelog_token"]: return values, None
        try:
            client = _client(config, getattr(self.args, "adventurelog_timeout", 180))
            existing = [{"id": str(x.get("id") or x.get("pk")), "name": _clean(x.get("name")), "mode": "existing"} for x in client.list_collections() if x.get("id") or x.get("pk")]
            values += existing
            return values, None
        except RuntimeError as exc: return values, str(exc)

    def thumbnail_status(self) -> str:
        config = self.config()
        if not config["immich_base_url"] or not config["immich_api_key"]:
            return "not configured"
        plan = self.read_plan() or {}
        asset = next((x for x in plan.get("assets", []) if x.get("id")), None) or next((x for x in self.manifest.get("assets", []) if x.get("id")), None)
        if not asset:
            return "no asset to check"
        url = config["immich_base_url"].rstrip("/") + "/api/assets/" + quote(str(asset["id"]), safe="") + "/thumbnail"
        try:
            request = urllib.request.Request(url, headers={"x-api-key": config["immich_api_key"], "Accept": "image/*"})
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read(1)
            return "ready"
        except urllib.error.HTTPError as exc:
            try:
                decoded = json.loads(exc.read(300).decode("utf-8", "replace"))
                detail = decoded.get("message", "") if isinstance(decoded, dict) else ""
            except (ValueError, json.JSONDecodeError):
                detail = ""
            if "asset.view" in detail:
                return "missing asset.view permission"
            return f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError):
            return "unreachable"

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        day = str(body.get("start_day") or body.get("day") or "")
        end_day = str(body.get("end_day") or day)
        asset_cap = int(body.get("asset_cap", 30))
        if not day: raise ValueError("choose a photo day")
        manifest = self.manifest
        if not manifest.get("assets"):
            config = self.config()
            if not config["immich_base_url"] or not config["immich_api_key"]: raise ValueError("no manifest assets and Immich is not configured")
            # Search a UTC safety band, then apply the Europe/Rome local-day
            # filter below. This includes photos taken around midnight local
            # time instead of accidentally dropping them at a UTC boundary.
            raw = importer.fetch_assets(importer.ImmichClient(config["immich_base_url"], config["immich_api_key"], importer.DEFAULT_SEARCH_PATH, timeout=getattr(self.args, "immich_timeout", 60)), date.fromisoformat(day) - timedelta(days=1), date.fromisoformat(end_day) + timedelta(days=1), 1000)
            assets = [importer.normalize_asset(x, config["immich_base_url"]) for x in raw]
            manifest = {"source": "Immich API", "assets": assets}
        choice = str(body.get("collection") or "isolated")
        mode = "isolated" if choice == "isolated" else "existing"
        collections, _ = self.collections()
        selected = next((x for x in collections if x["id"] == choice), None)
        requested_name = _clean(body.get("trip_name"))
        name = selected["name"] if selected and mode == "existing" else requested_name or f"{DEFAULT_ISOLATED_PREFIX} — {day}"
        base = build_review_plan(manifest, day, asset_cap, collection_mode=mode, collection_name=name, end_day=end_day)
        cache = self.args.poi_cache or self.plan_path.with_name("ui-poi-cache.json")
        if mode == "existing":
            base["collection"]["id"] = choice
        base["poi_errors"] = {}
        base["poi_lookup"] = {"enabled": False, "mode": "on demand per stop", "provider": "OpenStreetMap Overpass", "cache": str(cache), "max_fresh_requests": 1, "min_interval_seconds": self.args.poi_min_interval}
        self.save_plan(base)
        return base

    def preview_needs_background(self, body: dict[str, Any]) -> bool:
        if not self.manifest.get("assets"):
            return True
        start = str(body.get("start_day") or body.get("day") or "")
        end = str(body.get("end_day") or start)
        try:
            count = sum(1 for asset in self.manifest["assets"] if isinstance(asset, dict) and start <= (_local_day(asset, "Europe/Rome") or "") <= end)
            return count > 100
        except (TypeError, ValueError):
            return False

    def start_preview_job(self, body: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {"status": "queued", "message": "Waiting for a background worker"}
        def work() -> None:
            with self.jobs_lock:
                self.jobs[job_id] = {"status": "running", "message": "Loading photos, clustering stops, and preparing the review plan"}
            try:
                result = self.preview(body)
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "complete", "message": "Review plan ready", "plan": result}
            except Exception as exc:
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "error", "message": str(exc)}
        self.executor.submit(work)
        return job_id

    def poi(self, body: dict[str, Any]) -> dict[str, Any]:
        """Look up one reviewed stop only; the initial preview never calls Overpass."""
        plan = self.read_plan()
        if not plan:
            raise ValueError("no saved plan; load photos first")
        key = str(body.get("key") or "")
        group = next((g for g in plan.get("groups", []) if g.get("key") == key), None)
        if not group:
            raise ValueError("unknown stop")
        if not group.get("coordinates"):
            group["poi_candidates"] = []
            plan.setdefault("poi_errors", {})[key] = "this stop has no GPS coordinates"
            self.save_plan(plan)
            return plan
        cache = self.args.poi_cache or self.plan_path.with_name("ui-poi-cache.json")
        suggestions, errors = importer.suggest_pois(
            {"groups": [{"name": group["name"], "coordinates": group["coordinates"]}]},
            self.args.overpass_url, self.args.poi_radius, cache,
            max_requests=1, min_interval_seconds=self.args.poi_min_interval, request_timeout=self.args.poi_timeout,
        )
        group["poi_candidates"] = suggestions.get(group["name"], [])
        plan.setdefault("poi_errors", {}).pop(key, None)
        if errors:
            plan.setdefault("poi_errors", {})[key] = errors.get(group["name"], "POI lookup failed")
        plan.setdefault("poi_lookup", {}).update({"enabled": True, "mode": "on demand", "provider": "OpenStreetMap Overpass", "cache": str(cache), "max_fresh_requests": 1, "min_interval_seconds": self.args.poi_min_interval})
        self.save_plan(plan)
        return plan

    def start_poi_job(self, body: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {"status": "queued", "message": "Waiting for the POI lookup"}
        def work() -> None:
            with self.jobs_lock:
                self.jobs[job_id] = {"status": "running", "message": "Querying the cached/capped OpenStreetMap service for this stop"}
            try:
                result = self.poi(body)
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "complete", "message": "POI candidates ready", "plan": result}
            except Exception as exc:
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "error", "message": str(exc)}
        self.executor.submit(work)
        return job_id

    def _enrich_asset_ids(self, plan: dict[str, Any], asset_ids: list[str], metadata_workers: int = 1, force_refresh: bool = False) -> dict[str, str]:
        if metadata_workers < 1:
            raise ValueError("metadata_workers must be positive")
        config = self.config()
        if not config["immich_base_url"] or not config["immich_api_key"]:
            raise ValueError("Immich is not configured; add IMMICH_BASE_URL and IMMICH_API_KEY to the local .env")
        client = importer.ImmichClient(config["immich_base_url"], config["immich_api_key"], importer.DEFAULT_SEARCH_PATH, timeout=getattr(self.args, "immich_timeout", 60))
        assets_by_id = {str(asset.get("id")): asset for asset in plan.get("assets", []) if asset.get("id")}
        errors: dict[str, str] = {}
        unique_ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids))

        def load_one(asset_id: str) -> tuple[str, dict[str, Any] | None, str | None]:
            asset = assets_by_id.get(str(asset_id))
            if not asset:
                return asset_id, None, None
            if asset.get("_metadata_loaded") and not force_refresh:
                return asset_id, None, None
            try:
                detail = importer._asset_detail(client, str(asset_id))
                return asset_id, importer.normalize_asset(detail, config["immich_base_url"]), None
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                return asset_id, None, str(exc)
        with ThreadPoolExecutor(max_workers=metadata_workers) as executor:
            results = executor.map(load_one, unique_ids)
            for asset_id, normalized, error in results:
                if error:
                    errors[asset_id] = error
                    continue
                asset = assets_by_id.get(asset_id)
                if asset is None or normalized is None:
                    continue
                for field, value in normalized.items():
                    if value is not None or field in ("tags", "people", "camera"):
                        asset[field] = value
                asset["_metadata_loaded"] = True
        return errors

    def _save_enriched(self, plan: dict[str, Any], asset_ids: list[str], metadata_workers: int = 1, force_refresh: bool = False) -> dict[str, Any]:
        errors = self._enrich_asset_ids(plan, asset_ids, metadata_workers, force_refresh)
        plan.setdefault("metadata_errors", {})
        if errors:
            plan["metadata_errors"].update(errors)
        else:
            for asset_id in asset_ids:
                plan["metadata_errors"].pop(str(asset_id), None)
        recompute_plan(plan)
        self.save_plan(plan)
        return plan

    def enrich(self, body: dict[str, Any]) -> dict[str, Any]:
        """Load full Immich metadata for one reviewed stop and persist it."""
        plan = self.read_plan()
        if not plan:
            raise ValueError("no saved plan; load photos first")
        if plan.get("remote", {}).get("locations_complete"):
            raise ValueError("metadata editing is locked after pilot locations are created; start a new review")
        key = str(body.get("key") or "")
        group = next((g for g in plan.get("groups", []) if g.get("key") == key), None)
        if not group:
            raise ValueError("unknown stop")
        return self._save_enriched(plan, [str(asset_id) for asset_id in group.get("asset_ids", [])], getattr(self.args, "metadata_workers", 6), bool(body.get("refresh")))

    def enrich_all(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load full Immich metadata for every selected photo in the plan."""
        plan = self.read_plan()
        if not plan:
            raise ValueError("no saved plan; load photos first")
        if plan.get("remote", {}).get("locations_complete"):
            raise ValueError("metadata editing is locked after pilot locations are created; start a new review")
        asset_ids = [str(asset_id) for group in plan.get("groups", []) for asset_id in group.get("asset_ids", [])]
        return self._save_enriched(plan, asset_ids, getattr(self.args, "metadata_workers", 6), bool((body or {}).get("refresh")))

    def start_enrich_job(self, body: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {"status": "queued", "message": "Waiting to load photo metadata"}
        def work() -> None:
            with self.jobs_lock:
                self.jobs[job_id] = {"status": "running", "message": "Loading tags, people, descriptions, and camera metadata from Immich"}
            try:
                result = self.enrich(body)
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "complete", "message": "Photo metadata ready", "plan": result}
            except Exception as exc:
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "error", "message": str(exc)}
        self.executor.submit(work)
        return job_id

    def start_enrich_all_job(self) -> str:
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {"status": "queued", "message": "Waiting to load metadata for the selected photos"}
        def work() -> None:
            with self.jobs_lock:
                self.jobs[job_id] = {"status": "running", "message": "Loading tags, people, descriptions, and camera metadata from Immich"}
            try:
                result = self.enrich_all()
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "complete", "message": "Photo metadata ready", "plan": result}
            except Exception as exc:
                with self.jobs_lock:
                    self.jobs[job_id] = {"status": "error", "message": str(exc)}
        self.executor.submit(work)
        return job_id

    def reconcile_checkpoint(self, plan: dict[str, Any], client: importer.AdventureLogClient) -> None:
        """Reuse only stable stops from an older local checkpoint.

        A changed review plan must never delete or unlink the old remote
        objects. Matching stable keys can safely continue; stale keys are
        simply removed from the local checkpoint so new plan groups can be
        created alongside them.
        """
        state = _state_read(self.state_path)
        if not state or str(state.get("plan_fingerprint")) == plan_fingerprint(plan):
            return
        current_keys = {str(group.get("key")) for group in plan.get("groups", [])}
        old_stops = state.get("stops", {}) if isinstance(state.get("stops"), dict) else {}
        matching_keys = current_keys & {str(key) for key in old_stops}
        if not matching_keys:
            raise ValueError("the saved checkpoint belongs to a different review; use a new state file")
        collection_id = str(state.get("collection_id") or "")
        if not collection_id:
            raise ValueError("the saved checkpoint has no collection ID; use a new state file")
        known = [item for item in client.list_collections() if str(item.get("id") or item.get("pk")) == collection_id]
        if not known or _clean(known[0].get("name")) != _clean(plan["collection"]["name"]):
            raise ValueError("the saved checkpoint targets a different collection; use a new state file")
        state["stops"] = {key: value for key, value in old_stops.items() if str(key) in matching_keys}
        state["itinerary_items"] = {key: value for key, value in (state.get("itinerary_items") or {}).items() if str(key) in matching_keys}
        state["collection_name"] = plan["collection"]["name"]
        state["day"] = plan["day"]
        state["days"] = plan.get("days", [plan["day"]])
        state["plan_fingerprint"] = plan_fingerprint(plan)
        state["itinerary_complete"] = False
        _state_write(self.state_path, state)

    def start_create_job(self) -> str:
        """Create the reviewed collection, stops, links, and itinerary in order."""
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {"status": "queued", "phase": "locations", "progress": 0, "message": "Waiting to create the reviewed trip"}

        def update(phase: str, current: int, total: int, message: str) -> None:
            phase_ratio = current / max(total, 1)
            overall = phase_ratio * 0.75 if phase == "locations" else 0.75 + phase_ratio * 0.25
            with self.jobs_lock:
                self.jobs[job_id].update({"status": "running", "phase": phase, "current": current, "total": total, "progress": round(overall * 100), "message": message})

        def work() -> None:
            with self.jobs_lock:
                self.jobs[job_id].update({"status": "running", "phase": "locations", "progress": 0, "message": "Creating the reviewed collection and stops"})
            try:
                plan = self.read_plan()
                if not plan:
                    raise ValueError("no saved plan; load photos first")
                client = _client(self.config(), getattr(self.args, "adventurelog_timeout", 180))
                self.reconcile_checkpoint(plan, client)
                plan = self.read_plan() or plan
                apply_ui_locations(plan, client, self.state_path, progress=update, attachment_workers=getattr(self.args, "attachment_workers", 4))
                plan = self.read_plan() or plan
                plan["remote"]["locations_complete"] = True
                self.save_plan(plan)
                apply_ui_itinerary(plan, client, self.state_path, progress=update)
                plan = self.read_plan() or plan
                plan["remote"]["itinerary_complete"] = True
                self.save_plan(plan)
                report = verification_report(plan, _state_read(self.state_path) or {})
                report["adventurelog_url"] = self.config().get("adventurelog_base_url")
                with self.jobs_lock:
                    self.jobs[job_id].update({"status": "complete", "phase": "done", "progress": 100, "message": "Trip created and itinerary complete", "report": report})
            except Exception as exc:
                with self.jobs_lock:
                    self.jobs[job_id].update({"status": "error", "message": str(exc)})

        self.executor.submit(work)
        return job_id

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.jobs_lock:
            return dict(self.jobs[job_id]) if job_id in self.jobs else None

    def status(self) -> dict[str, Any]:
        collections, collection_error = self.collections()
        config = self.config(); plan = self.read_plan()
        completed = bool(plan and plan.get("remote", {}).get("itinerary_complete"))
        active_plan = plan if plan and not completed else None
        default_day = (active_plan or {}).get("day") or (self.manifest.get("summary", {}).get("days_with_photos") or [date.today().isoformat()])[0]
        thumbnails = self.thumbnail_status() if config["immich_base_url"] and config["immich_api_key"] else "not configured"
        return {"immich": bool(config["immich_base_url"] and config["immich_api_key"]) or bool(self.manifest.get("assets")), "thumbnails": thumbnails, "adventurelog": bool(config["adventurelog_base_url"] and config["adventurelog_token"]), "plan": bool(active_plan), "completed_plan": completed, "default_day": default_day, "collections": collections, "collection_error": collection_error}


class Handler(BaseHTTPRequestHandler):
    server_version = "ImmichAdventureReview/1.0"
    def app(self) -> App: return self.server.app  # type: ignore[attr-defined]
    def log_message(self, fmt: str, *args: Any) -> None: return
    def send_json(self, value: Any, code: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0")); value = json.loads(self.rfile.read(length) or b"{}")
        return value if isinstance(value, dict) else {}
    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                raw = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if path == "/api/status": self.send_json(self.app().status()); return
            if path == "/api/plan": self.send_json({"plan": self.app().read_plan()}); return
            if path.startswith("/api/jobs/"):
                job = self.app().job(path.rsplit("/", 1)[-1])
                self.send_json(job or {"error": "unknown job"}, 200 if job else 404); return
            if path.startswith("/media/thumbnail/"):
                asset_id = unquote(path.rsplit("/", 1)[-1])
                config = self.app().config()
                plan = self.app().read_plan() or {}
                asset = next((x for x in plan.get("assets", []) if str(x.get("id")) == asset_id), None)
                asset = asset or next((x for x in self.app().manifest.get("assets", []) if str(x.get("id")) == asset_id), None)
                # Prefer the current configured server over the URL captured
                # in an older manifest; manifests are portable across hosts.
                target = ((config["immich_base_url"].rstrip("/") + "/api/assets/" + quote(asset_id, safe="") + "/thumbnail") if config["immich_base_url"] else (asset or {}).get("thumbnail_url"))
                if not target: self.send_error(404); return
                headers = {"x-api-key": config["immich_api_key"]} if config["immich_api_key"] else {}
                request = urllib.request.Request(target, headers=headers)
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read(); content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
                self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "private, max-age=300"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            self.send_error(404)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc: self.send_json({"error": str(exc)}, 500)
    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path; body = self.body(); app = self.app()
            if path == "/api/preview":
                if app.preview_needs_background(body):
                    self.send_json({"job_id": app.start_preview_job(body), "status": "queued"}, 202)
                else:
                    self.send_json({"plan": app.preview(body)})
                return
            plan = app.read_plan()
            if not plan: raise ValueError("no saved plan; load photos first")
            if path == "/api/reset-checkpoint":
                backup = app.reset_checkpoint()
                self.send_json({"ok": True, "backup": bool(backup)})
                return
            if path == "/api/poi":
                if app.preview_needs_background({"start_day": plan.get("period", {}).get("start"), "end_day": plan.get("period", {}).get("end")}) or body.get("background"):
                    self.send_json({"job_id": app.start_poi_job(body), "status": "queued"}, 202)
                else:
                    self.send_json({"plan": app.poi(body)})
                return
            if path == "/api/enrich":
                key = str(body.get("key") or "")
                group = next((g for g in plan.get("groups", []) if g.get("key") == key), None)
                if not group: raise ValueError("unknown stop")
                if len(group.get("asset_ids", [])) > 10 or body.get("background"):
                    self.send_json({"job_id": app.start_enrich_job(body), "status": "queued"}, 202)
                else:
                    self.send_json({"plan": app.enrich(body)})
                return
            if path == "/api/enrich-all":
                selected_count = len(plan.get("selected_asset_ids", [])) or sum(len(group.get("asset_ids", [])) for group in plan.get("groups", []))
                if body.get("background") or selected_count > 10:
                    self.send_json({"job_id": app.start_enrich_all_job(), "status": "queued"}, 202)
                else:
                    self.send_json({"plan": app.enrich_all()})
                return
            if path == "/api/edit": app.save_plan(importer.json.loads(json.dumps(edit_plan(plan, str(body.get("operation")), body.get("payload") or {})))); self.send_json({"plan": app.read_plan()}); return
            if path == "/api/validate":
                config = app.config(); client = _client(config, getattr(app.args, "adventurelog_timeout", 180)) if config.get("adventurelog_base_url") and config.get("adventurelog_token") else None
                self.send_json(validate_plan(plan, client, app.state_path)); return
            if path == "/api/create-trip":
                self.send_json({"job_id": app.start_create_job(), "status": "queued"}, 202)
                return
            raise ValueError("unknown endpoint")
        except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, urllib.error.URLError) as exc: self.send_json({"error": str(exc)}, 400)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local review UI for the Immich to AdventureLog itinerary importer")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--manifest", type=Path, default=Path("immich-manifest.json"), help="offline manifest/fixture to review")
    p.add_argument("--plan", type=Path, default=Path("immich-review-plan.json"))
    p.add_argument("--state", type=Path, default=Path("immich-review-state.json"))
    p.add_argument("--poi-cache", type=Path)
    p.add_argument("--overpass-url", default=os.environ.get("OVERPASS_URL", importer.DEFAULT_OVERPASS_URL))
    p.add_argument("--poi-radius", type=int, default=100)
    p.add_argument("--poi-max-requests", type=int, default=4)
    p.add_argument("--poi-min-interval", type=float, default=2.0)
    p.add_argument("--poi-timeout", type=int, default=20)
    p.add_argument("--metadata-workers", type=int, default=6, help="concurrent Immich metadata requests (default: 6)")
    p.add_argument("--attachment-workers", type=int, default=4, help="concurrent AdventureLog image-link requests (default: 4)")
    p.add_argument("--immich-timeout", type=int, default=60, help="seconds per Immich request (default: 60)")
    p.add_argument("--adventurelog-timeout", type=int, default=180, help="seconds per AdventureLog request (default: 180)")
    return p


def main(argv: list[str] | None = None) -> int:
    importer.load_local_dotenv()
    args = build_parser().parse_args(argv)
    try:
        app = App(args); server = ThreadingHTTPServer((args.host, args.port), Handler); server.app = app  # type: ignore[attr-defined]
        print(f"Immich → AdventureLog review UI: http://{args.host}:{args.port}")
        print("Remote writes remain idle until you click Create reviewed trip after validation.")
        server.serve_forever()
    except KeyboardInterrupt: return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc: print(f"error: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
