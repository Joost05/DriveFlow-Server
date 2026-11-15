"""Road Discovery 2 connected-line staging and read-only map preview.

Independent from the active DriveFlow discovery database. Files are pinned to
the exact uploaded GeoJSON.gz artifacts. Discovery is resolved per SOURCE
SEGMENT, not per connected polyline. The v2 OSM layer never inherits a
geometric candidate's discovered status automatically.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import math
import os
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

from road_discovery_store import CATALOG as LEGACY_CATALOG

LAYERS = {
    "legacy": ("roads_nl_legacy_connected.geojson.gz", 4_419_325,
               "35d4ca806e009a69ef542aa71c327c695e568a305f3d60b21c7587f2ae11a5b1",
               75_309, 250_620),
    "osm": ("roads_nl_osm_connected.geojson.gz", 24_980_704,
            "5001a12e375037003038fba43010d1c8a9ff69e3d22fb791568aeddc33ca26f9",
            158_225, 1_393_159),
}
LOCK = threading.Lock()
BUILDING = False
STATE = {"state": "idle", "error": None}
CHUNK = 1024 * 1024
MAX_LINES = 250
MAX_BBOX_DEGREES = 0.20


def source_path(data_dir: Path, layer: str) -> Path:
    if layer not in LAYERS:
        raise ValueError("Unknown layer")
    return data_dir / "discovery2" / LAYERS[layer][0]


def index_path(data_dir: Path) -> Path:
    return data_dir / "discovery2" / "connected_lines.sqlite"


def stage(stream, length: int, sha_header: str, data_dir: Path, layer: str) -> dict:
    name, expected, digest, _, _ = LAYERS[layer]
    if length != expected or not hmac.compare_digest(sha_header.lower().strip(), digest):
        raise ValueError("Unexpected connected line file length or checksum")
    target = source_path(data_dir, layer)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Connected lines already staged")
    if shutil.disk_usage(data_dir).free < expected + 1024 * 1024 * 1024:
        raise ValueError("Insufficient free storage for line index")
    temp = target.parent / ("." + uuid.uuid4().hex + ".upload")
    remaining = length
    h = hashlib.sha256()
    try:
        with temp.open("xb") as output:
            while remaining:
                part = stream.read(min(CHUNK, remaining))
                if not part:
                    raise ValueError("Incomplete upload")
                output.write(part)
                h.update(part)
                remaining -= len(part)
            output.flush()
            os.fsync(output.fileno())
        if not hmac.compare_digest(h.hexdigest(), digest):
            raise ValueError("Uploaded line file checksum mismatch")
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {"state": "staged", "layer": layer, "bytes": expected, "activated": False}


def _verified(path: Path, layer: str) -> bool:
    _, expected, digest, _, _ = LAYERS[layer]
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected:
        return False
    h = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(CHUNK), b""):
            h.update(block)
    return hmac.compare_digest(h.hexdigest(), digest)


def status(data_dir: Path) -> dict:
    with LOCK:
        current = dict(STATE)
    current["layers"] = {
        layer: {"present": (p.is_file() and not p.is_symlink() and
                            p.stat().st_size == LAYERS[layer][1]),
                "expected_lines": LAYERS[layer][3]}
        for layer in LAYERS for p in (source_path(data_dir, layer),)
    }
    index = index_path(data_dir)
    current["index_available"] = index.is_file() and not index.is_symlink()
    current["activated"] = False
    return current


def _schema(db):
    db.executescript("""
        CREATE TABLE lines(
            fid INTEGER PRIMARY KEY,
            layer TEXT NOT NULL CHECK(layer IN ('legacy','osm')),
            properties TEXT NOT NULL,
            coordinates TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE bounds USING rtree(fid,minx,maxx,miny,maxy);
        CREATE TABLE manifest(layer TEXT PRIMARY KEY, lines INTEGER NOT NULL,
                              segments INTEGER NOT NULL, sha256 TEXT NOT NULL);
    """)


def _build(data_dir: Path) -> None:
    global BUILDING
    target = index_path(data_dir)
    temp = target.parent / ("." + uuid.uuid4().hex + ".lines.sqlite")
    try:
        for layer in LAYERS:
            if not _verified(source_path(data_dir, layer), layer):
                raise ValueError("Missing or invalid source layer: " + layer)
        import ijson
        with sqlite3.connect(temp) as db:
            _schema(db)
            for layer, (name, size, digest, expected_lines, expected_segments) in LAYERS.items():
                line_count = 0
                segment_count = 0
                with gzip.open(source_path(data_dir, layer), "rb") as src:
                    for f in ijson.items(src, "features.item", use_float=True):
                        props, geom = f["properties"], f["geometry"]
                        coords = geom["coordinates"]
                        ids = props["source_segment_ids"]
                        if (geom["type"] != "LineString" or len(coords) != len(ids) + 1
                                or not ids or not all(isinstance(s, str) and s for s in ids)):
                            raise ValueError("Connected line segment cardinality mismatch")
                        xs, ys = [p[0] for p in coords], [p[1] for p in coords]
                        if not all(math.isfinite(float(v)) for v in xs + ys):
                            raise ValueError("Non-finite coordinates")
                        cur = db.execute("INSERT INTO lines(layer,properties,coordinates) VALUES(?,?,?)",
                                         (layer, json.dumps(props, separators=(",", ":")),
                                          json.dumps(coords, separators=(",", ":"))))
                        db.execute("INSERT INTO bounds VALUES(?,?,?,?,?)",
                                   (cur.lastrowid, min(xs), max(xs), min(ys), max(ys)))
                        line_count += 1
                        segment_count += len(ids)
                        if line_count % 5000 == 0:
                            db.commit()
                if (line_count, segment_count) != (expected_lines, expected_segments):
                    raise ValueError("Unexpected connected line counts")
                db.execute("INSERT INTO manifest VALUES(?,?,?,?)",
                           (layer, line_count, segment_count, digest))
                db.commit()
        # Atomic no-clobber publication, never replace an existing index.
        os.link(temp, target)
        with LOCK:
            STATE.update(state="complete", error=None)
    except Exception as exc:
        with LOCK:
            STATE.update(state="failed", error=type(exc).__name__)
    finally:
        temp.unlink(missing_ok=True)
        with LOCK:
            BUILDING = False


def start_build(data_dir: Path) -> bool:
    global BUILDING
    with LOCK:
        if BUILDING or index_path(data_dir).exists() or index_path(data_dir).is_symlink():
            return False
        if not all(source_path(data_dir, l).is_file() for l in LAYERS):
            return False
        BUILDING = True
        STATE.update(state="running", error=None)
        threading.Thread(target=_build, args=(data_dir,), daemon=True,
                         name="discovery2-line-index").start()
        return True


def tiles(data_dir: Path, legacy_db: Path, layer: str, bbox: tuple,
          limit: int = MAX_LINES) -> dict:
    """Return partial-colored polyline pieces, never a falsely green full road."""
    if layer not in LAYERS or not 1 <= limit <= MAX_LINES:
        raise ValueError("Invalid layer or limit")
    if (len(bbox) != 4 or not all(math.isfinite(v) for v in bbox) or
            bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or
            bbox[2] - bbox[0] > MAX_BBOX_DEGREES or
            bbox[3] - bbox[1] > MAX_BBOX_DEGREES):
        raise ValueError("Invalid bounding box")
    path = index_path(data_dir)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Connected lines index not ready")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as index:
        rows = index.execute(
            "SELECT l.properties,l.coordinates FROM bounds b JOIN lines l ON b.fid=l.fid "
            "WHERE l.layer=? AND b.maxx>=? AND b.minx<=? AND b.maxy>=? AND b.miny<=? "
            "ORDER BY l.fid LIMIT ?",
            (layer, bbox[0], bbox[2], bbox[1], bbox[3], limit + 1)
        ).fetchall()
    results = []
    known = set()
    if layer == "legacy":
        if legacy_db.is_symlink() or not legacy_db.is_file():
            raise FileNotFoundError("Active discovery database missing")
        ids = set()
        for p, _ in rows[:limit]:
            ids.update(json.loads(p)["source_segment_ids"])
        id_list = sorted(ids)
        if id_list:
            with sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro",
                                 uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                for batch_start in range(0, len(id_list), 800):
                    batch = id_list[batch_start:batch_start + 800]
                    marks = ",".join("?" for _ in batch)
                    known.update(row[0] for row in db.execute(
                        "SELECT segment_id FROM discovery_segments WHERE catalog=? "
                        "AND segment_id IN (" + marks + ")", (LEGACY_CATALOG, *batch)))
    for raw_props, raw_coords in rows[:limit]:
        props, coords = json.loads(raw_props), json.loads(raw_coords)
        ids = props["source_segment_ids"]
        # Pieces correspond 1:1 with the retained original segment IDs.
        results.append({
            "road": props.get("road"), "layer": layer,
            "parts": [{"id": sid, "coordinates": [coords[i], coords[i + 1]],
                       "state": ("discovered" if sid in known else "undiscovered")
                       if layer == "legacy" else "unverified"}
                      for i, sid in enumerate(ids)]
        })
    return {"layer": layer, "lines": results, "truncated": len(rows) > limit,
            "active": False, "v2_progress_migrated": False}
