"""Road Discovery 2.0 alpha1: offline, opt-in OSM catalogue builder.

Requires a locally downloaded, trusted Netherlands .osm.pbf and the optional
`osmium` Python package (pip install osmium). NEVER invoked by the HA add-on.
Does not edit existing roads_nl.json, server SQLite or Discovery sync state.

Usage:
  python build_discovery2_catalog.py netherlands-latest.osm.pbf output-v2.json

Only explicit unconditional maxspeed >=60 km/h qualifies. This intentionally
undercounts roads: default/regulatory speed rules and time-dependent limits need
a separate validated source before admission. Old HA IDs are not assumed equal
to new OSM way/node IDs. No catalog switch is performed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

REVISION = "road-discovery-2-osm-explicit-v1"
ELIGIBLE_HIGHWAYS = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified",
})
EXCLUDED_HIGHWAYS = frozenset({
    "residential", "living_street", "service", "pedestrian",
    "path", "footway", "cycleway", "track", "construction",
    "proposed", "steps",
})
_NUMERIC_SPEED = re.compile(r"^(\d{1,3})(?:\s*(km/h|kmh|mph))?$", re.IGNORECASE)
EARTH_RADIUS_M = 6_371_000.0


def parse_maxspeed(tags: dict) -> int | None:
    """Strict OSM tag parser; never infer defaults or directional applicability."""
    if any(key in tags for key in (
        "maxspeed:conditional", "maxspeed:variable", "maxspeed:forward",
        "maxspeed:backward", "maxspeed:lanes", "maxspeed:advisory",
    )):
        return None
    raw = tags.get("maxspeed")
    if not isinstance(raw, str):
        return None
    match = _NUMERIC_SPEED.fullmatch(raw.strip())
    if not match:
        return None
    speed = int(match.group(1))
    if (match.group(2) or "").lower() == "mph":
        speed = round(speed * 1.609344)
    return speed if 5 <= speed <= 130 else None


def eligible(tags: dict) -> bool:
    highway = tags.get("highway")
    if highway not in ELIGIBLE_HIGHWAYS or highway in EXCLUDED_HIGHWAYS:
        return False
    # Access-limited/service-purpose ways are not part of the public drivable
    # catalogue, even if they carry a highway type in the whitelist.
    if tags.get("access") in {"no", "private"} or tags.get("motor_vehicle") in {"no", "private"}:
        return False
    if tags.get("area") == "yes":
        return False
    speed = parse_maxspeed(tags)
    return speed is not None and speed >= 60


def segment_id(way_id: int, a_node_id: int, b_node_id: int) -> str:
    """Stable for the same OSM way and unordered pair of consecutive nodes."""
    left, right = sorted((a_node_id, b_node_id))
    return f"osm:w{way_id}:n{left}-{right}"


def length_metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance, coordinates are (longitude, latitude)."""
    lon1, lat1 = a
    lon2, lat2 = b
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    h = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(
        math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(h)))


def validate_segment(segment: dict) -> None:
    if not segment["id"] or not segment["road"] or not segment["length_m"] > 0:
        raise ValueError("Invalid empty/zero-length segment")
    if segment["highway"] not in ELIGIBLE_HIGHWAYS or not 60 <= segment["maxspeed_kmh"] <= 130:
        raise ValueError("Segment fails explicit speed/highway filter")
    for point in (segment["a"], segment["b"]):
        if (len(point) != 2 or not all(math.isfinite(value) for value in point)
                or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
            raise ValueError("Invalid segment geometry")


def generate(pbf: Path, output: Path) -> dict:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Install optional offline builder dependency: pip install osmium") from exc
    if not pbf.is_file() or not pbf.name.endswith(".osm.pbf"):
        raise ValueError("Expected an existing .osm.pbf input file")
    if output.exists():
        raise FileExistsError("Output already exists; never overwrite an existing catalogue")
    output.parent.mkdir(parents=True, exist_ok=True)
    stats = Counter()
    seen = set()

    class Collector(osmium.SimpleHandler):
        def __init__(self, handle):
            super().__init__()
            self.handle = handle
            self.first = True

        def way(self, way):
            tags = dict(way.tags)
            stats["ways_seen"] += 1
            if not eligible(tags):
                stats["ways_excluded"] += 1
                return
            speed = parse_maxspeed(tags)
            highway = tags["highway"]
            road = tags.get("name") or tags.get("ref") or f"OSM weg {way.id}"
            stats["eligible_ways"] += 1
            for a, b in zip(way.nodes, list(way.nodes)[1:]):
                if not a.location.valid() or not b.location.valid():
                    stats["invalid_locations"] += 1
                    continue
                a_pt = (round(a.lon, 7), round(a.lat, 7))
                b_pt = (round(b.lon, 7), round(b.lat, 7))
                length = length_metres(a_pt, b_pt)
                if not 0.1 <= length <= 100_000:
                    stats["invalid_lengths"] += 1
                    continue
                sid = segment_id(way.id, a.ref, b.ref)
                if sid in seen:
                    stats["duplicate_ids"] += 1
                    continue
                seen.add(sid)
                segment = {
                    "id": sid, "road": road, "a": a_pt, "b": b_pt,
                    "length_m": round(length, 3), "highway": highway,
                    "maxspeed_kmh": speed,
                }
                validate_segment(segment)
                if not self.first:
                    self.handle.write(",")
                self.handle.write(json.dumps(segment, ensure_ascii=False, separators=(",", ":")))
                self.first = False
                stats["segments"] += 1
                stats["metres"] += length

    # Write in the target directory; final path becomes visible only on success.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                     suffix=".partial", delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write('{"revision":' + json.dumps(REVISION) + ',"segments":[')
            collector = Collector(temp)
            collector.apply_file(str(pbf), locations=True)
            temp.write("]}")
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    if not stats["segments"]:
        temp_path.unlink(missing_ok=True)
        raise ValueError("No eligible roads; refusing empty nationwide catalogue")
    os.replace(temp_path, output)
    sha = hashlib.sha256()
    with output.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(block)
    manifest = {
        "revision": REVISION, "source": str(pbf.name), "sha256": sha.hexdigest(),
        "size_bytes": output.stat().st_size,
        "eligible_ways": stats["eligible_ways"],
        "segments": stats["segments"],
        "length_km": round(stats["metres"] / 1000, 3),
        "excluded_ways": stats["ways_excluded"],
        "duplicate_ids": stats["duplicate_ids"],
        "invalid_locations": stats["invalid_locations"],
        "invalid_lengths": stats["invalid_lengths"],
        "sync_compatible_with_legacy": False,
        "release_ready": False,
    }
    report = output.with_name(output.name + ".manifest.json")
    report.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only Road Discovery 2.0 alpha1 OSM catalogue")
    parser.add_argument("pbf", type=Path, help="Locally downloaded Netherlands .osm.pbf")
    parser.add_argument("output", type=Path, help="New output JSON; must not exist")
    args = parser.parse_args()
    print(json.dumps(generate(args.pbf, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
