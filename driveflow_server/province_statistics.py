"""One-time PDOK province and CBS catalogue index for DriveFlow discoveries.

This never reads historical Home Assistant Road Discovery data. Geographical
assignment matches the Android client's midpoint/point-in-polygon rule.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from discovery_places import CATALOG as PLACES
from road_discovery_store import CATALOG as ROADS

REVISION = "pdok-brk-provinciegebied-2026+cbs-bevolkingskernen-2021"
PROVINCES_URL = "https://api.pdok.nl/kadaster/brk-bestuurlijke-gebieden/ogc/v1/collections/provinciegebied/items?f=json&limit=100"
PLACES_URL = "https://api.pdok.nl/cbs/bevolkingskernen-2021/ogc/v1/collections/bevolkingskernen/items?f=json&limit=1000"
MAX_PAGE_BYTES = 30_000_000
LOGGER = logging.getLogger("driveflow.provinces")


def create_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_province_roads (
            segment_id TEXT PRIMARY KEY, province TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_discovery_province_roads_province
            ON discovery_province_roads(province);
        CREATE TABLE IF NOT EXISTS discovery_province_places (
            place_id TEXT PRIMARY KEY, province TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_discovery_province_places_province
            ON discovery_province_places(province);
        CREATE TABLE IF NOT EXISTS discovery_province_names (
            name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS discovery_province_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def _page(url: str, prefix: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.pdok.nl" or not parsed.path.startswith(prefix):
        raise ValueError("unexpected PDOK catalogue URL")
    with urlopen(url, timeout=75) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("PDOK catalogue page too large")
    result = json.loads(raw)
    if not isinstance(result, dict) or not isinstance(result.get("features"), list):
        raise ValueError("PDOK returned invalid GeoJSON")
    return result


def _rings(geometry: dict):
    kind = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if kind == "Polygon":
        return [coords]
    if kind == "MultiPolygon":
        return coords
    return []


def _bounds(geometry: dict):
    points = [point for polygon in _rings(geometry) for ring in polygon for point in ring
              if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _ring_contains(lon: float, lat: float, ring: list) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        xi, yi = current[:2]
        xj, yj = previous[:2]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        previous = current
    return inside


def _contains(lon: float, lat: float, geometry: dict) -> bool:
    for polygon in _rings(geometry):
        if not polygon or not _ring_contains(lon, lat, polygon[0]):
            continue
        if not any(_ring_contains(lon, lat, hole) for hole in polygon[1:]):
            return True
    return False


def _match(lon: float, lat: float, provinces: list) -> str | None:
    for name, geometry, bounds in provinces:
        if bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3] and _contains(lon, lat, geometry):
            return name
    return None


def _place_id(feature: dict, props: dict) -> str:
    value = feature.get("id")
    if value is not None and str(value).strip():
        return str(value).strip()
    for key in ("kerncode", "kern_id", "id_kern", "statcode", "jrstatcode", "identificatie"):
        value = props.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def prepare(db: sqlite3.Connection, road_file: Path, progress=None) -> dict:
    """Download reference data once and publish mapping atomically on success."""
    def report(stage, done=0, total=None):
        if progress is not None:
            progress(stage, done, total)
    report('controleren')
    create_tables(db)
    old = db.execute("SELECT value FROM discovery_province_meta WHERE key='revision'").fetchone()
    if old and old[0] == REVISION:
        report('gereed', 12, 12)
        return {"ready": True, "cached": True}
    report("provincies_downloaden")
    payload = _page(PROVINCES_URL, "/kadaster/brk-bestuurlijke-gebieden/ogc/v1/collections/provinciegebied/")
    provinces = []
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        name = str(props.get("naam") or "").strip()
        geom = feature.get("geometry") or {}
        bounds = _bounds(geom)
        if name and bounds:
            provinces.append((name, geom, bounds))
    if len(provinces) != 12 or len({p[0] for p in provinces}) != 12:
        raise ValueError("PDOK province catalogue is incomplete")
    report("wegen_lezen")
    road_data = json.loads(road_file.read_text(encoding="utf-8"))
    segments = road_data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("road catalogue is missing")
    roads = []
    report("wegen_indelen", 0, len(segments))
    for index, segment in enumerate(segments):
        if index % 2000 == 0:
            report("wegen_indelen", index, len(segments))
        a, b = segment.get("a"), segment.get("b")
        if not isinstance(a, list) or not isinstance(b, list) or len(a) < 2 or len(b) < 2:
            continue
        name = _match((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, provinces)
        if name and segment.get("id"):
            roads.append((segment["id"], name))
    report("wegen_ingedeeld", len(roads), len(segments))
    if not roads:
        raise ValueError("no road segments could be assigned")
    places = []
    next_url = PLACES_URL
    seen = set()
    report("plaatsen_downloaden")
    while next_url and len(seen) < 30:
        report("plaatsen_downloaden", len(seen), None)
        if next_url in seen:
            raise ValueError("CBS pagination loop")
        seen.add(next_url)
        page = _page(next_url, "/cbs/bevolkingskernen-2021/ogc/v1/collections/bevolkingskernen/")
        for feature in page["features"]:
            props = feature.get("properties") or {}
            place_id = _place_id(feature, props)
            bounds = _bounds(feature.get("geometry") or {})
            if not place_id or not bounds:
                continue
            name = _match((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, provinces)
            if name:
                places.append((place_id, name))
        next_url = next((link.get("href") for link in page.get("links", []) if link.get("rel") == "next"), None)
    if next_url:
        raise ValueError("CBS pagination incomplete")
    if not places:
        raise ValueError("no CBS places could be assigned")
    report("opslaan", len(roads) + len(places), len(roads) + len(places))
    with db:
        db.execute("DELETE FROM discovery_province_roads")
        db.execute("DELETE FROM discovery_province_places")
        db.execute("DELETE FROM discovery_province_names")
        db.executemany("INSERT INTO discovery_province_names(name) VALUES(?)", [(p[0],) for p in provinces])
        db.executemany("INSERT OR REPLACE INTO discovery_province_roads(segment_id,province) VALUES(?,?)", roads)
        db.executemany("INSERT OR REPLACE INTO discovery_province_places(place_id,province) VALUES(?,?)", places)
        db.execute("INSERT OR REPLACE INTO discovery_province_meta(key,value) VALUES('revision',?)", (REVISION,))
    report("gereed", 12, 12)
    return {"ready": True, "roads_assigned": len(roads), "places_assigned": len(places)}


def statistics(db: sqlite3.Connection) -> dict:
    create_tables(db)
    ready = db.execute("SELECT value FROM discovery_province_meta WHERE key='revision'").fetchone()
    if not ready or ready[0] != REVISION:
        return {"provinces_ready": False, "provinces": []}
    road_rows = db.execute("""SELECT p.province,COUNT(*),COALESCE(SUM(l.length_m),0),
        COUNT(d.segment_id),COALESCE(SUM(CASE WHEN d.segment_id IS NOT NULL THEN l.length_m ELSE 0 END),0)
        FROM discovery_province_roads p JOIN discovery_road_lengths l ON l.segment_id=p.segment_id
        LEFT JOIN discovery_segments d ON d.segment_id=p.segment_id AND d.catalog=?
        GROUP BY p.province""", (ROADS,)).fetchall()
    place_rows = db.execute("""SELECT p.province,COUNT(*),COUNT(d.place_id)
        FROM discovery_province_places p LEFT JOIN discovery_places d
        ON d.place_id=p.place_id AND d.catalog=? GROUP BY p.province""", (PLACES,)).fetchall()
    roads = {r[0]: r for r in road_rows}
    places = {r[0]: r for r in place_rows}
    result = []
    for (name,) in db.execute("SELECT name FROM discovery_province_names ORDER BY name"):
        r = roads.get(name, (name, 0, 0, 0, 0))
        p = places.get(name, (name, 0, 0))
        road_pct = 100 * r[4] / r[2] if r[2] else 0
        place_pct = 100 * p[2] / p[1] if p[1] else 0
        result.append({"name": name, "road_segments_total": r[1], "road_segments_discovered": r[3],
                       "road_km_total": round(r[2] / 1000, 3), "road_km_discovered": round(r[4] / 1000, 3),
                       "road_percentage": round(road_pct, 4), "places_total": p[1],
                       "places_discovered": p[2], "places_percentage": round(place_pct, 4),
                       "percentage": round(.8 * road_pct + .2 * place_pct, 4)})
    return {"provinces_ready": True, "provinces": result,
            "places_total": sum(p[1] for p in places.values()),
            "places_percentage": round(100 * sum(p[2] for p in places.values()) / sum(p[1] for p in places.values()), 4)
            if places else None}
