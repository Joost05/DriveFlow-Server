"""DriveFlow Server v0.1.0 — private opaque snapshot vault.

No Home Assistant token, device permissions, or network access to HA Core needed.
Only accessible on its mapped port; DO NOT port forward this service.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from parking_geoapify import search as search_parking, ParkingError
from sync_store import create_tables, save_batch
from detail_store import summary, list_trip_details, list_records
from snapshot_index import create_tables as create_snapshot_tables, index_backup, index_existing_latest, save_detail_batch
from smart_odometer import create_tables as create_odometer_tables, store as store_odometer, records as odometer_records
from smart_fuel import create_tables as create_fuel_receipt_tables, save as save_fuel_receipt, records as fuel_receipt_records
from road_discovery_store import create_tables as create_discovery_tables, merge as merge_discoveries, status as discovery_status, changes as discovery_changes, CATALOG as DISCOVERY_CATALOG
from discovery_places import create_tables as create_place_discovery_tables, merge as merge_places, status as place_status, changes as place_changes, CATALOG as PLACE_CATALOG

VERSION = "0.6.4"
PORT = int(os.environ.get("DRIVEFLOW_PORT", "8098"))
DATA_DIR = Path(os.environ.get("DRIVEFLOW_DATA_DIR", "/data"))
FILES_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "driveflow_server.db"
MAX_COMPRESSED = 128 * 1024 * 1024
MAX_UNCOMPRESSED = 400 * 1024 * 1024
MAX_MEMBERS = 500
DEVICE_RE = re.compile(r"^[a-zA-Z0-9_\-]{8,80}$")
BACKUP_ID_RE = re.compile(r"^[a-f0-9]{32}$")
LOCK = threading.RLock()
log = logging.getLogger("driveflow")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_token() -> str:
    if os.environ.get("DRIVEFLOW_TOKEN"):
        token = os.environ["DRIVEFLOW_TOKEN"]
    else:
        config = DATA_DIR / "options.json"
        if not config.is_file():
            raise RuntimeError("api_token ontbreekt: stel het token in via HAOS app-configuratie")
        token = json.loads(config.read_text(encoding="utf8")).get("api_token", "")
    if not isinstance(token, str) or len(token) < 32:
        raise RuntimeError("api_token moet minimaal 32 tekens lang zijn")
    return token


def geoapify_key() -> str:
    key = os.environ.get("GEOAPIFY_API_KEY", "")
    if key:
        return key
    config = DATA_DIR / "options.json"
    if config.is_file():
        return str(json.loads(config.read_text(encoding="utf8")).get("geoapify_api_key") or "")
    return ""


@contextmanager
def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS backups (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            app_version TEXT NOT NULL,
            trips INTEGER NOT NULL,
            points INTEGER NOT NULL,
            ha_history INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            UNIQUE(device_id, content_hash)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_backups_created ON backups(created_at DESC)")
        create_tables(db)
        create_snapshot_tables(db)
        create_odometer_tables(db)
        create_fuel_receipt_tables(db)
        create_discovery_tables(db)
        create_place_discovery_tables(db)
        # Import existing verified snapshots without changing the originals.
        index_existing_latest(db, FILES_DIR, now_iso(), os.environ.get("DRIVEFLOW_TIMEZONE", "Europe/Amsterdam"))


def sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def validate_snapshot(path: Path) -> dict:
    """Checks ZIP paths, per-entry checksums, and live SQLite consistency. Never extracts entries to /data."""
    with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
        infos = archive.infolist()
        if len(infos) < 2 or len(infos) > MAX_MEMBERS:
            raise ValueError("ongeldig aantal archiefbestanden")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("manifest ontbreekt of archief bevat dubbele bestanden")
        total = sum(info.file_size for info in infos)
        if total > MAX_UNCOMPRESSED:
            raise ValueError("archief bevat te veel uitgepakte data")
        for entry in infos:
            name = entry.filename
            if (name.startswith("/") or "\\" in name or ".." in name.split("/")
                    or not (name == "manifest.json" or name.startswith(("databases/", "shared_prefs/", "files/")))):
                raise ValueError("ongeldig archiefpad")
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("mappen of symlinks zijn niet toegestaan in snapshot")
        raw = archive.read("manifest.json")
        if len(raw) > 128 * 1024:
            raise ValueError("manifest te groot")
        manifest = json.loads(raw)
        if manifest.get("format") != "driveflow-snapshot-v1":
            raise ValueError("snapshotformaat niet herkend")
        if manifest.get("package") != "nl.autohub.app":
            raise ValueError("andere appidentiteit")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("leeg bestandsmanifest")
        listed = set()
        for item in files:
            filename = item.get("path", "")
            if filename not in names or filename in listed:
                raise ValueError("manifest en archief komen niet overeen")
            listed.add(filename)
            info = archive.getinfo(filename)
            if info.file_size != item.get("size"):
                raise ValueError("bestandsgrootte wijkt af")
            digest = hashlib.sha256()
            with archive.open(filename, "r") as entry:
                for chunk in iter(lambda: entry.read(1024 * 1024), b""):
                    digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), str(item.get("sha256", ""))):
                raise ValueError("integriteitscontrole van archiefbestand mislukt")
        if listed != set(names) - {"manifest.json"}:
            raise ValueError("archief bevat onbekende bestanden")
        db_name = "databases/gps_points.db"
        if db_name not in listed:
            raise ValueError("gps_points.db ontbreekt; ritback-up is niet compleet")
        # Validate a true SQLite copy, not a file with pending WAL changes.
        with tempfile.TemporaryDirectory(dir=str(DATA_DIR)) as tmpdir:
            dbcopy = Path(tmpdir) / "gps_points.db"
            with archive.open(db_name, "r") as source, dbcopy.open("wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            db = sqlite3.connect(f"file:{dbcopy}?mode=ro", uri=True)
            try:
                result = db.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError("SQLite-integriteitscontrole mislukt")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"trips", "points", "ha_history"}.issubset(tables):
                    raise ValueError("rittabellen ontbreken")
                counts = {
                    "trips": db.execute("SELECT count(*) FROM trips").fetchone()[0],
                    "points": db.execute("SELECT count(*) FROM points").fetchone()[0],
                    "ha_history": db.execute("SELECT count(*) FROM ha_history").fetchone()[0],
                }
            finally:
                db.close()
        expected = manifest.get("counts", {})
        if counts != {k: expected.get(k) for k in counts}:
            raise ValueError("databasetellingen verschillen van manifest")
        version = str(manifest.get("app_version", ""))[:80]
        normalized = sorted((item["path"], item["size"], item["sha256"]) for item in files)
        content_hash = hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode("utf8")).hexdigest()
        return {"counts": counts, "app_version": version, "content_hash": content_hash}


class Api(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DriveFlowServer/0.5"

    def log_message(self, fmt, *args):
        # Prevent logging of tokens, query strings, app file names, request bodies.
        log.info("%s %s", self.client_address[0], fmt.split()[0] if fmt else "request")

    def json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.api_secret  # type: ignore[attr-defined]
        if not hmac.compare_digest(supplied.encode("utf8"), expected.encode("utf8")):
            self.json(401, {"error": "ongeldig API-token"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/health":
            self.json(200, {"ok": True, "service": "DriveFlow Server", "version": VERSION})
            return
        if not self.authenticated():
            return
        if path == "/v1/discovery/places/status":
            with connect_db() as db:
                self.json(200, place_status(db))
            return
        if path == "/v1/discovery/places/changes":
            q = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as db:
                    result = place_changes(db, int(q.get("since", ["0"])[0]),
                                           int(q.get("limit", ["1000"])[0]),
                                           q.get("catalog", [PLACE_CATALOG])[0])
                self.json(200, result)
            except (ValueError, OverflowError):
                self.json(400, {"error": "ongeldige plaats-synchronisatieparameters"})
            return
        if path == "/v1/discovery/status":
            with connect_db() as db:
                self.json(200, discovery_status(db))
            return
        if path == "/v1/discovery/changes":
            q = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as db:
                    result = discovery_changes(db, int(q.get("since", ["0"])[0]),
                                               int(q.get("limit", ["1000"])[0]),
                                               q.get("catalog", [DISCOVERY_CATALOG])[0])
                self.json(200, result)
            except (ValueError, OverflowError):
                self.json(400, {"error": "ongeldige synchronisatieparameters"})
            return
        if path == "/v1/parking/nearby":
            try:
                params = parse_qs(urlparse(self.path).query)
                lat = float(params["lat"][0]); lon = float(params["lon"][0])
                places = search_parking(lat, lon, self.server.geoapify_key)  # type: ignore[attr-defined]
                self.json(200, {"places": places, "source": "Geoapify", "occupancy_known": False})
            except (KeyError, IndexError, ValueError):
                self.json(400, {"error": "INVALID_LOCATION"})
            except ParkingError as exc:
                status = 400 if exc.kind == "INVALID_LOCATION" else 503 if exc.kind in ("NOT_CONFIGURED", "NETWORK", "SERVER") else 429 if exc.kind == "RATE_LIMIT" else 502
                log.warning("Parking lookup failed: kind=%s provider_http=%s", exc.kind, exc.status)
                self.json(status, {"error": exc.kind, "provider_status": exc.status})
            return
        if path == '/v1/fuel-receipts':
            q=parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as db:
                    result=fuel_receipt_records(db,q.get('vehicle',[None])[0],int(q.get('limit',['100'])[0]))
                self.json(200,{'receipts':result})
            except (ValueError,OverflowError):self.json(400,{'error':'ongeldige limiet'})
            return
        match=re.fullmatch(r'/v1/fuel-receipts/([a-zA-Z0-9_-]{8,80})/([a-f0-9-]{36})/photo',path)
        if match:
            device,uid=match.groups()
            with connect_db() as db:
                row=db.execute('SELECT photo_name,photo_sha256 FROM fuel_receipts WHERE device_id=? AND id=?',(device,uid)).fetchone()
            if not row:self.json(404,{'error':'bon ontbreekt'});return
            receipt=DATA_DIR/'fuel_receipts'/device/row['photo_name']
            if not receipt.is_file() or sha256_file(receipt)!=row['photo_sha256']:
                self.json(500,{'error':'fotocontrole mislukt'});return
            self.send_response(200)
            self.send_header('Content-Type','image/'+row['photo_name'].rsplit('.',1)[-1].replace('jpg','jpeg'))
            self.send_header('Content-Length',str(receipt.stat().st_size))
            self.send_header('Cache-Control','no-store');self.end_headers()
            with receipt.open('rb') as file:shutil.copyfileobj(file,self.wfile)
            return
        if path == "/v1/odometer":
            q=parse_qs(urlparse(self.path).query)
            device=q.get('device',[None])[0];vehicle=q.get('vehicle',[None])[0]
            if device and not DEVICE_RE.fullmatch(device):
                self.json(400,{'error':'ongeldig apparaat'});return
            try:
                with connect_db() as db:
                    result=odometer_records(db,device,vehicle,int(q.get('limit',['100'])[0]))
                self.json(200,{'confirmations':result})
            except (ValueError,OverflowError):self.json(400,{'error':'ongeldige limiet'})
            return
        match=re.fullmatch(r'/v1/odometer/([a-zA-Z0-9_-]{8,80})/([a-f0-9-]{36})/photo',path)
        if match:
            device,uid=match.groups()
            with connect_db() as db:
                row=db.execute('SELECT photo_name,photo_sha256 FROM odometer_events WHERE device_id=? AND id=?',(device,uid)).fetchone()
            if not row or not row['photo_name']:
                self.json(404,{'error':'foto ontbreekt'});return
            photo=DATA_DIR/'odometer_photos'/device/row['photo_name']
            if not photo.is_file() or sha256_file(photo)!=row['photo_sha256']:
                self.json(500,{'error':'fotocontrole mislukt'});return
            self.send_response(200)
            self.send_header('Content-Type','image/'+row['photo_name'].rsplit('.',1)[-1].replace('jpg','jpeg'))
            self.send_header('Content-Length',str(photo.stat().st_size))
            self.send_header('Cache-Control','no-store');self.end_headers()
            with photo.open('rb') as f:shutil.copyfileobj(f,self.wfile)
            return
        if path == "/v1/status":
            with connect_db() as db:
                result = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM backups").fetchone()
            self.json(200, {"ok": True, "version": VERSION, "backups": result[0], "stored_bytes": result[1]})
            return
        if path == "/v1/ha/records":
            q=parse_qs(urlparse(self.path).query)
            kind=q.get('kind',[''])[0]; key=q.get('vehicle',[None])[0]
            try:
                limit=int(q.get('limit',['100'])[0])
                with connect_db() as db:
                    rows=list_records(db,kind,key,limit)
                self.json(200,{"kind":kind,"records":rows})
            except (ValueError,OverflowError):
                self.json(400,{"error":"ongeldige parameters"})
            return
        if path == "/v1/ha/trips":
            q = parse_qs(urlparse(self.path).query)
            key = q.get("vehicle",[None])[0]
            try:
                limit = int(q.get("limit",["50"])[0])
            except ValueError:
                self.json(400,{"error":"ongeldige limiet"}); return
            with connect_db() as db:
                self.json(200,{"trips":list_trip_details(db,key,limit)})
            return
        if path == "/v1/ha/summary":
            with connect_db() as db:
                self.json(200, summary(db, os.environ.get("DRIVEFLOW_TIMEZONE", "Europe/Amsterdam")))
            return
        if path == "/v1/sync/status":
            with connect_db() as db:
                count = db.execute("SELECT COUNT(*) FROM sync_trips").fetchone()[0]
                devices = db.execute("SELECT COUNT(*) FROM sync_devices").fetchone()[0]
            self.json(200, {"ok": True, "trips":count, "devices":devices, "version":VERSION})
            return
        if path == "/v1/backups":
            with connect_db() as db:
                rows = db.execute("SELECT id,device_id,sha256,bytes,created_at,app_version,trips,points,ha_history FROM backups ORDER BY created_at DESC LIMIT 100").fetchall()
            self.json(200, {"backups": [dict(row) for row in rows]})
            return
        match = re.fullmatch(r"/v1/backups/([a-f0-9]{32})/download", path)
        if match:
            backup_id = match.group(1)
            with connect_db() as db:
                row = db.execute("SELECT file_name,bytes,sha256 FROM backups WHERE id=?", (backup_id,)).fetchone()
            if row is None:
                self.json(404, {"error": "back-up niet gevonden"})
                return
            source = FILES_DIR / row["file_name"]
            if not source.is_file() or source.stat().st_size != row["bytes"]:
                self.json(500, {"error": "back-upbestand ontbreekt of is beschadigd"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="driveflow-{backup_id}.zip"')
            self.send_header("Content-Length", str(row["bytes"]))
            self.send_header("X-Content-SHA256", row["sha256"])
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with source.open("rb") as file:
                shutil.copyfileobj(file, self.wfile, 1024 * 1024)
            return
        self.json(404, {"error": "route niet gevonden"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/discovery/places/merge":
            if not self.authenticated(): return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.json(415, {"error": "alleen JSON is toegestaan"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 524288:
                    self.json(413, {"error": "plaatsbatch te groot of leeg"}); return
                payload = json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:
                        result = merge_places(db, payload, now_iso())
                self.json(200, result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.json(422, {"error": str(exc)[:140]})
            except sqlite3.Error:
                self.json(500, {"error": "databasefout"})
            return
        if path == "/v1/discovery/merge":
            if not self.authenticated(): return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.json(415, {"error": "alleen JSON is toegestaan"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 524288:
                    self.json(413, {"error": "discoverybatch te groot of leeg"}); return
                payload = json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:
                        result = merge_discoveries(db, payload, now_iso())
                self.json(200, result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.json(422, {"error": str(exc)[:140]})
            except sqlite3.Error:
                self.json(500, {"error": "databasefout"})
            return
        if path == '/v1/fuel-receipts':
            if not self.authenticated():return
            device=self.headers.get('X-DriveFlow-Device-Id','')
            if not DEVICE_RE.fullmatch(device):self.json(400,{'error':'ongeldige apparaat-ID'});return
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                self.json(415,{'error':'alleen JSON'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 1<=length<=21_500_000:self.json(413,{'error':'tankbon te groot'});return
                payload=json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:result=save_fuel_receipt(db,DATA_DIR,device,payload,now_iso())
                self.json(200,result)
            except (ValueError,TypeError) as exc:self.json(422,{'error':str(exc)[:140]})
            except (sqlite3.Error,OSError):self.json(500,{'error':'opslagfout'})
            return
        if path == '/v1/odometer':
            if not self.authenticated():return
            device=self.headers.get('X-DriveFlow-Device-Id','')
            if not DEVICE_RE.fullmatch(device):self.json(400,{'error':'ongeldige apparaat-ID'});return
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                self.json(415,{'error':'alleen JSON'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 1<=length<=21_500_000:self.json(413,{'error':'registratie te groot'});return
                payload=json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:
                        result=store_odometer(db,DATA_DIR,device,payload,now_iso())
                self.json(200,result)
            except (ValueError,TypeError) as exc:self.json(422,{'error':str(exc)[:140]})
            except (sqlite3.Error,OSError):self.json(500,{'error':'opslagfout'})
            return
        if path == "/v1/sync/details":
            if not self.authenticated(): return
            device = self.headers.get("X-DriveFlow-Device-Id", "")
            if not DEVICE_RE.fullmatch(device):
                self.json(400,{"error":"ongeldige apparaat-ID"}); return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.json(415,{"error":"alleen JSON is toegestaan"}); return
            try:
                length=int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 524288:
                    self.json(413,{"error":"detailbatch te groot"}); return
                payload=json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:
                        result=save_detail_batch(db,device,payload,now_iso())
                self.json(200,result)
            except (ValueError,TypeError,json.JSONDecodeError) as exc:
                self.json(422,{"error":str(exc)[:140]})
            except sqlite3.Error:
                self.json(500,{"error":"databasefout"})
            return
        if path == "/v1/sync/batch":
            if not self.authenticated(): return
            device = self.headers.get("X-DriveFlow-Device-Id", "")
            if not DEVICE_RE.fullmatch(device):
                self.json(400,{"error":"ongeldige apparaat-ID"}); return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.json(415,{"error":"alleen JSON is toegestaan"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 524288:
                    self.json(413,{"error":"batch te groot of leeg"}); return
                payload = json.loads(self.rfile.read(length))
                with LOCK:
                    with connect_db() as db:
                        result = save_batch(db,device,payload,now_iso())
                self.json(200,result)
            except (ValueError,TypeError,json.JSONDecodeError) as exc:
                self.json(422,{"error":str(exc)[:140]})
            except sqlite3.Error:
                log.exception("Sync SQL failure")
                self.json(500,{"error":"databasefout"})
            return
        if path != "/v1/backups":
            self.json(404, {"error": "route niet gevonden"})
            return
        if not self.authenticated():
            return
        device = self.headers.get("X-DriveFlow-Device-Id", "")
        if not DEVICE_RE.fullmatch(device):
            self.json(400, {"error": "ongeldige apparaat-ID"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() not in ("application/zip", "application/octet-stream"):
            self.json(415, {"error": "alleen ZIP-back-ups zijn toegestaan"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_COMPRESSED:
            self.json(413 if length > MAX_COMPRESSED else 411, {"error": "ontbrekende of te grote Content-Length"})
            return
        tmp = FILES_DIR / ("." + uuid.uuid4().hex + ".upload")
        try:
            with tmp.open("xb") as dest:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("upload afgebroken")
                    dest.write(chunk)
                    remaining -= len(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            digest = sha256_file(tmp)
            expected_sha = self.headers.get("X-Content-SHA256", "").lower()
            if expected_sha and not hmac.compare_digest(digest, expected_sha):
                raise ValueError("transport-SHA256 wijkt af")
            data = validate_snapshot(tmp)
            with LOCK:
                with connect_db() as db:
                    previous = db.execute("SELECT id,created_at,sha256 FROM backups WHERE device_id=? AND content_hash=?", (device, data["content_hash"])).fetchone()
                    if previous is not None:
                        self.json(200, {"ok": True, "already_present": True, "id": previous["id"], "sha256": previous["sha256"], "counts": data["counts"]})
                        return
                    uid = uuid.uuid4().hex
                    target = FILES_DIR / (uid + ".zip")
                    tmp.replace(target)
                    try:
                        db.execute("INSERT INTO backups (id,device_id,sha256,content_hash,bytes,created_at,app_version,trips,points,ha_history,file_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (uid, device, digest, data["content_hash"], length, now_iso(), data["app_version"],
                                    data["counts"]["trips"], data["counts"]["points"], data["counts"]["ha_history"], target.name))
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
            try:
                with connect_db() as db:
                    index_backup(db, uid, device, target, now_iso(), os.environ.get("DRIVEFLOW_TIMEZONE", "Europe/Amsterdam"))
            except (ValueError,OSError,sqlite3.Error,zipfile.BadZipFile,KeyError) as exc:
                log.warning("New snapshot indexing skipped (%s)",type(exc).__name__)
            self.json(201, {"ok": True, "already_present": False, "id": uid, "sha256": digest, "counts": data["counts"]})
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            log.warning("Backup rejected (%s)", type(exc).__name__)
            self.json(422, {"error": str(exc)[:180]})
        finally:
            tmp.unlink(missing_ok=True)


def serve():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secret = api_token()
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Api)
    server.api_secret = secret  # type: ignore[attr-defined]
    server.geoapify_key = geoapify_key()  # type: ignore[attr-defined]
    log.info("DriveFlow Server %s ready on port %s, database in /data", VERSION, PORT)
    server.serve_forever(poll_interval=.5)


if __name__ == "__main__":
    serve()
