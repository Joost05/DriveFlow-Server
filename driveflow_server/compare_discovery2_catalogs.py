"""Read-only geographic comparison of legacy HA and OSM Road Discovery catalogues.

A *candidate* overlap is not an automatic discovery migration. Parallel lanes,
intersections, and different segmentations require review before any switch.
For 1.4M segments this streams both JSON files with the optional `ijson`
package and indexes only the legacy geometry in a temporary on-disk SQLite
RTree; RAM usage does not scale with catalogue size.

Example:
  py compare_discovery2_catalogs.py roads_nl.json roads_nl_2_alpha1.json report.json

No application databases, discovery state or input catalogues are changed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

METRES_PER_LAT = 111_195.0
METRES_PER_LON = 68_700.0  # Approximate NL projection; only for candidate lookup.
SEARCH_M = 18.0
MATCH_M = 10.0
ANGLE_COS = math.cos(math.radians(25))
AMBIGUITY_MARGIN_M = 3.0


def stream_segments(path: Path):
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError("Installeer streaming JSON-reader: py -m pip install ijson") from exc
    with path.open("rb") as source:
        yield from ijson.items(source, "segments.item", use_float=True)


def coordinates(segment):
    try:
        a, b = segment["a"], segment["b"]
        ax, ay = float(a[0]) * METRES_PER_LON, float(a[1]) * METRES_PER_LAT
        bx, by = float(b[0]) * METRES_PER_LON, float(b[1]) * METRES_PER_LAT
        if not all(math.isfinite(n) for n in (ax, ay, bx, by)):
            return None
        if math.hypot(bx - ax, by - ay) < 0.1:
            return None
        return ax, ay, bx, by
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def projection_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    v = dx * dx + dy * dy
    t = 0.0 if v < 1e-8 else ((px - ax) * dx + (py - ay) * dy) / v
    clamped = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + clamped * dx), py - (ay + clamped * dy)), t


def candidate_score(new, old):
    """Only accept near-parallel full new segment geometrically inside old.

    A short new line crossing a long old one, or a perpendicular crossing,
    must not be mistaken for the same drivable road.
    """
    ax, ay, bx, by = new
    cx, cy, dx, dy = old
    nu = (bx - ax, by - ay)
    ou = (dx - cx, dy - cy)
    nlen, olen = math.hypot(*nu), math.hypot(*ou)
    if nlen < 0.1 or olen < 0.1:
        return None
    cosine = abs((nu[0] * ou[0] + nu[1] * ou[1]) / (nlen * olen))
    if cosine < ANGLE_COS:
        return None
    d1, t1 = projection_distance(ax, ay, cx, cy, dx, dy)
    d2, t2 = projection_distance(bx, by, cx, cy, dx, dy)
    # Disallow extrapolating beyond old segment by more than 5m.
    slack = min(5.0 / olen, 0.5)
    if not (-slack <= t1 <= 1 + slack and -slack <= t2 <= 1 + slack):
        return None
    worst = max(d1, d2)
    return worst if worst <= MATCH_M else None


def old_index(db, legacy_path: Path):
    db.execute("CREATE TABLE lines (rid INTEGER PRIMARY KEY, sid TEXT NOT NULL UNIQUE, "
               "road TEXT, ax REAL, ay REAL, bx REAL, by REAL)")
    db.execute("CREATE VIRTUAL TABLE search USING rtree(rid, minx, maxx, miny, maxy)")
    total = 0
    invalid = 0
    for seg in stream_segments(legacy_path):
        geom = coordinates(seg)
        if geom is None or not seg.get("id"):
            invalid += 1
            continue
        ax, ay, bx, by = geom
        cur = db.execute("INSERT INTO lines(sid,road,ax,ay,bx,by) VALUES(?,?,?,?,?,?)",
                         (str(seg["id"]), str(seg.get("road", "")), *geom))
        db.execute("INSERT INTO search VALUES(?,?,?,?,?)",
                   (cur.lastrowid, min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)))
        total += 1
        if total % 10_000 == 0:
            db.commit()
    db.commit()
    return total, invalid


def match_segment(db, segment):
    new = coordinates(segment)
    if new is None:
        return "invalid_geometry", None, None
    ax, ay, bx, by = new
    rows = db.execute(
        "SELECT l.sid,l.road,l.ax,l.ay,l.bx,l.by FROM search s "
        "JOIN lines l ON s.rid=l.rid WHERE "
        "s.minx<=? AND s.maxx>=? AND s.miny<=? AND s.maxy>=?",
        (max(ax, bx) + SEARCH_M, min(ax, bx) - SEARCH_M,
         max(ay, by) + SEARCH_M, min(ay, by) - SEARCH_M))
    candidates = []
    for sid, road, cx, cy, dx, dy in rows:
        score = candidate_score(new, (cx, cy, dx, dy))
        if score is not None:
            candidates.append((score, sid, road))
    if not candidates:
        return "new", None, None
    candidates.sort()
    # Two overlapping old lines near the same coordinates might be separate
    # carriageways, bridges or parallel roads. Do not choose arbitrarily.
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= AMBIGUITY_MARGIN_M:
        return "ambiguous", None, round(candidates[0][0], 2)
    return "candidate", candidates[0][1], round(candidates[0][0], 2)


def read_discovered(path: Path | None):
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
        raise ValueError("Expected old discovery export JSON with an 'ids' list")
    ids = data["ids"]
    if any(not isinstance(s, str) for s in ids):
        raise ValueError("Invalid old discovery ID")
    return set(ids)


def compare(legacy: Path, newer: Path, report: Path,
            discovered: Path | None = None, mapping: Path | None = None):
    for path in (legacy, newer):
        if not path.is_file():
            raise FileNotFoundError(path)
    for path in (report, mapping):
        if path is not None and path.exists():
            raise FileExistsError("Output already exists: " + str(path))
    if legacy.resolve() == newer.resolve():
        raise ValueError("Old and new catalogue must be different files")
    old_ids = read_discovered(discovered)
    counts = Counter()
    matched_old_ids = set()
    matched_discovered_ids = set()
    sample = []
    # Never store temporary DB beside either input or in application data.
    with tempfile.TemporaryDirectory(prefix="driveflow-d2-compare-") as tmp:
        db = sqlite3.connect(str(Path(tmp) / "index.sqlite"))
        try:
            old_total, old_invalid = old_index(db, legacy)
            counts["old_segments"] = old_total
            counts["old_invalid"] = old_invalid
            writer = None
            destination = None
            try:
                if mapping is not None:
                    mapping.parent.mkdir(parents=True, exist_ok=True)
                    destination = mapping.open("x", encoding="utf-8", newline="")
                    writer = csv.writer(destination)
                    writer.writerow(("new_id", "status", "old_id", "offset_m", "old_discovered"))
                for seg in stream_segments(newer):
                    counts["new_segments"] += 1
                    status, old_id, distance = match_segment(db, seg)
                    counts[status] += 1
                    is_discovered = old_ids is not None and old_id in old_ids
                    if old_id is not None:
                        matched_old_ids.add(old_id)
                        if is_discovered:
                            matched_discovered_ids.add(old_id)
                            counts["new_segments_candidate_discovered"] += 1
                    if len(sample) < 25 and status in ("ambiguous", "invalid_geometry"):
                        sample.append({"new_id": str(seg.get("id")), "reason": status})
                    if writer is not None:
                        writer.writerow((seg.get("id", ""), status, old_id or "",
                                         "" if distance is None else distance,
                                         "" if old_ids is None else int(is_discovered)))
            finally:
                if destination is not None:
                    destination.close()
            counts["old_segments_with_candidate"] = len(matched_old_ids)
            counts["old_segments_unmatched"] = old_total - len(matched_old_ids)
            if old_ids is not None:
                counts["old_discovered_ids"] = len(old_ids)
                counts["old_discovered_ids_with_candidate"] = len(matched_discovered_ids)
                counts["old_discovered_ids_without_candidate"] = len(old_ids - matched_discovered_ids)
        finally:
            db.close()
    result = {
        "status": "review_only",
        "migration_performed": False,
        "safe_to_auto_migrate": False,
        "method": "endpoint-to-old-segment, heading <=25deg, <=10m offset; parallel roads ambiguous",
        "counts": dict(counts),
        "samples_for_manual_review": sample,
        "warning": "Candidate geometry matches are NOT proven identical routes; no discoveries migrated.",
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("x", encoding="utf-8") as out:
        json.dump(result, out, indent=2, ensure_ascii=False)
        out.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description="Compare old HA and new OSM road catalogues (read-only)")
    parser.add_argument("old", type=Path, help="original HA roads_nl.json")
    parser.add_argument("new", type=Path, help="generated roads_nl_2_alpha1.json")
    parser.add_argument("report", type=Path, help="new JSON output report")
    parser.add_argument("--discovered", type=Path, help="optional old export: {'ids':[...]} JSON")
    parser.add_argument("--mapping", type=Path, help="optional per-segment CSV; potentially large")
    args = parser.parse_args()
    result = compare(args.old, args.new, args.report, args.discovered, args.mapping)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
