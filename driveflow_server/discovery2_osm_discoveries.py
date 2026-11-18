"""OSM Discovery 2.1 additive namespace.

All OSM discoveries are stored in a separate SQLite file under /data/discovery2,
never in the original DriveFlow/HA discovery tables. Each submitted segment ID
must be proved by its line_id in the immutable connected-lines index; geometric
overlap with legacy IDs never confers discovered state.
"""
import json
import sqlite3
from pathlib import Path

from discovery2_lines import index_path

DB_NAME = "osm_discoveries.sqlite"
MAX_BATCH = 500


def _path(data_dir: Path) -> Path:
    return data_dir / "discovery2" / DB_NAME


def _connect(data_dir: Path) -> sqlite3.Connection:
    file = _path(data_dir)
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.is_symlink():
        raise OSError("OSM discovery DB symlink rejected")
    db = sqlite3.connect(file)
    db.execute("PRAGMA busy_timeout=15000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS osm_discoveries(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id TEXT NOT NULL UNIQUE,
            line_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            discovered_at TEXT NOT NULL
        );
    """)
    return db


def merge(data_dir: Path, payload: dict, device_id: str, now: str) -> dict:
    if not isinstance(payload, dict) or payload.get("namespace") != "osm":
        raise ValueError("Expected OSM discovery namespace")
    rows = payload.get("segments")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
        raise ValueError("OSM segments must be a nonempty batch of max 500")
    if not device_id or len(device_id) > 128:
        raise ValueError("Invalid device")
    index_path_value = index_path(data_dir)
    if index_path_value.is_symlink() or not index_path_value.is_file():
        raise FileNotFoundError("Connected line index missing")
    normalized = []
    for item in rows:
        if not isinstance(item, dict) or set(item) != {"id", "line_id"}:
            raise ValueError("Invalid OSM entry")
        segment_id, line_id = item["id"], item["line_id"]
        if (not isinstance(segment_id,str) or not 1 <= len(segment_id) <= 256
                or not isinstance(line_id,int) or isinstance(line_id,bool)
                or line_id < 1):
            raise ValueError("Invalid OSM segment identity")
        normalized.append((segment_id,line_id))
    # Validate against actual immutable source IDs, not just an ID pattern,
    # a nearby legacy segment or a client-supplied coordinate.
    with sqlite3.connect(index_path_value.resolve().as_uri()+"?mode=ro",uri=True) as idx:
        for segment_id,line_id in set(normalized):
            row = idx.execute(
                "SELECT properties FROM lines WHERE fid=? AND layer='osm'",
                (line_id,)).fetchone()
            if not row or segment_id not in json.loads(row[0])["source_segment_ids"]:
                raise ValueError("OSM segment ID does not belong to source line")
    with _connect(data_dir) as db:
        added = 0
        for segment_id,line_id in normalized:
            cur = db.execute(
                "INSERT OR IGNORE INTO osm_discoveries"
                "(segment_id,line_id,device_id,discovered_at) VALUES(?,?,?,?)",
                (segment_id,line_id,device_id,now))
            added += int(cur.rowcount > 0)
        db.commit()
    return {"ok":True,"namespace":"osm","received":len(normalized),"added":added}


def changes(data_dir: Path, since: int = 0, limit: int = 1000) -> dict:
    if not isinstance(since,int) or since < 0 or not 1 <= limit <= 2000:
        raise ValueError("Invalid OSM changes cursor")
    file = _path(data_dir)
    if not file.is_file():
        return {"ok":True,"namespace":"osm","cursor":since,"ids":[],"has_more":False}
    with _connect(data_dir) as db:
        rows = db.execute(
            "SELECT seq,segment_id FROM osm_discoveries WHERE seq>? ORDER BY seq LIMIT ?",
            (since,limit+1)).fetchall()
    page = rows[:limit]
    return {"ok":True,"namespace":"osm",
            "cursor":page[-1][0] if page else since,
            "ids":[r[1] for r in page],
            "has_more":len(rows)>limit}


def status(data_dir: Path) -> dict:
    file = _path(data_dir)
    if not file.is_file():
        return {"ok":True,"namespace":"osm","discovered_segments":0,"cursor":0}
    with _connect(data_dir) as db:
        count,cursor = db.execute(
            "SELECT COUNT(*),COALESCE(MAX(seq),0) FROM osm_discoveries").fetchone()
    return {"ok":True,"namespace":"osm","discovered_segments":count,"cursor":cursor}
