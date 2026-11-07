"""Verified, idempotent odometer evidence store; does not modify trip totals."""
import base64
import hashlib
import math
import os
import re
import uuid
from pathlib import Path
from sync_store import vehicle_key

UUID_RE=re.compile(r'^[a-f0-9-]{36}$')
ALLOWED={'camera','gallery','manual'}

def create_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS odometer_events (
      device_id TEXT NOT NULL, id TEXT NOT NULL, vehicle_key TEXT NOT NULL,
      vehicle_name TEXT NOT NULL, time_ms INTEGER NOT NULL, km REAL NOT NULL,
      source TEXT NOT NULL, ocr_text TEXT NOT NULL, estimated_km REAL,
      difference_km REAL, previous_km REAL, corrected_from TEXT,
      photo_name TEXT, photo_sha256 TEXT, received_at TEXT NOT NULL,
      PRIMARY KEY(device_id,id))''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_odometer_vehicle ON odometer_events(vehicle_key,time_ms DESC)')

def store(db,base:Path,device:str,payload:dict,now:str):
    if not isinstance(payload,dict):raise ValueError('ongeldige bevestiging')
    uid=payload.get('id')
    if not isinstance(uid,str) or not UUID_RE.fullmatch(uid):raise ValueError('ongeldige ID')
    name=payload.get('vehicle')
    if not isinstance(name,str) or not (1<=len(name)<=100):raise ValueError('ongeldig voertuig')
    source=payload.get('source')
    if source not in ALLOWED:raise ValueError('ongeldige bron')
    timestamp=payload.get('time_ms');km=payload.get('km')
    if not isinstance(timestamp,int) or not 1_500_000_000_000<timestamp<4_100_000_000_000:raise ValueError('ongeldige tijd')
    if not isinstance(km,(int,float)) or not math.isfinite(km) or not 0<=km<=5_000_000:raise ValueError('ongeldige kilometerstand')
    def optional(key):
        value=payload.get(key)
        if value is not None and (not isinstance(value,(int,float)) or not math.isfinite(value) or abs(value)>5_000_000):raise ValueError('ongeldige '+key)
        return value
    estimated=optional('estimated_km');diff=optional('difference_km');previous=optional('previous_km')
    ocr=payload.get('ocr_text','')
    if not isinstance(ocr,str) or len(ocr)>4000:raise ValueError('OCR tekst te lang')
    corrected=payload.get('corrected_from')
    if corrected is not None and (not isinstance(corrected,str) or not UUID_RE.fullmatch(corrected)):raise ValueError('ongeldige correctieverwijzing')
    photo=payload.get('photo_base64')
    ext=payload.get('photo_extension')
    photo_bytes=None;digest=None;filename=None
    if photo is not None:
        if source=='manual' or ext not in ('jpg','png','webp','heic','heif') or not isinstance(photo,str) or len(photo)>21_000_000:raise ValueError('ongeldige foto')
        try:photo_bytes=base64.b64decode(photo,validate=True)
        except Exception as exc:raise ValueError('ongeldige fotocodering') from exc
        if not 1<=len(photo_bytes)<=15_000_000:raise ValueError('foto te groot of leeg')
        digest=hashlib.sha256(photo_bytes).hexdigest()
        filename=uid+'.'+ext
    existing=db.execute('SELECT km,photo_sha256 FROM odometer_events WHERE device_id=? AND id=?',(device,uid)).fetchone()
    if existing:
        if float(existing[0])!=float(km) or existing[1]!=digest:raise ValueError('conflicterende ID')
        return {'ok':True,'already_present':True,'id':uid}
    dest=None
    if photo_bytes is not None:
        folder=base/'odometer_photos'/device
        folder.mkdir(parents=True,exist_ok=True)
        dest=folder/filename
        temp=folder/(uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('xb') as f:
                f.write(photo_bytes);f.flush();os.fsync(f.fileno())
            temp.replace(dest)
        finally:temp.unlink(missing_ok=True)
    try:
        db.execute('''INSERT INTO odometer_events
          (device_id,id,vehicle_key,vehicle_name,time_ms,km,source,ocr_text,estimated_km,difference_km,previous_km,corrected_from,photo_name,photo_sha256,received_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (device,uid,vehicle_key(name),name,timestamp,km,source,ocr,estimated,diff,previous,corrected,filename,digest,now))
    except Exception:
        if dest:dest.unlink(missing_ok=True)
        raise
    return {'ok':True,'already_present':False,'id':uid}

def records(db,device=None,vehicle=None,limit=100):
    sql='SELECT device_id,id,vehicle_key,vehicle_name,time_ms,km,source,ocr_text,estimated_km,difference_km,previous_km,corrected_from,photo_name,photo_sha256 FROM odometer_events WHERE 1=1'
    args=[]
    if device:sql+=' AND device_id=?';args.append(device)
    if vehicle:sql+=' AND vehicle_key=?';args.append(vehicle)
    sql+=' ORDER BY time_ms DESC LIMIT ?';args.append(min(max(int(limit),1),500))
    return [dict(r) for r in db.execute(sql,args)]
